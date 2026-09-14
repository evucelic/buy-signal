"""One headless Chrome session, always cleaned up.

fed_rate.py and valuations.py each built their own driver, and fed_rate's sat outside the
try that quits it: anything failing between the browser starting and the try beginning
(set_page_load_timeout on a half-dead session, most often) left a chrome process behind -
three times per retrying pass, ~10 passes a day, in a container with no PID or memory limit.
A context manager makes that unrepresentable: there is no path that starts a browser without
quitting it.
"""

from __future__ import annotations

import random
from contextlib import contextmanager

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from config import USER_AGENTS


def _options() -> Options:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1200")
    options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")
    return options


@contextmanager
def chrome_session(page_load_timeout: float):
    """Yield a headless Chrome driver, quitting it however the body ends."""
    driver = webdriver.Chrome(options=_options())
    try:
        driver.set_page_load_timeout(page_load_timeout)
        yield driver
    finally:
        driver.quit()
