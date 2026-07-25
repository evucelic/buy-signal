"""Collect CME FedWatch probabilities for three time horizons."""

from __future__ import annotations

import random
import time
from pathlib import Path

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import (
    FEDWATCH_CSV,
    FEDWATCH_URL,
    FETCH_JITTER_SEC,
    SCRAPE_BACKOFF_SEC,
    SCRAPE_RETRIES,
    USER_AGENTS,
)

_HORIZONS = {
    "nearest": pd.DateOffset(),
    "six_month": pd.DateOffset(months=6),
    "one_year": pd.DateOffset(years=1),
}

_TABLE_XPATH = (
    "//table[contains(concat(' ', normalize-space(@class), ' '), ' grid-thm ')"
    " and .//th[normalize-space()='Ease']"
    " and .//th[normalize-space()='No Change']"
    " and .//th[normalize-space()='Hike']]"
)

# Meeting-date tabs on the default "Current" view, e.g. id "...uccv_lvMeetings_ctrl3_lbMeeting".
_MEETING_TAB_XPATH = "//a[contains(@id, '_lvMeetings_ctrl') and contains(@id, '_lbMeeting')]"

_TAB_DATE_FORMAT = "%d %b%y"     # tab text, e.g. "29 Jul26"
_CARD_DATE_FORMAT = "%d %b %Y"   # meeting-info card text once a tab is selected, e.g. "29 Jul 2026"


def _build_driver() -> webdriver.Chrome:
    """Return a headless Chrome driver."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1200")
    options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    return driver


def _percentage(text: str) -> float:
    """Convert a percentage string to a decimal probability."""
    return float(text.replace("%", "").strip()) / 100


def _list_meeting_tabs(wait: WebDriverWait) -> list[tuple[str, pd.Timestamp]]:
    """Return (element id, meeting date) for every meeting tab, in DOM order."""
    tabs = wait.until(EC.presence_of_all_elements_located((By.XPATH, _MEETING_TAB_XPATH)))

    meetings: list[tuple[str, pd.Timestamp]] = []
    for tab in tabs:
        tab_id = tab.get_attribute("id")
        if not tab_id:
            continue
        try:
            date = pd.to_datetime(tab.text.strip(), format=_TAB_DATE_FORMAT)
        except ValueError:
            continue
        meetings.append((tab_id, date))

    if not meetings:
        raise ValueError("No FedWatch meeting tabs found.")
    return meetings


def _select_meeting_tabs(
    tabs: list[tuple[str, pd.Timestamp]],
    today: pd.Timestamp,
) -> dict[str, tuple[str, pd.Timestamp]]:
    """Pick the nearest, six-month, and one-year meeting tabs."""
    return {
        horizon: min(tabs, key=lambda tab: abs(tab[1] - (today + offset)))
        for horizon, offset in _HORIZONS.items()
    }


def _scrape_meeting_probabilities(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    tab_id: str,
    date: pd.Timestamp,
) -> dict:
    """Click a meeting tab and scrape its Ease/No Change/Hike probabilities."""
    driver.execute_script("arguments[0].click()", driver.find_element(By.ID, tab_id))

    label = f"{date:{_CARD_DATE_FORMAT}}"
    wait.until(
        EC.presence_of_element_located(
            (By.XPATH, f"//td[@class='center' and normalize-space()='{label}']")
        )
    )

    table = driver.find_element(By.XPATH, _TABLE_XPATH)
    values = table.find_elements(By.CSS_SELECTOR, "tr:last-child td.number")
    if len(values) != 3:
        raise ValueError(f"Unexpected probability row for {date.date()}: {len(values)} cells")

    ease, no_change, hike = map(_percentage, (cell.text for cell in values))
    return {
        "meeting_date": date,
        "prob_ease": ease,
        "prob_no_change": no_change,
        "prob_hike": hike,
    }


def _fetch_meeting_probabilities(today: pd.Timestamp) -> pd.DataFrame:
    """Scrape the nearest, six-month, and one-year meeting probabilities."""
    driver = _build_driver()

    try:
        driver.get(FEDWATCH_URL)
        wait = WebDriverWait(driver, 40)

        iframe = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "iframe[id^='cmeIframe-']")
            )
        )
        driver.switch_to.frame(iframe)

        tabs = _list_meeting_tabs(wait)
        selected = _select_meeting_tabs(tabs, today)

        rows = [
            {**_scrape_meeting_probabilities(driver, wait, tab_id, date), "horizon": horizon}
            for horizon, (tab_id, date) in selected.items()
        ]

        return (
            pd.DataFrame(rows)
            .sort_values("meeting_date")
            .reset_index(drop=True)
        )

    finally:
        driver.quit()


def _fetch_with_retries(today: pd.Timestamp) -> pd.DataFrame | None:
    """Fetch meeting probabilities with retry/backoff."""
    for attempt in range(SCRAPE_RETRIES):
        try:
            return _fetch_meeting_probabilities(today)
        except Exception as exc:
            if attempt + 1 == SCRAPE_RETRIES:
                print(
                    f"FedWatch scrape failed after {SCRAPE_RETRIES} tries "
                    f"({type(exc).__name__}); cache unchanged."
                )
                return None

            delay = (
                SCRAPE_BACKOFF_SEC * 2**attempt
                + random.uniform(0, 1)
            )
            print(
                f"FedWatch attempt {attempt + 1} failed "
                f"({type(exc).__name__}); retry in {delay:.0f}s"
            )
            time.sleep(delay)

    return None


def update_fed_rate_data(filepath: Path | str = FEDWATCH_CSV) -> None:
    """Refresh the cached FedWatch snapshot."""
    filepath = Path(filepath)
    time.sleep(random.uniform(*FETCH_JITTER_SEC))

    snapshot = _fetch_with_retries(pd.Timestamp.now().normalize())
    if snapshot is None:
        return

    filepath.parent.mkdir(parents=True, exist_ok=True)
    snapshot.to_csv(filepath, index=False)

    print(
        f"FedWatch cache updated: {len(snapshot)} meetings, "
        f"{snapshot['meeting_date'].min().date()} -> "
        f"{snapshot['meeting_date'].max().date()}"
    )


def latest_fedwatch(
    filepath: Path | str = FEDWATCH_CSV,
) -> pd.DataFrame:
    """Load the cached FedWatch snapshot."""
    return pd.read_csv(filepath, parse_dates=["meeting_date"])


if __name__ == "__main__":
    update_fed_rate_data()
    print(latest_fedwatch().to_string(index=False))
