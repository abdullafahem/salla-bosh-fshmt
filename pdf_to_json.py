#!/usr/bin/env python3
"""
Convert the faculty timetable PDF (ORARI-BACHELOR.pdf) into data/timetable.json
for the Salla Bosh web app.

    python pdf_to_json.py ORARI-BACHELOR.pdf
    python pdf_to_json.py ORARI-BACHELOR.pdf --report     # also print what was found per block

Needs: pip install pdfplumber   +   poppler's `pdftotext` (reads this PDF's text cleanly):
    Windows : https://github.com/oschwartz10612/poppler-windows/releases  (add bin/ to PATH)
    macOS   : brew install poppler
    Linux   : sudo apt install poppler-utils

How the PDF is read
-------------------
The PDF is a list of blocks, one per program / year / parallel. Each block has
  * a title  "ORARI PER VITIN AKADEMIK 2026-2027 / BACHELOR / INFERMIERI"
  * a line   "VITI I PARE / SIMESTRI I PARE / PARALELI I PARE / GR. 1,2,3,4"
  * its room "GODINA 1 KATI 2 AUDITOR 1"  (sometimes per week range:
             "JAVA I DERI JAVA VI GODINA 1 KATI 3 AUDITOR 1")
  * weeks    "JAVA E PARE" + a header with the real dates + one row per hour.
Every filled cell = that block's auditorium is in use at that date and hour.
"LAB 3" / "Lab 1 gr 2" in a cell is the lab session number, not a room.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "timetable.json"

# Holidays: the cell is dropped.
HOLIDAY_RE = re.compile(r"^\W*(pushim|fest[eë]|-+)\W*$", re.I)
# Lab sessions and practice (praktika) happen in labs / hospitals, so the group's
# auditorium is FREE at that time. The PDF itself confirms this: other programs are
# scheduled into an auditorium while its own group is at "LABORATOR".
LAB_RE = re.compile(r"\bLAB\b|\bLab\.?\s*\d|\bLab\s+gr|\blaborator", re.I)
PRACTICE_RE = re.compile(r"\bpraktik", re.I)

ORDINALS = [  # longest first, so "NJEMBEDHJETE" wins over "DHJETE"
    ("GJASHTEMBEDHJETE", 16), ("PESEMBEDHJETE", 15), ("KATERMBEDHJETE", 14),
    ("TREMBEDHJETE", 13), ("DYMBEDHJETE", 12), ("NJEMBEDHJETE", 11), ("DHJETE", 10),
    ("NENTE", 9), ("TETE", 8), ("SHTATE", 7), ("GJASHTE", 6), ("PESTE", 5),
    ("KATERT", 4), ("TRETE", 3), ("DYTE", 2), ("PARE", 1),
]
ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50}
DAY_IDX = {"HENE": 0, "MARTE": 1, "MERKURE": 2, "ENJTE": 3, "PREMTE": 4, "SHTUNE": 5}
YEARS = {"PARE": 1, "DYTE": 2, "TRETE": 3, "KATERT": 4}

# --------------------------------------------------------------------------- helpers
_FLAT = str.maketrans("ëËçÇéÉ", "eEcCeE")


def flat(s: str) -> str:
    """Remove Albanian accents WITHOUT changing string length (keeps column positions)."""
    return s.translate(_FLAT)


def roman(s: str) -> int:
    s = s.upper()
    total = 0
    for i, ch in enumerate(s):
        v = ROMAN.get(ch, 0)
        total += -v if i + 1 < len(s) and ROMAN.get(s[i + 1], 0) > v else v
    return total


def ordinal(s: str):
    s = flat(s).upper().replace(" ", "")
    for word, n in ORDINALS:
        if s.startswith(word):
            return n
    return None


TIME_RE = re.compile(r"(\d{1,2})\s*[.,:]\s*(\d{2})\s*-\s*(\d{1,2})\s*[.,:]\s*(\d{2})")
WEEK_RE = re.compile(r"\bJAVA\s+E\s+([A-Z]+)", re.I)
HEADER_DAY_RE = re.compile(r"\bE\s+(HENE|MARTE|MERKURE|ENJTE|PREMTE|SHTUNE)\b\s*([0-9][0-9\s/.]*)?", re.I)
ROOM_RE = re.compile(r"GODINA\s*(\d+)\s*KATI\s*(\d+)\s*AUDITOR[I]?\s*(\d+)", re.I)
RANGE_RE = re.compile(r"JAVA\s+([IVXL]+)\s+(?:DERI|DHE|-)\s+(?:JAVA\s+)?([IVXL]+)", re.I)
ONE_ROMAN_RE = re.compile(r"JAVA\s+([IVXL]+)\b(?!\s+(?:DERI|DHE))", re.I)
ONE_ORD_RE = re.compile(r"JAVA\s+E\s+([A-Z]+)", re.I)
TITLE_RE = re.compile(r"ORARI\s+PER\s+VITIN\s+AKADEMIK\s+(\d{4})\s*-\s*(\d{4})\s*/\s*BACHELOR\s*/\s*(.+)", re.I)
COURSE_TABLE_RE = re.compile(r"\bNR\b.*\bLENDET\b", re.I)
TYPE_ONLY_RE = re.compile(r"^(LEX|LEKS\w*|SEM(INAR)?|LAB|PRAKTIK\w*)\b[\s\dA-Za-z]{0,10}$", re.I)


def chunks(line: str, start_col: int = 0):
    """Split a layout line into text pieces separated by 2+ spaces -> [(col, text)]"""
    out = []
    for m in re.finditer(r"\S+(?: \S+)*", line):
        if m.end() > start_col:
            s = max(m.start(), start_col)
            txt = line[s:m.end()].strip()
            if txt:
                out.append((s, txt))
    return out


def class_type(t: str) -> str:
    if re.search(r"\bLEX\b|\bLEKS|\bL\s?\d+\b", t, re.I):
        return "Leksion"
    if re.search(r"\bSEM(?:INAR)?\b|\bSEM\s*\d|\bS\s?\d+\b", t, re.I):   # not "SEMUNDJE"
        return "Seminar"
    return "Mësim"


def classify(text: str):
    """-> (type, busy) or None to drop the cell.
    busy = the block's auditorium is used. A cell like 'Statistike G1 S3/Anatomi Lab 2'
    keeps the room busy because one group still has a seminar there."""
    t = flat(text).strip()
    if not t or HOLIDAY_RE.match(t):
        return None
    toks = t.split()
    if all(len(x) <= 2 and x.isalpha() for x in toks) and len(t.replace(" ", "")) <= 4:
        return None                       # pieces of letter-spaced "P R A K T I K E"
    parts = [p for p in re.split(r"\s*/\s*", t) if p.strip()]
    kinds = []
    for part in parts:
        tight = part.replace(" ", "").lower()       # "P R A K T I K E", "pra kti ke"
        if PRACTICE_RE.search(part) or "praktik" in tight:
            kinds.append("Praktikë")
        elif LAB_RE.search(part) or "laborator" in tight:
            kinds.append("Lab")
        else:
            kinds.append(class_type(part))
    typed = {k for k in kinds if k != "Mësim"}
    if len(typed) == 1:                 # "Foniatri / Teknikat Lab gr 1 / Lab gr 2" -> all Lab
        only = next(iter(typed))
        kinds = [only if k == "Mësim" else k for k in kinds]
    in_room = [k for k in kinds if k not in ("Lab", "Praktikë")]
    if in_room:
        return (in_room[0], 1)
    return ("Lab" if "Lab" in kinds else "Praktikë", 0)


# --------------------------------------------------------------------------- parsing
class Block:
    def __init__(self, program, year_from, year_to):
        self.program = program
        self.academic = (year_from, year_to)
        self.info = ""           # "Viti 1, Paraleli 1, Gr. 1, 2, 3, 4"
        self.rooms = []          # [(from_week, to_week_or_None, room_id)]
        self.weeks = {}          # week_no -> {"monday": date|None, "cells": [(day, start, end, text)]}
        self.start_line = 0      # position in the PDF text, used as a stable id ("p12y340")

    def room_for(self, week):
        found = None
        for a, b, rid in self.rooms:
            if a is None:
                found = found or rid
            elif b is None and week >= a:          # "from week N onward"
                found = rid
            elif b is not None and a <= week <= b:
                return rid
        return found


def parse_info(line: str) -> str:
    f = flat(line).upper()
    parts = []
    m = re.search(r"VITI\s+I\s+([A-Z]+)", f)
    if m and m.group(1) in YEARS:
        parts.append(f"Viti {YEARS[m.group(1)]}")
    m = re.search(r"PARALELI\s+I\s+([A-Z]+)", f)
    if m and ordinal(m.group(1)):
        parts.append(f"Paraleli {ordinal(m.group(1))}")
    m = re.search(r"GR\.?\s*([\d,\s.]+)", f)
    if m:
        gr = re.sub(r"[\s.]+", "", m.group(1)).strip(",").replace(",", ", ")
        if gr:
            parts.append(f"Gr. {gr}")
    return ", ".join(parts)


def parse_room_line(text: str, block: Block):
    f = flat(text).upper()
    m = ROOM_RE.search(f)
    if not m:
        return False
    rid = f"G{int(m[1])}-K{int(m[2])}-A{int(m[3])}"
    before = f[:m.start()]
    r = RANGE_RE.search(before)
    if r:
        block.rooms.append((roman(r[1]), roman(r[2]), rid))
    elif ONE_ORD_RE.search(before) and ordinal(ONE_ORD_RE.search(before)[1]):
        block.rooms.append((ordinal(ONE_ORD_RE.search(before)[1]), None, rid))  # from week N on
    elif ONE_ROMAN_RE.search(before):
        n = roman(ONE_ROMAN_RE.search(before)[1])
        block.rooms.append((n, n, rid))             # "JAVA XI GODINA..." = only week 11
    else:
        block.rooms.append((None, None, rid))
    return True


def parse_dates(text: str, academic):
    """'E HENE 5/10/2026 E MARTE 6/10/2026 ...' -> {day_idx: date|None}"""
    dates = {}
    for m in HEADER_DAY_RE.finditer(flat(text).upper()):
        nums = [int(x) for x in re.findall(r"\d+", m[2] or "")]
        dt = None
        if len(nums) >= 2:
            day, month = nums[0], nums[1]
            # the printed year has typos in the PDF (e.g. "19/01/2026"), so take it from the month
            year = academic[0] if month >= 8 else academic[1]
            try:
                dt = date(year, month, day)
            except ValueError:
                dt = None
        dates[DAY_IDX[m[1]]] = dt
    return dates


def week_monday(dates: dict):
    mondays = Counter(dt - timedelta(days=d) for d, dt in dates.items() if dt and dt.weekday() == d)
    return mondays.most_common(1)[0][0] if mondays else None


# ---- reading the PDF: words (poppler) + table cells (pdfplumber) -----------------
WORD_RE = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>')


def poppler_words(pdf: Path):
    """{page_index: [(x0, y0, x1, y1, text)]} using pdftotext -bbox"""
    exe = shutil.which("pdftotext")
    if not exe:
        sys.exit("pdftotext not found. Install poppler (see the top of this file) and try again.")
    res = subprocess.run([exe, "-bbox", "-enc", "UTF-8", str(pdf), "-"], capture_output=True)
    if res.returncode != 0:
        sys.exit(f"pdftotext failed: {res.stderr.decode(errors='replace')}")
    xml = res.stdout.decode("utf-8", errors="replace")
    pages = {}
    for i, chunk in enumerate(xml.split("<page ")[1:]):
        pages[i] = [(float(a), float(b), float(c), float(d), html.unescape(t))
                    for a, b, c, d, t in WORD_RE.findall(chunk) if t.strip()]
    return pages


def text_lines(words, tol=1.2):
    """group words into visual lines -> [(y0, y1, text, words)] sorted top to bottom"""
    lines = []
    for w in sorted(words, key=lambda w: (round(w[1], 1), w[0])):
        for ln in lines:
            if abs(ln["y"] - w[1]) <= tol:
                ln["w"].append(w)
                break
        else:
            lines.append({"y": w[1], "w": [w]})
    out = []
    for ln in sorted(lines, key=lambda l: l["y"]):
        ws = sorted(ln["w"], key=lambda w: w[0])
        out.append((min(w[1] for w in ws), max(w[3] for w in ws), " ".join(w[4] for w in ws), ws))
    return out


def page_cells(plumber_page):
    settings = {"vertical_strategy": "lines", "horizontal_strategy": "lines",
                "snap_tolerance": 1.5, "join_tolerance": 1.5, "intersection_tolerance": 1.5}
    cells = []
    for t in plumber_page.find_tables(settings):
        cells += [c for c in t.cells if c]
    return cells


def words_in(cell, words):
    x0, y0, x1, y1 = cell
    ins = [w for w in words if x0 - 0.3 <= (w[0] + w[2]) / 2 <= x1 + 0.3 and y0 - 0.3 <= (w[1] + w[3]) / 2 <= y1 + 0.3]
    ins.sort(key=lambda w: (round(w[1], 0), w[0]))
    out, prev = "", None
    for w in ins:
        if prev is not None:
            same_line = abs(w[1] - prev[1]) < 0.5 * (prev[3] - prev[1])
            gap = w[0] - prev[2]
            # letter-spaced text ("Pa ri met e") is drawn in pieces with tiny gaps: glue them
            out += "" if same_line and 0 <= gap < 0.14 * (prev[3] - prev[1]) else " "
        out += w[4]
        prev = w
    return re.sub(r"\s+", " ", out).strip()


def parse_pdf(pdf_path: Path, warn):
    import pdfplumber  # imported here so --help works without it

    words_by_page = poppler_words(pdf_path)
    blocks: list[Block] = []
    cur = None
    week = None            # {"no", "monday", "days": [(day, x0, x1)]}
    last_days = None

    with pdfplumber.open(str(pdf_path)) as pdf:
        for pi, page in enumerate(pdf.pages):
            words = words_by_page.get(pi, [])
            lines = text_lines(words)
            cells = page_cells(page)
            cell_text = {c: words_in(c, words) for c in cells}

            # 1) walk the text lines top to bottom: titles, rooms, week titles, week headers
            events = []    # (y, kind, payload)
            for li, (y0, y1, text, ws) in enumerate(lines):
                f = flat(text).upper()
                t = TITLE_RE.search(f)
                if t:
                    nxt = " ".join(flat(x[2]) for x in lines[li + 1:li + 3]).upper()
                    if "VITI" in nxt or "GODINA" in nxt or cur is None:
                        events.append((y0, "title", (t[3].strip().upper(), int(t[1]), int(t[2]))))
                    continue
                if COURSE_TABLE_RE.search(f):
                    events.append((y0, "course_table", None))
                    continue
                if re.search(r"\bVITI\b", f) and len(f) < 160:
                    events.append((y0, "info", text))
                if "GODINA" in f and "KATI" in f:
                    events.append((y0, "room", text))
                wm = WEEK_RE.search(f)
                if wm and ordinal(wm[1]) and f.strip().startswith("JAVA") and not TIME_RE.search(f):
                    events.append((y0, "week", ordinal(wm[1])))
                if len(HEADER_DAY_RE.findall(f)) >= 3:
                    events.append((y0, "header", (text, ws, y1)))

            # 2) apply events; collect week "bands" on this page
            bands = []     # (y_from, y_to, block, week)
            if cur is not None and week is not None:
                bands.append([0, None, cur, week])   # a week continuing from the previous page
            for y, kind, payload in events:
                if kind == "title":
                    cur = Block(*payload)
                    cur.start_line = f"p{pi + 1}y{int(y)}"
                    blocks.append(cur)
                    week = None
                elif kind == "course_table" and cur is not None and cur.weeks:
                    nb = Block(cur.program, *cur.academic)
                    nb.info = "(blloku pa titull në PDF)"
                    nb.start_line = f"p{pi + 1}y{int(y)}"
                    warn(f"page {pi + 1}: a timetable block without title/room after "
                         f"'{cur.program} {cur.info}'. Its lessons are skipped unless you add its room in pdf_overrides.json "
                         f"under the key \"{nb.start_line}\"")
                    blocks.append(nb)
                    cur, week = nb, None
                elif cur is None:
                    continue
                elif kind == "info" and not cur.info:
                    cur.info = parse_info(payload)
                elif kind == "room":
                    parse_room_line(payload, cur)
                elif kind == "week":
                    n = payload
                    if week is not None and n == week["no"] and week["monday"] is None and not week["seen_rows"]:
                        continue                      # same title printed twice in a row
                    last = max(cur.weeks) if cur.weeks else 0
                    if n <= last:
                        warn(f"page {pi + 1}: week title repeats in {cur.program} {cur.info}; used week {last + 1}")
                        n = last + 1
                    week = {"no": n, "monday": None, "days": last_days, "seen_rows": False}
                    cur.weeks.setdefault(n, {"monday": None, "cells": []})
                    if bands and bands[-1][1] is None:
                        bands[-1][1] = y
                    bands.append([y, None, cur, week])
                elif kind == "header" and week is not None:
                    text, ws, y1 = payload
                    if week.get("has_header"):     # dates without a "JAVA E ..." title -> next week
                        n = max(cur.weeks) + 1
                        week = {"no": n, "monday": None, "days": last_days, "seen_rows": False}
                        cur.weeks.setdefault(n, {"monday": None, "cells": []})
                        if bands and bands[-1][1] is None:
                            bands[-1][1] = y
                        bands.append([y, None, cur, week])
                    week["has_header"] = True
                    week["monday"] = week_monday(parse_dates(text, cur.academic))
                    cur.weeks[week["no"]]["monday"] = week["monday"]
                    days = header_columns(ws, cells, y, y1)
                    if days:
                        week["days"] = last_days = days
            if bands and bands[-1][1] is None:
                bands[-1][1] = page.height + 1

            # 3) put every cell into its week band, day columns and hour rows
            for y_from, y_to, blk, wk in bands:
                if not wk.get("days"):
                    continue
                first_left = min(x0 for _d, x0, _x1 in wk["days"])
                band_cells = [c for c in cells if y_from - 0.5 <= c[1] and c[3] <= y_to + 0.5]
                rows = []
                for c in band_cells:
                    if c[2] <= first_left + 1:
                        tm = TIME_RE.search(cell_text[c])
                        if tm:
                            rows.append((c[1], c[3], int(tm[1]) * 60 + int(tm[2]), int(tm[3]) * 60 + int(tm[4])))
                rows = sorted(set(rows))
                if not rows:
                    continue
                wk["seen_rows"] = True
                slots = {}
                for c in band_cells:
                    txt = cell_text[c]
                    if not txt or c[2] <= first_left + 1 or c[1] < rows[0][0] - 0.5:
                        continue
                    hit_days = [d for d, x0, x1 in wk["days"]
                                if x0 - 0.5 <= (c[0] + c[2]) / 2 <= x1 + 0.5 or c[0] - 0.5 <= (x0 + x1) / 2 <= c[2] + 0.5]
                    for ry0, ry1, s, e in rows:
                        overlap = min(c[3], ry1) - max(c[1], ry0)
                        if overlap > 0.4 * (ry1 - ry0):
                            for d in hit_days:
                                slots.setdefault((d, s, e), []).append((c[0], txt))
                cells_out = blk.weeks[wk["no"]]["cells"]
                for (d, s, e), parts in slots.items():
                    parts.sort()
                    cells_out.append((d, s, e, " ".join(p for _x, p in parts)))
    return blocks


def header_columns(header_words, cells, y0, y1):
    """Day columns [(day, x_left, x_right)] from the header words and the cell grid."""
    found = []
    for i, w in enumerate(header_words):
        name = flat(w[4]).upper()
        if name in DAY_IDX and i > 0 and flat(header_words[i - 1][4]).upper() == "E":
            found.append((DAY_IDX[name], header_words[i - 1][0]))
    if len(found) < 2:
        return None
    ymid = (y0 + y1) / 2
    lefts = []
    for d, x in found:
        # left edge of the header cell that contains this day name
        box = [c for c in cells if c[0] - 0.5 <= x <= c[2] + 0.5 and c[1] - 0.5 <= ymid <= c[3] + 0.5]
        left = min((c[0] for c in box), default=None)
        lefts.append((d, left, x))
    out = []
    for i, (d, left, x) in enumerate(lefts):
        if left is None:   # no grid line: halfway to the previous header
            left = (lefts[i - 1][2] + x) / 2 if i else x - 20
        nxt = lefts[i + 1] if i + 1 < len(lefts) else None
        if nxt:
            right = nxt[1] if nxt[1] is not None else (x + nxt[2]) / 2
        else:
            widths = [b - a for (_d1, a, _), (_d2, b, _) in zip(lefts, lefts[1:]) if a is not None and b is not None]
            right = left + (sorted(widths)[len(widths) // 2] if widths else 60)
        out.append((d, left, right))
    return out


# --------------------------------------------------------------------------- output
def canonical_calendar(blocks, warn):
    votes = {}
    for b in blocks:
        for n, wk in b.weeks.items():
            if wk["monday"]:
                votes.setdefault(n, Counter())[wk["monday"]] += 1
    cal = {n: c.most_common(1)[0][0] for n, c in votes.items()}
    for b in blocks:
        for n, wk in b.weeks.items():
            if n in cal and wk["monday"] != cal[n]:
                if wk["monday"]:
                    warn(f"{b.program} {b.info}: week {n} printed as {wk['monday']}, used {cal[n]} like the other programs")
                wk["monday"] = cal[n]
    return cal


def build(blocks, pdf_name, overrides, warn):
    canonical_calendar(blocks, warn)
    rooms, programs, lessons = {}, [], []
    skipped = 0
    for bi, b in enumerate(blocks):
        key = f"{b.start_line}"
        for item in overrides.get("rooms", {}).get(key, []):
            b.rooms.append((item.get("from"), item.get("to"), item["room"]))
        if key in overrides.get("info", {}):
            b.info = overrides["info"][key]
        name = b.program.title().replace("Teknik Laboratori", "Teknik Laboratori")
        programs.append({"name": name, "info": b.info})
        pidx = len(programs) - 1
        if not b.rooms:
            n = sum(len(w["cells"]) for w in b.weeks.values())
            if n:
                warn(f"{name} {b.info} ({b.start_line}): no room found, {n} lessons skipped")
            skipped += n
            continue
        for wno, wk in sorted(b.weeks.items()):
            rid = b.room_for(wno)
            if wk["monday"] is None:
                continue
            for day, s, e, txt in wk["cells"]:
                cls = classify(txt)
                if cls is None:
                    continue
                if rid is None:
                    skipped += 1
                    continue
                g, k, a = (int(x[1:]) for x in rid.split("-"))
                rooms.setdefault(rid, {"id": rid, "building": g, "floor": k, "number": a,
                                       "name": f"Auditori {a}"})
                d = (wk["monday"] + timedelta(days=day)).isoformat()
                lessons.append([d, s, e, rid, pidx, re.sub(r"\s+", " ", txt)[:140], cls[0], cls[1]])
    lessons.sort(key=lambda x: (x[0], x[1], x[3]))
    # same room, same time, two blocks -> keep both (shows a clash in the PDF), drop exact duplicates
    uniq, seen = [], set()
    for l in lessons:
        k = (l[0], l[1], l[3], l[4], l[5])
        if k not in seen:
            seen.add(k)
            uniq.append(l)
    room_list = sorted(rooms.values(), key=lambda r: (r["building"], r["floor"], r["number"]))
    dates = sorted({l[0] for l in uniq})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": pdf_name,
        "title": overrides.get("title", "Orari Bachelor"),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "rooms": room_list,
        "programs": programs,
        "lesson_fields": ["date", "start_min", "end_min", "room", "program", "text", "type", "busy"],
        "lessons": uniq,
        "skipped_lessons": skipped,
        "skipped_blocks": [f"{p['name']} {p['info']}" for p, b in zip(programs, blocks)
                           if not b.rooms and any(w["cells"] for w in b.weeks.values())],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=OUT)
    ap.add_argument("--report", action="store_true", help="print a summary per block")
    a = ap.parse_args()

    warnings = []
    warn = warnings.append
    ov_file = HERE / "pdf_overrides.json"
    overrides = json.loads(ov_file.read_text(encoding="utf-8")) if ov_file.exists() else {}

    blocks = parse_pdf(a.pdf, warn)
    data = build(blocks, a.pdf.name, overrides, warn)
    if not data["lessons"]:
        sys.exit("No lessons found - is this the right PDF?")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    if a.report:
        for b in blocks:
            n = sum(len(w["cells"]) for w in b.weeks.values())
            rooms = ", ".join(f"{r[2]}" + (f" (java {r[0]}-{r[1] or '...'})" if r[0] else "") for r in b.rooms) or "NO ROOM"
            print(f"  {b.start_line:>9}  {b.program:<18} {b.info:<38} weeks={len(b.weeks):>2} cells={n:>4}  {rooms}")
    for w in warnings:
        print(f"[warn] {w}")
    busy = sum(l[7] for l in data["lessons"])
    print(f"[saved] {a.out}: {len(data['rooms'])} auditoriums, {busy} busy hours "
          f"(+{len(data['lessons']) - busy} lab/praktikë hours elsewhere), "
          f"{data['first_date']} to {data['last_date']}, {data['skipped_lessons']} cells without a room")


if __name__ == "__main__":
    main()
