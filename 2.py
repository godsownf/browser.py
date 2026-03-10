import os
import time
import json
import logging
from urllib.parse import urlparse
from dotenv import load_dotenv
from typing import Dict, Any, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# =====================================================
# Configuration & Logging
# =====================================================
load_dotenv("config.env")

def env(k: str, d: Optional[str] = None) -> str:
    """
    Retrieves an environment variable or returns a default value.

    Args:
        k: The environment variable key.
        d: The default value to return if the environment variable is not set.

    Returns:
        The value of the environment variable or the default value.
    """
    return os.getenv(k, d)

def on(k: str) -> bool:
    """
    Checks if an environment variable is set to '1'.

    Args:
        k: The environment variable key.

    Returns:
        True if the environment variable is '1', False otherwise.
    """
    return env(k, "0") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s"
)

# =====================================================
# Cookie Handling (RFC-compliant)
# =====================================================
def _domain_match(cookie_domain: Optional[str], host: str) -> bool:
    """
    Checks if a cookie's domain matches the target host.

    Args:
        cookie_domain: The domain specified in the cookie.
        host: The target host to match against.

    Returns:
        True if the domain matches, False otherwise.
    """
    if not cookie_domain:
        return False
    cookie_domain = cookie_domain.lstrip(".").lower()
    host = host.lower()
    return host == cookie_domain or host.endswith("." + cookie_domain)

def _path_match(cookie_path: Optional[str], request_path: str) -> bool:
    """
    Checks if a cookie's path matches the requested path.

    Args:
        cookie_path: The path specified in the cookie.
        request_path: The requested path to match against.

    Returns:
        True if the path matches, False otherwise.
    """
    if not cookie_path:
        return True
    return request_path.startswith(cookie_path if cookie_path.startswith("/") else "/" + cookie_path)

def load_cookies_domain_safe(driver: webdriver.Chrome, cookie_file: str, target_url: str) -> None:
    """
    Loads cookies from a JSON file into the browser, ensuring domain and path
    compatibility with the target URL.

    Args:
        driver: The Selenium WebDriver instance.
        cookie_file: The path to the JSON cookie file.
        target_url: The URL to which the cookies should be applied.
    """
    if not os.path.exists(cookie_file):
        logging.info("No cookie file found.")
        return

    parsed_url = urlparse(target_url)
    base_url = f"{parsed_url.scheme}://{parsed_url.hostname}/"
    request_path = parsed_url.path or "/"

    driver.get(base_url) # Navigate to base to ensure cookies are set for the domain

    with open(cookie_file, "r", encoding="utf-8") as f:
        cookies = json.load(f)

    added_count = 0
    skipped_count = 0

    for cookie_data in cookies:
        cookie = dict(cookie_data) # Ensure we're working with a mutable dictionary
        cookie.pop("sameSite", None) # Remove SameSite attribute for broader compatibility

        if not _domain_match(cookie.get("domain"), parsed_url.hostname):
            skipped_count += 1
            continue
        if not _path_match(cookie.get("path", "/"), request_path):
            skipped_count += 1
            continue
        if cookie.get("secure") and parsed_url.scheme != "https":
            skipped_count += 1
            continue

        try:
            driver.add_cookie(cookie)
            added_count += 1
        except Exception as e:
            logging.warning(f"Failed to add cookie: {cookie}. Error: {e}")
            skipped_count += 1

    driver.get(target_url)
    driver.refresh()
    logging.info(f"Cookies applied: {added_count}, skipped: {skipped_count}")

# =====================================================
# Browser Bootstrap (Selenium Stealth & Configuration)
# =====================================================
def start_browser() -> webdriver.Chrome:
    """
    Initializes and configures the Selenium Chrome browser instance.

    Returns:
        A configured Selenium Chrome WebDriver instance.

    Raises:
        Exception: If the Chrome WebDriver fails to initialize.
    """
    opts = Options()

    # Selenium Stealth configurations
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--disable-blink-features=AutomationControlled")

    # Persistent profile option
    if on("PERSIST_PROFILE"):
        profile_dir = env('PROFILE_DIR')
        if profile_dir:
            opts.add_argument(f"--user-data-dir={profile_dir}")
        else:
            logging.warning("PERSIST_PROFILE is enabled but PROFILE_DIR is not set.")

    # User agent and window size
    user_agent = env('USER_AGENT', "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
    window_width = env('WINDOW_WIDTH', '1920')
    window_height = env('WINDOW_HEIGHT', '1080')
    language = env('LANG', 'en-US,en;q=0.9')

    opts.add_argument(f"user-agent={user_agent}")
    opts.add_argument(f"--window-size={window_width},{window_height}")
    opts.add_argument(f"--lang={language}")

    # Initialize WebDriver
    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=opts
        )
    except Exception as e:
        logging.error(f"Failed to initialize Chrome WebDriver: {e}")
        raise

    # Timezone override
    if env("TIMEZONE"):
        try:
            driver.execute_cdp_cmd(
                "Emulation.setTimezoneOverride",
                {"timezoneId": env("TIMEZONE")}
            )
        except Exception as e:
            logging.warning(f"Could not set timezone override: {e}")

    return driver

# =====================================================
# Fingerprint Observation & Overrides (Passive)
# =====================================================
def inject_fp_detection(driver: webdriver.Chrome) -> None:
    """
    Injects JavaScript to detect fingerprinting techniques.

    Args:
        driver: The Selenium WebDriver instance.
    """
    if not on("FP_DETECT"):
        return

    driver.execute_script("""
    (() => {
      if (window.__fp_used) return;
      window.__fp_used = {webgl:false, canvas:false, audio:false};

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

def apply_fp_overrides(driver: webdriver.Chrome, used: Dict[str, bool]) -> None:
    """
    Applies JavaScript overrides to mask fingerprinting data.

    Args:
        driver: The Selenium WebDriver instance.
        used: A dictionary indicating which fingerprinting surfaces were detected as used.
    """
    if used.get("webgl") and on("FP_WEBGL"):
        driver.execute_script(f"""
        const g = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p){{
          if(p===37445) return "{env('WEBGL_VENDOR', 'NVIDIA')}";
          if(p===37446) return "{env('WEBGL_RENDERER', 'NVIDIA GeForce RTX 3080')}";
          return g.call(this,p);
        }};
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

# =====================================================
# Login Flow (Cookie-first, Token Optional)
# =====================================================
def browser_login() -> None:
    """
    Handles the browser initialization, cookie loading, and optional token login.
    Includes fingerprint detection and overrides.
    """
    driver = start_browser()
    url = env("TARGET_URL")
    if not url:
        logging.error("TARGET_URL environment variable not set.")
        driver.quit()
        return

    driver.get(url)
    time.sleep(2) # Initial page load wait

    # Load cookies if a cookie file is specified
    cookie_file = env("COOKIE_FILE")
    if cookie_file:
        load_cookies_domain_safe(driver, cookie_file, url)
    else:
        logging.info("COOKIE_FILE not set, skipping cookie loading.")

    # Apply login token if provided
    login_token = env("LOGIN_TOKEN")
    if login_token:
        driver.execute_script(f"""
        (() => {{
          setTimeout(() => {{
            localStorage.token = "{login_token}";
            location.reload();
          }}, 500);
        }})();
        """)
        time.sleep(3) # Allow time for token to be applied and page to reload

    # Fingerprint detection and overrides
    inject_fp_detection(driver)
    time.sleep(2) # Give JS time to inject and run

    used = driver.execute_script("return window.__fp_used || {}")
    logging.info(f"Fingerprint surfaces detected as used: {used}")

    apply_fp_overrides(driver, used)

    logging.info("Session stabilized and browser ready.")

    # Keep the browser open
    while True:
        time.sleep(60)

# =====================================================
# Entry Point
# =====================================================
if __name__ == "__main__":
    browser_login()
```
