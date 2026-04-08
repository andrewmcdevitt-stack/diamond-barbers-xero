"""
xero_add_payruns.py
-------------------
Opens Xero Payroll in Firefox and clicks "Add Pay Run" for each org.
This activates all employees so xero_fill_payslips.py can write their earnings.

Run BEFORE:  xero_fill_payslips.py

Run with:  python agent/xero_add_payruns.py
Requires:  data/firefox_profile/  (from xero_web_login.py)
"""

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

DATA_DIR    = Path(__file__).parent.parent / "data"
PROFILE_DIR = DATA_DIR / "firefox_profile"

ORGS = [
    {"name": "Darwin", "cid": "!79ZCm"},
    {"name": "Cairns", "cid": "!BJv4H"},
    {"name": "Parap",  "cid": "!Mb!v8"},
]


async def add_pay_run_for_org(page, org):
    name = org["name"]
    cid  = org["cid"]
    url  = f"https://payroll.xero.com/PayRun/PayRun?CID={cid}"

    print(f"\n  -- {name} --")
    print(f"  Navigating to {url}...")
    await page.goto(url, wait_until="load", timeout=30000)
    await page.wait_for_timeout(4000)

    page_text = (await page.inner_text("body")).upper()

    # Skip if a draft already exists
    if "DRAFT" in page_text:
        print(f"  Draft pay run already exists — skipping.")
        return True

    # Look for "Add Pay Run" button
    try:
        btn = page.get_by_role("button", name="Add Pay Run")
        await btn.wait_for(timeout=10000)
        await btn.click()
        print(f"  Clicked 'Add Pay Run'.")
        await page.wait_for_timeout(6000)

        page_text2 = (await page.inner_text("body")).upper()
        if "DRAFT" in page_text2:
            print(f"  Pay run created successfully.")
            return True
        else:
            print(f"  WARNING: Draft not confirmed after clicking.")
            return False

    except Exception as e:
        print(f"  ERROR: Could not find or click 'Add Pay Run' button: {e}")
        print(f"  PAGE TEXT (first 500 chars): {page_text[:500]}")
        return False


async def run():
    if not PROFILE_DIR.exists():
        print("ERROR: No Firefox profile found.")
        print("Run:  python agent/xero_web_login.py  first.")
        return

    headless = os.environ.get("CI", "false").lower() == "true"

    async with async_playwright() as p:
        print("Opening Firefox with persistent profile...")
        context = await p.firefox.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        print("Checking Xero session...")
        await page.goto("https://payroll.xero.com/PayRun/PayRun", wait_until="load", timeout=30000)
        await page.wait_for_timeout(3000)

        if "payroll.xero.com" not in page.url:
            print("Session expired — run xero_web_login.py first.")
            await context.close()
            return

        print("Session valid.")

        results = {}
        for org in ORGS:
            results[org["name"]] = await add_pay_run_for_org(page, org)

        print("\n" + "="*40)
        for name, ok in results.items():
            print(f"  {name}: {'OK' if ok else 'FAILED'}")
        print("="*40)

        await context.close()


if __name__ == "__main__":
    asyncio.run(run())
