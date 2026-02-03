import os, json, asyncio, logging
from urllib.parse import urlparse
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# ======================================================
# Config helpers
# ======================================================
load_dotenv("config.env")

def env(k, d=None): return os.getenv(k, d)
def on(k): return env(k, "0") == "1"

# ======================================================
# Logging
# ======================================================
logging.basicConfig(
    level=getattr(logging, env("LOG_LEVEL", "INFO")),
    format="[%(levelname)s] %(message)s"
)

# ======================================================
# Policy loader (per-site)
# ======================================================
def load_site_policy(url):
    policy_dir = env("POLICY_DIR")
    if not policy_dir:
        return
    host = urlparse(url).hostname
    path = os.path.join(policy_dir, f"{host}.json")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for k, v in json.load(f).items():
            os.environ[k] = str(v)
    logging.info(f"Loaded policy for {host}")

# ======================================================
# Fingerprint init script (applied before any site JS)
# ======================================================
def fingerprint_init_script():
    scripts = []

    if on("REMOVE_NAVIGATOR_WEBDRIVER"):
        scripts.append(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )

    if env("FAKE_HARDWARE_CONCURRENCY"):
        scripts.append(f"""
        Object.defineProperty(navigator,'hardwareConcurrency',{{
          get:()=>{int(env('FAKE_HARDWARE_CONCURRENCY'))}
        }});
        """)

    if env("FAKE_DEVICE_MEMORY"):
        scripts.append(f"""
        Object.defineProperty(navigator,'deviceMemory',{{
          get:()=>{int(env('FAKE_DEVICE_MEMORY'))}
        }});
        """)

    if on("FP_WEBGL"):
        scripts.append(f"""
        const g=WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter=function(p){{
          if(p===37445) return "{env('WEBGL_VENDOR')}";
          if(p===37446) return "{env('WEBGL_RENDERER')}";
          return g.call(this,p);
        }};
        """)

    if on("FP_CANVAS"):
        scripts.append("""
        const t=HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL=function(){
          const c=this.getContext('2d'); c.globalAlpha=0.999999;
          return t.apply(this,arguments);
        };
        """)

    if on("FP_AUDIO"):
        scripts.append("""
        const o=AudioContext.prototype.getChannelData;
        AudioContext.prototype.getChannelData=function(){
          const d=o.apply(this,arguments);
          for(let i=0;i<d.length;i+=100)d[i]+=1e-7;
          return d;
        };
        """)

    if on("FP_DETECT"):
        scripts.append("""
        window.__fp_used={webgl:false,canvas:false,audio:false};
        try{
          const g=WebGLRenderingContext.prototype.getParameter;
          WebGLRenderingContext.prototype.getParameter=function(){
            window.__fp_used.webgl=true;
            return g.apply(this,arguments);
          };
        }catch(e){}
        try{
          const t=HTMLCanvasElement.prototype.toDataURL;
          HTMLCanvasElement.prototype.toDataURL=function(){
            window.__fp_used.canvas=true;
            return t.apply(this,arguments);
          };
        }catch(e){}
        try{
          const a=AudioContext.prototype.getChannelData;
          AudioContext.prototype.getChannelData=function(){
            window.__fp_used.audio=true;
            return a.apply(this,arguments);
          };
        }catch(e){}
        """)

    return "\n".join(scripts)

# ======================================================
# Network / request alignment
# ======================================================
async def align_requests(context):
    if not on("USE_CUSTOM_HEADERS"):
        return

    headers = {}
    if env("ACCEPT_LANGUAGE"):
        headers["Accept-Language"] = env("ACCEPT_LANGUAGE")

    if env("CUSTOM_HEADERS"):
        try:
            headers.update(json.loads(env("CUSTOM_HEADERS")))
        except Exception:
            logging.warning("CUSTOM_HEADERS is not valid JSON")

    async def route_handler(route, request):
        await route.continue_(headers={**request.headers, **headers})

    await context.route("**/*", route_handler)

# ======================================================
# Main async flow
# ======================================================
async def main():
    target = env("TARGET_URL")
    load_site_policy(target)

    os.makedirs(env("FP_LOG_DIR"), exist_ok=True)
    os.makedirs(os.path.dirname(env("STORAGE_STATE")) or ".", exist_ok=True)

    headless = on("HEADLESS") or on("CI")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        context = await browser.new_context(
            user_agent=env("USER_AGENT"),
            locale=env("LOCALE", env("LANG")),
            timezone_id=env("TIMEZONE"),
            viewport={
                "width": int(env("WINDOW_WIDTH")),
                "height": int(env("WINDOW_HEIGHT"))
            },
            device_scale_factor=float(env("DEVICE_SCALE_FACTOR", "1")),
            is_mobile=on("IS_MOBILE"),
            has_touch=on("HAS_TOUCH"),
            storage_state=env("STORAGE_STATE")
            if os.path.exists(env("STORAGE_STATE")) else None
        )

        await context.add_init_script(fingerprint_init_script())
        await align_requests(context)

        page = await context.new_page()
        await page.goto(target, wait_until="domcontentloaded")

        if env("LOGIN_TOKEN"):
            await page.add_init_script(
                f'localStorage.token="{env("LOGIN_TOKEN")}"'
            )
            await page.reload()

        await page.wait_for_load_state("networkidle")

        if on("FP_DETECT") and on("ENABLE_FP_LOG"):
            used = await page.evaluate("window.__fp_used || {}")
            host = urlparse(target).hostname
            with open(os.path.join(env("FP_LOG_DIR"), f"{host}.json"), "w") as f:
                json.dump(used, f, indent=2)
            logging.info(f"Fingerprint used: {used}")

        if on("EXPORT_STORAGE"):
            await context.storage_state(path=env("STORAGE_STATE"))
            logging.info("storageState exported")

        logging.info("Session ready")

        if on("CI"):
            await browser.close()
        else:
            timeout = int(env("SESSION_TIMEOUT", "3600"))
            await asyncio.sleep(timeout)

if __name__ == "__main__":
    asyncio.run(main())
