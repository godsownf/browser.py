import os, time, json, logging
from urllib.parse import urlparse
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# ======================================================
# Config & Logging
# ======================================================
load_dotenv("config.env")

def env(k, d=None): return os.getenv(k, d)
def on(k): return env(k, "0") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s"
)

# ======================================================
# Cookie rules (RFC‑correct)
# ======================================================
def _domain_match(cd, host):
    if not cd: return False
    cd = cd.lstrip(".").lower()
    host = host.lower()
    return host == cd or host.endswith("." + cd)

def _path_match(cp, req):
    if not cp: return True
    return req.startswith(cp if cp.startswith("/") else "/" + cp)

def load_cookies_domain_safe(driver, cookie_file, target_url):
    if not os.path.exists(cookie_file):
        logging.info("No cookie file found")
        return

    p = urlparse(target_url)
    base = f"{p.scheme}://{p.hostname}/"
    req_path = p.path or "/"

    driver.get(base)

    with open(cookie_file, "r", encoding="utf-8") as f:
        cookies = json.load(f)

    added = skipped = 0

    for c in cookies:
        c = dict(c)
        c.pop("sameSite", None)

        if not _domain_match(c.get("domain"), p.hostname):
            skipped += 1; continue
        if not _path_match(c.get("path", "/"), req_path):
            skipped += 1; continue
        if c.get("secure") and p.scheme != "https":
            skipped += 1; continue

        try:
            driver.add_cookie(c)
            added += 1
        except Exception:
            skipped += 1

    driver.get(target_url)
    driver.refresh()
    logging.info(f"Cookies applied: {added}, skipped: {skipped}")

# ======================================================
# Browser bootstrap (boring = good)
# ======================================================
def start_browser():
    opts = Options()
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--disable-blink-features=AutomationControlled")

    if on("PERSIST_PROFILE"):
        opts.add_argument(f"--user-data-dir={env('PROFILE_DIR')}")

    opts.add_argument(f"user-agent={env('USER_AGENT')}")
    opts.add_argument(f"--window-size={env('WINDOW_WIDTH')},{env('WINDOW_HEIGHT')}")
    opts.add_argument(f"--lang={env('LANG')}")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts
    )

    if env("TIMEZONE"):
        driver.execute_cdp_cmd(
            "Emulation.setTimezoneOverride",
            {"timezoneId": env("TIMEZONE")}
        )

    return driver

# ======================================================
# Fingerprint observation (passive)
# ======================================================
def inject_fp_detection(driver):
    if not on("FP_DETECT"): return

    driver.execute_script("""
    (() => {
      if (window.__fp_used) return;
      window.__fp_used = {webgl:false,canvas:false,audio:false};

      try {
        const g = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(){
          window.__fp_used.webgl = true;
          return g.apply(this, arguments);
        };
      } catch(e){}

      try {
        const t = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(){
          window.__fp_used.canvas = true;
          return t.apply(this, arguments);
        };
      } catch(e){}

      try {
        const a = AudioContext.prototype.getChannelData;
        AudioContext.prototype.getChannelData = function(){
          window.__fp_used.audio = true;
          return a.apply(this, arguments);
        };
      } catch(e){}
    })();
    """)

def apply_fp_overrides(driver, used):
    if used.get("webgl") and on("FP_WEBGL"):
        driver.execute_script(f"""
        const g = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p){
          if(p===37445) return "{env('WEBGL_VENDOR')}";
          if(p===37446) return "{env('WEBGL_RENDERER')}";
          return g.call(this,p);
        };
        """)

    if used.get("canvas") and on("FP_CANVAS"):
        driver.execute_script("""
        const t = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(){
          const c=this.getContext('2d'); c.globalAlpha=0.999999;
          return t.apply(this,arguments);
        };
        """)

    if used.get("audio") and on("FP_AUDIO"):
        driver.execute_script("""
        const o = AudioContext.prototype.getChannelData;
        AudioContext.prototype.getChannelData = function(){
          const d=o.apply(this,arguments);
          for(let i=0;i<d.length;i+=100)d[i]+=1e-7;
          return d;
        };
        """)

# ======================================================
# Login flow (cookie‑first, token optional)
# ======================================================
def browser_login():
    driver = start_browser()
    url = env("TARGET_URL")
    driver.get(url)
    time.sleep(2)

    load_cookies_domain_safe(driver, env("COOKIE_FILE"), url)

    if env("LOGIN_TOKEN"):
        driver.execute_script(f"""
        (() => {{
          setTimeout(() => {{
            localStorage.token = "{env('LOGIN_TOKEN')}";
            location.reload();
          }}, 500);
        }})();
        """)

    inject_fp_detection(driver)
    time.sleep(2)

    used = driver.execute_script("return window.__fp_used || {}")
    logging.info(f"Fingerprint surfaces used: {used}")

    apply_fp_overrides(driver, used)

    logging.info("Session stabilized")

    while True:
        time.sleep(60)

# ======================================================
# Entry
# ======================================================
if __name__ == "__main__":
    browser_login()
