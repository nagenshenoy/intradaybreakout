#!/usr/bin/env bash
# Create a virtualenv on first run, install deps, start the app.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
pip install -q -r requirements.txt
python app.py
