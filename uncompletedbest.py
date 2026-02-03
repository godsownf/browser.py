import os, json, asyncio, logging, hashlib
from urllib.parse import urlparse
from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext

# ======================================================
# Config helpers
# ======================================================
load_dotenv("config.env")
def env(k, d=None): return os.getenv(k, d)
def on(k): return env(k, "0") == "1"

logging.basicConfig(
    level=getattr(logging, env("LOG_LEVEL", "INFO")),
    format="[%(levelname)s] %(message)s"
)

# ======================================================
# Utilities
# ======================================================
def mkdir(p): os.makedirs(p, exist_ok=True)
def sha(s): return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

# ======================================================
# Policy loader (per-site)
# ======================================================
def load_site_policy(url):
    host = urlparse(url).hostname
    p = os.path.join(env("POLICY_DIR"), f"{host}.json")
    if os.path.exists(p):
        with open(p) as f:
            for k,v in json.load(f).items():
                os.environ[k] = str(v)
        logging.info(f"Policy loaded for {host}")

# ======================================================
# Fingerprint init (pre‑JS)
# ======================================================
def fingerprint_init_script():
    s=[]
    if on("REMOVE_NAVIGATOR_WEBDRIVER"):
        s.append("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    if env("FAKE_HARDWARE_CONCURRENCY"):
        s.append(f"Object.defineProperty(navigator,'hardwareConcurrency',{{get:()=>{int(env('FAKE_HARDWARE_CONCURRENCY'))}}});")
    if env("FAKE_DEVICE_MEMORY"):
        s.append(f"Object.defineProperty(navigator,'deviceMemory',{{get:()=>{int(env('FAKE_DEVICE_MEMORY'))}}});")
    if on("FP_WEBGL"):
        s.append(f"""
        const g=WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter=function(p){{
          if(p===37445) return "{env('WEBGL_VENDOR')}";
          if(p===37446) return "{env('WEBGL_RENDERER')}";
          return g.call(this,p);
        }};
        """)
    if on("FP_CANVAS"):
        s.append("""
        const t=HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL=function(){
          const c=this.getContext('2d'); c.globalAlpha=0.999999;
          return t.apply(this,arguments);
        };
        """)
    if on("FP_AUDIO"):
        s.append("""
        const o=AudioContext.prototype.getChannelData;
        AudioContext.prototype.getChannelData=function(){
          const d=o.apply(this,arguments);
          for(let i=0;i<d.length;i+=100)d[i]+=1e-7;
          return d;
        };
        """)
    if on("FP_DETECT"):
        s.append("""
        window.__fp_used={webgl:false,canvas:false,audio:false};
        try{const g=WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter=function(){
          window.__fp_used.webgl=true; return g.apply(this,arguments);};}catch(e){}
        try{const t=HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL=function(){
          window.__fp_used.canvas=true; return t.apply(this,arguments);};}catch(e){}
        try{const a=AudioContext.prototype.getChannelData;
        AudioContext.prototype.getChannelData=function(){
          window.__fp_used.audio=true; return a.apply(this,arguments);};}catch(e){}
        """)
    return "\n".join(s)

# ======================================================
# Network alignment + capture
# ======================================================
def normalize_req(req):
    return {
        "url": req.url,
        "method": req.method,
        "headers": {k.lower(): v for k,v in req.headers.items()},
        "postData": req.post_data or ""
    }

async def align_and_capture(context: BrowserContext, out_dir: str):
    mkdir(out_dir)
    captured = []

    headers_add = {}
    if env("ACCEPT_LANGUAGE"):
        headers_add["Accept-Language"] = env("ACCEPT_LANGUAGE")
    if on("USE_CUSTOM_HEADERS") and env("CUSTOM_HEADERS"):
        try: headers_add.update(json.loads(env("CUSTOM_HEADERS")))
        except: logging.warning("CUSTOM_HEADERS invalid JSON")

    async def route(route, request):
        await route.continue_(headers={**request.headers, **headers_add})

    context.on("request", lambda r: captured.append(normalize_req(r)))
    await context.route("**/*", route)

    async def dump():
        with open(os.path.join(out_dir, "requests.json"), "w") as f:
            json.dump(captured, f, indent=2)
    return dump, captured

def diff_requests(a, b):
    sa = {sha(json.dumps(x, sort_keys=True)): x for x in a}
    sb = {sha(json.dumps(x, sort_keys=True)): x for x in b}
    added = [sb[k] for k in sb.keys() - sa.keys()]
    removed = [sa[k] for k in sa.keys() - sb.keys()]
    return {"added": added, "removed": removed}

# ======================================================
# Advanced one-account runner
# ======================================================
async def run_account(p, account_id, headless_override=None):
    target = env("TARGET_URL")
    load_site_policy(target)

    base_art = os.path.join("artifacts", f"acct_{account_id}")
    mkdir(base_art)

    headless = headless_override if headless_override is not None else (on("HEADLESS") or on("CI"))

    browser = await p.chromium.launch(headless=headless)
    ctx = await browser.new_context(
        viewport={"width": int(env("WINDOW_WIDTH", 1920)), "height": int(env("WINDOW_HEIGHT", 1080))},
        user_agent=env("USER_AGENT"),
        locale=env("LOCALE"),
        timezone_id=env("TIMEZONE"),
        device_scale_factor=int(env("DEVICE_SCALE_FACTOR", 1)),
        is_mobile=on("IS_MOBILE"),
        has_touch=on("HAS_TOUCH"),
        storage_state=os.path.join(env("PROFILE_DIR", "profiles"), f"acct_{account_id}_state.json") if on("EXPORT_STORAGE") else None
    )

    # Inject fingerprints
    await ctx.add_init_script(fingerprint_init_script())

    # Setup network capture
    dump_requests, captured_requests = await align_and_capture(ctx, base_art)

    page = await ctx.new_page()
    try:
        logging.info(f"[Account {account_id}] Navigating to {target}")
        await page.goto(target, timeout=int(env("SESSION_TIMEOUT", 3600)) * 1000)
        if env("WAIT_FOR_SELECTOR"):
            await page.wait_for_selector(env("WAIT_FOR_SELECTOR"), timeout=int(env("SESSION_TIMEOUT", 3600)) * 1000)
    except Exception as e:
        logging.error(f"[Account {account_id}] Navigation error: {e}")
    finally:
        # Dump captured requests
        await dump_requests()
        # Export storage state
        if on("EXPORT_STORAGE"):
            storage_path = os.path.join(base_art, "storage_state.json")
            await ctx.storage_state(path=storage_path)
            logging.info(f"[Account {account_id}] Storage state saved at {storage_path}")

        await ctx.close()
        await browser.close()
        logging.info(f"[Account {account_id}] Finished")
        return captured_requests

# ======================================================
# Advanced multi-account orchestrator with concurrency limit
# ======================================================
async def run_multiple_accounts(account_ids, concurrency=3):
    semaphore = asyncio.Semaphore(concurrency)
    results = []

    async def sem_task(acc_id):
        async with semaphore:
            return await run_account(p, acc_id)

    async with async_playwright() as p:
        tasks = [sem_task(acc_id) for acc_id in account_ids]
        results = await asyncio.gather(*tasks)
    return results

# ======================================================
# Entry point
# ======================================================
if __name__ == "__main__":
    account_ids = range(1, int(env("NUM_ACCOUNTS", 1)) + 1)
    all_requests = asyncio.run(run_multiple_accounts(account_ids, concurrency=int(env("MAX_CONCURRENCY", 3))))
    logging.info(f"All accounts finished. Captured requests: {len(all_requests)} sets")
