# Salla Bosh (PDF version)

Free auditoriums for the faculty, from the official timetable PDF
(`pdf/ORARI-BACHELOR.pdf`, academic year 2026–2027, 5 Oct 2026 to 29 Jan 2027).

## Run it on your computer

```bash
pip install -r requirements.txt
# + poppler (for pdftotext):
#   Windows: https://github.com/oschwartz10612/poppler-windows/releases  (add the bin folder to PATH)
#   macOS:   brew install poppler
#   Linux:   sudo apt install poppler-utils
python app.py            # open http://localhost:5000
```

`data/timetable.json` is already generated from your PDF, so the app works
even before you install poppler. Poppler is only needed to convert a new PDF.

## New timetable next semester

Put the new PDF in `pdf/` and restart `python app.py` (it converts automatically),
or run it by hand to see warnings:

```bash
python pdf_to_json.py pdf/NEW.pdf --report
```

## How the PDF is read

* Each block (program / year / parallel) has one auditorium, e.g.
  `GODINA 1 KATI 2 AUDITOR 1`, sometimes per week range (`JAVA I DERI JAVA VI ...`).
* Every class cell in that block = its auditorium is busy at that date and hour.
  Cells merged over several hours count for every hour (read from the table's grid lines).
* **Lab sessions ("LAB 3", "LABORATOR ...") and praktikë** happen in labs / hospitals,
  so the auditorium counts as free then. The app shows "Grupi është në laborator".
  The PDF confirms this: other programs are scheduled into those auditoriums at the same time.
* PDF typos (wrong years, a repeated or missing week title) are corrected automatically,
  using the calendar shared by all programs.

## pdf_overrides.json

The PDF has one block on page 11 (Infermieri, after Year 3 Parallel 1) with no title
and no room, so its hours are not counted. If it is Year 3 Parallel 2 in
Godina 1 Kati 3 Auditori 2, set:

```json
"rooms": { "p11y82": [ { "room": "G1-K3-A2" } ] },
"info":  { "p11y82": "Viti 3, Paraleli 2, Gr. 5, 6, 7, 8" }
```

Room ids are `G<godina>-K<kati>-A<auditor>`. Use `"from"` / `"to"` (week numbers) for
rooms that change during the semester. Then run the converter again.

## Free hosting

**GitHub Pages (recommended).** Create a GitHub repo, upload this folder, then
*Settings → Pages → Source: GitHub Actions*. The workflow in `.github/workflows/pages.yml`
installs poppler, converts the newest PDF in `pdf/` and publishes the site.
To update, upload the new PDF to `pdf/` on GitHub; the site rebuilds by itself.

**Render.** Works with `render.yaml`, but Render has no poppler, so it serves the
`data/timetable.json` you committed. Convert new PDFs on your computer first.
The free plan sleeps when unused (slow first load).
