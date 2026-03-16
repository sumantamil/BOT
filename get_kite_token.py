"""
Kite API Token Auto-Refresher

Fully automated: launches a headless browser, logs into Zerodha,
captures the OAuth callback, exchanges the request_token for an
access_token, and writes it directly to .env.

Run every morning before 9:15 AM IST:
    python get_kite_token.py

Required in .env:
    KITE_API_KEY, KITE_API_SECRET
    ZERODHA_USER_ID, ZERODHA_PASSWORD
    ZERODHA_PIN         (6-digit Zerodha PIN for 2FA)
    ZERODHA_TOTP_SECRET (optional — overrides PIN if set)

Note: Access tokens expire DAILY at midnight IST.
"""

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
from kiteconnect import KiteConnect
from loguru import logger
from playwright.async_api import async_playwright

try:
    import pyotp
    PYOTP_AVAILABLE = True
except ImportError:
    PYOTP_AVAILABLE = False

ENV_PATH = Path(__file__).parent / ".env"
REDIRECT_HOST = "http://127.0.0.1"


def _update_env_token(access_token: str):
    """Overwrite KITE_ACCESS_TOKEN in .env without touching other values."""
    text = ENV_PATH.read_text(encoding="utf-8")
    if re.search(r"^KITE_ACCESS_TOKEN=", text, re.MULTILINE):
        text = re.sub(
            r"^(KITE_ACCESS_TOKEN=).*$",
            f"KITE_ACCESS_TOKEN={access_token}",
            text,
            flags=re.MULTILINE,
        )
    else:
        text += f"\nKITE_ACCESS_TOKEN={access_token}\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    logger.info("✅ .env updated with new access token")


async def _auto_login(login_url: str, user_id: str, password: str,
                      totp_secret: str, pin: str) -> str:
    """Uses playwright to drive the Zerodha Kite login and return request_token."""
    request_token = None
    captured = asyncio.Event()

    async def _extract_token(url: str) -> bool:
        """Extract request_token from a redirect URL. Returns True if found."""
        nonlocal request_token
        if REDIRECT_HOST in url and "request_token" in url:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            token = qs.get("request_token", [None])[0]
            if token:
                request_token = token
                captured.set()
                return True
        return False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Capture token from route interception (fetch/XHR)
        async def handle_route(route):
            url = route.request.url
            if await _extract_token(url):
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<html><body><h2>Token captured.</h2></body></html>"
                )
            else:
                await route.continue_()

        # Capture token from page navigation (browser redirect)
        async def handle_request(request):
            await _extract_token(request.url)

        await page.route("**/*", handle_route)
        page.on("request", handle_request)

        logger.info("Opening Kite login page (headless)...")
        await page.goto(login_url, wait_until="domcontentloaded")

        # --- Step 1: User ID + Password ---
        await page.wait_for_selector('input[type="text"]', timeout=15000)
        await page.fill('input[type="text"]', user_id)
        await page.fill('input[type="password"]', password)
        await page.click('button[type="submit"]')
        logger.info("Credentials submitted, waiting for 2FA page...")
        await asyncio.sleep(2)

        # --- Step 2: 2FA ---
        if totp_secret and PYOTP_AVAILABLE:
            # Fully automated TOTP — generate fresh code right now
            otp = pyotp.TOTP(totp_secret).now()
            logger.info("Using TOTP secret for 2FA (auto-generated)...")
        else:
            # Always prompt for a FRESH OTP — never use a stored value
            # because TOTP codes expire every 30 seconds
            print("\n" + "="*52)
            print("  2FA required — Zerodha login page is ready.")
            print("  Open your Authenticator app and enter the")
            print("  CURRENT 6-digit OTP (it changes every 30s).")
            print("="*52)
            otp = input("  OTP: ").strip()
            print("="*52 + "\n")
            if not otp:
                logger.error("No OTP entered. Aborting.")
                await browser.close()
                return None

        # Fill the 2FA field — Zerodha uses different input types on different pages
        submitted = False
        for sel in ['input[type="number"]', 'input[type="password"]', 'input[type="text"]']:
            try:
                await page.wait_for_selector(sel, timeout=5000)
                await page.fill(sel, otp)
                await page.click('button[type="submit"]')
                logger.info("2FA code submitted")
                submitted = True
                break
            except Exception:
                continue
        if not submitted:
            logger.warning("Could not find 2FA input field — login may have already progressed.")

        # --- Wait for redirect to callback URL ---
        logger.info("Waiting for OAuth redirect (up to 30s)...")
        try:
            await asyncio.wait_for(captured.wait(), timeout=30)
        except asyncio.TimeoutError:
            current_url = page.url
            logger.error("Timed out waiting for Kite OAuth redirect.")
            logger.error(f"Browser was at: {current_url}")
            logger.error(
                "Possible causes:\n"
                "  1. Wrong OTP — try again immediately with the current code\n"
                "  2. Kite app redirect URL must be set to: http://127.0.0.1\n"
                "     (set at https://kite.trade/ → My Apps → Redirect URL)\n"
                "  3. Wrong User ID or Password"
            )
            await browser.close()
            return None

        await browser.close()

    return request_token


async def auto_refresh_token():
    """Full automated flow: login → capture token → save to .env."""
    load_dotenv(ENV_PATH)

    api_key = os.getenv("KITE_API_KEY", "").strip()
    api_secret = os.getenv("KITE_API_SECRET", "").strip()
    user_id = os.getenv("ZERODHA_USER_ID", "").strip()
    password = os.getenv("ZERODHA_PASSWORD", "").strip()
    totp_secret = os.getenv("ZERODHA_TOTP_SECRET", "").strip()
    pin = os.getenv("ZERODHA_PIN", "").strip()

    missing = [k for k, v in {
        "KITE_API_KEY": api_key,
        "KITE_API_SECRET": api_secret,
        "ZERODHA_USER_ID": user_id,
        "ZERODHA_PASSWORD": password,
    }.items() if not v]
    if missing:
        logger.error(f"Missing required .env values: {', '.join(missing)}")
        return

    if not totp_secret and not pin:
        logger.info("ZERODHA_PIN not set — will prompt interactively just before 2FA submission.")

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    logger.info("Starting automated Kite token refresh...")
    request_token = await _auto_login(login_url, user_id, password, totp_secret, pin)

    if not request_token:
        logger.error("Failed to capture request_token.")
        return

    logger.info(f"request_token captured: {request_token[:8]}...")

    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session["access_token"]
        _update_env_token(access_token)
        logger.info(f"Token: ...{access_token[-6:]}  (restart bot to apply)")
        logger.info("Run: python main.py")
    except Exception as e:
        logger.error(f"Failed to generate session: {e}")


def get_kite_credentials():
    """Legacy entry-point — now delegates to automated flow."""
    asyncio.run(auto_refresh_token())


if __name__ == "__main__":
    get_kite_credentials()
