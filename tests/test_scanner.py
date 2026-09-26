"""Tests use a fake fetcher, so no network is needed."""
import datetime as dt
import io
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app                      # noqa: E402
from scanner import (IST, ScanConfig, ScanTab, ScannerEngine, _rel_vol,  # noqa: E402
                     calculate_indicators, check_conditions, filter_results, parse_filters)

_TODAY = pd.Timestamp("2026-01-08", tz="Asia/Kolkata")  # fixed weekday anchor; keeps data generators
                                                         # deterministic regardless of the real date


def intraday(n_days=6, per_day=75, last_move=0.0, freq="5min", today_vol_mult=1.0, base_vol=1000.0):
    """
    Flat-ish random walk on NSE hours; `last_move` is a % jump on the final candle.
    Volume is a deterministic `base_vol` for every candle except the last (today's)
    trading day, which is scaled by `today_vol_mult` - so Rel Vol comes out to exactly
    `today_vol_mult` when there are >= 2 days of data.
    Anchored to a fixed weekday (not the real wall-clock date), so tests don't flake
    out over a weekend.
    """
    rng = np.random.default_rng(1)
    days = pd.bdate_range(end=_TODAY, periods=n_days)
    idx = []
    for d in days:
        idx += list(pd.date_range(d + pd.Timedelta(hours=9, minutes=15), periods=per_day,
                                  freq=freq, tz="Asia/Kolkata"))
    close = 100 + np.cumsum(rng.normal(0, 0.05, len(idx)))
    close[-1] = close[-2] * (1 + last_move / 100)
    volume = np.full(len(idx), float(base_vol))
    volume[-per_day:] *= today_vol_mult
    df = pd.DataFrame({"Open": np.r_[close[0], close[:-1]], "High": close + 0.05,
                       "Low": close - 0.05, "Close": close, "Volume": volume},
                      index=pd.DatetimeIndex(idx))
    df.iloc[-1, df.columns.get_loc("High")] = max(close[-1], close[-2]) + 0.05
    df.iloc[-1, df.columns.get_loc("Low")] = min(close[-1], close[-2]) - 0.05
    return df


def daily(n=40, drift=0.0):
    idx = pd.bdate_range(end=_TODAY, periods=n, tz="Asia/Kolkata")
    close = 100 + np.cumsum(np.full(n, drift)) + np.random.default_rng(2).normal(0, 0.3, n)
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1e5}, index=idx)


def weekly(high=101.0, low=95.0):
    # This one must track the real wall-clock date: _weekly_levels() in scanner.py
    # compares its data against dt.date.today() to tell "the current week" apart
    # from a completed one, so the test data has to line up with the real date.
    end = pd.Timestamp.now(tz="Asia/Kolkata").normalize()
    monday = end - pd.Timedelta(days=end.weekday())
    idx = pd.DatetimeIndex([monday - pd.Timedelta(weeks=2), monday - pd.Timedelta(weeks=1), monday])
    return pd.DataFrame({"Open": 100.0, "High": [high + 5, high, high + 20], "Low": [low - 5, low, low],
                         "Close": 100.0, "Volume": 1e6}, index=idx)


def make_fetcher(moves, vol_mults=None):
    """moves: symbol -> % jump on the last candle. vol_mults: symbol -> today's Rel Vol multiplier."""
    vol_mults = vol_mults or {}

    def fetch(symbol, period, interval, **kw):
        if symbol == "BROKEN.NS":
            raise RuntimeError("yahoo said no")
        mult = vol_mults.get(symbol, 1.0)
        if interval == "5m":
            return intraday(last_move=moves.get(symbol, 0.0), today_vol_mult=mult)
        if interval == "60m":
            return intraday(n_days=20, per_day=7, freq="60min", last_move=moves.get(symbol, 0.0), today_vol_mult=mult)
        if interval == "1d":
            return daily()
        if interval == "1wk":
            return weekly()
        raise AssertionError(interval)
    return fetch


CFG = ScanConfig()


def test_above_and_below_breakouts_detected():
    tab = ScanTab("5m")
    up = calculate_indicators(intraday(last_move=+2.0, today_vol_mult=2.0))
    r = check_conditions(up, "UP.NS", tab, CFG, make_fetcher({}))
    assert r and r["Band Status"] == "Above Band"
    assert r["Change%"] != 0 and r["LTP"] > 100
    assert r["RelVol"] == pytest.approx(2.0, rel=1e-6)

    down = calculate_indicators(intraday(last_move=-2.0, today_vol_mult=0.4))
    r = check_conditions(down, "DN.NS", tab, CFG, make_fetcher({}))
    assert r and r["Band Status"] == "Below Band"
    assert r["RelVol"] == pytest.approx(0.4, rel=1e-6)


def test_rel_vol_calculation_and_edge_cases():
    tab5 = ScanTab("5m")
    tab60 = ScanTab("60m")

    # today's average / full previous day's average, capped at intervals_per_day candles
    df = calculate_indicators(intraday(n_days=3, today_vol_mult=1.5))
    assert _rel_vol(df, tab5) == pytest.approx(1.5, rel=1e-6)

    df60 = calculate_indicators(intraday(n_days=3, per_day=7, freq="60min", today_vol_mult=0.75))
    assert _rel_vol(df60, tab60) == pytest.approx(0.75, rel=1e-6)

    # only one trading day of data -> nothing to compare against -> 0.0, no crash
    assert _rel_vol(calculate_indicators(intraday(n_days=1)), tab5) == 0.0

    # previous day present but with zero volume -> 0.0 rather than a ZeroDivisionError
    df0 = calculate_indicators(intraday(n_days=2))
    df0.loc[df0.index.normalize() == df0.index.normalize()[0], "Volume"] = 0.0
    assert _rel_vol(df0, tab5) == 0.0

    assert _rel_vol(pd.DataFrame(), tab5) == 0.0


def test_no_breakout_returns_none():
    tab = ScanTab("5m")
    flat = calculate_indicators(intraday(last_move=0.0))
    assert check_conditions(flat, "FLAT.NS", tab, CFG, make_fetcher({})) is None


def test_weekly_levels_and_json_safe():
    import json
    tab = ScanTab("5m")
    up = calculate_indicators(intraday(last_move=+3.0))
    r = check_conditions(up, "UP.NS", tab, CFG, make_fetcher({}))
    # weekly(): previous week high=101, low=95 -> LTP ~103 is beyond the high => Green
    assert r["WeekHL_Color"] == "Green" and r["Week1Pct"] > 0
    json.dumps(r, allow_nan=False)  # no NaN/inf anywhere


def test_filters():
    rows = [
        {"id": 1, "Symbol": "AAA.NS", "Band Status": "Above Band", "RSI(14)": 82.0, "Change%": 2.0,
         "Scan#": 1, "ActionType": "New Signal", "RelVol": 2.5, "WeekHL_Color": "Green", "Week2HL_Color": "Black"},
        {"id": 2, "Symbol": "BBB.NS", "Band Status": "Below Band", "RSI(14)": 20.0, "Change%": -3.0,
         "Scan#": 2, "ActionType": "Continue 1", "RelVol": 0.6, "WeekHL_Color": "Red", "Week2HL_Color": "Red"},
    ]
    f = lambda **kw: filter_results(rows, parse_filters(kw))  # noqa: E731
    assert len(f()) == 2
    assert [r["id"] for r in f(symbol="aaa")] == [1]
    assert [r["id"] for r in f(band="Below Band")] == [2]
    assert [r["id"] for r in f(rsi_op="gt", rsi_val="50")] == [1]
    assert [r["id"] for r in f(rsi_op="lt", rsi_val="50")] == [2]
    assert [r["id"] for r in f(change_op="<", change_val="0")] == [2]
    assert [r["id"] for r in f(relvol_op=">=", relvol_val="1")] == [1]
    assert [r["id"] for r in f(relvol_op="<", relvol_val="1")] == [2]
    assert [r["id"] for r in f(relvol_op="=", relvol_val="2.5")] == [1]
    assert [r["id"] for r in f(week1="Red")] == [2]
    assert [r["id"] for r in f(scan_from="2")] == [2]
    assert [r["id"] for r in f(action_types="New Signal")] == [1]
    assert len(f(action_types="")) == 2            # nothing selected == no restriction
    with pytest.raises(ValueError):
        parse_filters({"scan_from": "5", "scan_to": "2"})
    with pytest.raises(ValueError):
        parse_filters({"rsi_val": "abc"})


def wait_until(pred, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_engine_tracks_new_and_continue_and_errors():
    eng = ScannerEngine(fetcher=make_fetcher({"UP.NS": 2.0}))
    eng.set_symbols(["UP.NS", "FLAT.NS", "BROKEN.NS"])
    ok, _ = eng.start({"scan_5m": True, "scan_60m": False, "scan_interval": 5})
    assert ok
    tab = eng.tabs["5m"]
    assert wait_until(lambda: tab.scan_count >= 1 and len(tab.all_results) >= 1)
    eng.stop()
    assert wait_until(lambda: not eng.scanning)
    assert tab.all_results[0]["ActionType"] == "New Signal"
    assert eng.error_count >= 1 and eng.errors[-1]["symbol"] == "BROKEN.NS"
    # a second scan of the same symbol+band must be a "Continue 1"
    eng._stop.clear()                       # start() normally does this
    n = len(tab.all_results)
    eng._scan_tab(tab, ScanConfig())
    assert len(tab.all_results) == n + 1
    assert tab.all_results[-1]["ActionType"] == "Continue 1"
    assert eng.status  # never empty


def test_engine_parallel_workers():
    eng = ScannerEngine(fetcher=make_fetcher({"A.NS": 2.0, "B.NS": -2.0}))
    eng.set_symbols(["A.NS", "B.NS", "C.NS", "D.NS"])
    eng._scan_tab(eng.tabs["5m"], ScanConfig(workers=4))
    assert {r["Symbol"] for r in eng.tabs["5m"].all_results} == {"A.NS", "B.NS"}


def _clock(*times):
    """Returns each given IST datetime once, then repeats the last one forever."""
    it = iter(times)
    last = times[-1]

    def now():
        nonlocal last
        last = next(it, last)
        return last
    return now


def test_auto_stop_lets_the_in_progress_scan_finish():
    # Before cutoff at the top of the loop, but past cutoff by the time the 5m scan
    # finishes -> the 5m scan must still complete, and 60m must never start.
    # (First "before" is consumed by start()'s arming check.)
    before = dt.datetime(2026, 1, 5, 15, 20, tzinfo=IST)
    after = dt.datetime(2026, 1, 5, 15, 26, tzinfo=IST)
    eng = ScannerEngine(fetcher=make_fetcher({}), now_fn=_clock(before, before, after))
    eng.set_symbols(["AAA.NS", "BBB.NS"])
    ok, _ = eng.start({"scan_5m": True, "scan_60m": True, "scan_interval": 5})
    assert ok
    assert wait_until(lambda: not eng.scanning)
    snap = eng.snapshot()
    assert snap["auto_stopped"] is True and "3:25" in snap["status"]
    assert eng.tabs["5m"].scan_count == 1     # completed
    assert eng.tabs["60m"].scan_count == 0    # never started


def test_no_auto_stop_before_cutoff():
    before = dt.datetime(2026, 1, 5, 11, 0, tzinfo=IST)
    eng = ScannerEngine(fetcher=make_fetcher({}), now_fn=_clock(before))
    eng.set_symbols(["AAA.NS"])
    eng.start({"scan_5m": True, "scan_interval": 5})
    assert wait_until(lambda: eng.tabs["5m"].scan_count >= 1)
    assert eng.scanning and not eng.snapshot()["auto_stopped"]
    eng.stop()
    assert wait_until(lambda: not eng.scanning)
    assert eng.snapshot()["auto_stopped"] is False


def test_starting_after_cutoff_scans_normally_and_never_auto_stops():
    """
    Regression test: starting a scan when the clock is already past 3:25 PM IST
    (e.g. evening testing, or a browser reload late in the day) must scan exactly
    like any other time of day - not stop itself before the first pass ever runs.
    The auto-stop only applies to a scan that was already running before the cutoff.
    """
    after = dt.datetime(2026, 1, 5, 21, 26, tzinfo=IST)
    eng = ScannerEngine(fetcher=make_fetcher({}), now_fn=_clock(after))
    eng.set_symbols(["AAA.NS", "BBB.NS"])
    ok, _ = eng.start({"scan_5m": True, "scan_60m": True, "scan_interval": 5})
    assert ok
    assert wait_until(lambda: eng.tabs["5m"].scan_count >= 1 and eng.tabs["60m"].scan_count >= 1)
    assert eng.scanning and not eng.snapshot()["auto_stopped"]   # never armed, never stops itself
    eng.stop()
    assert wait_until(lambda: not eng.scanning)
    assert eng.snapshot()["auto_stopped"] is False


def test_update_config_applies_from_the_next_scan_pass():
    """
    Live setup changes must not disturb a pass already in progress: while the 5m
    scan of a slow symbol is in flight, flipping to 60m-only must have no effect
    until that pass finishes, after which the *next* pass (checked right after the
    wait interval) picks up the new settings and scans 60m instead of 5m.
    """
    gate = threading.Event()
    base = make_fetcher({})

    def fetch(symbol, period, interval, **kw):
        if symbol == "SLOW.NS" and interval == "5m":
            gate.wait(10)
        return base(symbol, period, interval, **kw)

    eng = ScannerEngine(fetcher=fetch)
    eng.set_symbols(["SLOW.NS"])
    assert eng.start({"scan_5m": True, "scan_60m": False, "scan_interval": 5})[0]
    try:
        assert wait_until(lambda: eng.snapshot()["progress"]["symbol"] == "SLOW.NS")
        assert eng.tabs["5m"].scan_count == 1        # the in-flight pass has already started
        ok, msg = eng.update_config({"scan_5m": False, "scan_60m": True, "scan_interval": 5})
        assert ok and "next scan" in msg
        assert eng.tabs["60m"].scan_count == 0        # next pass hasn't started yet - unaffected so far
    finally:
        gate.set()
    assert wait_until(lambda: eng.tabs["60m"].scan_count >= 1)  # next pass switched to 60m
    assert eng.tabs["5m"].scan_count == 1                       # 5m was not scanned again
    eng.stop()
    assert wait_until(lambda: not eng.scanning)


def test_update_config_rejects_no_timeframe():
    eng = ScannerEngine(fetcher=make_fetcher({}))
    ok, msg = eng.update_config({"scan_5m": False, "scan_60m": False})
    assert not ok and "at least one" in msg


BUNDLED = Path(__file__).resolve().parent.parent / "data" / "ScannerData.xlsx"


@pytest.fixture
def client():
    eng = ScannerEngine(fetcher=make_fetcher({"UP.NS": 2.0, "DN.NS": -2.0}))
    app = create_app(eng)
    return app.test_client(), eng


def test_api_end_to_end(client):
    c, eng = client
    assert c.get("/").status_code == 200
    assert c.get("/static/js/app.js").status_code == 200

    # start without symbols -> 409
    assert c.post("/api/scan/start", json={}).status_code == 409

    # bad file type
    r = c.post("/api/import", data={"file": (io.BytesIO(b"x"), "a.txt")}, content_type="multipart/form-data")
    assert r.status_code == 400

    csv = b"Symbol\nup.ns\n DN.NS \nFLAT.NS\nup.ns\n"
    r = c.post("/api/import", data={"file": (io.BytesIO(csv), "list.csv")}, content_type="multipart/form-data")
    assert r.get_json()["count"] == 3        # header dropped, trimmed, upper-cased, de-duplicated

    # xlsx import
    buf = io.BytesIO()
    pd.DataFrame({"Symbol": ["UP.NS", "DN.NS", "FLAT.NS"]}).to_excel(buf, index=False)
    buf.seek(0)
    r = c.post("/api/import", data={"file": (buf, "list.xlsx")}, content_type="multipart/form-data")
    assert r.get_json()["count"] == 3

    assert c.post("/api/scan/start", json={"scan_5m": False, "scan_60m": False}).status_code == 409
    assert c.post("/api/scan/start", json={"scan_5m": True, "scan_interval": 5}).get_json()["ok"]
    assert c.post("/api/scan/start", json={}).status_code == 409   # already running
    tab = eng.tabs["5m"]
    assert wait_until(lambda: len(tab.all_results) >= 2)

    # live scan-setup update while running
    assert c.post("/api/scan/config", json={"scan_5m": False, "scan_60m": False}).status_code == 400
    r = c.post("/api/scan/config", json={"scan_5m": True, "scan_60m": True, "scan_interval": 5}).get_json()
    assert r["ok"] and eng.config.scan_60m is True

    st = c.get("/api/state").get_json()
    assert st["scanning"] and st["symbols"] == 3 and st["tabs"]["5m"]["total_results"] >= 2

    res = c.get("/api/results?tab=5m&band=Above Band").get_json()
    assert res["total_filtered"] >= 1 and all(r["Band Status"] == "Above Band" for r in res["rows"])
    assert c.get("/api/results?tab=5m&scan_from=9&scan_to=1").status_code == 400
    assert c.get("/api/results?tab=nope").status_code == 400

    exp = c.get("/api/export?tab=5m")
    assert exp.status_code == 200 and exp.mimetype == "text/csv"
    header = exp.get_data(as_text=True).splitlines()[0]
    assert header.startswith("Scan#,Time,Symbol,LTP") and "Week1 %vsLevel" in header

    c.post("/api/scan/stop")
    assert wait_until(lambda: not eng.scanning)
    assert c.post("/api/clear", json={"tab": "5m"}).get_json()["ok"]
    assert c.get("/api/results?tab=5m").get_json()["total_all"] == 0
    assert c.get("/api/export?tab=5m").status_code == 404


# ------------------------------------------------------------------ v2 ----
import threading                                     # noqa: E402

import sources                                       # noqa: E402


def test_header_detection():
    frame = lambda rows: pd.DataFrame(rows, dtype=str)   # noqa: E731
    # headerless: the first row is a real symbol and must NOT be dropped
    assert sources.symbols_from_frame(frame([["icicibank.ns"], ["INFY.NS"]])) == ["ICICIBANK.NS", "INFY.NS"]
    # headered
    assert sources.symbols_from_frame(frame([["Symbol"], ["tcs.ns"], [" tcs.ns "], [None]])) == ["TCS.NS"]
    # picks the Symbol column, not column A
    assert sources.symbols_from_frame(frame([["Company", "Ticker"], ["Infosys", "INFY.NS"]])) == ["INFY.NS"]
    assert sources.symbols_from_frame(frame([])) == []


@pytest.mark.skipif(not BUNDLED.exists(), reason="bundled workbook missing")
def test_bundled_workbook_sheets():
    sheets = sources.BundledWorkbook(BUNDLED).sheets()
    assert list(sheets) == ["Nifty50", "Nifty500", "Smallcap500", "NiftyTotalMarket"]
    assert len(sheets["Nifty50"]) == 50 and sheets["Nifty50"][0] == "ICICIBANK.NS"   # first row kept
    assert len(sheets["Smallcap500"]) == 420                                          # 1 duplicate removed
    assert all(s.endswith(".NS") for v in sheets.values() for s in v)


def test_quick_load_and_multisheet_upload(client):
    c, eng = client
    src = c.get("/api/sources").get_json()
    assert [s["name"] for s in src["bundled"]["sheets"]] == ["Nifty50", "Nifty500", "Smallcap500", "NiftyTotalMarket"]
    assert src["upload"] is None

    r = c.post("/api/load", json={"source": "bundled", "sheet": "Nifty50"}).get_json()
    assert r["count"] == 50 and "Nifty50" in r["file_name"] and len(eng.stock_list) == 50
    assert c.post("/api/load", json={"source": "bundled", "sheet": "Nope"}).status_code == 404
    assert c.post("/api/load", json={"source": "deadbeef", "sheet": "x"}).status_code == 404

    # multi-sheet, headerless upload -> needs a sheet choice, no symbols loaded yet
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as xw:
        pd.DataFrame(["AAA.NS", "BBB.NS", "CCC.NS"]).to_excel(xw, sheet_name="Alpha", index=False, header=False)
        pd.DataFrame(["Symbol", "ZZZ.NS"]).to_excel(xw, sheet_name="Beta", index=False, header=False)
        pd.DataFrame([[None]]).to_excel(xw, sheet_name="Empty", index=False, header=False)
    buf.seek(0)
    up = c.post("/api/import", data={"file": (buf, "mine.xlsx")}, content_type="multipart/form-data").get_json()
    assert up["needs_sheet"] and [s["name"] for s in up["sheets"]] == ["Alpha", "Beta"]   # empty sheet dropped
    assert len(eng.stock_list) == 50                                                      # unchanged so far
    assert c.get("/api/sources").get_json()["upload"]["token"] == up["token"]             # survives page reload

    assert c.post("/api/load", json={"source": up["token"], "sheet": "Alpha"}).get_json()["count"] == 3
    assert eng.stock_list == ["AAA.NS", "BBB.NS", "CCC.NS"]
    assert c.post("/api/load", json={"source": up["token"], "sheet": "Beta"}).get_json()["count"] == 1  # header skipped
    assert eng.stock_list == ["ZZZ.NS"]


def test_headerless_csv_upload_keeps_first_symbol(client):
    c, eng = client
    r = c.post("/api/import", data={"file": (io.BytesIO(b"ICICIBANK.NS\nINFY.NS\n"), "x.csv")},
               content_type="multipart/form-data").get_json()
    assert r["count"] == 2 and eng.stock_list[0] == "ICICIBANK.NS"


def test_progress_and_results_appear_while_scanning():
    gate = threading.Event()
    base = make_fetcher({"UP.NS": 2.0})

    def fetch(symbol, period, interval, **kw):
        if symbol == "GATE.NS":
            gate.wait(10)                      # hold the scan open mid-way
        return base(symbol, period, interval, **kw)

    eng = ScannerEngine(fetcher=fetch)
    eng.set_symbols(["UP.NS", "GATE.NS", "LAST.NS"])
    assert eng.start({"scan_5m": True, "scan_interval": 5})[0]
    try:
        # UP.NS is finished, the scan is blocked on GATE.NS -> the signal is already published
        assert wait_until(lambda: len(eng.tabs["5m"].all_results) == 1)
        p = eng.snapshot()["progress"]
        assert p["phase"] == "scanning" and p["symbol"] == "GATE.NS"
        assert (p["started"], p["total"], p["found"]) == (2, 3, 1)
        assert eng.snapshot()["scanning"]
    finally:
        gate.set()
    assert wait_until(lambda: eng.snapshot()["progress"]["phase"] == "waiting")
    assert eng.snapshot()["progress"]["wait_total"] == 5
    eng.stop()
    assert wait_until(lambda: not eng.scanning)
    assert eng.snapshot()["progress"]["phase"] == "idle"
