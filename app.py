"""
Intraday Breakout Scanner - Flask edition.

Run:  python app.py        then open  http://127.0.0.1:5000

Note: scan state lives in memory inside this process, so run a single
process (the default `python app.py`, or gunicorn with `--workers 1`).
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from scanner import (ACTION_TYPES, SYMBOL_FORMATS, TAB_DEFS, ScannerEngine, parse_filters,
                     results_to_csv)
from sources import BundledWorkbook, UploadStore, parse_csv, parse_workbook, sheet_summary

BASE_DIR = Path(__file__).resolve().parent
BUNDLED_PATH = Path(os.environ.get("SCANNER_DATA", BASE_DIR / "data" / "ScannerData.xlsx"))

RESULT_LIMIT = 1000  # max rows sent to the browser per request (newest first)


def create_app(engine: ScannerEngine | None = None, bundled_path: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload cap
    eng = engine or ScannerEngine()
    app.extensions["scanner"] = eng
    bundled = BundledWorkbook(bundled_path or BUNDLED_PATH)
    uploads = UploadStore()

    def bad(msg: str, code: int = 400):
        return jsonify(ok=False, error=msg), code

    def tab_or_400():
        key = request.values.get("tab") or (request.get_json(silent=True) or {}).get("tab") or "5m"
        return key if key in TAB_DEFS else None

    # ---------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return render_template("index.html", action_types=ACTION_TYPES, tabs=TAB_DEFS)

    # ------------------------------------------------------------------ API
    @app.get("/api/state")
    def state():
        return jsonify(eng.snapshot())

    def load_list(symbols: list[str], label: str):
        eng.set_symbols(symbols, label)
        note = " It applies from the next scan." if eng.snapshot()["scanning"] else ""
        return jsonify(ok=True, count=len(symbols), file_name=label, note=note)

    def upload_info():
        latest = uploads.latest()
        if not latest:
            return None
        token, item = latest
        return {"token": token, "file_name": item["file_name"], "sheets": sheet_summary(item["sheets"])}

    @app.get("/api/sources")
    def sources():
        sheets = bundled.sheets()
        return jsonify(ok=True,
                       bundled={"available": bool(sheets), "file_name": bundled.file_name,
                                "sheets": sheet_summary(sheets)},
                       upload=upload_info())

    @app.post("/api/import")
    def import_symbols():
        f = request.files.get("file")
        if not f or not f.filename:
            return bad("No file uploaded.")
        name = f.filename
        ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
        try:
            if ext == "csv":
                symbols = parse_csv(f.stream)
                if not symbols:
                    return bad("No symbols found in the first column of the file.")
                return load_list(symbols, name)
            if ext != "xlsx":
                return bad("Unsupported file type. Please upload a .csv or .xlsx file.")
            sheets = parse_workbook(f.stream)
        except Exception as e:  # noqa: BLE001
            return bad(f"Failed to import file: {e}")
        if not sheets:
            return bad("No symbols found in any sheet of the workbook.")
        if len(sheets) == 1:                       # one sheet: nothing to choose
            sheet, symbols = next(iter(sheets.items()))
            return load_list(symbols, name)
        token = uploads.add(name, sheets)          # several sheets: let the user pick one
        return jsonify(ok=True, needs_sheet=True, token=token, file_name=name,
                       sheets=sheet_summary(sheets))

    @app.post("/api/load")
    def load_sheet():
        body = request.get_json(silent=True) or {}
        source, sheet = body.get("source"), body.get("sheet")
        if source == "bundled":
            sheets, label = bundled.sheets(), bundled.file_name
        else:
            item = uploads.get(str(source or ""))
            if not item:
                return bad("That upload is no longer available. Please upload the file again.", 404)
            sheets, label = item["sheets"], item["file_name"]
        if sheet not in sheets:
            return bad(f"Sheet '{sheet}' was not found.", 404)
        return load_list(sheets[sheet], f"{label} \u203a {sheet}")

    @app.post("/api/scan/start")
    def scan_start():
        ok, msg = eng.start(request.get_json(silent=True) or {})
        return (jsonify(ok=True, message=msg) if ok else bad(msg, 409))

    @app.post("/api/scan/config")
    def scan_config():
        ok, msg = eng.update_config(request.get_json(silent=True) or {})
        return (jsonify(ok=True, message=msg) if ok else bad(msg))

    @app.post("/api/scan/stop")
    def scan_stop():
        eng.stop()
        return jsonify(ok=True)

    @app.post("/api/clear")
    def clear():
        key = tab_or_400()
        if not key:
            return bad("Unknown tab.")
        eng.clear(key)
        return jsonify(ok=True)

    @app.get("/api/results")
    def results():
        key = tab_or_400()
        if not key:
            return bad("Unknown tab.")
        try:
            filters = parse_filters(request.args)
        except ValueError as e:
            return bad(str(e))
        rows = eng.query(key, filters)
        rows_newest_first = rows[::-1]
        return jsonify(
            ok=True,
            tab=key,
            total_all=eng.snapshot()["tabs"][key]["total_results"],
            total_filtered=len(rows),
            above=sum(1 for r in rows if r["Band Status"] == "Above Band"),
            below=sum(1 for r in rows if r["Band Status"] == "Below Band"),
            limit=RESULT_LIMIT,
            rows=rows_newest_first[:RESULT_LIMIT],
        )

    @app.get("/api/scans")
    def scans():
        key = tab_or_400()
        if not key:
            return bad("Unknown tab.")
        return jsonify(ok=True, tab=key, scans=eng.scan_list(key))

    @app.get("/api/export")
    def export():
        key = tab_or_400()
        if not key:
            return bad("Unknown tab.")
        try:
            filters = parse_filters(request.args)
        except ValueError as e:
            return bad(str(e))
        symbol_format = request.args.get("symbol_format") or "ns"
        if symbol_format not in SYMBOL_FORMATS:
            return bad(f"Unknown symbol_format. Use one of: {', '.join(SYMBOL_FORMATS)}.")
        rows = eng.query(key, filters)
        if not rows:
            return bad("No (filtered) results in this tab to export.", 404)
        fname = f"breakout_{key}_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
        return Response(results_to_csv(rows, symbol_format), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        threaded=True,
    )
