"""Independent calendar clock: no extra hourly polling or changes to the US alert schedule."""
from datetime import datetime, timezone
import logging
import threading

from buysignal.portfolio import sessions

log = logging.getLogger(__name__)


class PrecloseService:
    def __init__(self, store, deliver, *, lock=None):
        self.store, self.deliver = store, deliver
        self.stopped = threading.Event()
        self.thread = None
        self.lock = lock if lock is not None else threading.RLock()

    def check(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self.lock:
            due = sessions.due(now, self.store)
            if not due or self.stopped.is_set():
                return False
            if self.deliver(due, now):
                self.store.mark_delivered(due, datetime.now(timezone.utc))
                return True
            return False

    def run(self):
        while not self.stopped.is_set():
            try:
                self.check()
            except Exception:
                log.exception('Personal pre-close update failed; will retry while the venues remain open')
            try:
                delay = sessions.next_delay(datetime.now(timezone.utc), self.store)
            except Exception:
                delay = 60
            self.stopped.wait(delay)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='personal-preclose')
        self.thread.start()

    def stop(self):
        self.stopped.set()
