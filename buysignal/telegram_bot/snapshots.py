"""Bounded, atomic JSON snapshots keep buttons tied to the original report after restart."""
from __future__ import annotations

from datetime import datetime
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
                    path.unlink(missing_ok=True)
            signals = []
            for signal in result.subsignals:
                # 460 prices retain a full visible year plus the SMA200 baseline.
                values = {k: v for k, v in vars(signal).items() if k != 'history'}
                frame = signal.history
                values['history'] = None if frame is None else frame.tail(460).to_json(orient='split', date_format='iso')
                values['datetime_index'] = frame is not None and isinstance(frame.index, pd.DatetimeIndex)
                signals.append(values)
            token = secrets.token_hex(6)
            self._write(token, {'created': time.time(), 'sent_at': now.isoformat(), 'signals': signals, 'panel': None})
            return token

    def load(self, token):
        with _LOCK:
            path = self._path(token)
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError('Invalid snapshot size')
            data = json.loads(path.read_text())
            if time.time() - data['created'] > MAX_AGE_SEC:
                raise ValueError('Report expired')
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

    def set_panel(self, token, panel):
        with _LOCK:
            data = self.load(token)
            data['panel'] = panel
            self._write(token, data)
