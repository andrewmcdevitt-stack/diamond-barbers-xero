"""
xero_web_login.py
-----------------
One-time login for the Xero Payroll web interface.
Uses a persistent Firefox profile so the session survives between runs.

Run with:  python agent/xero_web_login.py

When Xero eventually logs you out (weeks/months), just run this again.
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

DATA_DIR     = Path(__file__).parent.parent / "data"
PROFILE_DIR  = DATA_DIR / "firefox_profile"


async def login():
    async with async_playwright() as p:
        print("Opening Firefox with persistent profile...")
        PROFILE_DIR.mkdir(exist_ok=True)

        context = await p.firefox.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Patch bot-detection signals so Akamai doesn't block the login page
        await stealth_async(page)

        # Navigate to payroll — this redirects naturally to login.xero.com if not logged in.
        # Going via payroll (not directly to login.xero.com) avoids Akamai's bot block.
        print("Navigating to Xero payroll (will redirect to login if needed)...")
        await page.goto("https://payroll.xero.com/PayRun/PayRun", wait_until="load", timeout=30000)
        await page.wait_for_timeout(1500)

        if "payroll.xero.com" in page.url:
            print(f"Already logged in — {page.url}")
        else:
            print("=" * 60)
            print("Log in to Xero in the browser window:")
            print("  1. Enter your email and password")
            print("  2. Complete MFA (authenticator app code)")
            print("  3. Tick 'Stay signed in' if shown")
            print("You have 5 minutes.")
            print("=" * 60)

            try:
                await page.wait_for_function(
                    "() => window.location.hostname === 'payroll.xero.com'",
                    timeout=300_000,
                )
            except Exception:
                pass

            if "payroll.xero.com" not in page.url:
                print("\nERROR: Login timed out. Please try again.")
                await context.close()
                return

        print(f"Logged in — {page.url}")

        await page.wait_for_timeout(1000)
        print(f"Payroll URL: {page.url}")
        print("\nSession profile saved. You can now run:  python agent/xero_fill_payslips.py")
        await context.close()


if __name__ == "__main__":
    asyncio.run(login())
