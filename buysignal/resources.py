"""Shared budgets for expensive work and explicitly forced data refreshes."""

from contextlib import contextmanager
from contextvars import ContextVar
import threading

# Chromium and plot rendering share this lock, including across Telegram and scheduler threads.
HEAVY_WORK_LOCK = threading.RLock()
FORCE_REFRESH = ContextVar("buysignal_force_refresh", default=False)


@contextmanager
def force_refreshes():
    token = FORCE_REFRESH.set(True)
    try:
        yield
    finally:
        FORCE_REFRESH.reset(token)
