"""
Salla Bosh (PDF version) - web server.

    python app.py         -> http://localhost:5000

On startup, if a PDF in pdf/ is newer than data/timetable.json, it is converted
automatically. To update the timetable, replace the PDF in pdf/ and restart.
"""
import os
import subprocess
import sys
from pathlib import Path

from flask import Flask, abort, send_from_directory

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "timetable.json"
PDF_DIR = HERE / "pdf"

app = Flask(__name__, static_folder="static")


def convert_if_needed():
    pdfs = sorted(PDF_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
    if not pdfs:
        return
    newest = pdfs[-1]
    if DATA.exists() and DATA.stat().st_mtime >= newest.stat().st_mtime:
        return
    print(f"[convert] {newest.name} -> {DATA}")
    res = subprocess.run([sys.executable, str(HERE / "pdf_to_json.py"), str(newest)])
    if res.returncode != 0:
        print("[convert] failed - the old data (if any) is still used")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/data.json")
def data():
    if not DATA.exists():
        abort(404)
    return send_from_directory(DATA.parent, DATA.name, max_age=600)


convert_if_needed()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
