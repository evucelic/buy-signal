"""Bounded, atomic JSON snapshots keep buttons tied to the original report after restart."""
from __future__ import annotations

from datetime import datetime
from collections import OrderedDict
from copy import deepcopy
import shutil
import json
import logging
import math
from pathlib import Path
import queue
import re
import secrets
import threading
import time

import pandas as pd

from buysignal import config
from buysignal.signals.buy_signal import BuySignal

log = logging.getLogger(__name__)
_LOCK = threading.RLock()
# One worker draws the non-default ranges after a report is sent; buttons never plot.
_JOBS: queue.Queue = queue.Queue()
_WORKER_LOCK = threading.Lock()
_worker: threading.Thread | None = None
PENDING_GRACE_SEC = 180  # after this a chart still marked pending is treated as unavailable
_CHART_KEY = re.compile(r'[a-z_]+:[A-Z0-9]*(@[a-z0-9]+)?')
MAX_SNAPSHOTS = 32
MAX_AGE_SEC = 14 * 86400
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_REPORT_ASSET_BYTES = 8 * 1024 * 1024


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    return value


class SnapshotStore:
    def __init__(self, directory=None, background=False):
        self.directory = Path(directory) if directory else config.DATA_DIR / 'reports'
        self._view_cache = OrderedDict()
        # Off by default so a store is deterministic; the running bot turns it on.
        self.background = background

    def _asset_path(self, token, key):
        return self.directory / 'assets' / token / (key.replace(':', '_').replace('@', '-') + '.png')

    def _path(self, token):
        if not re.fullmatch(r'[0-9a-f]{12}', token):
            raise ValueError('Invalid report id')
        return self.directory / (token + '.json')

    def _write(self, token, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(token)
        temporary = path.with_suffix('.tmp')
        encoded = json.dumps(_clean(data), allow_nan=False, separators=(',', ':'))
        if len(encoded.encode()) > MAX_FILE_BYTES:
            raise ValueError('Report snapshot exceeds storage budget')
        temporary.write_text(encoded)
        temporary.replace(path)

    def save(self, result: BuySignal, now: datetime):
        with _LOCK:
            self.directory.mkdir(parents=True, exist_ok=True)
            paths = sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
            for i, path in enumerate(paths):
                if i >= MAX_SNAPSHOTS - 1 or time.time() - path.stat().st_mtime > MAX_AGE_SEC:
                    self._remove(path.stem)
            token = secrets.token_hex(6)
            self._write(token, {'created': time.time(), 'sent_at': now.isoformat(), 'panel': None})
        self.prepare(token, result, now.isoformat())
        return token

    def save_panels(self, panels: dict, keyboard, now: datetime) -> str:
        """Store ready-made tabs, {view: (caption, png or None)}; return the token their buttons carry.

        `keyboard(token, view)` builds each tab's buttons once the token exists. Every chart is
        already drawn, so nothing is queued for the background worker.
        """
        with _LOCK:
            self.directory.mkdir(parents=True, exist_ok=True)
            token = secrets.token_hex(6)
            created = time.time()
            self._write(token, {'created': created, 'sent_at': now.isoformat(), 'panel': None})
            views, used = {}, 0
            for view, (caption, png) in panels.items():
                base, _, chosen = view.partition('@')
                key = f'{base}:' + (f'@{chosen}' if chosen else '')
                total = self._store_chart(token, key, png, used) if png else None
                used = total if total is not None else used
                views[view] = {'caption': caption, 'keyboard': keyboard(token, view), 'chart_key': key if total is not None else None}
            self._write_views(token, {'created': created, 'views': views, 'panel': None, 'file_ids': {}, 'pending': {},
                                      'self_panel': True})
            self._trim_assets(token)
        return token

    def load(self, token):
        with _LOCK:
            path = self._path(token)
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError('Invalid snapshot size')
            data = json.loads(path.read_text())
            if time.time() - data['created'] > MAX_AGE_SEC:
                raise ValueError('Report expired')
            manifest = self.directory / 'views' / (token + '.json')
            if manifest.exists():
                data['panel'] = self.load_views(token).get('panel')
            return data

    def _remove(self, token):
        self._path(token).unlink(missing_ok=True)
        (self.directory / 'views' / (token + '.json')).unlink(missing_ok=True)
        shutil.rmtree(self.directory / 'assets' / token, ignore_errors=True)
        self._view_cache.pop(token, None)

    def _write_views(self, token, manifest):
        path = self.directory / 'views' / (token + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        encoded = json.dumps(_clean(manifest), allow_nan=False, separators=(',', ':'))
        if len(encoded.encode()) > MAX_FILE_BYTES:
            raise ValueError('Prepared report exceeds storage budget')
        temporary.write_text(encoded)
        temporary.replace(path)
        self._view_cache[token] = (path.stat().st_mtime_ns, deepcopy(manifest))
        self._view_cache.move_to_end(token)
        while len(self._view_cache) > 8:
            self._view_cache.popitem(last=False)

    def _store_chart(self, token, key, png, used):
        """Write one PNG atomically; return the report's asset total, or None when over budget."""
        if png is None or used + len(png) > MAX_REPORT_ASSET_BYTES:
            return None
        target = self._asset_path(token, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(png)
        temporary.replace(target)
        return used + len(png)

    def prepare(self, token, result, sent_at):
        from buysignal.telegram_bot import prepared
        # Heavy work runs outside the snapshot lock; existing reports remain navigable.
        captions, jobs = prepared.plan(result, token, sent_at)
        later = sorted((job for job in jobs if not job.primary), key=prepared.background_order) if self.background else []
        used, missing = 0, set()
        for job in jobs:
            if self.background and not job.primary:
                continue
            total = self._store_chart(token, job.key, prepared.render(job), used)
            if total is None:
                missing.add(job.key)
            else:
                used = total
        for item in captions.values():
            if item['chart_key'] in missing:
                item['chart_key'] = None
        manifest = {'created': time.time(), 'views': captions, 'panel': None, 'file_ids': {},
                    'pending': {job.key: time.time() for job in later}}
        with _LOCK:
            # Keep the original expiry, including for reports prepared after a restart.
            source = json.loads(self._path(token).read_text())
            manifest['created'] = source['created']
            manifest['panel'] = source.get('panel')
            self._write_views(token, manifest)
            self._trim_assets(token)
        if later:
            _JOBS.put((self, token, later, used))
            _start_worker()

    def _trim_assets(self, token):
        all_assets = self.directory / 'assets'
        size = sum(p.stat().st_size for p in all_assets.rglob('*.png'))
        for path in sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime):
            if size <= MAX_ASSET_BYTES:
                break
            if path.stem != token:
                folder = all_assets / path.stem
                size -= sum(p.stat().st_size for p in folder.glob('*.png'))
                self._remove(path.stem)

    def _finish(self, token, jobs, used):
        """Draw the remaining ranges one by one, publishing each as soon as it exists."""
        from buysignal.telegram_bot import prepared
        for job in jobs:
            total = self._store_chart(token, job.key, prepared.render(job), used)
            with _LOCK:
                try:
                    manifest = self.load_views(token)
                except (OSError, ValueError, KeyError, TypeError):
                    return  # the report expired or was evicted while its charts were drawn
                manifest.get('pending', {}).pop(job.key, None)
                if total is None:
                    for item in manifest['views'].values():
                        if item['chart_key'] == job.key:
                            item['chart_key'] = None
                else:
                    used = total
                self._write_views(token, manifest)
        with _LOCK:
            self._trim_assets(token)

    def drain(self):
        """Block until queued background charts are finished (tests, benchmarks, shutdown)."""
        _JOBS.join()

    def load_views(self, token):
        self._path(token)  # validate the identifier before using it as a directory name
        with _LOCK:
            path = self.directory / 'views' / (token + '.json')
            if path.exists():
                stat = path.stat()
                if stat.st_size > MAX_FILE_BYTES:
                    raise ValueError('Invalid prepared report size')
                cached = self._view_cache.get(token)
                if cached and cached[0] == stat.st_mtime_ns:
                    data = deepcopy(cached[1])
                else:
                    data = json.loads(path.read_text())
                    self._view_cache[token] = (stat.st_mtime_ns, deepcopy(data))
                    while len(self._view_cache) > 8:
                        self._view_cache.popitem(last=False)
                if time.time() - data['created'] > MAX_AGE_SEC:
                    raise ValueError('Report expired')
                return data
        raise ValueError('Report has no prepared views')

    def chart_bytes(self, token, key):
        self._path(token)
        if not _CHART_KEY.fullmatch(key):
            raise ValueError('Invalid chart identifier')
        path = self._asset_path(token, key)
        if path.stat().st_size > MAX_REPORT_ASSET_BYTES:
            raise ValueError('Invalid chart size')
        return path.read_bytes()

    def remember_media(self, token, chart_key, file_id):
        if file_id:
            with _LOCK:
                manifest = self.load_views(token)
                if manifest['file_ids'].get(chart_key) == file_id:
                    return
                manifest['file_ids'][chart_key] = file_id
                self._write_views(token, manifest)

    def mark_self_panel(self, token):
        """The report is sent as its own panel, so a tap on it edits that message."""
        with _LOCK:
            manifest = self.load_views(token)
            manifest['self_panel'] = True
            self._write_views(token, manifest)

    def set_panel(self, token, panel):
        with _LOCK:
            manifest = self.load_views(token)
            manifest['panel'] = panel
            self._write_views(token, manifest)


def _work():
    while True:
        store, token, jobs, used = _JOBS.get()
        try:
            store._finish(token, jobs, used)
        except Exception:
            log.exception('Background chart preparation failed')
        finally:
            _JOBS.task_done()


def _start_worker():
    global _worker
    with _WORKER_LOCK:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name='report-charts', daemon=True)
            _worker.start()
