"""One headless Chrome session, always cleaned up.

fed_rate.py and valuations.py each built their own driver, and fed_rate's sat outside the
try that quits it: anything failing between the browser starting and the try beginning
(set_page_load_timeout on a half-dead session, most often) left a chrome process behind -
three times per retrying pass, ~10 passes a day, in a container with no PID or memory limit.
A context manager makes that unrepresentable: there is no path that starts a browser without
quitting it.

quit() is not the whole story, though. When a tab crashes, Chrome's helper processes are
orphaned and reparented to PID 1, which reaps nothing unless it is an init - hence `init: true`
in docker-compose.yml; without it the zombies filled pids_limit and every later session died
with "Chrome instance exited". The profile goes in a per-session temp dir removed here, since
a crashed session never cleans its own, and chromedriver logs verbosely to CHROMEDRIVER_LOG
so the next startup failure has something to examine.
"""

from __future__ import annotations

import tempfile
from contextlib import ExitStack, contextmanager

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from buysignal.config import CHROMEDRIVER_LOG, CHROME_VERBOSE_LOG
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
        service = Service(service_args=["--verbose"] if CHROME_VERBOSE_LOG else [], log_output=log_file)
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
