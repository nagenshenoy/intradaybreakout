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

from persistence import SessionStore, previous_trading_date, trading_date

log = logging.getLogger("scanner")

ACTION_TYPES = ["New Signal"] + [f"Continue {i}" for i in range(1, 11)]

IST = ZoneInfo("Asia/Kolkata")
AUTO_STOP_TIME = dt.time(15, 25)  # NSE market closes 3:30 PM IST; scanner auto-stops 5 min before

# Per-timeframe settings (identical to the values hard-coded in the Tk app).
# intervals_per_day is also the candle count used for Rel Vol's "previous day"
# average: 375 minutes of NSE trading (9:15 AM - 3:30 PM) / interval minutes.
TAB_DEFS = {
    # yfinance serves at most 7 calendar days (~5 trading days) of 1-minute data, so
    # lookback windows longer than that are simply capped at the data available.
    "1m": dict(name="1 Minute", interval="1m", period="7d",
               intervals_per_day=375, tv_interval="1"),
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
    scan_1m: bool = False
    scan_5m: bool = True
    scan_60m: bool = False
    lookback_days: int = 2
    scan_interval: int = 60
    workers: int = 1
    rsi_period: int = 14
    # True: Change% = move from the previous trading day's close (default).
    # False: Change% = move from today's opening price.
    change_from_prev_close: bool = True

    @property
    def any_timeframe(self) -> bool:
        return self.scan_1m or self.scan_5m or self.scan_60m

    @classmethod
    def from_dict(cls, d: dict) -> "ScanConfig":
        def clamp(v, lo, hi, default):
            try:
                return max(lo, min(hi, int(float(v))))
            except (TypeError, ValueError):
                return default

        def as_bool(v, default):
            if v is None:
                return default
            if isinstance(v, str):
                return v.strip().lower() not in ("", "0", "false", "no", "off")
            return bool(v)

        return cls(
            scan_1m=bool(d.get("scan_1m", False)),
            scan_5m=bool(d.get("scan_5m", True)),
            scan_60m=bool(d.get("scan_60m", False)),
            lookback_days=clamp(d.get("lookback_days"), 0, 10, 2),  # 0 = Bollinger band only, no lookback comparison
            scan_interval=clamp(d.get("scan_interval"), 5, 3600, 60),
            workers=clamp(d.get("workers"), 1, 8, 1),
            rsi_period=clamp(d.get("rsi_period"), 2, 50, 14),
            change_from_prev_close=as_bool(d.get("change_from_prev_close"), True),
        )


class ScanTab:
    """State for one timeframe (1m / 5m / 60m): counters, results, signal tracker."""

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
def calculate_indicators(data: pd.DataFrame, rsi_period: int = 14) -> pd.DataFrame | None:
    try:
        bb = BollingerBands(close=data["Close"], window=20, window_dev=2)
        data["BB_Upper"] = bb.bollinger_hband()
        data["BB_Lower"] = bb.bollinger_lband()
        data["RSI"] = RSIIndicator(close=data["Close"], window=int(rsi_period)).rsi()
        data.attrs["rsi_period"] = int(rsi_period)   # so each signal records the period it used
        return data
    except Exception:  # noqa: BLE001 - mirrors the original "skip bad data"
        return None


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
    Breakout rule:
      Above Band: close > upper Bollinger band AND close > highest high of the
                  previous `lookback_days` of candles.
      Below Band: close < lower band AND close < lowest low of that window.
    With `lookback_days == 0` no lookback comparison is made at all: the signal is
    purely the Bollinger band test (close > upper band, or close < lower band).
    On a hit, enrich with volume / RSI / previous-day-high context.
    """
    if data is None or len(data) < 2:
        return None

    current_close = float(data["Close"].iloc[-1])
    current_rsi = data["RSI"].iloc[-1]
    bb_upper = data["BB_Upper"].iloc[-1]
    bb_lower = data["BB_Lower"].iloc[-1]

    band_status = None
    if cfg.lookback_days == 0:
        # Band-only mode: nothing to compare against beyond the bands themselves.
        if current_close > bb_upper:
            band_status = "Above Band"
        elif current_close < bb_lower:
            band_status = "Below Band"
    else:
        lookback_intervals = tab.intervals_per_day * cfg.lookback_days
        lookback_data = data.iloc[max(0, len(data) - lookback_intervals - 1):-1]
        if lookback_data.empty:
            return None
        lookback_high = lookback_data["High"].max()
        lookback_low = lookback_data["Low"].min()
        if current_close > bb_upper and current_close > lookback_high:
            band_status = "Above Band"
        elif current_close < bb_lower and current_close < lookback_low:
            band_status = "Below Band"
    if band_status is None:
        return None

    # Prior sessions come from the same candles (no extra request per symbol).
    last_date = data.index[-1].date()
    session = data[data.index.date == last_date]
    prior = data[data.index.date < last_date]

    # Change%: from the previous trading day's close (default), or from the open of the
    # latest session. If the data holds no earlier session (nothing to take a previous
    # close from), it falls back to the session open rather than showing nothing.
    change_pct = 0.0
    base = 0.0
    if cfg.change_from_prev_close and not prior.empty:
        base = float(prior["Close"].iloc[-1])
    if not base and not session.empty:
        base = float(session["Open"].iloc[0])
    if base:
        change_pct = (current_close - base) / base * 100

    # Previous trading day's high: the highest High of the most recent earlier session.
    prev_day_high = None
    if not prior.empty:
        prev_session = prior[prior.index.date == prior.index[-1].date()]
        prev_day_high = _num(prev_session["High"].max(), default=None)

    result = {
        "Symbol": symbol,
        "LTP": _num(current_close),
        "Change%": _num(change_pct),
        "RSI": _num(current_rsi) if pd.notna(current_rsi) else 0.0,
        "RSIPeriod": int(data.attrs.get("rsi_period", cfg.rsi_period)),
        "Band Status": band_status,
        "RelVol": _num(_rel_vol(data, tab)),
        "PrevDayHigh": prev_day_high,
    }
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
        if f["rsi_op"] == "gt" and not r["RSI"] > f["rsi_val"]:
            continue
        if f["rsi_op"] == "lt" and not r["RSI"] < f["rsi_val"]:
            continue
        if f["change_op"] in CHANGE_OPS and not CHANGE_OPS[f["change_op"]](r["Change%"], f["change_val"]):
            continue
        out.append(r)
    return out


EXPORT_COLUMNS = [
    ("Scan#", "Scan#"), ("Time", "Time"), ("Symbol", "Symbol"), ("LTP", "LTP"),
    ("Change%", "Change%"), ("RSI", "RSI"), ("Band Status", "Band Status"),
    ("ActionType", "ActionType"), ("RelVol", "RelVol"),
    ("PrevDayHigh", "PrevDayHigh"),
]

# Export-time symbol formats. Symbols are stored internally as Yahoo Finance
# tickers (e.g. "RELIANCE.NS"); these just reshape that string for CSV export.
SYMBOL_FORMATS = ("ns", "plain", "nse")


def format_symbol(symbol: str, fmt: str = "ns") -> str:
    """Reshape one symbol for export: 'ns' -> RELIANCE.NS (as stored), 'plain'
    -> RELIANCE, 'nse' -> NSE:RELIANCE. Unrecognized formats fall back to 'ns'."""
    s = str(symbol).strip()
    plain = s[:-3] if s.upper().endswith(".NS") else s
    if fmt == "plain":
        return plain
    if fmt == "nse":
        return f"NSE:{plain}"
    return f"{plain}.NS"


def results_to_csv(rows: list[dict], symbol_format: str = "ns") -> str:
    # The RSI header names its period, e.g. "RSI(14)". If the exported rows were produced
    # with different periods (setting changed mid-run) it is just "RSI".
    periods = {r.get("RSIPeriod") for r in rows} - {None}
    rsi_label = f"RSI({periods.pop()})" if len(periods) == 1 else "RSI"
    labels = [rsi_label if k == "RSI" else label for k, label in EXPORT_COLUMNS]
    df = pd.DataFrame(rows)
    df = df.reindex(columns=[k for k, _ in EXPORT_COLUMNS])
    if "Symbol" in df.columns and not df.empty:
        df["Symbol"] = df["Symbol"].apply(lambda s: format_symbol(s, symbol_format))
    df.columns = labels
    for col in ("LTP", "Change%", rsi_label, "RelVol", "PrevDayHigh"):
        df[col] = df[col].astype(float).round(2)
    return df.to_csv(index=False)


# --------------------------------------------------------------------------- #
# Engine: owns the symbol list, tabs and the background scan thread
# --------------------------------------------------------------------------- #
def _idle_progress() -> dict:
    return {"phase": "idle", "tab": None, "tab_name": None, "scan_no": 0, "symbol": "",
            "started": 0, "done": 0, "total": 0, "found": 0, "wait_total": 0, "wait_left": 0}


class ScannerEngine:
    def __init__(self, fetcher=fetch_history, now_fn=lambda: dt.datetime.now(IST),
                 store: SessionStore | None = None,
                 date_fn=lambda: dt.datetime.now(IST).date()):
        self.fetcher = fetcher
        self.now_fn = now_fn          # injectable clock, so tests can simulate 3:25 PM IST
        self.date_fn = date_fn        # injectable calendar date (kept apart from now_fn)
        self.store = store            # JSON snapshot of the day's output; None = in-memory only
        self.session_date: dt.date | None = None   # trading date the current results belong to
        self.restored_rows = 0
        self.persist_error: str | None = None
        self._persist_lock = threading.Lock()
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
        if self.store is not None:
            self._restore()

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
            if not cfg.any_timeframe:
                return False, "Please select at least one scan type (1m, 5m or 60m)."
            self.config = cfg
            self._begin_session()
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
        self._persist()
        return True, "Scan started."

    def update_config(self, cfg_dict: dict) -> tuple[bool, str]:
        """
        Change scan setup parameters (timeframes, lookback, interval, workers) at any
        time, including while a scan is running. A scan pass already in progress keeps
        using the settings it started with; the new settings take effect starting with
        the next scan pass (checked between passes, in the usual 1m, 5m, 60m order).
        """
        cfg = ScanConfig.from_dict(cfg_dict)
        if not cfg.any_timeframe:
            return False, "Please select at least one scan type (1m, 5m or 60m)."
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
        self._persist()

    # ---- day session: JSON snapshot ------------------------------------- #
    def _begin_session(self):
        """Called when a scan is started. Output from an earlier trading date is wiped now
        (not before), so yesterday's results stay viewable until the new scan begins.
        Sat/Sun belong to Friday's session, so starting then keeps Friday's data."""
        today = trading_date(self.date_fn())
        if self.session_date is not None and self.session_date != today:
            for t in self.tabs.values():
                t.clear()
            self.restored_rows = 0
        self.session_date = today

    def _persist(self):
        """Write the snapshot (atomic). Never raises: a disk problem must not stop scanning."""
        if self.store is None:
            return
        with self.lock:
            if self.session_date is None:
                return
            payload = {
                "session_date": self.session_date.isoformat(),
                "saved_at": dt.datetime.now(IST).isoformat(timespec="seconds"),
                "seq": self._seq,
                "tabs": {k: {"scan_count": t.scan_count,
                             "signal_tracker": dict(t.signal_tracker),
                             "results": list(t.all_results)}
                         for k, t in self.tabs.items()},
            }
        try:
            with self._persist_lock:
                self.store.save(payload)
            self.persist_error = None
        except Exception as e:  # noqa: BLE001
            log.warning("could not save session snapshot: %s", e)
            self.persist_error = str(e)[:200]

    def _restore(self):
        """Load a saved snapshot if it belongs to the current or the previous trading date."""
        data = self.store.load()
        if not data:
            return
        try:
            saved = dt.date.fromisoformat(str(data["session_date"]))
        except (KeyError, ValueError):
            return
        today = self.date_fn()
        if saved not in (trading_date(today), previous_trading_date(today)):
            return
        restored, max_id = 0, 0
        for key, tab in self.tabs.items():
            saved_tab = (data.get("tabs") or {}).get(key)
            if not isinstance(saved_tab, dict):
                continue
            rows = []
            for r in saved_tab.get("results") or []:
                try:
                    if not isinstance(r, dict):
                        continue
                    r = {**r, "id": int(r["id"]), "Scan#": int(r["Scan#"])}
                    for need in ("Symbol", "Band Status", "Time", "ActionType"):
                        if not isinstance(r.get(need), str):
                            raise ValueError(need)
                except (KeyError, TypeError, ValueError):
                    continue
                rows.append(r)
            tracker = {}
            for k, v in (saved_tab.get("signal_tracker") or {}).items():
                try:
                    tracker[str(k)] = int(v)
                except (TypeError, ValueError):
                    pass
            try:
                count = int(saved_tab.get("scan_count") or 0)
            except (TypeError, ValueError):
                count = 0
            tab.all_results = rows
            tab.signal_tracker = tracker
            tab.scan_count = max([count] + [r["Scan#"] for r in rows])
            tab.version += 1
            restored += len(rows)
            max_id = max([max_id] + [r["id"] for r in rows])
        try:
            self._seq = max(int(data.get("seq") or 0), max_id)
        except (TypeError, ValueError):
            self._seq = max_id
        self.session_date = saved
        self.restored_rows = restored

    # ---- scan loop ------------------------------------------------------ #
    def _loop(self):
        """
        Runs 1m/5m/60m scan passes back-to-back, then waits `scan_interval` seconds,
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
                for key, enabled in (("1m", cfg.scan_1m), ("5m", cfg.scan_5m), ("60m", cfg.scan_60m)):
                    if not enabled:
                        continue
                    self._scan_tab(self.tabs[key], cfg)
                    if self._stop.is_set():
                        break
                    if armed and self._past_auto_stop():
                        auto_stopped = True
                        break
                if self._stop.is_set() or auto_stopped:
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
            self.errors.append({"time": self.now_fn().strftime("%H:%M:%S"),
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
            # The Scan# is only "used up" once a pass actually produces output, so passes
            # with no signals never leave a gap in the sequence.
            tab.scan_count = max(tab.scan_count, scan_no)
            tab.version += 1
            self.progress["found"] += 1

    def _scan_tab(self, tab: ScanTab, cfg: ScanConfig):
        with self.lock:
            # Provisional number: the next unused Scan#. tab.scan_count only advances when
            # this pass records a signal (see _record_match), so an empty pass hands the
            # same number to the next pass instead of skipping it.
            scan_no = tab.scan_count + 1
            symbols = list(self.stock_list)  # snapshot: a list change mid-scan applies next scan
            total = len(symbols)
            self.progress = {**_idle_progress(), "phase": "scanning", "tab": tab.key,
                             "tab_name": tab.name, "scan_no": scan_no, "total": total}
        scan_time = self.now_fn().strftime("%H:%M:%S")

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
                data = calculate_indicators(data, cfg.rsi_period)
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
        self._persist()   # one snapshot per pass keeps the file current without per-signal writes

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
                "session": {"date": self.session_date.isoformat() if self.session_date else None,
                            "restored": self.restored_rows,
                            "persistence": self.store is not None,
                            "error": self.persist_error},
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

    def scan_list(self, tab_key: str) -> list[dict]:
        """Distinct Scan# values that actually have signals in this tab (for the
        export dropdown), newest first, each with its scan time and signal count."""
        with self.lock:
            rows = list(self.tabs[tab_key].all_results)
        agg: dict[int, dict] = {}
        for r in rows:
            n = r["Scan#"]
            e = agg.setdefault(n, {"scan_no": n, "time": r["Time"], "count": 0})
            e["count"] += 1
        return sorted(agg.values(), key=lambda e: e["scan_no"], reverse=True)
