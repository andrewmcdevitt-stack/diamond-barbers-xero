"""
xero_add_payruns.py
-------------------
Opens Xero Payroll in Firefox and clicks "Add Pay Run" for each org.
This activates all employees so xero_create_payrun.py can write their earnings.

Screenshots are saved to data/debug_screenshots/ at every key step.

Run with:  python agent/xero_add_payruns.py
Requires:  data/firefox_profile/  (from xero_web_login.py)
"""

import asyncio
import os
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

DATA_DIR       = Path(__file__).parent.parent / "data"
PROFILE_DIR    = DATA_DIR / "firefox_profile"
SCREENSHOT_DIR = DATA_DIR / "debug_screenshots"

ORGS = [
    {"name": "Darwin", "cid": "!79ZCm"},
    {"name": "Cairns", "cid": "!BJv4H"},
    {"name": "Parap",  "cid": "!Mb!v8"},
]


async def screenshot(page, label):
    """Save a screenshot with a timestamp and label."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.utcnow().strftime("%H%M%S")
    path = SCREENSHOT_DIR / f"{ts}_{label}.png"
    await page.screenshot(path=str(path), full_page=True)
    print(f"  [screenshot] {path.name}")
    return path


async def add_pay_run_for_org(page, org):
    name = org["name"]
    cid  = org["cid"]
    url  = f"https://payroll.xero.com/PayRun/PayRun?CID={cid}"

    print(f"\n  -- {name} --")
    print(f"  Navigating to {url}...")
    await page.goto(url, wait_until="load", timeout=30000)
    await page.wait_for_timeout(4000)

    await screenshot(page, f"{name}_01_loaded")

    page_text = (await page.inner_text("body")).upper()

    # Detect login page
    if "LOG IN TO XERO" in page_text or page.url.startswith("https://login.xero.com"):
        print(f"  ERROR: Redirected to login page — session expired.")
        await screenshot(page, f"{name}_ERROR_login_page")
        return False

    # Skip if a draft already exists
    if "DRAFT" in page_text:
        print(f"  Draft pay run already exists — skipping.")
        await screenshot(page, f"{name}_02_draft_exists")
        return True

    # Look for "Add Pay Run" button
    try:
        btn = page.get_by_role("button", name="Add Pay Run")
        await btn.wait_for(timeout=10000)
        await screenshot(page, f"{name}_02_before_click")
        await btn.click()
        print(f"  Clicked 'Add Pay Run'.")
        await page.wait_for_timeout(6000)

        await screenshot(page, f"{name}_03_after_click")
        page_text2 = (await page.inner_text("body")).upper()

        if "DRAFT" in page_text2:
            print(f"  Pay run created successfully.")
            return True
        else:
            print(f"  WARNING: Draft not confirmed after clicking.")
            await screenshot(page, f"{name}_WARNING_no_draft")
            print(f"  PAGE TEXT (first 500 chars): {page_text2[:500]}")
            return False

    except Exception as e:
        print(f"  ERROR: Could not find or click 'Add Pay Run' button: {e}")
        await screenshot(page, f"{name}_ERROR_no_button")
        print(f"  PAGE TEXT (first 500 chars): {page_text[:500]}")
        return False


async def run():
    if not PROFILE_DIR.exists():
        print("ERROR: No Firefox profile found.")
        print("Run:  python agent/xero_web_login.py  first.")
        return

    headless = os.environ.get("CI", "false").lower() == "true"
    print(f"Running {'headless' if headless else 'headed'} Firefox...")

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
        await screenshot(page, "00_session_check")

        if not page.url.startswith("https://payroll.xero.com"):
            print(f"Session expired — current URL: {page.url}")
            print("Run xero_web_login.py locally, push profile to xero-session, then retry.")
            await context.close()
            raise SystemExit(1)

        page_text_check = (await page.inner_text("body")).upper()
        if "LOG IN TO XERO" in page_text_check:
            print("Session appears invalid — login page detected on payroll.xero.com.")
            await screenshot(page, "00_ERROR_false_session")
            await context.close()
            raise SystemExit(1)

        print("Session valid.")

        results = {}
        for org in ORGS:
            results[org["name"]] = await add_pay_run_for_org(page, org)

        await context.close()

    print("\n" + "="*40)
    for name, ok in results.items():
        print(f"  {name}: {'OK' if ok else 'FAILED'}")
    print("="*40)

    if not all(results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(run())
