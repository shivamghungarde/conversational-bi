"""Minimal .xlsx reader (standard library only): first-row headers, one sheet.

Handles shared strings, inline strings, booleans, numbers and date-formatted
cells. Formulas are read from their cached values. Good enough for exported
reports; use a CSV export for anything exotic.
"""
from __future__ import annotations

import datetime as dt
import re
import zipfile
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
BUILTIN_DATE_FORMATS = set(range(14, 23)) | {45, 46, 47}
EPOCH = dt.datetime(1899, 12, 30)


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _date_styles(z: zipfile.ZipFile) -> set:
    """Indexes of cell styles that format numbers as dates."""
    try:
        root = ET.fromstring(z.read("xl/styles.xml"))
    except KeyError:
        return set()
    custom = {}
    for fmt in root.findall("m:numFmts/m:numFmt", NS):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", fmt.get("formatCode", "")).lower()
        custom[int(fmt.get("numFmtId"))] = bool(re.search(r"[dy]|mmm", code))
    styles = set()
    for i, xf in enumerate(root.findall("m:cellXfs/m:xf", NS)):
        fid = int(xf.get("numFmtId", 0))
        if fid in BUILTIN_DATE_FORMATS or custom.get(fid):
            styles.add(i)
    return styles


def sheet_names(path) -> list:
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        return [s.get("name") for s in wb.findall("m:sheets/m:sheet", NS)]


def read(path, sheet: str | None = None) -> tuple:
    """Return (headers, rows) from the named sheet, or the sheet with the most rows."""
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", NS)}
        sheets = []
        for s in wb.findall("m:sheets/m:sheet", NS):
            target = targets[s.get(f"{{{NS['r']}}}id")].lstrip("/")
            sheets.append((s.get("name"), target if target.startswith("xl/") else f"xl/{target}"))
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        date_styles = _date_styles(z)

        def parse(target: str) -> list:
            grid = []
            for row in ET.fromstring(z.read(target)).findall("m:sheetData/m:row", NS):
                values: dict = {}
                for c in row.findall("m:c", NS):
                    kind, style = c.get("t"), int(c.get("s", 0))
                    v = c.find("m:v", NS)
                    if kind == "inlineStr":
                        val = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
                    elif v is None or v.text is None:
                        continue
                    elif kind == "s":
                        val = shared[int(v.text)]
                    elif kind == "b":
                        val = v.text == "1"
                    elif kind in ("str", "e"):
                        val = v.text
                    else:
                        num = float(v.text)
                        if style in date_styles:
                            d = EPOCH + dt.timedelta(days=num)
                            val = d.date().isoformat() if d.time() == dt.time() else d.isoformat(sep=" ")
                        else:
                            val = int(num) if num.is_integer() else num
                    values[_col_index(c.get("r"))] = val
                if values:
                    width = max(values) + 1
                    grid.append([values.get(i) for i in range(width)])
            return grid

        if sheet:
            target = dict(sheets)[sheet]
            grid = parse(target)
        else:
            grid = max((parse(t) for _, t in sheets), key=len)
    if not grid:
        return [], []
    headers = [str(h).strip() if h is not None else f"column_{i + 1}" for i, h in enumerate(grid[0])]
    width = len(headers)
    rows = [(r + [None] * width)[:width] for r in grid[1:] if any(v not in (None, "") for v in r)]
    return headers, rows
