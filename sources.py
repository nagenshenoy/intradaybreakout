"""
Symbol-list sources: bundled workbook, uploaded CSV/XLSX, and header detection.

Sheets in the bundled ScannerData.xlsx are *headerless* (just a column of
tickers), while user files usually have a "Symbol" header. We read everything
with header=None and decide per sheet whether the first row is a header.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from pathlib import Path

import pandas as pd

# A header cell that names the ticker column (preferred) ...
SYMBOL_HEADERS = {"SYMBOL", "SYMBOLS", "TICKER", "TICKERS", "TICKER SYMBOL", "SCRIP", "SCRIPS",
                  "STOCK", "STOCKS", "STOCK SYMBOL", "TRADING SYMBOL", "TRADINGSYMBOL", "INSTRUMENT", "CODE"}
# ... or a generic header we should skip if it is in the first column.
OTHER_HEADERS = {"NAME", "COMPANY", "COMPANY NAME", "SECURITY", "SECURITY NAME", "SR NO", "S.NO"}

_JUNK = {"", "NAN", "NONE", "NULL"}


def symbols_from_frame(df: pd.DataFrame) -> list[str]:
    """Extract a de-duplicated, upper-cased symbol list from a header=None frame."""
    if df is None or df.empty:
        return []
    first = [str(v).strip().upper() for v in df.iloc[0].tolist()]
    col, start = 0, 0
    sym_cols = [i for i, v in enumerate(first) if v in SYMBOL_HEADERS]
    if sym_cols:
        col, start = sym_cols[0], 1
    elif first and first[0] in OTHER_HEADERS:
        col, start = 0, 1
    vals = (str(v).strip().upper() for v in df.iloc[start:, col].dropna().tolist())
    return list(dict.fromkeys(v for v in vals if v not in _JUNK))


def parse_csv(stream) -> list[str]:
    df = pd.read_csv(stream, header=None, dtype=str, encoding="utf-8-sig", skip_blank_lines=True)
    return symbols_from_frame(df)


def parse_workbook(stream) -> dict[str, list[str]]:
    """sheet name -> symbols (sheets with no symbols are dropped), in workbook order."""
    frames = pd.read_excel(stream, sheet_name=None, header=None, dtype=str)
    out = {}
    for name, df in frames.items():
        syms = symbols_from_frame(df)
        if syms:
            out[str(name)] = syms
    return out


def sheet_summary(sheets: dict[str, list[str]]) -> list[dict]:
    return [{"name": n, "count": len(s)} for n, s in sheets.items()]


class BundledWorkbook:
    """The ScannerData.xlsx shipped with the app; re-read only if the file changes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._mtime = None
        self._sheets: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    @property
    def file_name(self) -> str:
        return self.path.name

    def sheets(self) -> dict[str, list[str]]:
        with self._lock:
            if not self.path.exists():
                self._mtime, self._sheets = None, {}
                return {}
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                with open(self.path, "rb") as fh:
                    self._sheets = parse_workbook(fh)
                self._mtime = mtime
            return self._sheets


class UploadStore:
    """Parsed multi-sheet uploads kept server-side, addressed by token (small LRU)."""

    def __init__(self, max_items: int = 5):
        self.max_items = max_items
        self._items: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, file_name: str, sheets: dict[str, list[str]]) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._items[token] = {"file_name": file_name, "sheets": sheets}
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)
        return token

    def get(self, token: str) -> dict | None:
        with self._lock:
            item = self._items.get(token)
            if item:
                self._items.move_to_end(token)
            return item

    def latest(self) -> tuple[str, dict] | None:
        with self._lock:
            if not self._items:
                return None
            token = next(reversed(self._items))
            return token, self._items[token]
