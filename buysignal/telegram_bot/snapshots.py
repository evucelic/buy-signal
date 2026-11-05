"""Bounded, atomic JSON snapshots keep buttons tied to the original report after restart."""
from __future__ import annotations

from datetime import datetime
from collections import OrderedDict
from copy import deepcopy
import shutil
from io import StringIO
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time

import pandas as pd

from buysignal import config
from buysignal.signals.base import SubSignal
from buysignal.signals.buy_signal import BuySignal

_LOCK = threading.RLock()
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
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else config.DATA_DIR / 'reports'
        self._view_cache = OrderedDict()

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

    def signals(self, data):
        result = {}
        for original in data['signals']:
            values = original.copy()
            encoded = values.pop('history')
            dates = values.pop('datetime_index')
            if encoded is not None:
                frame = pd.read_json(StringIO(encoded), orient='split', convert_dates=False)
                if dates:
                    frame.index = pd.to_datetime(frame.index)
                for column in ('date', 'month', 'meeting_date'):
                    if column in frame:
                        frame[column] = pd.to_datetime(frame[column])
                values['history'] = frame
            result[values['name']] = SubSignal(**values)
        return result

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

    def prepare(self, token, result, sent_at):
        from buysignal.telegram_bot import prepared
        # Heavy work runs outside the snapshot lock; existing reports remain navigable.
        captions, charts = prepared.build(result, token, sent_at)
        manifest = {'created': time.time(), 'views': captions, 'panel': None, 'file_ids': {}}
        total = 0
        assets = self.directory / 'assets' / token
        assets.mkdir(parents=True, exist_ok=True)
        for key, png in charts.items():
            total += len(png)
            if total > MAX_REPORT_ASSET_BYTES:
                for item in captions.values():
                    if item['chart_key'] == key:
                        item['chart_key'] = None
                continue
            target = assets / (key.replace(':', '_') + '.png')
            temporary = target.with_suffix('.tmp')
            temporary.write_bytes(png)
            temporary.replace(target)
        with _LOCK:
            # Keep the original expiry, including for reports prepared after a restart.
            source = json.loads(self._path(token).read_text())
            manifest['created'] = source['created']
            manifest['panel'] = source.get('panel')
            self._write_views(token, manifest)
            all_assets = self.directory / 'assets'
            size = sum(p.stat().st_size for p in all_assets.rglob('*.png'))
            for path in sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime):
                if size <= MAX_ASSET_BYTES:
                    break
                if path.stem != token:
                    folder = all_assets / path.stem
                    size -= sum(p.stat().st_size for p in folder.glob('*.png'))
                    self._remove(path.stem)

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
        # Legacy snapshots migrate once. No source download is part of this path.
        source = self.load(token)
        signals = list(self.signals(source).values())
        self.prepare(token, BuySignal(0, 'none', 0, signals), source['sent_at'])
        return self.load_views(token)

    def chart_bytes(self, token, key):
        self._path(token)
        if not re.fullmatch(r'[a-z_]+:[A-Z0-9]*', key):
            raise ValueError('Invalid chart identifier')
        path = self.directory / 'assets' / token / (key.replace(':', '_') + '.png')
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

    def set_panel(self, token, panel):
        with _LOCK:
            manifest = self.load_views(token)
            manifest['panel'] = panel
            self._write_views(token, manifest)
