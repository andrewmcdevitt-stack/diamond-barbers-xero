"""
xero_create_payrun.py
---------------------
Creates a DRAFT pay run in Xero for last week (via Xero API) and fills in
each employee's earnings lines with data read from GHL:
  - Monday-Friday hours
  - Saturday hours
  - Sunday hours
  - Tips
  - Commission

The pay run is left as DRAFT — log in to Xero to review and post it.
Run BEFORE xero_fill_payslips.py.

Run with:  python agent/xero_create_payrun.py
Requires:  data/xero_token.json  (from xero_auth.py)
           GHL_API_KEY + GHL_LOCATION_ID in environment
"""

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

DATA_DIR      = Path(__file__).parent.parent / "data"
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

# ── Earnings rate IDs per Xero org ────────────────────────────────────────────
ORG_RATES = {
    "Diamond Barbers Pty Ltd": {
        "monday":     "ba08c024-1289-420e-8e46-4a00d989815b",
        "tuesday":    "00cfd9c0-cf8d-46b1-8503-02c8b4e25e50",
        "wednesday":  "414c36eb-b5cc-4b25-8063-9358930e8368",
        "thursday":   "95b47ce1-19f0-4fd1-9fa6-386b27ea3696",
        "friday":     "4652ee75-937b-41a8-adc3-40b570ec24dc",
        "saturday":   "b7771727-60d0-4e37-8f60-fca50b0f4423",
        "sunday":     "c7d4a9e4-e735-485f-8700-c7d29a17dff4",
        "tips":       "759bbf1f-a20a-4123-bb17-80842dc688ec",
        "commission": "fb04b066-99fa-4b56-815b-94092a009e38",
    },
    "DIAMOND BARBERS CAIRNS PTY LTD": {
        "monday":     "0ac27a0f-b798-4f26-b53a-7e1c1c300f03",
        "tuesday":    "10a69950-d8b3-4be3-bba1-363cefab7342",
        "wednesday":  "3538e9ac-111d-4653-95bc-b6194278e013",
        "thursday":   "29ab0820-40de-4b45-9f77-ecc4ee2392aa",
        "friday":     "5e2a8ce0-e9d4-4b97-9d9a-bb28e1a927d4",
        "saturday":   "18b97f5d-455b-42ee-846b-63550acb6b5d",
        "sunday":     "3c88813b-e892-4b9b-9e27-22c5fa734379",
        "tips":       "d6aef20e-4ed4-4d92-88c8-3dd3afa6eb23",
        "commission": "42714ec9-fb41-4498-9cea-b0a2c8b6f4f3",
    },
    "D.B. Parap Pty Ltd": {
        "monday":     "2c266681-811c-4c02-9ea0-f133885b214c",
        "tuesday":    "3b2a9946-1718-43d1-821a-5102dd089d55",
        "wednesday":  "856c518d-88ce-4af7-9f4b-f05cab2fda5d",
        "thursday":   "1ba75264-93a0-4170-a7f5-edccf065ec9c",
        "friday":     "bf2ecf3b-13ba-4f85-8cb2-564b18694390",
        "saturday":   "3d92631e-7e25-4ba6-9c4c-d0e3a822e674",
        "sunday":     "ca82390d-dacf-4dfc-8bad-29647fc118fa",
        "tips":       "f9261b3a-0659-48e4-990c-40d770cef73c",
        "commission": "9b40d911-89b7-401b-82c1-662fa9e2c782",
    },
}

SKIP_ORGS = {"DB WULGURU PTY LTD"}

EXCLUDED_EMPLOYEES = {
    "andrew mcdevitt",
    "andrew  mcdevitt",
    "nicole diamantis",
    "nicole  diamantis",
}

# GHL xero_org → Xero API tenant name
GHL_ORG_TO_XERO = {
    "Diamond Barbers Darwin": "Diamond Barbers Pty Ltd",
    "Diamond Barbers Parap":  "D.B. Parap Pty Ltd",
    "Diamond Barbers Cairns": "DIAMOND BARBERS CAIRNS PTY LTD",
}

XERO_TO_GHL = {
    "anthony  crispo":      "anthony crispo",
    "jairo espinosa mejia": "jairo espinosa",
}

# Employees whose Fresha location doesn't match their Xero payroll org.
# These override the xero_org value from GHL.
EMPLOYEE_ORG_OVERRIDE = {
    "andrea palma":        "Diamond Barbers Darwin",
    "brazil lamsen":       "Diamond Barbers Darwin",
    "krish manocha":       "Diamond Barbers Parap",
    "vincenzo vanzanella": "Diamond Barbers Parap",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def norm(name):
    return " ".join(name.lower().split())


def resolve_ghl_name(xero_name):
    n = norm(xero_name)
    return XERO_TO_GHL.get(n, n)


def parse_xero_date(date_str):
    if not date_str:
        return None
    m = re.match(r"/Date\((\d+)", str(date_str))
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(date_str)[:10]


def last_week_dates():
    DARWIN_TZ   = timezone(timedelta(hours=9, minutes=30))
    today       = datetime.now(DARWIN_TZ).date()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


# ── GHL data loading ──────────────────────────────────────────────────────────

def load_from_ghl():
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
            print(f"  WARNING: GHL page {page} returned {r.status_code}")
            break
        records = r.json().get("records", [])
        all_records.extend(records)
        if len(records) < 100:
            break
        page += 1

    if not all_records:
        return {}

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
        data[norm(name)] = {
            "xero_org":       p.get("xero_org", ""),
            "monday_hrs":     round(float(p.get("monday_hours",         0) or 0), 2),
            "tuesday_hrs":    round(float(p.get("tuesday_hours",        0) or 0), 2),
            "wednesday_hrs":  round(float(p.get("wednesday_hours",      0) or 0), 2),
            "thursday_hrs":   round(float(p.get("thursday_hours",       0) or 0), 2),
            "friday_hrs":     round(float(p.get("friday_hours",         0) or 0), 2),
            "saturday_hrs":   round(float(p.get("saturday_hours",       0) or 0), 2),
            "sunday_hrs":     round(float(p.get("sunday_hours",         0) or 0), 2),
            "ph_hrs":         round(float(p.get("public_holiday_hours", 0) or 0), 2),
            "tips":           round(float(p.get("tips",        0) or 0), 2),
            "commission":     round(float(p.get("commissions", 0) or 0), 2),
        }

    print(f"  Loaded {len(data)} employees.")
    for n, d in sorted(data.items()):
        print(f"    {n:35s}  org={d['xero_org']}")
    return data


# ── Token management ──────────────────────────────────────────────────────────

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


def xero_post(path, tenant_id, access_token, body):
    data = json.dumps(body).encode()
    req  = urllib.request.Request(
        f"https://api.xero.com{path}", data=data,
        headers={"Authorization": f"Bearer {access_token}",
                 "Xero-Tenant-Id": tenant_id,
                 "Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            import gzip; raw = gzip.decompress(raw)
        except Exception:
            pass
        raise Exception(f"HTTP {e.code}: {raw.decode('utf-8', errors='replace')}") from None


# ── Per-org processing ────────────────────────────────────────────────────────

def process_org(tenant_id, tenant_name, access_token, ghl_data):
    print(f"\n{'='*60}")
    print(f"ORG: {tenant_name}")
    print(f"{'='*60}")

    if tenant_name in SKIP_ORGS:
        print("  Skipping.")
        return

    rates = ORG_RATES.get(tenant_name)
    if not rates:
        print("  No rate configuration — skipping.")
        return

    # Find which GHL org label maps to this Xero tenant
    ghl_org_label = next((k for k, v in GHL_ORG_TO_XERO.items() if v == tenant_name), None)
    if not ghl_org_label:
        print("  No GHL org mapping — skipping.")
        return

    date_from, date_to = last_week_dates()
    date_from_str = date_from.strftime("%Y-%m-%d")
    date_to_str   = date_to.strftime("%Y-%m-%d")
    print(f"  Pay period: {date_from_str} to {date_to_str}")

    # Fetch Xero employees
    emp_data  = xero_get("/payroll.xro/1.0/Employees", tenant_id, access_token)
    employees = emp_data.get("Employees", [])
    emp_id_map = {}
    for e in employees:
        full = f"{e.get('FirstName','')} {e.get('LastName','')}".strip()
        emp_id_map[norm(full)] = e["EmployeeID"]

    # Build payslip list from GHL data
    payslip_list = []
    skipped      = []

    for xero_norm, emp_id in emp_id_map.items():
        if xero_norm in EXCLUDED_EMPLOYEES:
            continue

        ghl_key = resolve_ghl_name(xero_norm)
        emp = ghl_data.get(ghl_key)

        # Fallback: first-name match
        if not emp:
            first = ghl_key.split()[0]
            emp = next(
                (v for k, v in ghl_data.items()
                 if k.split()[0] == first and v.get("xero_org") == ghl_org_label),
                None
            )

        # Apply employee-level org override if defined
        if xero_norm in EMPLOYEE_ORG_OVERRIDE:
            if emp:
                emp = dict(emp)
                emp["xero_org"] = EMPLOYEE_ORG_OVERRIDE[xero_norm]

        if not emp or emp.get("xero_org") != ghl_org_label:
            actual_org = emp.get("xero_org", "NOT IN GHL") if emp else "NOT IN GHL"
            print(f"  UNMATCHED: {xero_norm!r} — GHL org={actual_org!r}, expected={ghl_org_label!r}")
            continue

        total_hrs = (
            emp["monday_hrs"] + emp["tuesday_hrs"] + emp["wednesday_hrs"] +
            emp["thursday_hrs"] + emp["friday_hrs"] +
            emp["saturday_hrs"] + emp["sunday_hrs"]
        )
        if total_hrs == 0:
            skipped.append(f"{xero_norm} (no hours)")
            continue

        lines = []
        if emp["monday_hrs"]    > 0: lines.append({"EarningsRateID": rates["monday"],    "NumberOfUnits": emp["monday_hrs"]})
        if emp["tuesday_hrs"]   > 0: lines.append({"EarningsRateID": rates["tuesday"],   "NumberOfUnits": emp["tuesday_hrs"]})
        if emp["wednesday_hrs"] > 0: lines.append({"EarningsRateID": rates["wednesday"], "NumberOfUnits": emp["wednesday_hrs"]})
        if emp["thursday_hrs"]  > 0: lines.append({"EarningsRateID": rates["thursday"],  "NumberOfUnits": emp["thursday_hrs"]})
        if emp["friday_hrs"]    > 0: lines.append({"EarningsRateID": rates["friday"],    "NumberOfUnits": emp["friday_hrs"]})
        if emp["saturday_hrs"]  > 0: lines.append({"EarningsRateID": rates["saturday"],  "NumberOfUnits": emp["saturday_hrs"]})
        if emp["sunday_hrs"]    > 0: lines.append({"EarningsRateID": rates["sunday"],    "NumberOfUnits": emp["sunday_hrs"]})
        if emp["tips"]          > 0: lines.append({"EarningsRateID": rates["tips"],      "NumberOfUnits": 1, "RatePerUnit": emp["tips"]})
        if emp["commission"]    > 0: lines.append({"EarningsRateID": rates["commission"],"NumberOfUnits": 1, "RatePerUnit": emp["commission"]})

        payslip_list.append({
            "EmployeeID":    emp_id,
            "EarningsLines": lines,
            "_name":         xero_norm,
            "_emp":          emp,
        })

    print(f"  {len(payslip_list)} employees to fill, {len(skipped)} skipped")

    def clean(ps):
        return {k: v for k, v in ps.items() if not k.startswith("_")}

    # Get PayrollCalendarID from most recent pay run
    runs_data   = xero_get("/payroll.xro/1.0/PayRuns", tenant_id, access_token)
    all_runs    = runs_data.get("PayRuns", [])
    if not all_runs:
        print("  No existing pay runs found — cannot determine calendar ID.")
        return

    all_runs.sort(
        key=lambda r: parse_xero_date(r.get("PaymentDate") or r.get("PayRunPeriodEndDate", "")) or "",
        reverse=True,
    )
    calendar_id = all_runs[0].get("PayrollCalendarID")
    if not calendar_id:
        print("  Could not determine PayrollCalendarID.")
        return

    # Check for existing draft or already-posted run for this period
    existing = next((r for r in all_runs if r.get("PayRunStatus") == "DRAFT"), None)
    if not existing:
        for r in all_runs:
            if (parse_xero_date(r.get("PayRunPeriodStartDate", "")) == date_from_str and
                    parse_xero_date(r.get("PayRunPeriodEndDate", "")) == date_to_str):
                existing = r
                break

    if existing:
        status = existing.get("PayRunStatus")
        run_id = existing.get("PayRunID")
        if status == "POSTED":
            print(f"  Already POSTED ({run_id}) — skipping.")
            return
        print(f"  Found existing DRAFT pay run: {run_id}")
    else:
        # Create new draft pay run with payslip data embedded
        print("  Creating new DRAFT pay run...")
        result = xero_post("/payroll.xro/1.0/PayRuns", tenant_id, access_token, [{
            "PayrollCalendarID":     calendar_id,
            "PayRunPeriodStartDate": f"{date_from_str}T00:00:00",
            "PayRunPeriodEndDate":   f"{date_to_str}T00:00:00",
            "Payslips":              [clean(ps) for ps in payslip_list],
        }])
        runs   = result if isinstance(result, list) else result.get("PayRuns", [{}])
        run_id = runs[0].get("PayRunID")
        if not run_id:
            print(f"  ERROR: No PayRunID in response.")
            return
        print(f"  Created: {run_id}")

    # Activate each payslip via the Payslip endpoint
    print("  Activating payslips...")
    run_detail  = xero_get(f"/payroll.xro/1.0/PayRuns/{run_id}", tenant_id, access_token)
    slips       = run_detail.get("PayRuns", [{}])[0].get("Payslips", [])
    emp_to_slip = {s["EmployeeID"]: s["PayslipID"] for s in slips if "PayslipID" in s}
    print(f"  Found {len(emp_to_slip)} payslip IDs")

    ok = 0
    for ps in payslip_list:
        slip_id = emp_to_slip.get(ps["EmployeeID"])
        if not slip_id:
            print(f"  No PayslipID for {ps['_name']}")
            continue
        try:
            xero_post(f"/payroll.xro/1.0/Payslip/{slip_id}", tenant_id, access_token, [{
                "PayslipID":     slip_id,
                "EarningsLines": clean(ps)["EarningsLines"],
            }])
            e = ps["_emp"]
            print(f"  OK  {ps['_name']:30s}  "
                  f"mon={e['monday_hrs']}h  tue={e['tuesday_hrs']}h  "
                  f"wed={e['wednesday_hrs']}h  thu={e['thursday_hrs']}h  "
                  f"fri={e['friday_hrs']}h  sat={e['saturday_hrs']}h  "
                  f"sun={e['sunday_hrs']}h  tips=${e['tips']}  comm=${e['commission']}")
            ok += 1
        except Exception as ex:
            print(f"  ERROR {ps['_name']}: {ex}")

    print(f"\n  Done — {ok}/{len(payslip_list)} payslips filled.")
    if skipped:
        for s in skipped:
            print(f"  Skipped: {s}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Refreshing Xero token...")
    token        = json.loads(TOKEN_FILE.read_text())
    token        = refresh_token(token)
    access_token = token["access_token"]
    tenants      = token.get("tenants", [])

    ghl_data = load_from_ghl()
    if not ghl_data:
        print("No GHL data — aborting.")
        return

    date_from, date_to = last_week_dates()
    print(f"Pay period: {date_from} to {date_to}")

    for tenant in tenants:
        try:
            process_org(tenant["id"], tenant["name"], access_token, ghl_data)
        except Exception as e:
            print(f"ERROR processing {tenant['name']}: {e}")

    print("\n\nDone. Log in to Xero to review and post the draft pay runs.")


if __name__ == "__main__":
    main()
