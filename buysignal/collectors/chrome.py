"""One headless Chrome session, always cleaned up.

A crashed tab orphans helper processes to PID 1, which compose's `init: true` reaps. Each session
gets its own temporary profile, removed here because a crashed session never removes it.
"""

from __future__ import annotations

import tempfile
from contextlib import ExitStack, contextmanager

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from buysignal.config import CHROMEDRIVER_LOG, CHROME_VERBOSE_LOG
from buysignal.redaction import public_env
from buysignal.resources import HEAVY_WORK_LOCK


def _options(profile_dir: str) -> Options:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-sync")
    options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
    options.page_load_strategy = "eager"
    options.add_argument("--window-size=1920,1200")
    options.add_argument(f"--user-data-dir={profile_dir}")
    return options


@contextmanager
def chrome_session(page_load_timeout: float):
    """Yield a headless Chrome driver, quitting it and removing its profile however the body ends."""
    with HEAVY_WORK_LOCK, ExitStack() as stack:
        profile_dir = stack.enter_context(tempfile.TemporaryDirectory(prefix="buysignal-chrome-", ignore_cleanup_errors=True))
        CHROMEDRIVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        log_file = stack.enter_context(open(CHROMEDRIVER_LOG, "w"))
        # The browser loads third-party pages: no credentials in its environment.
        service = Service(service_args=["--verbose"] if CHROME_VERBOSE_LOG else [], log_output=log_file, env=public_env())
        driver = webdriver.Chrome(options=_options(profile_dir), service=service)
        try:
            driver.set_page_load_timeout(page_load_timeout)
            # Match the installed browser rather than a hardcoded Chrome version.
            agent = driver.execute_script("return navigator.userAgent")
            if isinstance(agent, str) and "HeadlessChrome" in agent:
                driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": agent.replace("HeadlessChrome", "Chrome")})
            yield driver
        finally:
            driver.quit()
