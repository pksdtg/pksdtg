"""
NHM Namkum Ranchi - Court Case Status Report (Writ) generator
--------------------------------------------------------------
1. Opens your case portal in a real Chrome/Chromium window
2. Logs in with your User ID and Password (CAPTCHA/OTP: you type it, script waits)
3. Reads the case list (all pages)
4. Keeps your Hindi "Remarks" and other manual columns from the previous report
5. Writes an Excel report in the same layout as "WRIT 30.10.2026" -> print to PDF

Your password is NEVER stored in this file. It is read from the environment
variables PORTAL_USER / PORTAL_PASS, or asked for when the script runs.

Run:   python fetch_case_report.py
       python fetch_case_report.py --previous "WRIT 29.10.2026.xlsx"
"""

import argparse
import getpass
import os
import re
import sys
from datetime import date, datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# =============================================================================
#  SETTINGS  -  edit only this block to match your portal
# =============================================================================
CONFIG = {
    # --- Portal pages --------------------------------------------------------
    "LOGIN_URL": "https://YOUR-PORTAL-ADDRESS/login",
    "CASE_LIST_URL": "https://YOUR-PORTAL-ADDRESS/cases",  # page with the case table

    # --- Login form (CSS selectors; right-click field > Inspect to find) ------
    "USER_FIELD": "input[name='username'], #username, #userid, input[type='text']",
    "PASS_FIELD": "input[type='password']",
    "LOGIN_BUTTON": "button[type='submit'], input[type='submit'], #btnLogin",
    # Something that only appears AFTER a successful login (logout link, etc.)
    "LOGGED_IN_MARK": "text=/logout|sign out|लॉग आउट/i",
    # True if the login page has a CAPTCHA or OTP you must type yourself
    "MANUAL_CAPTCHA": True,

    # --- Case table ----------------------------------------------------------
    "TABLE": "table",                     # the table that lists the cases
    "NEXT_PAGE": "a:has-text('Next'), button:has-text('Next'), li.next a",
    "MAX_PAGES": 50,
    # Optional: choose a filter on the page before reading (e.g. Case Category = Writ)
    # list of ("select" | "click" | "fill", selector, value)
    "BEFORE_READ_ACTIONS": [],

    # Portal column heading  ->  report column.   Matching ignores case/spaces.
    # Add every spelling your portal uses; unmatched portal columns are ignored.
    "COLUMN_MAP": {
        "dir sl no": "Dir Sl No", "diary no": "Dir Sl No", "entry no": "Dir Sl No",
        "case type": "CASE TYPE", "type": "CASE TYPE",
        "case no": "CASE NO", "case number": "CASE NO", "number": "CASE NO",
        "year": "YEAR", "case year": "YEAR",
        "petitioner": "Petitioner's Name", "petitioner name": "Petitioner's Name",
        "petitioner's name": "Petitioner's Name",
        "govt advocate": "Govt. Advocate", "government advocate": "Govt. Advocate",
        "advocate": "Govt. Advocate", "gp/sc": "Govt. Advocate",
        "subject": "Subject/Status", "subject/status": "Subject/Status", "matter": "Subject/Status",
        "section": "Section",
        "respondents": "Case related", "respondent": "Case related", "case related": "Case related",
        "district": "District",
        "oath no": "Oath No", "affidavit no": "Oath No",
        "date": "Date", "affidavit date": "Date", "next date": "Date", "hearing date": "Date",
        "status": "Present Status", "present status": "Present Status", "case status": "Present Status",
        "remarks": "Remarks", "brief": "Remarks", "brief facts": "Remarks",
    },
    # If the portal shows "WPS/1264/2025" in one column, map that column to
    # "CASE FULL" and it will be split into CASE TYPE / CASE NO / YEAR.
    # e.g.  "case no": "CASE FULL"

    # Keep only writ cases (CASE TYPE starting with any of these). [] = keep all
    "CASE_TYPE_FILTER": ["WP", "W.P"],
    "DEFAULT_SECTION": "NHM",

    # --- Report --------------------------------------------------------------
    "TITLE": "National Health Mission, Namkum Ranchi, Court Case status as on {date} (Writ)",
    "OUTPUT_FILE": "WRIT {date}.xlsx",
    "HEADLESS": False,   # keep False so you can see the browser / type CAPTCHA
}
# =============================================================================

REPORT_COLUMNS = [
    # (heading, width)
    ("Sl N0", 4), ("Dir Sl No", 6), ("CASE OF TYPE", 6), ("CASE NO", 6),
    ("YEAR", 5), ("Petitioner's Name", 11), ("Govt. Advocate", 6),
    ("Subject/ Status", 9), ("Section", 5), ("Case related", 16),
    ("District", 9), ("Oath No", 6), ("Date", 8), ("Present Status", 6),
    ("Remarks", 95),
]
# internal keys in the same order as REPORT_COLUMNS
KEYS = ["Sl No", "Dir Sl No", "CASE TYPE", "CASE NO", "YEAR", "Petitioner's Name",
        "Govt. Advocate", "Subject/Status", "Section", "Case related", "District",
        "Oath No", "Date", "Present Status", "Remarks"]


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def norm_head(s):
    return re.sub(r"[\s.:_-]+", " ", norm(s).lower()).strip()


def case_key(r):
    t = re.sub(r"[^A-Z]", "", str(r.get("CASE TYPE", "")).upper())
    n = re.sub(r"\D", "", str(r.get("CASE NO", "")))
    y = re.sub(r"\D", "", str(r.get("YEAR", "")))
    if not n:
        # no case number: fall back to the petitioner so such rows don't all
        # collapse into one ('', '', '') key and get dropped as duplicates
        return (t, "", y, norm(r.get("Petitioner's Name", "")).lower())
    return (t, n.lstrip("0"), y)


def cell_text(v):
    """Excel cell value -> text; dates come back as DD.MM.YYYY, not 2026-10-30 00:00:00."""
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# ----------------------------------------------------------------------------- portal
def login(page, user, pwd):
    c = CONFIG
    print("Opening login page ...")
    page.goto(c["LOGIN_URL"], wait_until="domcontentloaded")
    page.locator(c["USER_FIELD"]).first.fill(user)
    page.locator(c["PASS_FIELD"]).first.fill(pwd)
    if c["MANUAL_CAPTCHA"]:
        print("\n>>> Type the CAPTCHA / OTP in the browser window and click Login.")
        print(">>> The script continues by itself once you are logged in (5 min max).\n")
    else:
        page.locator(c["LOGIN_BUTTON"]).first.click()
    try:
        page.locator(c["LOGGED_IN_MARK"]).first.wait_for(timeout=300_000)
    except PWTimeout:
        sys.exit("Login did not complete. Check User ID / Password and LOGGED_IN_MARK.")
    print("Logged in.")


def read_table(page):
    """Return list of dicts {report key: value} from the current page's table."""
    table = page.locator(CONFIG["TABLE"]).first
    table.wait_for(timeout=60_000)
    data = table.evaluate("""t => {
        const rows = [...t.querySelectorAll('tr')];
        return rows.map(r => [...r.querySelectorAll('th,td')].map(c => c.innerText));
    }""")
    if not data:
        return []
    # header = first row that contains a known heading
    cmap = {norm_head(k): v for k, v in CONFIG["COLUMN_MAP"].items()}
    hdr_i = next((i for i, row in enumerate(data)
                  if sum(norm_head(x) in cmap for x in row) >= 2), 0)
    headers = [cmap.get(norm_head(h)) for h in data[hdr_i]]
    out = []
    for row in data[hdr_i + 1:]:
        if len(row) < 2 or not any(norm(x) for x in row):
            continue
        rec = {}
        for h, val in zip(headers, row):
            if h and norm(val) and h not in rec:
                rec[h] = val.strip()
        if "CASE FULL" in rec:
            m = re.match(r"\s*([A-Za-z.()\s]+?)\s*[/\-\s]\s*(\d+)\s*[/\-\s]\s*(\d{4})", rec.pop("CASE FULL"))
            if m:
                rec.setdefault("CASE TYPE", m.group(1).strip())
                rec.setdefault("CASE NO", m.group(2))
                rec.setdefault("YEAR", m.group(3))
        if rec.get("CASE NO") or rec.get("Petitioner's Name"):
            out.append(rec)
    return out


def fetch_cases(page):
    c = CONFIG
    if c["CASE_LIST_URL"]:
        page.goto(c["CASE_LIST_URL"], wait_until="domcontentloaded")
    for kind, sel, val in c["BEFORE_READ_ACTIONS"]:
        loc = page.locator(sel).first
        {"select": lambda: loc.select_option(label=val),
         "fill": lambda: loc.fill(val),
         "click": lambda: loc.click()}[kind]()
        page.wait_for_load_state("networkidle")
    cases, seen = [], set()
    for p in range(1, c["MAX_PAGES"] + 1):
        page.wait_for_load_state("networkidle")
        rows = read_table(page)
        new = [r for r in rows if case_key(r) not in seen]
        for r in new:
            seen.add(case_key(r))
        cases += new
        print(f"  page {p}: {len(rows)} rows ({len(new)} new)")
        nxt = page.locator(c["NEXT_PAGE"])
        if not new or nxt.count() == 0 or not nxt.first.is_visible() or not nxt.first.is_enabled():
            break
        cls = (nxt.first.get_attribute("class") or "") + (nxt.first.get_attribute("aria-disabled") or "")
        if "disabled" in cls or "true" in cls:
            break
        nxt.first.click()
    return cases


# ----------------------------------------------------------------------------- merge
def load_previous(path):
    """Read an earlier report (.xlsx in this layout) -> {case_key: row dict}."""
    if not path:
        return {}
    if not os.path.exists(path):
        print(f"WARNING: previous report not found: {path}  (Remarks will be empty)")
        return {}
    ws = load_workbook(path, data_only=True).active
    prev = {}
    for row in ws.iter_rows(min_row=3, values_only=True):
        if not row or row[3] in (None, "") and row[5] in (None, ""):
            continue
        rec = {k: cell_text(v) for k, v in zip(KEYS, row)}
        prev[case_key(rec)] = rec
    print(f"Previous report: {len(prev)} cases loaded from {path}")
    return prev


def merge(cases, prev):
    flt = [f.upper() for f in CONFIG["CASE_TYPE_FILTER"]]
    final, used = [], set()
    for r in cases:
        if flt and not str(r.get("CASE TYPE", "")).upper().replace(" ", "").startswith(tuple(flt)):
            continue
        old = prev.get(case_key(r), {})
        for k in KEYS:
            if old.get(k) and (not norm(r.get(k)) or k == "Remarks"):
                r[k] = old[k]          # fill blanks; always keep your own Hindi remarks
        r.setdefault("Section", CONFIG["DEFAULT_SECTION"])
        used.add(case_key(r))
        final.append(r)
    # cases in old report but not found on portal - keep them, flagged
    for k, old in prev.items():
        if k not in used:
            old = dict(old)
            old["Present Status"] = old.get("Present Status") or ""
            old["_missing"] = True
            final.append(old)
    for i, r in enumerate(final, 1):
        r["Sl No"] = i
    return final


# ----------------------------------------------------------------------------- excel
def write_report(rows, out_path, as_on):
    wb = Workbook()
    ws = wb.active
    ws.title = "WRIT"
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hindi = "Nirmala UI"   # Windows font that renders Hindi correctly

    n = len(REPORT_COLUMNS)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
    ws.cell(1, 1, CONFIG["TITLE"].format(date=as_on)).font = Font(name="Calibri", size=16, bold=True)
    ws.cell(1, 1).alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28

    for j, (head, width) in enumerate(REPORT_COLUMNS, 1):
        c = ws.cell(2, j, head)
        c.font = Font(name="Calibri", size=7, bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border
        ws.column_dimensions[get_column_letter(j)].width = width
    ws.row_dimensions[2].height = 30

    for i, r in enumerate(rows, 3):
        for j, k in enumerate(KEYS, 1):
            v = r.get(k, "")
            if k in ("Sl No", "Dir Sl No", "CASE NO", "YEAR", "Oath No") and re.fullmatch(r"\d+", str(v)):
                v = int(v)
            c = ws.cell(i, j, v)
            c.border = border
            c.font = Font(name=hindi if k == "Remarks" else "Calibri", size=7,
                          color="C00000" if r.get("_missing") else None)
            c.alignment = Alignment(vertical="center", wrap_text=True,
                                    horizontal="justify" if k == "Remarks" else "center")
        # row height from remarks length (~180 chars per line at this width/size)
        lines = max(3, len(str(r.get("Remarks", ""))) // 170 + 1,
                    str(r.get("Case related", "")).count("\n") + 1)
        ws.row_dimensions[i].height = min(409, lines * 10.5)

    # print setup: landscape, fit all columns on one page width, repeat heading rows
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:2"
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = ws.page_margins.right = 0.25
    ws.page_margins.top = ws.page_margins.bottom = 0.4
    ws.freeze_panes = "A3"
    wb.save(out_path)


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Fetch writ cases from portal and build the NHM report")
    ap.add_argument("--previous", help="last report .xlsx (keeps your Remarks etc.)")
    ap.add_argument("--date", help="'as on' date, DD.MM.YYYY (default: today)")
    ap.add_argument("--out", help="output .xlsx file name")
    a = ap.parse_args()

    as_on = a.date or date.today().strftime("%d.%m.%Y")
    out = a.out or CONFIG["OUTPUT_FILE"].format(date=as_on)

    user = os.environ.get("PORTAL_USER") or input("Portal User ID: ").strip()
    pwd = os.environ.get("PORTAL_PASS") or getpass.getpass("Portal Password (hidden): ")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=CONFIG["HEADLESS"], channel="chrome")
        except Exception:
            browser = p.chromium.launch(headless=CONFIG["HEADLESS"])
        page = browser.new_page()
        login(page, user, pwd)
        print("Reading cases ...")
        cases = fetch_cases(page)
        browser.close()

    print(f"Portal returned {len(cases)} cases.")
    rows = merge(cases, load_previous(a.previous))
    if not rows:
        sys.exit("No writ cases found. Check TABLE / COLUMN_MAP / CASE_TYPE_FILTER in SETTINGS.")
    write_report(rows, out, as_on)
    missing = sum(1 for r in rows if r.get("_missing"))
    print(f"\nDone: {out}  ({len(rows)} cases)")
    if missing:
        print(f"  {missing} case(s) from the previous report were not on the portal - shown in red.")
    print("Open it in Excel > File > Print > 'Microsoft Print to PDF' to get the PDF.")


if __name__ == "__main__":
    main()
