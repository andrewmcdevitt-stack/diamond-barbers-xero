"""
list_earnings_rates.py
----------------------
Prints all earnings rates for every Xero org so rate IDs can be copied into ORG_RATES.
Run once via GitHub Actions — output appears in the workflow log.

Run with:  python agent/list_earnings_rates.py
Requires:  data/xero_token.json
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

DATA_DIR      = Path(__file__).parent.parent / "data"
TOKEN_FILE    = DATA_DIR / "xero_token.json"
CLIENT_ID     = os.environ["XERO_CLIENT_ID"]
CLIENT_SECRET = os.environ["XERO_CLIENT_SECRET"]


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


def main():
    token_data   = json.loads(TOKEN_FILE.read_text())
    token_data   = refresh_token(token_data)
    access_token = token_data["access_token"]
    tenants      = token_data.get("tenants", [])

    for tenant in tenants:
        tenant_id   = tenant["id"]
        tenant_name = tenant["name"]
        print(f"\n{'='*60}")
        print(f"ORG: {tenant_name}  (tenantId={tenant_id})")
        print(f"{'='*60}")
        try:
            data  = xero_get("/payroll.xro/1.0/payitems", tenant_id, access_token)
            rates = data.get("PayItems", {}).get("EarningsRates", [])
            for r in sorted(rates, key=lambda x: x.get("Name", "")):
                status = "" if r.get("CurrentRecord", True) else "  [ARCHIVED]"
                print(f"  {r['EarningsRateID']}  {r['Name']}{status}")
        except Exception as e:
            print(f"  ERROR: {e}")


if __name__ == "__main__":
    main()
