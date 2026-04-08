"""
xero_fill_payslips.py
---------------------
Reads last week's payroll data from GHL and fills each employee's payslip
(hours, tips, commission) in every draft pay run in Xero.

Run with:  python agent/xero_fill_payslips.py

Requires:
  data/firefox_profile/   (from xero_web_login.py)
  data/xero_token.json    (from xero_auth.py)
  GHL_API_KEY + GHL_LOCATION_ID in environment
  XERO_CLIENT_ID + XERO_CLIENT_SECRET in environment
"""

import asyncio
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

DATA_DIR      = Path(__file__).parent.parent / "data"
PROFILE_DIR   = DATA_DIR / "firefox_profile"
TOKEN_FILE    = DATA_DIR / "xero_token.json"
CLIENT_ID     = os.environ["XERO_CLIENT_ID"]
CLIENT_SECRET = os.environ["XERO_CLIENT_SECRET"]
GHL_API_KEY     = os.environ["GHL_API_KEY"]
GHL_LOCATION_ID = os.environ["GHL_LOCATION_ID"]
GHL_BASE        = "https://services.leadconnectorhq.com"
GHL_HEADERS     = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Version":       "2021-07-28",
    "Accept":        "application/json",
    "Content-Type":  "application/json",
}

# ── Org config ────────────────────────────────────────────────────────────────
# Xero API tenant name → {display name, CID}
ORG_CONFIG = {
    "DIAMOND BARBERS CAIRNS PTY LTD": {"display": "Diamond Barbers Cairns", "cid": "!BJv4H"},
    "Diamond Barbers Pty Ltd":         {"display": "Diamond Barbers Darwin", "cid": "!79ZCm"},
    "D.B. Parap Pty Ltd":              {"display": "Diamond Barbers Parap",  "cid": "!Mb!v8"},
}

# GHL xero_org value → Xero API tenant name
GHL_ORG_TO_XERO = {
    "Diamond Barbers Darwin": "Diamond Barbers Pty Ltd",
    "Diamond Barbers Parap":  "D.B. Parap Pty Ltd",
    "Diamond Barbers Cairns": "DIAMOND BARBERS CAIRNS PTY LTD",
}

SKIP_ORGS = {"DB WULGURU PTY LTD"}

EXCLUDED_EMPLOYEES = {
    "andrew mcdevitt",
    "nicole diamantis",
}

# Xero name → GHL name overrides for known mismatches
XERO_TO_GHL = {
    "anthony  crispo":      "anthony crispo",
    "jairo espinosa mejia": "jairo espinosa",
    "nikolaos diamantis":   "nico diamantis",
    "vincenzo vanzanella":  "vince vincenzo",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def norm(name):
    return " ".join(name.lower().split())


def resolve_ghl_name(xero_name):
    n = norm(xero_name)
    return XERO_TO_GHL.get(n, n)


def last_week_dates():
    DARWIN_TZ   = timezone(timedelta(hours=9, minutes=30))
    today       = datetime.now(DARWIN_TZ).date()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


# ── GHL data loading ──────────────────────────────────────────────────────────

def load_from_ghl():
    """
    Load the most recent week's payroll data from GHL.
    Returns dict: normalised_name -> employee data dict.
    """
    print("Loading payroll data from GHL...")
    all_records = []
    page = 1
    while True:
        r = requests.post(
            f"{GHL_BASE}/objects/custom_objects.payroll/records/search",
            headers=GHL_HEADERS,
            json={"locationId": GHL_LOCATION_ID, "page": page, "pageLimit": 100},
        )
        if r.status_code not in (200, 201):
            print(f"  WARNING: GHL search page {page} returned {r.status_code}")
            break
        records = r.json().get("records", [])
        all_records.extend(records)
        if len(records) < 100:
            break
        page += 1

    if not all_records:
        print("  No GHL records found.")
        return {}

    # Most recent week
    weeks = sorted(set(
        rec.get("properties", {}).get("week_start", "")
        for rec in all_records
        if rec.get("properties", {}).get("week_start")
    ), reverse=True)

    if not weeks:
        return {}

    latest_week = weeks[0]
    print(f"  Using week: {latest_week}")

    week_records = [
        rec for rec in all_records
        if rec.get("properties", {}).get("week_start") == latest_week
    ]

    data = {}
    for rec in week_records:
        p    = rec.get("properties", {})
        name = (p.get("employee_name") or "").strip()
        if not name:
            continue
        weekday_hrs = (
            float(p.get("monday_hours",    0) or 0) +
            float(p.get("tuesday_hours",   0) or 0) +
            float(p.get("wednesday_hours", 0) or 0) +
            float(p.get("thursday_hours",  0) or 0) +
            float(p.get("friday_hours",    0) or 0)
        )
        data[norm(name)] = {
            "ghl_name":     name,
            "xero_org":     p.get("xero_org", ""),
            "weekday_hrs":  round(weekday_hrs, 2),
            "saturday_hrs": round(float(p.get("saturday_hours",       0) or 0), 2),
            "sunday_hrs":   round(float(p.get("sunday_hours",         0) or 0), 2),
            "ph_hrs":       round(float(p.get("public_holiday_hours", 0) or 0), 2),
            "tips":         round(float(p.get("tips",        0) or 0), 2),
            "commission":   round(float(p.get("commissions", 0) or 0), 2),
        }

    print(f"  Loaded {len(data)} employees from GHL.")
    return data


# ── Xero API ──────────────────────────────────────────────────────────────────

def refresh_token(token_data):
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    data  = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": token_data["refresh_token"],
    }).encode()
    req = urllib.request.Request(
        "https://identity.xero.com/connect/token", data=data,
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type":  "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as r:
        new = json.loads(r.read())
    new["tenants"] = token_data.get("tenants", [])
    TOKEN_FILE.write_text(json.dumps(new, indent=2))
    return new


def xero_get(path, tenant_id, access_token):
    req = urllib.request.Request(
        f"https://api.xero.com{path}",
        headers={"Authorization": f"Bearer {access_token}",
                 "Xero-Tenant-Id": tenant_id, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def get_org_employees(tenant_id, access_token, ghl_data, xero_org_key):
    """
    Returns list of dicts with Xero employee info + GHL data,
    filtered to only employees belonging to xero_org_key (GHL org label).
    """
    emp_data  = xero_get("/payroll.xro/1.0/Employees", tenant_id, access_token)
    employees = emp_data.get("Employees", [])

    result = []
    for e in employees:
        full_name = f"{e.get('FirstName','')} {e.get('LastName','')}".strip()
        if norm(full_name) in EXCLUDED_EMPLOYEES:
            continue

        ghl_key = resolve_ghl_name(full_name)
        emp = ghl_data.get(ghl_key)

        # Fallback: first-name match
        if not emp:
            first = ghl_key.split()[0]
            emp = next((v for k, v in ghl_data.items() if k.split()[0] == first), None)

        if not emp:
            continue

        # Only include if employee belongs to this org
        if emp.get("xero_org") != xero_org_key:
            continue

        if emp.get("weekday_hrs", 0) + emp.get("saturday_hrs", 0) + emp.get("sunday_hrs", 0) == 0:
            continue

        result.append({
            "xero_name":    full_name,
            "first_name":   e.get("FirstName", ""),
            "last_name":    e.get("LastName", ""),
            "weekday_hrs":  emp["weekday_hrs"],
            "saturday_hrs": emp["saturday_hrs"],
            "sunday_hrs":   emp["sunday_hrs"],
            "tips":         emp["tips"],
            "commission":   emp["commission"],
        })
    return result


# ── Playwright payslip filling ────────────────────────────────────────────────

async def clear_and_type(page, locator, value):
    await locator.click(timeout=5000)
    await page.keyboard.press("Control+A")
    await locator.type(str(value), delay=40)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(500)


async def fill_payslip_page(page, emp):
    name = emp["xero_name"]
    print(f"      Filling {name}: wk={emp['weekday_hrs']}h  sat={emp['saturday_hrs']}h  "
          f"sun={emp['sunday_hrs']}h  tips=${emp['tips']}  comm=${emp['commission']}")

    try:
        await page.wait_for_selector("input", state="visible", timeout=15000)
    except PWTimeout:
        print(f"        WARNING: no inputs visible after 15s — skipping {name}")
        return
    await page.wait_for_timeout(1500)

    async def set_hours_row(label, value):
        if value <= 0:
            return
        try:
            row = page.locator("tr").filter(has_text=label).first
            if await row.count() == 0:
                print(f"        WARNING: no row for '{label}'")
                return
            inp = row.locator("input").first
            await inp.wait_for(state="visible", timeout=8000)
            await clear_and_type(page, inp, value)
        except PWTimeout:
            print(f"        WARNING: no row for '{label}'")

    async def set_fixed_amount_row(label, amount):
        if amount <= 0:
            return
        try:
            rows = page.locator("tr").filter(has_text=label)
            if await rows.count() == 0:
                add_btn = page.get_by_text("+ Add Earnings Line")
                await add_btn.click(timeout=5000)
                await page.wait_for_timeout(700)
                select_el = page.locator("select").last
                await select_el.select_option(label=label)
                await page.wait_for_timeout(700)

            rows = page.locator("tr").filter(has_text=label)
            row  = rows.first
            inp  = row.locator("input").last
            await inp.wait_for(state="visible", timeout=5000)
            await clear_and_type(page, inp, amount)
        except PWTimeout:
            print(f"        WARNING: could not set '{label}'")

    await set_hours_row("MONDAY-FRIDAY", emp["weekday_hrs"])
    await set_hours_row("SATURDAY",      emp["saturday_hrs"])
    await set_hours_row("SUNDAY",        emp["sunday_hrs"])
    await set_fixed_amount_row("Tips",       emp["tips"])
    await set_fixed_amount_row("Commission", emp["commission"])

    try:
        save_btn = page.get_by_role("button", name=re.compile(r"Save", re.IGNORECASE))
        await save_btn.first.click(timeout=5000)
        await page.wait_for_timeout(2000)
        error = page.locator("text=There was an error")
        if await error.count() > 0:
            print(f"        ERROR: Xero reported an error saving. Check manually.")
        else:
            print(f"        Saved OK")
    except PWTimeout:
        print(f"        WARNING: Save button not found")


async def process_org_web(page, display_name, cid, employees, date_from, date_to):
    print(f"\n  === {display_name} (CID={cid}) ===")

    await page.goto(f"https://payroll.xero.com/PayRun/PayRun?CID={cid}",
                    wait_until="load", timeout=30000)
    await page.wait_for_timeout(2000)
    print(f"    Payroll URL: {page.url}")

    date_patterns = [
        date_to.strftime("%d %b %Y"),
        f"{date_to.day} {date_to.strftime('%b %Y')}",
    ]

    pay_run_url = None
    for pattern in date_patterns:
        try:
            link = page.get_by_text(pattern, exact=False).first
            if await link.count() > 0:
                print(f"    Found pay run: '{pattern}'")
                await link.click(timeout=8000)
                await page.wait_for_load_state("load", timeout=30000)
                await page.wait_for_timeout(1500)
                pay_run_url = page.url
                break
        except Exception:
            pass

    if not pay_run_url:
        print(f"    ERROR: Pay run for {date_from.strftime('%Y-%m-%d')} not found.")
        print(f"    The pay run may need to be created first — run xero_create_payrun.py")
        return

    print(f"    Pay run page: {pay_run_url}")

    for emp in employees:
        first = emp["first_name"]
        last  = emp["last_name"]
        full  = emp["xero_name"]

        print(f"      Processing: {full}")

        await page.goto(pay_run_url, wait_until="load", timeout=20000)
        await page.wait_for_timeout(1500)

        row_count = await page.locator("tr").filter(has_text=first).filter(has_text=last).count()
        if row_count == 0:
            row_count = await page.locator("tr").filter(has_text=first).count()
        if row_count == 0:
            print(f"        SKIP: {full} not found on pay run page")
            continue

        payslip_url = await page.evaluate("""([firstName, lastName]) => {
            const rows = document.querySelectorAll('tr');
            for (const row of rows) {
                const text = row.textContent || '';
                if (text.includes(firstName) && text.includes(lastName)) {
                    const links = row.querySelectorAll('a');
                    for (const a of links) {
                        if (a.href && a.href.includes('PaySlip')) return a.href;
                    }
                }
            }
            return null;
        }""", [first, last])

        if not payslip_url:
            print(f"        Activating...")
            activated = await page.evaluate("""([firstName, lastName]) => {
                const rows = document.querySelectorAll('tr');
                for (const row of rows) {
                    const text = row.textContent || '';
                    if (text.includes(firstName) && text.includes(lastName)) {
                        const radio = row.querySelector('input[type="radio"]');
                        if (radio) { radio.click(); return 'radio'; }
                        const inp = row.querySelector('input');
                        if (inp) { inp.click(); return 'input'; }
                        const cells = row.querySelectorAll('td');
                        if (cells.length > 0) {
                            cells[cells.length - 1].click();
                            return 'td';
                        }
                    }
                }
                return null;
            }""", [first, last])
            print(f"        Activation result: {activated}")
            if not activated:
                print(f"        Could not activate {full}")
                continue

            await page.wait_for_timeout(2500)

            if "PaySlip" in page.url or "payslip" in page.url.lower():
                await fill_payslip_page(page, emp)
                continue

            payslip_url = await page.evaluate("""([firstName, lastName]) => {
                const rows = document.querySelectorAll('tr');
                for (const row of rows) {
                    const text = row.textContent || '';
                    if (text.includes(firstName) && text.includes(lastName)) {
                        const links = row.querySelectorAll('a');
                        for (const a of links) {
                            if (a.href && a.href.includes('PaySlip')) return a.href;
                        }
                    }
                }
                return null;
            }""", [first, last])

        if not payslip_url:
            print(f"        No payslip link found for {full} — skipping")
            continue

        print(f"        Navigating to payslip: {payslip_url}")
        await page.goto(payslip_url, wait_until="load", timeout=20000)
        await page.wait_for_timeout(1500)

        if "PaySlip" in page.url or "payslip" in page.url.lower():
            await fill_payslip_page(page, emp)
        else:
            print(f"        ERROR: ended up at {page.url} — skipping")

    print(f"    Done with {display_name}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not PROFILE_DIR.exists():
        print("ERROR: No Firefox profile found.")
        print("Run first:  python agent/xero_web_login.py")
        return

    # Load all payroll data from GHL
    ghl_data = load_from_ghl()
    if not ghl_data:
        print("No GHL data found — aborting.")
        return

    print("Refreshing Xero API token...")
    token        = json.loads(TOKEN_FILE.read_text())
    token        = refresh_token(token)
    access_token = token["access_token"]
    tenants      = token.get("tenants", [])

    date_from, date_to = last_week_dates()
    print(f"Pay period: {date_from} to {date_to}")

    # Build per-org employee data
    org_employee_data = {}
    for tenant in tenants:
        api_name = tenant["name"]
        if api_name in SKIP_ORGS:
            continue
        cfg = ORG_CONFIG.get(api_name)
        if not cfg:
            continue
        # Find the GHL org label that maps to this Xero tenant
        ghl_org_label = next(
            (k for k, v in GHL_ORG_TO_XERO.items() if v == api_name), None
        )
        if not ghl_org_label:
            continue
        try:
            employees = get_org_employees(tenant["id"], access_token, ghl_data, ghl_org_label)
            org_employee_data[api_name] = {
                "display": cfg["display"], "cid": cfg["cid"], "employees": employees
            }
            print(f"  {cfg['display']}: {len(employees)} employees to fill")
        except Exception as e:
            print(f"  ERROR loading employees for {api_name}: {e}")

    if not org_employee_data:
        print("No orgs to process.")
        return

    headless = os.environ.get("CI", "false").lower() == "true"

    async with async_playwright() as p:
        context = await p.firefox.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1400, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto("https://go.xero.com/", wait_until="load", timeout=30000)
        await page.wait_for_timeout(1500)
        if "login.xero.com" in page.url or "identity.xero.com" in page.url:
            print("\nERROR: Xero session expired. Run xero_web_login.py again.")
            await context.close()
            return

        for api_name, info in org_employee_data.items():
            await process_org_web(
                page, info["display"], info["cid"], info["employees"], date_from, date_to
            )

        await context.close()

    print("\n\nAll done. Review and post the pay runs in Xero.")


if __name__ == "__main__":
    asyncio.run(main())
