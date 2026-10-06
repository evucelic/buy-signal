"""Collect CME FedWatch probabilities for three time horizons."""

from __future__ import annotations

import logging
import math
import random
import re
import time
import threading
from pathlib import Path

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from buysignal.collectors.chrome import chrome_session
from buysignal.collectors.cache import write_csv
from buysignal.config import (
    FEDWATCH_CSV,
    FEDWATCH_MAX_AGE_HOURS,
    FEDWATCH_HISTORY_CSV,
    FEDWATCH_PAGE_LOAD_TIMEOUT_SEC,
    FEDWATCH_TOOL_URL,
    FEDWATCH_URL,
    FETCH_JITTER_SEC,
    SCRAPE_BACKOFF_SEC,
    SCRAPE_RETRIES,
)

log = logging.getLogger(__name__)
_UPDATE_LOCK = threading.Lock()

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


def _parse_meeting_date(text: str) -> pd.Timestamp:
    text = " ".join(text.split())
    for fmt in ("%d %b%y", "%d %b %y", "%d %b %Y", "%d %B %Y"):
        try:
            return pd.to_datetime(text, format=fmt)
        except ValueError:
            pass
    raise ValueError(f"Unrecognized FedWatch meeting date: {text!r}")


def _validate_probabilities(values: tuple[float, ...]) -> None:
    if (
        len(values) != 3
        or any(not math.isfinite(v) or not 0 <= v <= 1 for v in values)
        or abs(sum(values) - 1) > 0.005
    ):
        raise ValueError(f"Invalid FedWatch probability distribution: {values}")


def _today() -> pd.Timestamp:
    return pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()


def _validate_snapshot(snapshot: pd.DataFrame, today: pd.Timestamp) -> pd.DataFrame:
    """Reject incomplete or rolled-over snapshots before publication and scoring."""
    required = {"meeting_date", "horizon", "prob_ease", "prob_no_change", "prob_hike"}
    if not required.issubset(snapshot.columns):
        raise ValueError("FedWatch snapshot columns missing; refresh required")
    if len(snapshot) != len(_HORIZONS) or set(snapshot["horizon"]) != set(_HORIZONS):
        raise ValueError("FedWatch horizons missing or duplicated; refresh required")

    snapshot = snapshot.copy()
    snapshot["meeting_date"] = pd.to_datetime(snapshot["meeting_date"], errors="raise")
    if snapshot["meeting_date"].isna().any():
        raise ValueError("FedWatch meeting date missing; refresh required")
    if (snapshot["meeting_date"] < today).any():
        raise ValueError("FedWatch snapshot refers to a past meeting; refresh required")
    dates = snapshot.set_index("horizon")["meeting_date"]
    if not dates["nearest"] <= dates["six_month"] <= dates["one_year"]:
        raise ValueError("FedWatch horizon dates are out of order; refresh required")
    for row in snapshot.itertuples():
        _validate_probabilities((float(row.prob_ease), float(row.prob_no_change), float(row.prob_hike)))
    if {"reference_lower_bps", "reference_upper_bps"}.issubset(snapshot.columns):
        references = snapshot[["reference_lower_bps", "reference_upper_bps"]]
        if references.notna().any().any():
            if references.isna().any().any() or len(references.drop_duplicates()) != 1:
                raise ValueError("FedWatch reference range changed or was incomplete during collection")
            if (references.reference_lower_bps >= references.reference_upper_bps).any():
                raise ValueError("Invalid FedWatch reference target range")
    return snapshot


def _reference_metadata(text: str) -> dict:
    """The reference range and CME observation time make comparisons meaningful."""
    result = {"reference_lower_bps": None, "reference_upper_bps": None, "source_asof": None}
    reference = re.search(r"Current target rate is\s*(\d+)\s*[-–]\s*(\d+)", text, re.IGNORECASE)
    if reference:
        result["reference_lower_bps"], result["reference_upper_bps"] = map(int, reference.groups())
    observed = re.search(r"Data as of\s+(\d{1,2} \w+ \d{4} \d{1,2}:\d{2}:\d{2})\s+CT", text)
    if observed:
        date = pd.to_datetime(observed.group(1), format="%d %b %Y %H:%M:%S")
        result["source_asof"] = date.tz_localize("America/Chicago").tz_convert("UTC").isoformat()
    return result


def _add_comparisons(snapshot: pd.DataFrame, previous: pd.DataFrame | None) -> pd.DataFrame:
    """Match meeting dates, not rolling horizon labels; reset after reference changes."""
    snapshot = snapshot.copy()
    for column in ("ease", "no_change", "hike"):
        snapshot[f"delta_{column}_pp"] = float("nan")
    snapshot["comparison_since"] = ""
    snapshot["comparison_status"] = "No comparable previous snapshot"
    if previous is None or "meeting_date" not in previous:
        return snapshot
    previous = previous.copy()
    previous["meeting_date"] = pd.to_datetime(previous["meeting_date"], errors="coerce")
    reference_columns = ["reference_lower_bps", "reference_upper_bps"]
    for index, row in snapshot.iterrows():
        matches = previous.loc[previous.meeting_date == row.meeting_date]
        if matches.empty:
            snapshot.loc[index, "comparison_status"] = "New meeting; baseline starts with this refresh"
            continue
        before = matches.iloc[-1]
        if any(pd.isna(row.get(c)) or pd.isna(before.get(c)) for c in reference_columns):
            snapshot.loc[index, "comparison_status"] = "Reference target range unavailable; baseline reset"
            continue
        if any(row[c] != before[c] for c in reference_columns):
            snapshot.loc[index, "comparison_status"] = "Reference target range changed; baseline reset"
            continue
        if pd.isna(before.get("fetched_at")):
            continue
        try:
            _validate_probabilities(tuple(float(before[f"prob_{c}"]) for c in ("ease", "no_change", "hike")))
            since = pd.Timestamp(before["fetched_at"])
            elapsed = pd.Timestamp(row["fetched_at"]) - since
            if elapsed.total_seconds() <= 0:
                continue
            old_source, new_source = before.get("source_asof"), row.get("source_asof")
            if pd.notna(old_source) and pd.notna(new_source) and pd.Timestamp(new_source) <= pd.Timestamp(old_source):
                snapshot.loc[index, "comparison_status"] = "Source timestamp unchanged or older; no new comparable observation"
                continue
        except (ValueError, TypeError, KeyError):
            continue
        for column in ("ease", "no_change", "hike"):
            snapshot.loc[index, f"delta_{column}_pp"] = (float(row[f"prob_{column}"]) - float(before[f"prob_{column}"])) * 100
        snapshot.loc[index, "comparison_since"] = since.isoformat()
        snapshot.loc[index, "comparison_status"] = "Same meeting and current target range"
    return snapshot


def _matches_date(text: str, date: pd.Timestamp) -> bool:
    try:
        return _parse_meeting_date(text) == date
    except ValueError:
        return False


def _percentage(text: str) -> float:
    """Convert a percentage string to a decimal probability."""
    return float(text.replace("%", "").strip()) / 100


def _list_meeting_tabs(wait: WebDriverWait) -> list[tuple[str, pd.Timestamp]]:
    """Return (element id, meeting date) for every meeting tab, in DOM order."""
    def read_tabs(browser):
        try:
            meetings = []
            for tab in browser.find_elements(By.XPATH, _MEETING_TAB_XPATH):
                tab_id = tab.get_attribute("id")
                if not tab_id:
                    continue
                try:
                    date = _parse_meeting_date(tab.text)
                except ValueError:
                    continue
                meetings.append((tab_id, date))
            return meetings or False
        except StaleElementReferenceException:
            return False

    return wait.until(read_tabs, message="FedWatch meeting tabs did not become ready")


def _select_meeting_tabs(
    tabs: list[tuple[str, pd.Timestamp]],
    today: pd.Timestamp,
) -> dict[str, tuple[str, pd.Timestamp]]:
    """Pick the nearest, six-month, and one-year meeting tabs."""
    tabs = [tab for tab in tabs if tab[1] >= today]
    if not tabs:
        raise ValueError("No upcoming FedWatch meetings available")
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

    def read_selected(browser):
        # Postbacks replace elements; re-read the selected date and values together.
        try:
            updating = browser.execute_script(
                "return Boolean(window.Sys && Sys.WebForms && Sys.WebForms.PageRequestManager && "
                "Sys.WebForms.PageRequestManager.getInstance().get_isInAsyncPostBack());"
            )
            if updating is True:
                return False
            cards = browser.find_elements(
                By.XPATH,
                "//td[contains(concat(' ', normalize-space(@class), ' '), ' center ')]",
            )
            if not any(_matches_date(card.text, date) for card in cards):
                return False
            table = browser.find_element(By.XPATH, _TABLE_XPATH)
            values = tuple(
                _percentage(cell.text)
                for cell in table.find_elements(By.CSS_SELECTOR, "tr:last-child td.number")
            )
            _validate_probabilities(values)
            return values
        except (StaleElementReferenceException, NoSuchElementException, ValueError):
            return False

    ease, no_change, hike = wait.until(
        read_selected,
        message=f"FedWatch probabilities for {date.date()} did not become ready",
    )
    metadata = _reference_metadata(str(driver.find_element(By.TAG_NAME, "body").text))
    return {
        **metadata,
        "meeting_date": date,
        "prob_ease": ease,
        "prob_no_change": no_change,
        "prob_hike": hike,
    }


def _fetch_meeting_probabilities(today: pd.Timestamp) -> pd.DataFrame:
    """Scrape the nearest, six-month, and one-year meeting probabilities."""
    with chrome_session(FEDWATCH_PAGE_LOAD_TIMEOUT_SEC) as driver:
        try:
            driver.get(FEDWATCH_URL)
        except TimeoutException:
            log.warning("FedWatch outer page load timed out; checking tool readiness")
        wait = WebDriverWait(driver, 40)

        try:
            wait.until(
                EC.frame_to_be_available_and_switch_to_it(
                    (By.CSS_SELECTOR, "iframe[id^='cmeIframe-'], iframe[src*='quikstrike.net']")
                ), message="CME FedWatch iframe unavailable"
            )
        except TimeoutException:
            # CME's landing-page layout may change independently of its embedded tool.
            driver.switch_to.default_content()
            log.warning("FedWatch iframe unavailable; opening CME's hosted tool directly")
            try:
                driver.get(FEDWATCH_TOOL_URL)
            except TimeoutException:
                log.warning("FedWatch direct tool load timed out; checking meeting tabs")

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


def _fetch_with_retries(today: pd.Timestamp) -> tuple[pd.DataFrame | None, str | None]:
    """Fetch meeting probabilities with retry/backoff. Returns (data, error_message)."""
    last_error = None
    for attempt in range(SCRAPE_RETRIES):
        try:
            return _validate_snapshot(_fetch_meeting_probabilities(today), today), None
        except Exception as exc:
            log.debug("FedWatch attempt failed", exc_info=True)
            message = str(exc).split("Stacktrace:")[0].strip().removeprefix("Message:").strip()
            message = " ".join(message.split())[:300]
            last_error = f"{type(exc).__name__}: {message or 'FedWatch page/tool did not become ready'}"
            if attempt + 1 == SCRAPE_RETRIES:
                log.error("FedWatch scrape failed after %d tries (%s); cache unchanged.", SCRAPE_RETRIES, last_error)
                return None, last_error

            delay = (
                SCRAPE_BACKOFF_SEC * 2**attempt
                + random.uniform(0, 1)
            )
            log.debug("FedWatch attempt %d failed (%s); retry in %.0fs", attempt + 1, last_error, delay)
            time.sleep(delay)

    return None, last_error


def update_fed_rate_data(filepath: Path | str = FEDWATCH_CSV) -> str | None:
    """Serialize current-cache and observation-history publication."""
    with _UPDATE_LOCK:
        return _update_fed_rate_data(filepath)


def _update_fed_rate_data(filepath: Path | str) -> str | None:
    """Refresh the cached FedWatch snapshot. Returns an error message, or None on success."""
    filepath = Path(filepath)
    time.sleep(random.uniform(*FETCH_JITTER_SEC))

    today = _today()
    snapshot, error = _fetch_with_retries(today)
    if snapshot is None:
        return error or "unknown error"

    try:
        snapshot = _validate_snapshot(snapshot, today)
    except (ValueError, TypeError) as exc:
        return f"Invalid FedWatch snapshot: {exc}"
    try:
        previous = pd.read_csv(filepath)
    except (OSError, ValueError):
        previous = None
    snapshot["fetched_at"] = pd.Timestamp.now(tz="UTC").isoformat()
    snapshot = _add_comparisons(snapshot, previous)
    # Archive successful observations separately from the replaceable current cache.
    history_path = (FEDWATCH_HISTORY_CSV if filepath == Path(FEDWATCH_CSV)
                    else filepath.with_name(f"{filepath.stem}_history.csv"))
    try:
        history = pd.read_csv(history_path)
    except (OSError, ValueError):
        history = pd.DataFrame()
    history = pd.concat([history, snapshot], ignore_index=True) if not history.empty else snapshot.copy()
    write_csv(history, history_path)
    write_csv(snapshot, filepath)

    log.info(
        "FedWatch cache updated: %d meetings, %s -> %s",
        len(snapshot),
        snapshot["meeting_date"].min().date(),
        snapshot["meeting_date"].max().date(),
    )
    return None


def latest_fedwatch(
    filepath: Path | str = FEDWATCH_CSV,
) -> pd.DataFrame:
    """Load the cached FedWatch snapshot."""
    filepath = Path(filepath)
    age = time.time() - filepath.stat().st_mtime
    if age > FEDWATCH_MAX_AGE_HOURS * 3600:
        raise ValueError(f"FedWatch cache is older than {FEDWATCH_MAX_AGE_HOURS}h; refresh required")
    snapshot = pd.read_csv(filepath, parse_dates=["meeting_date"])
    return _validate_snapshot(snapshot, _today())


def demands_refresh() -> bool:
    """A meeting rollover must invalidate even a cache refreshed earlier today."""
    try:
        latest_fedwatch()
    except (OSError, ValueError, TypeError):
        return True
    return False



if __name__ == "__main__":
    from buysignal import logs

    logs.configure()
    update_fed_rate_data()
    print(latest_fedwatch().to_string(index=False))
