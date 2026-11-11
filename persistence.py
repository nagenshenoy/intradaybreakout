"""
Day-session persistence: a JSON snapshot of the scan output, no database.

The scanner keeps its results in memory. After every scan pass the engine writes them
(plus the Scan# counters and the "New Signal / Continue N" tracker) to one JSON file, so
a restart can pick the session back up. The snapshot is tagged with the *trading date*
it belongs to:

  * Mon-Fri  -> that day.
  * Sat/Sun  -> the Friday before, so Friday's output is kept over the weekend.

On startup the snapshot is restored if it belongs to the current trading date or the
one before it. It is only wiped when a new scan is *started* on a different trading
date (see ScannerEngine.start), exactly as if you had pressed Clear on every tab.

NSE holidays are not modelled (that would need a holiday calendar): a holiday on a
weekday counts as a trading date here.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import tempfile
from pathlib import Path

log = logging.getLogger("scanner.persistence")

SCHEMA_VERSION = 1


def trading_date(d: dt.date) -> dt.date:
    """The trading date a calendar date belongs to: weekdays are themselves, and
    Saturday / Sunday belong to the Friday before."""
    wd = d.weekday()          # Mon=0 ... Sun=6
    if wd == 5:
        return d - dt.timedelta(days=1)
    if wd == 6:
        return d - dt.timedelta(days=2)
    return d


def previous_trading_date(d: dt.date) -> dt.date:
    """The trading date before `trading_date(d)` (Monday -> the Friday before)."""
    return trading_date(trading_date(d) - dt.timedelta(days=1))


def _jsonable(obj):
    """Make a structure strictly JSON-safe: numpy scalars -> Python, NaN/inf -> None."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):   # numpy scalar
        try:
            obj = obj.item()
        except Exception:  # noqa: BLE001
            return str(obj)
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class SessionStore:
    """Reads/writes the snapshot file. Writes are atomic (temp file + rename), so a crash
    mid-write can never leave a half-written, unreadable snapshot behind."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def save(self, payload: dict) -> None:
        payload = _jsonable({"version": SCHEMA_VERSION, **payload})
        text = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self) -> dict | None:
        """The saved snapshot, or None if there is none or it is unreadable / wrong version."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable session file %s: %s", self.path, e)
            return None
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            log.warning("ignoring session file %s: unsupported format", self.path)
            return None
        return data
