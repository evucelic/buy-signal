# TODO implement the changes below

"""Scrape CME FedWatch meeting probabilities and cache the meeting data.

Collection plan for the current FedRate workflow:
- pull the FedWatch page
- find the meeting closest to one year from today
- if two meetings are equally far away, choose the later one
- scrape the meeting's Probabilities table from the QuikStrike iframe
- use the Ease / No Change / Hike percentages directly as the meeting probabilities
- cache the selected meeting date plus those three probabilities for the signal layer

The brittle Selenium fetch (`_fetch_html`) should stay separate from the pure parsing
helper so the scraping logic is testable offline.
"""
import io
import os
import random
import time
import urllib.request

import pandas as pd

from config import (FEDWATCH_CSV, FEDWATCH_URL, FETCH_JITTER_SEC, FRED_TARGET_RANGE_CSV,
                    SCRAPE_BACKOFF_SEC, SCRAPE_RETRIES, USER_AGENTS)


def _build_driver():
    """Headless Chrome with the stealth needed for CME to serve the real page."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"):
        opts.add_argument(arg)
    opts.add_argument(f"--window-size={random.randint(1680, 1920)},{random.randint(1200, 1440)}")
    opts.add_argument(f"user-agent={random.choice(USER_AGENTS)}")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                           {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"})
    driver.set_page_load_timeout(60)
    return driver


def _fetch_html():
    """Scrape and return the FedWatch probabilities table HTML (meeting x bucket grid)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    driver = _build_driver()
    try:
        driver.get(FEDWATCH_URL)
        wait = WebDriverWait(driver, 40)
        driver.switch_to.frame(wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "iframe[id^='cmeIframe-']"))))   # the QuikStrike iframe
        tab = wait.until(EC.presence_of_element_located(
            (By.XPATH, "//a[normalize-space()='Probabilities']")))
        driver.execute_script("arguments[0].click();", tab)   # JS click dodges the sticky header
        table = wait.until(EC.presence_of_element_located(
            (By.XPATH, "//table[contains(., 'Conditional Meeting Probabilities')]")))
        return table.get_attribute("outerHTML")
    finally:
        driver.quit()


def _bucket_midpoint(label):
    """Midpoint (%) of a bucket like '275-300' (bps) or '4.25-4.50' (%)."""
    low, high = (float(x) for x in label.split("-"))
    if low > 100 and high > 100:   # bps -> percent
        low, high = low / 100, high / 100
    return (low + high) / 2


def _parse_probabilities(html):
    """Probabilities table HTML -> DataFrame[meeting_date, expected_rate]. Pure, no network."""
    df = pd.read_html(io.StringIO(html))[0]
    df.columns = [c[-1] if isinstance(c, tuple) else c for c in df.columns]

    dates = pd.to_datetime(df["Meeting Date"], format="%m/%d/%Y")
    buckets = [c for c in df.columns if c != "Meeting Date"]
    probs = df[buckets].apply(lambda s: s.astype(str).str.rstrip("%").astype(float) / 100)
    midpoints = pd.Series({b: _bucket_midpoint(b) for b in buckets})
    return pd.DataFrame({"meeting_date": dates,
                         "expected_rate": probs.mul(midpoints, axis=1).sum(axis=1)})


def _current_target_rate():
    """Current fed funds target midpoint (%) from FRED's range bounds."""
    raw = urllib.request.urlopen(FRED_TARGET_RANGE_CSV, timeout=30).read()
    last = pd.read_csv(io.BytesIO(raw)).dropna().iloc[-1]
    return (float(last["DFEDTARU"]) + float(last["DFEDTARL"])) / 2


def update_fed_rate_data(filepath=FEDWATCH_CSV):
    """Scrape FedWatch + current rate; cache the snapshot (overwrite). Never crashes."""
    filepath = str(filepath)
    time.sleep(random.uniform(*FETCH_JITTER_SEC))   # jitter before the first attempt

    for attempt in range(SCRAPE_RETRIES):
        try:
            snapshot = _parse_probabilities(_fetch_html())
            break
        except Exception as exc:   # scrape/parse is brittle; back off and retry
            if attempt + 1 == SCRAPE_RETRIES:
                print(f"FedWatch scrape failed after {SCRAPE_RETRIES} tries "
                      f"({type(exc).__name__}); cache unchanged.")
                return
            wait = SCRAPE_BACKOFF_SEC * 2 ** attempt + random.uniform(0, 1)
            print(f"FedWatch attempt {attempt + 1} failed ({type(exc).__name__}); retry in {wait:.0f}s")
            time.sleep(wait)

    try:
        current = _current_target_rate()
    except Exception as exc:   # fall back to the nearest meeting if FRED is unreachable
        current = float(snapshot["expected_rate"].iloc[0])
        print(f"FRED current-rate fetch failed ({type(exc).__name__}); using nearest meeting {current:.3f}%")
    snapshot["current_rate"] = current

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    snapshot.to_csv(filepath, index=False)
    print(f"FedWatch cache updated: {len(snapshot)} meetings, "
          f"{snapshot['meeting_date'].iloc[0].date()} -> {snapshot['meeting_date'].iloc[-1].date()}, "
          f"current {current:.3f}%")


def latest_fedwatch(filepath=FEDWATCH_CSV):
    """Cached snapshot as DataFrame[meeting_date, expected_rate, current_rate]."""
    return pd.read_csv(filepath, parse_dates=["meeting_date"])


if __name__ == "__main__":
    update_fed_rate_data()
    print(latest_fedwatch().to_string(index=False))
