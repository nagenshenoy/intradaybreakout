"""
Scanning engine for the Intraday Bollinger-Band Breakout scanner.

This module contains everything that used to live inside the Tkinter app,
minus the GUI: indicator maths, the breakout conditions, result filtering,
and the background scan loop. It has no Flask dependency, so it can be unit
tested (or reused from a CLI) on its own.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands

log = logging.getLogger("scanner")

ACTION_TYPES = ["New Signal"] + [f"Continue {i}" for i in range(1, 11)]

IST = ZoneInfo("Asia/Kolkata")
AUTO_STOP_TIME = dt.time(15, 25)  # NSE market closes 3:30 PM IST; scanner auto-stops 5 min before

# Per-timeframe settings (identical to the values hard-coded in the Tk app).
# intervals_per_day is also the candle count used for Rel Vol's "previous day"
# average: 375 minutes of NSE trading (9:15 AM - 3:30 PM) / interval minutes.
TAB_DEFS = {
    "5m": dict(name="5 Minute", interval="5m", period="10d",
               intervals_per_day=75, tv_interval="5"),
    "60m": dict(name="60 Minute", interval="60m", period="60d",
                intervals_per_day=7, tv_interval="60"),
}


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
def fetch_history(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
    """Thin wrapper over yfinance so tests can swap in fake data."""
    return yf.Ticker(symbol).history(period=period, interval=interval, **kwargs)


def _num(x, default: float = 0.0) -> float:
    """float() that never returns NaN/inf (they are not valid JSON)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


# --------------------------------------------------------------------------- #
# Config + per-tab state
# --------------------------------------------------------------------------- #
@dataclass
class ScanConfig:
    scan_5m: bool = True
    scan_60m: bool = False
    lookback_days: int = 2
    scan_interval: int = 60
    workers: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> "ScanConfig":
        def clamp(v, lo, hi, default):
            try:
                return max(lo, min(hi, int(float(v))))
            except (TypeError, ValueError):
                return default

        return cls(
            scan_5m=bool(d.get("scan_5m", True)),
            scan_60m=bool(d.get("scan_60m", False)),
            lookback_days=clamp(d.get("lookback_days"), 1, 10, 2),
            scan_interval=clamp(d.get("scan_interval"), 5, 3600, 60),
            workers=clamp(d.get("workers"), 1, 8, 1),
        )


class ScanTab:
    """State for one timeframe (5m / 60m): counters, results, signal tracker."""

    def __init__(self, key: str):
        d = TAB_DEFS[key]
        self.key = key
        self.name = d["name"]
        self.interval = d["interval"]
        self.period = d["period"]
        self.intervals_per_day = d["intervals_per_day"]
        self.tv_interval = d["tv_interval"]
        self.version = 0          # bumped whenever results change (UI polls this)
        self.clear()

    def clear(self):
        self.scan_count = 0
        self.all_results: list[dict] = []
        self.signal_tracker: dict[str, int] = {}
        self.version += 1


# --------------------------------------------------------------------------- #
# Indicators + breakout conditions
# --------------------------------------------------------------------------- #
def calculate_indicators(data: pd.DataFrame) -> pd.DataFrame | None:
    try:
        bb = BollingerBands(close=data["Close"], window=20, window_dev=2)
        data["BB_Upper"] = bb.bollinger_hband()
        data["BB_Lower"] = bb.bollinger_lband()
        data["RSI"] = RSIIndicator(close=data["Close"], window=14).rsi()
        return data
    except Exception:  # noqa: BLE001 - mirrors the original "skip bad data"
        return None


def _weekly_levels(symbol, current_close, fetcher) -> dict:
    """Previous-week and 2-weeks-ago high/low, and where LTP sits against them."""
    out = dict(WeekHL_Color="Black", PrevWeekHigh=0.0, PrevWeekLow=0.0, Week1Pct=None,
               Week2HL_Color="Black", Prev2WeekHigh=0.0, Prev2WeekLow=0.0, Week2Pct=None)
    try:
        weekly = fetcher(symbol, period="3wk", interval="1wk", auto_adjust=False)
    except Exception as e:  # noqa: BLE001
        log.warning("weekly fetch failed for %s: %s", symbol, e)
        return out
    if weekly is None or weekly.empty:
        return out

    today = dt.date.today()
    last_week_start = weekly.index[-1].date()
    current_week_row = today.isocalendar()[:2] == last_week_start.isocalendar()[:2]

    if current_week_row:
        p1 = -2 if len(weekly) >= 2 else None
        p2 = -3 if len(weekly) >= 3 else None
    else:
        p1 = -1 if len(weekly) >= 1 else None
        p2 = -2 if len(weekly) >= 2 else None

    def level(idx):
        if idx is None:
            return 0.0, 0.0
        return _num(weekly["High"].iloc[idx]), _num(weekly["Low"].iloc[idx])

    def classify(high, low):
        if high > 0 and current_close > high:
            return "Green", (current_close - high) / high * 100
        if low > 0 and current_close < low:
            return "Red", (current_close - low) / low * 100
        return "Black", None

    h1, l1 = level(p1)
    h2, l2 = level(p2)
    c1, pct1 = classify(h1, l1)
    c2, pct2 = classify(h2, l2)
    out.update(WeekHL_Color=c1, PrevWeekHigh=h1, PrevWeekLow=l1, Week1Pct=pct1,
               Week2HL_Color=c2, Prev2WeekHigh=h2, Prev2WeekLow=l2, Week2Pct=pct2)
    return out


def _rel_vol(data: pd.DataFrame, tab: ScanTab) -> float:
    """
    Rel Vol = (average volume of today's candles, 9:15 AM through the current
    candle) / (average volume of the full previous trading day's candles,
    capped at `tab.intervals_per_day` candles - 75 for 5m, 7 for 60m).
    0.0 if there isn't a previous day of data to compare against.
    """
    if "Volume" not in data.columns or data.empty:
        return 0.0
    dates = data.index.normalize()
    last_date = dates[-1]
    today = data.loc[dates == last_date, "Volume"]
    if today.empty:
        return 0.0
    today_avg = today.mean()

    prior_dates = dates[dates < last_date]
    if prior_dates.empty:
        return 0.0
    prev_date = prior_dates[-1]
    prev_day = data.loc[dates == prev_date, "Volume"].iloc[-tab.intervals_per_day:]
    if prev_day.empty:
        return 0.0
    prev_avg = prev_day.mean()
    if not prev_avg or prev_avg <= 0:
        return 0.0
    return float(today_avg / prev_avg)


def check_conditions(data: pd.DataFrame, symbol: str, tab: ScanTab,
                     cfg: ScanConfig, fetcher=fetch_history) -> dict | None:
    """
    Breakout rule (unchanged from the Tk app):
      Above Band: close > upper Bollinger band AND close > highest high of the
                  previous `lookback_days` of candles.
      Below Band: close < lower band AND close < lowest low of that window.
    On a hit, enrich with volume / RSI / weekly-level context.
    """
    if data is None or len(data) < 2:
        return None

    current_close = float(data["Close"].iloc[-1])
    current_rsi = data["RSI"].iloc[-1]
    bb_upper = data["BB_Upper"].iloc[-1]
    bb_lower = data["BB_Lower"].iloc[-1]

    lookback_intervals = tab.intervals_per_day * cfg.lookback_days
    lookback_data = data.iloc[max(0, len(data) - lookback_intervals - 1):-1]
    if lookback_data.empty:
        return None
    lookback_high = lookback_data["High"].max()
    lookback_low = lookback_data["Low"].min()

    band_status = None
    if current_close > bb_upper and current_close > lookback_high:
        band_status = "Above Band"
    elif current_close < bb_lower and current_close < lookback_low:
        band_status = "Below Band"
    if band_status is None:
        return None

    # Change% = move since the open of the latest session in the data.
    change_pct = 0.0
    last_date = data.index[-1].date()
    session = data[data.index.date == last_date]
    if not session.empty:
        day_open = float(session["Open"].iloc[0])
        if day_open:
            change_pct = (current_close - day_open) / day_open * 100

    result = {
        "Symbol": symbol,
        "LTP": _num(current_close),
        "Change%": _num(change_pct),
        "RSI(14)": _num(current_rsi) if pd.notna(current_rsi) else 0.0,
        "Band Status": band_status,
        "RelVol": _num(_rel_vol(data, tab)),
    }
    result.update(_weekly_levels(symbol, current_close, fetcher))
    return result


# --------------------------------------------------------------------------- #
# Filtering (server-side; mirrors the Tk "Apply Filters" logic)
# --------------------------------------------------------------------------- #
CHANGE_OPS = {
    "=": lambda a, b: a == b, ">": lambda a, b: a > b, "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
}
RELVOL_OPS = CHANGE_OPS


def parse_filters(args) -> dict:
    """Turn query-string args into a validated filter dict. Raises ValueError."""
    def fnum(name, default=None):
        raw = (args.get(name) or "").strip()
        if raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            raise ValueError("Please enter valid numbers for RSI, Change%, Rel Vol, or Scan# range.")

    f = {
        "symbol": (args.get("symbol") or "").strip().upper(),
        "band": args.get("band") or "All",
        "change_op": args.get("change_op") or "none",
        "change_val": fnum("change_val", 0.0),
        "rsi_op": args.get("rsi_op") or "none",
        "rsi_val": fnum("rsi_val", 50.0),
        "relvol_op": args.get("relvol_op") or "none",
        "relvol_val": fnum("relvol_val", 1.0),
        "week1": args.get("week1") or "All",
        "week2": args.get("week2") or "All",
        "scan_from": fnum("scan_from"),
        "scan_to": fnum("scan_to"),
        "action_types": None,
    }
    if "action_types" in args:
        sel = [s for s in (args.get("action_types") or "").split(",") if s]
        f["action_types"] = sel or None  # empty selection == no restriction (as in the Tk app)
    if f["scan_from"] is not None and f["scan_to"] is not None and f["scan_from"] > f["scan_to"]:
        raise ValueError("Scan# 'From' value cannot be greater than 'To'.")
    return f


def filter_results(results: list[dict], f: dict) -> list[dict]:
    out = []
    for r in results:
        if f["symbol"] and f["symbol"] not in r["Symbol"]:
            continue
        if f["band"] != "All" and r["Band Status"] != f["band"]:
            continue
        if f["action_types"] and r.get("ActionType") not in f["action_types"]:
            continue
        if f["relvol_op"] in RELVOL_OPS and not RELVOL_OPS[f["relvol_op"]](r.get("RelVol", 0.0), f["relvol_val"]):
            continue
        if f["scan_from"] is not None and r["Scan#"] < f["scan_from"]:
            continue
        if f["scan_to"] is not None and r["Scan#"] > f["scan_to"]:
            continue
        if f["rsi_op"] == "gt" and not r["RSI(14)"] > f["rsi_val"]:
            continue
        if f["rsi_op"] == "lt" and not r["RSI(14)"] < f["rsi_val"]:
            continue
        if f["week1"] != "All" and r.get("WeekHL_Color") != f["week1"]:
            continue
        if f["week2"] != "All" and r.get("Week2HL_Color") != f["week2"]:
            continue
        if f["change_op"] in CHANGE_OPS and not CHANGE_OPS[f["change_op"]](r["Change%"], f["change_val"]):
            continue
        out.append(r)
    return out


EXPORT_COLUMNS = [
    ("Scan#", "Scan#"), ("Time", "Time"), ("Symbol", "Symbol"), ("LTP", "LTP"),
    ("Change%", "Change%"), ("RSI(14)", "RSI(14)"), ("Band Status", "Band Status"),
    ("ActionType", "ActionType"), ("RelVol", "RelVol"),
    ("WeekHL_Color", "Week1 Status"), ("Week1Pct", "Week1 %vsLevel"),
    ("PrevWeekHigh", "PrevWeekHigh"), ("PrevWeekLow", "PrevWeekLow"),
    ("Week2HL_Color", "Week2 Status"), ("Week2Pct", "Week2 %vsLevel"),
    ("Prev2WeekHigh", "Prev2WeekHigh"), ("Prev2WeekLow", "Prev2WeekLow"),
]


def results_to_csv(rows: list[dict]) -> str:
    df = pd.DataFrame(rows)
    df = df.reindex(columns=[k for k, _ in EXPORT_COLUMNS])
    df.columns = [label for _, label in EXPORT_COLUMNS]
    for col in ("LTP", "Change%", "RSI(14)", "RelVol", "Week1 %vsLevel", "Week2 %vsLevel",
                "PrevWeekHigh", "PrevWeekLow", "Prev2WeekHigh", "Prev2WeekLow"):
        df[col] = df[col].astype(float).round(2)
    return df.to_csv(index=False)


# --------------------------------------------------------------------------- #
# Engine: owns the symbol list, tabs and the background scan thread
# --------------------------------------------------------------------------- #
def _idle_progress() -> dict:
    return {"phase": "idle", "tab": None, "tab_name": None, "scan_no": 0, "symbol": "",
            "started": 0, "done": 0, "total": 0, "found": 0, "wait_total": 0, "wait_left": 0}


class ScannerEngine:
    def __init__(self, fetcher=fetch_history, now_fn=lambda: dt.datetime.now(IST)):
        self.fetcher = fetcher
        self.now_fn = now_fn          # injectable clock, so tests can simulate 3:25 PM IST
        self.lock = threading.RLock()
        self.stock_list: list[str] = []
        self.file_name: str | None = None
        self.tabs = {k: ScanTab(k) for k in TAB_DEFS}
        self.config = ScanConfig()
        self.scanning = False
        self.status = "Ready"
        self.progress = _idle_progress()
        self.auto_stopped = False
        self._auto_stop_armed = False   # only a scan started *before* 3:25 PM IST can auto-stop
        self.errors: deque = deque(maxlen=50)
        self.error_count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seq = 0

    def _past_auto_stop(self) -> bool:
        """True at/after 3:25 PM IST - 5 minutes before the NSE close."""
        return self.now_fn().time() >= AUTO_STOP_TIME

    # ---- symbol list ---------------------------------------------------- #
    def set_symbols(self, symbols: list[str], file_name: str | None = None):
        """Replace the list. A running scan keeps its snapshot; the new list applies next scan."""
        with self.lock:
            self.stock_list = list(symbols)
            self.file_name = file_name

    # ---- control -------------------------------------------------------- #
    def start(self, cfg_dict: dict) -> tuple[bool, str]:
        with self.lock:
            if self.scanning:
                return False, "A scan is already running."
            if not self.stock_list:
                return False, "Please load a stock list first."
            cfg = ScanConfig.from_dict(cfg_dict)
            if not cfg.scan_5m and not cfg.scan_60m:
                return False, "Please select at least one scan type (5m or 60m)."
            self.config = cfg
            self.scanning = True
            self.status = "Starting..."
            self.progress = _idle_progress()
            self.auto_stopped = False
            # Only a scan that's actually started before the cutoff can be auto-stopped by
            # it later. Starting fresh after 3:25 PM (testing, catching up, etc.) must always
            # work, so arm the cutoff only when we're starting before it.
            self._auto_stop_armed = not self._past_auto_stop()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="scan-loop")
            self._thread.start()
        return True, "Scan started."

    def update_config(self, cfg_dict: dict) -> tuple[bool, str]:
        """
        Change scan setup parameters (timeframes, lookback, interval, workers) at any
        time, including while a scan is running. A scan pass already in progress keeps
        using the settings it started with; the new settings take effect starting with
        the next scan pass (checked between passes, in the usual 5m-then-60m order).
        """
        cfg = ScanConfig.from_dict(cfg_dict)
        if not cfg.scan_5m and not cfg.scan_60m:
            return False, "Please select at least one scan type (5m or 60m)."
        with self.lock:
            self.config = cfg
        return True, "Scan settings updated. They apply from the next scan."

    def stop(self):
        self._stop.set()
        with self.lock:
            if self.scanning:
                self.status = "Stopping..."

    def clear(self, tab_key: str):
        with self.lock:
            self.tabs[tab_key].clear()

    # ---- scan loop ------------------------------------------------------ #
    def _loop(self):
        """
        Runs 5m/60m scan passes back-to-back, then waits `scan_interval` seconds,
        repeating until stopped. Auto-stop (3:25 PM IST, 5 min before NSE close) only
        applies to a scan that was already running before that time (see `start()`);
        it's checked only between tabs/passes, never mid-symbol-loop, so a tab that is
        already scanning is always allowed to finish before the engine stops.
        """
        auto_stopped = False
        armed = self._auto_stop_armed
        try:
            while not self._stop.is_set():
                if armed and self._past_auto_stop():
                    auto_stopped = True
                    break
                cfg = self.config
                if cfg.scan_5m:
                    self._scan_tab(self.tabs["5m"], cfg)
                if self._stop.is_set():
                    break
                if armed and self._past_auto_stop():
                    auto_stopped = True
                    break
                if cfg.scan_60m:
                    self._scan_tab(self.tabs["60m"], cfg)
                if self._stop.is_set():
                    break
                if armed and self._past_auto_stop():
                    auto_stopped = True
                    break
                for i in range(cfg.scan_interval):
                    left = cfg.scan_interval - i
                    with self.lock:
                        self.status = f"Next scan in {left}s..."
                        self.progress = {**_idle_progress(), "phase": "waiting",
                                         "wait_total": cfg.scan_interval, "wait_left": left}
                    if self._stop.wait(1):
                        break
        except Exception as e:  # noqa: BLE001
            log.exception("scan loop crashed")
            self._record_error("-", "-", f"scan loop crashed: {e}")
        finally:
            with self.lock:
                self.scanning = False
                self.auto_stopped = auto_stopped
                self.status = "Stopped: market close (3:25 PM IST) reached" if auto_stopped else "Stopped"
                self.progress = _idle_progress()

    def _record_error(self, symbol: str, tab: str, msg: str):
        with self.lock:
            self.error_count += 1
            self.errors.append({"time": dt.datetime.now().strftime("%H:%M:%S"),
                                "symbol": symbol, "tab": tab, "error": msg})

    def _record_match(self, tab: ScanTab, scan_no: int, scan_time: str, stock: dict):
        """Publish one signal immediately so the UI can show it while the scan is still running."""
        with self.lock:
            key = f"{stock['Symbol']}_{stock['Band Status']}"
            action = "New Signal"
            if key in tab.signal_tracker:
                tab.signal_tracker[key] += 1
                action = f"Continue {tab.signal_tracker[key] - 1}"
            else:
                tab.signal_tracker[key] = 1
            self._seq += 1
            tab.all_results.append({**stock, "id": self._seq, "Scan#": scan_no,
                                    "Time": scan_time, "ActionType": action})
            tab.version += 1
            self.progress["found"] += 1

    def _scan_tab(self, tab: ScanTab, cfg: ScanConfig):
        with self.lock:
            tab.scan_count += 1
            scan_no = tab.scan_count
            symbols = list(self.stock_list)  # snapshot: a list change mid-scan applies next scan
            total = len(symbols)
            self.progress = {**_idle_progress(), "phase": "scanning", "tab": tab.key,
                             "tab_name": tab.name, "scan_no": scan_no, "total": total}
        scan_time = dt.datetime.now().strftime("%H:%M:%S")

        def work(symbol: str):
            if self._stop.is_set():
                return
            with self.lock:
                self.progress["started"] += 1
                self.progress["symbol"] = symbol
                self.status = f"[{tab.name}] Scanning {symbol} ({self.progress['started']}/{total})"
            try:
                data = self.fetcher(symbol, period=tab.period, interval=tab.interval)
                if data is None or data.empty:
                    return
                data = calculate_indicators(data)
                if data is None:
                    return
                result = check_conditions(data, symbol, tab, cfg, self.fetcher)
                if result:
                    self._record_match(tab, scan_no, scan_time, result)
            except Exception as e:  # noqa: BLE001
                self._record_error(symbol, tab.key, str(e)[:200])
            finally:
                with self.lock:
                    self.progress["done"] += 1

        if cfg.workers > 1:
            with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
                list(ex.map(work, symbols))
        else:
            for sym in symbols:
                if self._stop.is_set():
                    break
                work(sym)

    # ---- read side ------------------------------------------------------ #
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "scanning": self.scanning,
                "status": self.status,
                "auto_stopped": self.auto_stopped,
                "progress": dict(self.progress),
                "symbols": len(self.stock_list),
                "file_name": self.file_name,
                "config": self.config.__dict__.copy(),
                "error_count": self.error_count,
                "errors": list(self.errors)[-10:],
                "tabs": {k: {"name": t.name, "scan_count": t.scan_count,
                             "total_results": len(t.all_results), "version": t.version}
                         for k, t in self.tabs.items()},
            }

    def query(self, tab_key: str, f: dict) -> list[dict]:
        with self.lock:
            rows = list(self.tabs[tab_key].all_results)
        return filter_results(rows, f)
