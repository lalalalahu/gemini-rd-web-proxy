import os
import re
import time
import json
import asyncio
import hashlib
from pathlib import Path
from typing import List, Optional, Dict, Any, Union

from playwright_stealth import Stealth

from fastapi import FastAPI, HTTPException, Depends, Request, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, Page, BrowserContext

# ==============================================================================
# CONFIGURATION & CONSTANTS
# ==============================================================================
TARGET_URL = "https://gemini.rakyatdigital.gov.my"
API_KEY = os.getenv("API_KEY", "api_key")  # Change this or set API_KEY env variable
AVAILABLE_MODELS = ["Auto"]

DATA_DIR = Path.home() / ".gemini-service"
PROFILE_DIR = DATA_DIR / "chrome-profile"
LOGIN_FLAG = DATA_DIR / "logged-in"

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Gemini Enterprise Local Gateway")

security = HTTPBearer()

def verify_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials

playwright_instance = None
context: Optional[BrowserContext] = None
session_pages: Dict[str, Page] = {}
page_locks: Dict[str, asyncio.Lock] = {}
session_creation_locks: Dict[str, asyncio.Lock] = {}
is_ready = False

# Gemini Enterprise often renders a short plan (for example, “I will search…”) before
# it starts retrieval.  Those status messages are not a completed assistant response.
INITIAL_RESPONSE_GRACE_SECONDS = float(os.getenv("INITIAL_RESPONSE_GRACE_SECONDS", "15"))
INTERMEDIATE_STATUS_GRACE_SECONDS = float(os.getenv("INTERMEDIATE_STATUS_GRACE_SECONDS", "60"))
FINAL_RESPONSE_QUIET_SECONDS = float(os.getenv("FINAL_RESPONSE_QUIET_SECONDS", "10"))

INTERMEDIATE_STATUS_RE = re.compile(
    r"^\s*(?:i(?:'ll| will)|let me)\s+(?:search|check|look(?:\s+up)?|retrieve|gather|analy[sz]e)\b",
    re.IGNORECASE,
)


def is_intermediate_status(text: str) -> bool:
    """True when Gemini has announced a tool/retrieval action, not an answer."""
    return bool(INTERMEDIATE_STATUS_RE.match(text or ""))

JS_STATUS_AND_EXTRACTOR = """(turnEl) => {
    const stopButton = document.querySelector('.send-button.stop, md-icon-button.send-button.stop, [data-aria-label="Stop"], [aria-label="Stop"]');
    const hasStop = stopButton !== null;
    let hasStopIcon = false;
    document.querySelectorAll('.send-button md-icon, md-icon-button md-icon').forEach(icon => {
        if (icon.textContent.trim().toLowerCase() === 'stop') hasStopIcon = true;
    });

    function getDeepText(node) {
        if (!node) return '';
        let tag = '';
        if (node.nodeType === Node.ELEMENT_NODE) {
            const cls = (typeof node.className === 'string') ? node.className.toLowerCase() : '';
            tag = (node.tagName || '').toLowerCase();
            const role = node.getAttribute('role') || '';

            // Keep assistant text, but reject controls and retrieval UI noise.
            if (cls.includes('working-on-it-footer') || cls.includes('working-on-it-spark') || tag === 'ucs-lottie-animation') return '';
            if (['svg', 'details', 'button', 'menu', 'md-menu', 'img'].includes(tag) || tag.includes('button') || tag.includes('chip')) return '';
            if (role === 'progressbar') return '';
            if (node.getAttribute('aria-hidden') === 'true' || cls.includes('sr-only')) return '';

            const isExcludedClass = [
                'question-block', 'question-wrapper', 'user-query', 'user-message',
                'suggestion', 'diagnostic', 'progress', 'step', 'thought', 'loading',
                'spark', 'table-actions', 'citation-slot'
            ].some(c => cls.includes(c));
            if (isExcludedClass) return '';
        }

        const nl = String.fromCharCode(10);
        if (node.nodeType === Node.TEXT_NODE) {
            return (node.textContent || '').trim().toLowerCase() === 'spark' ? '' : (node.textContent || '');
        }

        if (tag === 'br') return nl;

        let text = '';
        if (node.shadowRoot) text += getDeepText(node.shadowRoot);
        if (node.childNodes) {
            for (let child of node.childNodes) text += getDeepText(child);
        }

        // Preserve all visible text without inventing Markdown.  In this
        // corporate UI, PRE is also used as a layout wrapper, not only code.
        if (tag === 'td' || tag === 'th') return text.trim() + ' | ';
        if (tag === 'tr') return text.trim() + nl;
        if (['p', 'div', 'li', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table'].includes(tag)) {
            return text + nl;
        }
        return text;
    }

    // Prefer the completed assistant summary.  Text streamers are a fallback
    // because they may contain only the preliminary tool/retrieval message.
    let rawText = "";
    const summary = turnEl.querySelector('ucs-summary, .summary, [data-message-author-role="assistant"]');
    if (summary) {
        rawText = getDeepText(summary);
    } else {
        const streamers = turnEl.querySelectorAll('ucs-text-streamer');
        streamers.forEach(s => {
            rawText += getDeepText(s) + String.fromCharCode(10);
        });
    }

    // Enterprise search can keep working after the Stop button disappears.
    // Detect its retrieval/progress UI separately from the answer extractor.
    const hasBusyElement = Boolean(turnEl.querySelector(
        '[aria-busy="true"], [role="progressbar"], ucs-lottie-animation'
    ));
    // Do not infer activity from visible words such as “Searching”.  This UI
    // keeps completed search cards in the final transcript, which would make
    // a text-based detector report “busy” forever.
    const hasActiveWork = hasBusyElement;

    return {
        isGenerating: hasStop || hasStopIcon,
        hasActiveWork,
        text: rawText,
        length: rawText.length
    };
}"""

def clean_response_text(raw_text: str) -> str:
    """Cleans up structural artifacts but preserves table and markdown spacing."""
    if not raw_text:
        return ""
    
    text = raw_text.replace('\r', '')
    import re
    # Just collapse multiple blank lines down to double blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.replace(r'\_', '_').strip()

# ==============================================================================
# PYDANTIC SCHEMAS (UPDATED FOR TOOL CALLING)
# ==============================================================================
class Message(BaseModel):
    role: str
    content: Optional[Union[str, List[Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None

class FunctionDef(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict] = None

class Tool(BaseModel):
    type: str
    function: FunctionDef

class ChatRequest(BaseModel):
    model: Optional[str] = "gemini-enterprise"
    messages: List[Message]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Any] = None
    # These are optional provider extensions.  `user` is part of the OpenAI
    # request shape; the two explicit IDs support clients that expose them.
    user: Optional[str] = None
    conversation_id: Optional[str] = None
    session_id: Optional[str] = None

# ==============================================================================
# TOOL CALLING HELPERS
# ==============================================================================
def get_content_text(content: Any) -> str:
    """Helper to extract text from string or list content"""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join([
            c.get("text", "") 
            for c in content 
            if isinstance(c, dict) and c.get("type") == "text"
        ])
    return ""


def get_session_id(req: ChatRequest, request: Request) -> str:
    """Return a stable, privacy-safe browser-page key for one client chat."""
    candidates = [
        req.session_id,
        req.conversation_id,
        req.user,
        request.headers.get("x-session-id"),
        request.headers.get("x-conversation-id"),
        request.headers.get("x-opencode-session-id"),
        request.headers.get("x-thread-id"),
    ]
    client_key = next((str(value).strip() for value in candidates if value and str(value).strip()), None)

    if not client_key:
        # Chat Completions has no required conversation ID.  Most clients send
        # the transcript on every turn, so its first user message is a stable
        # fallback that prevents unrelated new chats from sharing a browser tab.
        first_user = next((message for message in req.messages if message.role == "user"), None)
        client_key = get_content_text(first_user.content) if first_user else ""

    if not client_key:
        client_key = json.dumps(
            [{"role": message.role, "content": get_content_text(message.content)} for message in req.messages],
            sort_keys=True,
        )

    digest = hashlib.sha256(client_key.encode("utf-8")).hexdigest()[:24]
    return f"chat-{digest}"

def format_tools_prompt(tools: List[Tool]) -> str:
    """Format tools into a clear prompt for Gemini"""
    if not tools:
        return ""
    
    prompt = """## TOOL CALLING SYSTEM

═══════════════════════════════════════════════════════════════════════════════
                    🚨🚨🚨 YOU ARE A TOOL-EXECUTING AI 🚨🚨🚨
═══════════════════════════════════════════════════════════════════════════════

YOU HAVE FULL ACCESS TO THE USER'S COMPUTER THROUGH TOOLS.
YOU CAN READ, WRITE, EDIT, LIST, SEARCH, AND EXECUTE ANYTHING.

NEVER SAY:
❌ "I don't have access to..."
❌ "I can only see files in..."
❌ "I cannot access your computer..."
❌ "Would you like me to... instead?"

YOU MUST:
✅ USE THE TOOLS PROVIDED
✅ EXECUTE WHAT THE USER ASKS
✅ NEVER REFUSE OR MAKE EXCUSES

If user asks to list Desktop → USE list tool on /Users/macbookair/Desktop
If user asks to read a file → USE read tool
If user asks to edit → USE edit tool
JUST DO IT. NO EXCUSES.

═══════════════════════════════════════════════════════════════════════════════
                    🚨 WRITE vs EDIT - ABSOLUTE RULE 🚨
═══════════════════════════════════════════════════════════════════════════════

WRITE TOOL IS PROHIBITED FOR EXISTING FILES!

• File already exists? → YOU MUST USE EDIT TOOL. WRITE IS FORBIDDEN.
• User says "update", "change", "modify", "fix", "edit", "improve", "enhance" → EDIT TOOL ONLY
• WRITE tool is ONLY for creating brand new files that don't exist yet

═══════════════════════════════════════════════════════════════════════════════
                    ⛔ NEVER PUT CODE DIRECTLY IN JSON ⛔
═══════════════════════════════════════════════════════════════════════════════

ALL code/content must be in markdown code blocks with placeholders:
- WRITE: USE_CODE_BLOCK_ABOVE
- EDIT: USE_OLD_CODE_ABOVE and USE_NEW_CODE_ABOVE

═══════════════════════════════════════════════════════════════════════════════
                    🔴🔴🔴 EDIT TOOL - CRITICAL FORMAT 🔴🔴🔴
═══════════════════════════════════════════════════════════════════════════════

THE EDIT TOOL HAS A VERY SPECIFIC FORMAT. FOLLOW IT EXACTLY OR IT WILL FAIL.

STEP 1: Write the OLD code (code to find) in a markdown code block
STEP 2: Write the NEW code (replacement) in a SECOND markdown code block  
STEP 3: Write the JSON with PLACEHOLDERS (not actual code!)

✅ CORRECT EDIT FORMAT:

Old code to replace:
```html
<section id="about">Old content here</section>
```

New replacement:
```html
<section id="skills">New content here</section>
<section id="about">Old content here</section>
```

{"tool_calls": [{"name": "edit", "arguments": {"filePath": "/path/file.html", "oldString": "USE_OLD_CODE_ABOVE", "newString": "USE_NEW_CODE_ABOVE"}}]}

❌ WRONG - NEVER DO THIS:
{"tool_calls": [{"name": "edit", "arguments": {"filePath": "/path.html", "oldString": "<actual code here>", "newString": "<actual code here>"}}]}

❌ WRONG - NEVER PUT USE_OLD_CODE_ABOVE INSIDE newString:
{"tool_calls": [{"name": "edit", "arguments": {"newString": "USE_OLD_CODE_ABOVE\n<code>"}}]}

THE PLACEHOLDERS ARE LITERAL STRINGS:
- oldString MUST be exactly: "USE_OLD_CODE_ABOVE"
- newString MUST be exactly: "USE_NEW_CODE_ABOVE"

═══════════════════════════════════════════════════════════════════════════════
                         📁 OTHER FILE OPERATIONS
═══════════════════════════════════════════════════════════════════════════════

READ FILE:
{"tool_calls": [{"name": "read", "arguments": {"filePath": "/path/file.txt"}}]}

WRITE NEW FILE (ONLY for files that DON'T EXIST):
```html
<!DOCTYPE html>
<html><body>Content</body></html>
```
{"tool_calls": [{"name": "write", "arguments": {"filePath": "/new-file.html", "content": "USE_CODE_BLOCK_ABOVE"}}]}

═══════════════════════════════════════════════════════════════════════════════
                         🔍 SEARCH & NAVIGATION
═══════════════════════════════════════════════════════════════════════════════

FIND FILES:
{"tool_calls": [{"name": "glob", "arguments": {"pattern": "**/*.tsx"}}]}

SEARCH CONTENT:
{"tool_calls": [{"name": "grep", "arguments": {"pattern": "functionName", "path": "/project"}}]}

LIST DIRECTORY:
{"tool_calls": [{"name": "list", "arguments": {"path": "/directory"}}]}

EXECUTE COMMAND:
{"tool_calls": [{"name": "execute", "arguments": {"command": "npm test"}}]}

MULTIPLE TOOLS:
{"tool_calls": [{"name": "read", "arguments": {"filePath": "/a.js"}}, {"name": "read", "arguments": {"filePath": "/b.js"}}]}

═══════════════════════════════════════════════════════════════════════════════
                         ⛔ FORBIDDEN ⛔
═══════════════════════════════════════════════════════════════════════════════

❌ Refusing to use tools or making excuses
❌ WRITE tool on existing file (USE EDIT!)
❌ Code directly in JSON (use code blocks + placeholders)
❌ YAML format
❌ JSON in code blocks

═══════════════════════════════════════════════════════════════════════════════
                         AVAILABLE TOOLS
═══════════════════════════════════════════════════════════════════════════════

"""
    for tool in tools:
        func = tool.function
        prompt += f"▸ {func.name}"
        if func.description:
            prompt += f" - {func.description}"
        prompt += "\n"
        
        if func.parameters and func.parameters.get('properties'):
            props = func.parameters['properties']
            required = func.parameters.get('required', [])
            for pname, pinfo in props.items():
                req = " ⚠REQUIRED" if pname in required else ""
                ptype = pinfo.get('type', '')
                desc = pinfo.get('description', '')
                prompt += f"    {pname} ({ptype}){req}: {desc}\n"
        prompt += "\n"
    
    prompt += """═══════════════════════════════════════════════════════════════════════════════
REMEMBER: YOU HAVE FULL SYSTEM ACCESS. USE THE TOOLS. NEVER REFUSE.
═══════════════════════════════════════════════════════════════════════════════
"""
    return prompt


def format_conversation(messages: List[Message], tools: Optional[List[Tool]] = None) -> str:
    """Format messages into a conversation prompt"""
    formatted = []
    
    if tools:
        formatted.append(format_tools_prompt(tools))
    
    for msg in messages:
        role = msg.role
        content = get_content_text(msg.content)
        
        if role == "system":
            formatted.append(f"System Instructions:\n{content}")
        elif role == "user":
            formatted.append(f"User: {content}")
        elif role == "assistant":
            if msg.tool_calls:
                tc_str = json.dumps({"tool_calls": [
                    {"name": tc.get("function", {}).get("name"), 
                     "arguments": json.loads(tc.get("function", {}).get("arguments", "{}"))}
                    for tc in msg.tool_calls
                ]})
                formatted.append(f"Assistant: {tc_str}")
            elif content:
                formatted.append(f"Assistant: {content}")
        elif role == "tool":
            tool_name = msg.name or "tool"
            formatted.append(f"Tool Result ({tool_name}):\n{content}")
    
    return "\n\n".join(formatted)

def parse_tool_calls(response: str) -> Optional[List[Dict]]:
    """Extract tool calls from response - handles multiple formats robustly"""
    cleaned = response.replace('\\_', '_')
    
    # Method 1: Standard JSON with "tool_calls": [...]
    start = cleaned.find('"tool_calls"')
    if start != -1:
        arr_start = cleaned.find('[', start)
        if arr_start != -1:
            depth = 0
            for i, c in enumerate(cleaned[arr_start:], arr_start):
                if c == '[':
                    depth += 1
                elif c == ']':
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cleaned[arr_start:i+1])
                        except:
                            break
    
    # Method 2: YAML-style "tool_calls:" - parse manually
    if 'tool_calls:' in cleaned:
        try:
            lines = cleaned.split('\n')
            tools = []
            current_tool = None
            in_args = False
            
            for line in lines:
                stripped = line.strip()
                if stripped.startswith('- name:'):
                    if current_tool:
                        tools.append(current_tool)
                    current_tool = {"name": stripped.split(':', 1)[1].strip(), "arguments": {}}
                    in_args = False
                elif stripped == 'arguments:' and current_tool:
                    in_args = True
                elif in_args and current_tool and ':' in stripped and not stripped.startswith('-'):
                    key, val = stripped.split(':', 1)
                    current_tool["arguments"][key.strip()] = val.strip()
            
            if current_tool:
                tools.append(current_tool)
            
            if tools:
                return tools
        except:
            pass
    
    # Method 3: Find any JSON object with "name" and "arguments"
    pattern = r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:\s*(\{[^\{\}]*\})'
    matches = re.findall(pattern, cleaned)
    if matches:
        tools = []
        for name, args_str in matches:
            try:
                args = json.loads(args_str)
            except:
                args = {}
            tools.append({"name": name, "arguments": args})
        if tools:
            return tools
    
    return None

# ==============================================================================
# BROWSER & SESSION MANAGEMENT
# ==============================================================================
async def check_logged_in(page: Page, timeout: int = 5000) -> bool:
    try:
        await page.wait_for_selector('ucs-prosemirror-editor', timeout=timeout)
        return True
    except:
        return False

async def fetch_available_models(page: Page):
    global AVAILABLE_MODELS
    try:
        print("  [Debug] Fetching available models from UI...", flush=True)
        # Click the model dropdown to open it
        await page.locator('#model-selector-menu-anchor, .action-model-selector').first.click()
        await asyncio.sleep(1.0)
        
        # 1. Use Playwright's locator! This natively PIERCES the Shadow DOM to find the items.
        items = await page.locator('md-menu-item, [role="menuitem"]').all()
        fetched_models = []
        
        for item in items:
            # 2. Only process items actively visible in the opened dropdown
            if await item.is_visible():
                
                # 3. Pass the specific element into an evaluator to extract text while skipping icons
                raw_text = await item.evaluate("""(el) => {
                    function getDeepText(node) {
                        if (!node) return '';
                        
                        if (node.nodeType === Node.ELEMENT_NODE) {
                            const tag = (node.tagName || '').toLowerCase();
                            // Completely ignore Material Icons so we don't get "rocket_launch" etc.
                            if (tag === 'md-icon') return ''; 
                        }
                        
                        let text = '';
                        if (node.nodeType === Node.TEXT_NODE) {
                            text += node.textContent;
                        } else if (node.nodeType === Node.ELEMENT_NODE) {
                            const tag = (node.tagName || '').toLowerCase();
                            // Add newlines for block elements so we can separate titles from descriptions
                            if (['p', 'div', 'br', 'li', 'span'].includes(tag)) {
                                text += '\\n';
                            }
                        }
                        
                        // Dive into the element's shadow root if it has one
                        if (node.shadowRoot) {
                            text += getDeepText(node.shadowRoot);
                        }
                        // Dive into its children
                        if (node.childNodes) {
                            for (let child of node.childNodes) {
                                text += getDeepText(child);
                            }
                        }
                        return text;
                    }
                    return getDeepText(el).trim();
                }""")
                
                if not raw_text:
                    continue
                    
                # 4. Split by newline and take the first line (Title), ignoring descriptions
                lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
                if lines:
                    model_name = lines[0]
                    # Double check it's not an empty string or a leftover UI artifact
                    if model_name and model_name not in fetched_models and model_name.lower() not in ['check', 'close']:
                        fetched_models.append(model_name)
                
        if fetched_models:
            AVAILABLE_MODELS = fetched_models
            print(f"  [Debug] Successfully fetched models: {AVAILABLE_MODELS}", flush=True)
        else:
            print("  [Debug] Fetched models list was empty. Using defaults.", flush=True)
            
        # Close the dropdown securely
        await page.keyboard.press('Escape')
        await asyncio.sleep(0.5)
        
    except Exception as e:
        print(f"  [Debug] Failed to fetch dynamic models: {e}. Using defaults.", flush=True)

async def init_browser():
    global playwright_instance, context, is_ready
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    
    first_time = not LOGIN_FLAG.exists()
    
    if first_time:
        print("\n" + "="*50)
        print("  FIRST TIME SETUP - Please log into account via SSO")
        print("="*50 + "\n")
    else:
        print("🚀 Starting service in headless mode...")
        
    playwright_instance = await async_playwright().start()
    
    headless_args = [] if first_time else ["--headless=new"]
    
    context = await playwright_instance.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        channel="chrome",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            #*headless_args
        ],
        viewport={"width": 1280, "height": 900},
    )
    
    page = await context.new_page()
    
    await Stealth().apply_stealth_async(page)
    
    await page.goto(TARGET_URL)
    
    if first_time:
        print("📌 Browser opened - please log into your account")
        print("   Waiting for login...\n")
        for i in range(150):
            if await check_logged_in(page, timeout=3000):
                LOGIN_FLAG.write_text("ok")
                print("\n✅ Login saved! Restarting in headless mode...\n")
                await page.close()
                await context.close()
                await playwright_instance.stop()
                return await init_browser()
            await asyncio.sleep(2)
            if i % 15 == 0 and i > 0:
                print(f"   Still waiting... ({i*2}s)")
                
        print("❌ Login timeout")
        return
        
    # --- FIX STARTS HERE: Handle headless SSO / "Choose an account" ---
    print(f"  [Debug] Headless browser landed on URL: {page.url}", flush=True)
    try:
        await asyncio.sleep(3)
        account_btn = await page.query_selector('div[data-identifier], .lCoei, [data-email], [data-authuser="0"]')
        if account_btn and ("signin" in page.url or "ServiceLogin" in page.url or "account" in page.url):
            print("  → Bypassing 'Choose an account' screen in headless mode...", flush=True)
            await account_btn.click()
            await asyncio.sleep(5)
    except Exception as e:
        pass
    # --- FIX ENDS HERE ---

    if not await check_logged_in(page, timeout=25000):
        print(f"❌ Session expired or not ready. Stuck on: {page.url}", flush=True)
        print("Deleting login flag, please restart", flush=True)
        LOGIN_FLAG.unlink(missing_ok=True)
        return

    # Fetch enterprise models
    await fetch_available_models(page)
    
    await page.close()
    
    is_ready = True
    print("✅ Service ready! Local gateway is online.")
    print("🎯 API: http://localhost:8080/v1/chat/completions\n")

async def get_or_create_session_page(session_id: str = "default") -> Page:
    """Get exactly one persistent Vertex page for each client conversation."""
    global session_pages, page_locks, session_creation_locks

    # Multiple requests for a new Hermes chat commonly arrive together (for
    # example, streamed completion plus title generation).  Without this lock,
    # both requests create separate Vertex pages for the same key.
    creation_lock = session_creation_locks.setdefault(session_id, asyncio.Lock())
    async with creation_lock:
        page = session_pages.get(session_id)
        if page and not page.is_closed():
            return page

        session_pages.pop(session_id, None)
        page_locks.pop(session_id, None)

        print(f"  → New session: {session_id}", flush=True)
        # Never borrow an arbitrary existing browser tab.  A new proxy session
        # must start a new Vertex /r/session/... conversation.
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        await page.goto(TARGET_URL)
        await asyncio.sleep(2)

        try:
            account_btn = await page.query_selector('div[data-identifier], .lCoei, [data-email], [data-authuser="0"]')
            if account_btn and "signin" in page.url:
                print("  → Bypassing 'Choose an account' screen...", flush=True)
                await account_btn.click()
                await asyncio.sleep(3)
        except Exception as e:
            print(f"  [Debug] Account chooser check failed: {e}", flush=True)

        await page.wait_for_selector('ucs-prosemirror-editor', timeout=15000)
        session_pages[session_id] = page
        page_locks[session_id] = asyncio.Lock()
        print(f"  ✓ Session {session_id} ready", flush=True)
        return page

async def switch_model(page: Page, target_model: str):
    if not target_model or target_model.lower() == "gemini-enterprise":
        return
        
    try:
        label_locator = page.locator('.model-selector-label')
        if await label_locator.count() > 0:
            current_label = await label_locator.first.inner_text()
            if target_model.lower() in current_label.lower():
                return
            
            await page.locator('#model-selector-menu-anchor, .action-model-selector').first.click()
            await asyncio.sleep(1.0)
            
            target_regex = re.compile(target_model, re.IGNORECASE)
            options = page.locator('md-menu-item, [role="menuitem"]').filter(has_text=target_regex)
            
            clicked = False
            
            if await options.count() > 0:
                for i in range(await options.count()):
                    if await options.nth(i).is_visible():
                        await options.nth(i).click()
                        clicked = True
                        break
            
            if not clicked:
                fallback_options = page.locator('ucs-model-selector').get_by_text(target_regex)
                if await fallback_options.count() > 0:
                    for i in range(await fallback_options.count()):
                        class_name = await fallback_options.nth(i).evaluate("(el) => el.className || ''")
                        if 'model-selector-label' not in class_name and await fallback_options.nth(i).is_visible():
                            await fallback_options.nth(i).click()
                            clicked = True
                            break
            
            if clicked:
                await asyncio.sleep(1.0)
            else:
                await page.keyboard.press('Escape')
                
    except Exception as e:
        print(f"  [Debug] Error switching model: {e}", flush=True)

# ==============================================================================
# CORE EXTRACTION & PROMPT EXECUTION
# ==============================================================================
async def send_to_gemini(page: Page, text: str, model: str = None, timeout: int = 180) -> str:
    if model:
        await switch_model(page, model)

    response_selector = '.turn'
    existing_responses = await page.query_selector_all(response_selector)
    response_count_before = len(existing_responses)
    
    try:
        input_box = page.locator('ucs-prosemirror-editor#agent-search-prosemirror-editor, ucs-prosemirror-editor').first
        await input_box.wait_for(state="visible", timeout=10000)
        await input_box.click()
        await asyncio.sleep(0.3)
        
        await page.keyboard.press('Control+A')
        await page.keyboard.press('Meta+A')
        await page.keyboard.press('Backspace')
        await asyncio.sleep(0.1)
        
        # Use copy-paste style fast text insertion for very large tool prompts to avoid slow typing
        await page.evaluate('''([box, txt]) => {
            box.focus();
            document.execCommand('insertText', false, txt);
        }''', [await input_box.element_handle(), text])
        
        await asyncio.sleep(0.3)
    except Exception as e:
        print(f"Error targeting chat box: {e}", flush=True)
        
    try:
        send_button = page.locator('.send-button.submit, button[aria-label="Submit"]').first
        await send_button.wait_for(state="visible", timeout=3000)
        await send_button.click()
    except:
        await page.keyboard.press('Enter')
        
    await asyncio.sleep(1)

    response_text = None
    start_time = time.monotonic()
    previous_text = ""
    first_content_at: Optional[float] = None
    last_text_change_at: Optional[float] = None
    last_active_work_at: Optional[float] = None
    has_seen_generation_start = False
    has_seen_active_work = False

    while (time.monotonic() - start_time) < timeout:
        try:
            response_divs = await page.query_selector_all('.turn')
            current_count = len(response_divs)
            
            if current_count > response_count_before or (response_count_before == 0 and current_count > 0):
                last_turn = response_divs[-1]
                
                state = await last_turn.evaluate(JS_STATUS_AND_EXTRACTOR)
                
                is_generating = state.get("isGenerating", False)
                has_active_work = state.get("hasActiveWork", False)
                current_text = clean_response_text(state.get("text", ""))
                now = time.monotonic()
                text_changed = current_text != previous_text
                previous_text = current_text

                if current_text and first_content_at is None:
                    first_content_at = now
                if text_changed:
                    last_text_change_at = now
                if has_active_work:
                    has_seen_active_work = True
                    last_active_work_at = now

                if current_text or is_generating or has_active_work:
                    has_seen_generation_start = True

                if (
                    has_seen_generation_start
                    and current_text
                    and not is_generating
                    and not has_active_work
                    and first_content_at is not None
                    and last_text_change_at is not None
                ):
                    grace_seconds = (
                        INTERMEDIATE_STATUS_GRACE_SECONDS
                        if is_intermediate_status(current_text) and not has_seen_active_work
                        else INITIAL_RESPONSE_GRACE_SECONDS
                    )
                    quiet_since = max(
                        last_text_change_at,
                        last_active_work_at or last_text_change_at,
                    )
                    if (
                        now - first_content_at >= grace_seconds
                        and now - quiet_since >= FINAL_RESPONSE_QUIET_SECONDS
                    ):
                        response_text = current_text
                        break
        except Exception as e:
            print(f"  [Debug] Error polling response: {e}", flush=True)
            
        await asyncio.sleep(0.5)
        
    if not response_text:
        raise HTTPException(status_code=504, detail="Gateway timed out waiting for AI response.")
        
    return response_text
    
async def stream_from_gemini(session_id: str, page: Page, text: str, model: str = None, timeout: int = 180):
    """Yield an OpenAI-compatible SSE stream while Gemini is generating."""
    completion_id = f"chatcmpl-{int(time.time() * 1000)}"
    created = int(time.time())

    def event(delta: Dict[str, Any], finish_reason: Optional[str] = None) -> str:
        """Encode one Chat Completions chunk in the OpenAI SSE format."""
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async with page_locks[session_id]:
        if model:
            await switch_model(page, model)

        response_selector = '.turn'
        existing_responses = await page.query_selector_all(response_selector)
        response_count_before = len(existing_responses)
        
        try:
            input_box = page.locator('ucs-prosemirror-editor#agent-search-prosemirror-editor, ucs-prosemirror-editor').first
            await input_box.wait_for(state="visible", timeout=10000)
            await input_box.click()
            await asyncio.sleep(0.3)
            
            await page.keyboard.press('Control+A')
            await page.keyboard.press('Meta+A')
            await page.keyboard.press('Backspace')
            await asyncio.sleep(0.1)
            
            await page.evaluate('''([box, txt]) => {
                box.focus();
                document.execCommand('insertText', false, txt);
            }''', [await input_box.element_handle(), text])
            
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"Error targeting chat box: {e}", flush=True)
            
        try:
            send_button = page.locator('.send-button.submit, button[aria-label="Submit"]').first
            await send_button.wait_for(state="visible", timeout=3000)
            await send_button.click()
        except:
            await page.keyboard.press('Enter')
            
        await asyncio.sleep(1)

        start_time = time.monotonic()
        previous_text = ""
        first_content_at: Optional[float] = None
        last_text_change_at: Optional[float] = None
        last_active_work_at: Optional[float] = None
        last_keepalive_at = start_time
        has_seen_generation_start = False
        has_seen_active_work = False

        while (time.monotonic() - start_time) < timeout:
            try:
                response_divs = await page.query_selector_all('.turn')
                current_count = len(response_divs)
                
                if current_count > response_count_before or (response_count_before == 0 and current_count > 0):
                    last_turn = response_divs[-1]
                    state = await last_turn.evaluate(JS_STATUS_AND_EXTRACTOR)
                    
                    is_generating = state.get("isGenerating", False)
                    has_active_work = state.get("hasActiveWork", False)
                    current_text = clean_response_text(state.get("text", ""))
                    now = time.monotonic()
                    text_changed = current_text != previous_text
                    previous_text = current_text

                    if current_text and first_content_at is None:
                        first_content_at = now
                    if text_changed:
                        last_text_change_at = now
                    if has_active_work:
                        has_seen_active_work = True
                        last_active_work_at = now

                    if current_text or is_generating or has_active_work:
                        has_seen_generation_start = True

                    # Browser-rendered enterprise responses can replace the
                    # provisional plan with the final answer.  Buffer the UI
                    # snapshots and emit one complete OpenAI content delta only
                    # after retrieval and rendering have both gone quiet.
                    if (
                        has_seen_generation_start
                        and current_text
                        and not is_generating
                        and not has_active_work
                        and first_content_at is not None
                        and last_text_change_at is not None
                    ):
                        grace_seconds = (
                            INTERMEDIATE_STATUS_GRACE_SECONDS
                            if is_intermediate_status(current_text) and not has_seen_active_work
                            else INITIAL_RESPONSE_GRACE_SECONDS
                        )
                        quiet_since = max(
                            last_text_change_at,
                            last_active_work_at or last_text_change_at,
                        )
                        if (
                            now - first_content_at >= grace_seconds
                            and now - quiet_since >= FINAL_RESPONSE_QUIET_SECONDS
                        ):
                            yield event({"role": "assistant", "content": ""})
                            yield event({"content": current_text})
                            yield event({}, "stop")
                            yield "data: [DONE]\n\n"
                            break
            except Exception as e:
                # Do not hide streaming failures: swallowing them makes the
                # client appear to hang and obscures the actual extractor issue.
                print(f"  [Debug] Error polling stream: {e}", flush=True)

            # A standard SSE comment keeps the HTTP response alive while the
            # enterprise UI is retrieving documents.  It is ignored by clients.
            now = time.monotonic()
            if now - last_keepalive_at >= 10:
                yield ": keep-alive\n\n"
                last_keepalive_at = now

            await asyncio.sleep(0.5)


# ==============================================================================
# API ROUTING
# ==============================================================================
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(init_browser())

@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google"
            }
            for m in AVAILABLE_MODELS
        ]
    }

@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(req: ChatRequest, request: Request):
    if not is_ready:
        raise HTTPException(status_code=503, detail="Gateway is initializing or awaiting authentication.")
        
    session_id = get_session_id(req, request)
    page = await get_or_create_session_page(session_id)
    
    # Generate formatted conversation string combining tools and messages
    conversation = format_conversation(req.messages, req.tools)
    print(
        f"📥 [{time.strftime('%H:%M:%S')}] Chat {session_id[-8:]}: "
        f"{len(req.messages)} msgs (Stream: {req.stream})...",
        flush=True,
    )

    # 1. STREAMING MODE (Prevents Hermes Timeout)
    if req.stream:
        # Pass session_id to the generator so it can lock the page while streaming
        return StreamingResponse(
            stream_from_gemini(session_id, page, conversation, model=req.model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 2. NORMAL/BLOCKING MODE (Includes your Tool Calling logic)
    async with page_locks[session_id]:
        ai_reply = await send_to_gemini(page, conversation, model=req.model)
        
        tool_calls = None
        finish_reason = "stop"
        
        # If tools were provided, attempt to extract them from the response
        if req.tools:
            parsed_tools = parse_tool_calls(ai_reply)
            if parsed_tools:
                tool_calls = []
                for i, tc in enumerate(parsed_tools):
                    tool_calls.append({
                        "id": f"call_{int(time.time())}_{i}",
                        "type": "function",
                        "function": {
                            "name": tc.get("name"),
                            "arguments": json.dumps(tc.get("arguments", {}))
                        }
                    })
                finish_reason = "tool_calls"
                print(f"🔧 Tool calls detected: {[tc['function']['name'] for tc in tool_calls]}")
        
        msg = {"role": "assistant"}
        if tool_calls:
            msg["tool_calls"] = tool_calls
            msg["content"] = None
        else:
            msg["content"] = ai_reply
            
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": msg,
                    "finish_reason": finish_reason
                }
            ],
            "usage": {
                "prompt_tokens": len(conversation),
                "completion_tokens": len(ai_reply),
                "total_tokens": len(conversation) + len(ai_reply)
            }
        }
        
        
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
