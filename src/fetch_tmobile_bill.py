#!/usr/bin/env python3
"""
T-Mobile Bill Fetcher (best-effort, Layer B).

Logs into my.t-mobile.com with credentials from the macOS Keychain, completes
2FA by reading the one-time code out of Messages (~/Library/Messages/chat.db),
and downloads the latest statement PDF into bills/.

This is the one inherently fragile piece of the pipeline: T-Mobile has no API,
runs bot-detection, and changes their markup. Every step therefore:
  * is driven by selectors in config.json (`tmobile.selectors`) so tuning needs
    no code change, and
  * raises a clear error on failure. The orchestrator (run_pipeline.py) catches
    that, notifies you, and falls back to "drop the PDF in bills/ yourself".

FIRST-TIME SETUP (one time, makes headless runs reliable):
  1. Store credentials in the Keychain (two items under one service):
       security add-generic-password -s tmobile-login -a username -w '<your T-Mobile username>'
       security add-generic-password -s tmobile-login -a password -w '<your T-Mobile password>'
  2. Seed the logged-in browser profile interactively:
       python3 src/fetch_tmobile_bill.py --headed --dry-run
     Complete any login / 2FA in the window that opens. Cookies persist under
     state/tmobile_profile/, so later headless runs usually skip 2FA entirely.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = "state/tmobile_profile"
DEBUG_DIR = "logs"


# ---------------------------------------------------------------------------
# Credentials (macOS Keychain)
# ---------------------------------------------------------------------------

def _keychain_secret(service: str, account: str) -> Optional[str]:
    """Read a generic-password value from the login Keychain, or None."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() or None
    except subprocess.CalledProcessError:
        return None


def load_credentials(service: str) -> Dict[str, str]:
    user = _keychain_secret(service, "username")
    pw = _keychain_secret(service, "password")
    if not user or not pw:
        raise RuntimeError(
            f"T-Mobile credentials not found in Keychain (service '{service}').\n"
            "Store them once with:\n"
            f"  security add-generic-password -s {service} -a username -w '<username>'\n"
            f"  security add-generic-password -s {service} -a password -w '<password>'"
        )
    return {"username": user, "password": pw}


# ---------------------------------------------------------------------------
# 2FA: pull the one-time code from Messages (chat.db)
# ---------------------------------------------------------------------------

def read_latest_otp(chat_db_path: str, sender_pattern: str,
                    since_ts: float, max_wait: int) -> Optional[str]:
    """Poll chat.db for a 4-8 digit code in a recent T-Mobile message.

    `since_ts` is a unix timestamp; only messages newer than it are considered
    (so we never reuse an old code). Returns the code string or None on timeout.
    """
    db = os.path.expanduser(chat_db_path)
    if not os.path.exists(db):
        print(f"[otp] chat.db not found at {db} — cannot auto-read 2FA code")
        return None

    sender_re = re.compile(sender_pattern, re.IGNORECASE)
    code_re = re.compile(r'\b(\d{4,8})\b')
    deadline = time.time() + max_wait
    cutoff_ns = int((since_ts - APPLE_EPOCH_OFFSET) * 1e9)

    print(f"[otp] Waiting up to {max_wait}s for a T-Mobile code in Messages...")
    while time.time() < deadline:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cur = con.cursor()
            cur.execute(
                """
                SELECT h.id, m.text, m.date
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.rowid
                WHERE m.date > ? AND m.is_from_me = 0 AND m.text IS NOT NULL
                ORDER BY m.date DESC
                LIMIT 25
                """,
                (cutoff_ns,),
            )
            rows = cur.fetchall()
            con.close()
        except sqlite3.Error as err:
            print(f"[otp] chat.db read failed (Full Disk Access?): {err}")
            return None

        for sender, text, _date_ns in rows:
            hay = f"{sender or ''} {text or ''}"
            if not sender_re.search(hay):
                continue
            if not re.search(r'code|verification|verify|one[- ]?time', text, re.IGNORECASE):
                # Be strict: only messages that look like a verification code.
                continue
            m = code_re.search(text)
            if m:
                print(f"[otp] Found code {m.group(1)}")
                return m.group(1)
        time.sleep(5)

    print("[otp] Timed out waiting for a 2FA code")
    return None


# ---------------------------------------------------------------------------
# Bill month / filename
# ---------------------------------------------------------------------------

def _bills_dir(download_dir: str) -> Path:
    d = Path(download_dir).expanduser()
    if not d.is_absolute():  # anchor relative dirs to the project root
        d = ROOT / d
    return d


def current_bill_filename(download_dir: str, when: Optional[datetime] = None) -> Path:
    when = when or datetime.now()
    return _bills_dir(download_dir) / f"SummaryBill{when.strftime('%b%Y')}.pdf"


_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
           'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def bill_month_from_pdf(pdf_path) -> tuple:
    """Return (mon3, year) e.g. ('May', '2026') from the bill's own statement
    date, so a downloaded bill is named for the month it actually covers — not
    whatever calendar month we happen to fetch it in. Raises if undetermined."""
    import pymupdf
    doc = pymupdf.open(str(pdf_path))
    text = doc[0].get_text()
    doc.close()

    # Primary anchor: the "Bill issue date" label followed by "<Month> <d>, <yyyy>".
    m = re.search(r'Bill issue date\s+([A-Za-z]{3,9})\s+\d{1,2},\s*(\d{4})', text)
    if not m:  # fallback: "Here's your bill for <Month>." + any 4-digit year on page
        mon_m = re.search(r"bill for\s+([A-Za-z]{3,9})", text, re.IGNORECASE)
        yr_m = re.search(r'\b(20\d{2})\b', text)
        if mon_m and yr_m:
            m = (mon_m.group(1), yr_m.group(1))
            mon, year = m[0][:3].capitalize(), m[1]
            if mon in _MONTHS:
                return mon, year
        raise RuntimeError("Could not determine the bill's statement month from the PDF")

    mon, year = m.group(1)[:3].capitalize(), m.group(2)
    if mon not in _MONTHS:
        raise RuntimeError(f"Unrecognized bill month '{mon}' parsed from PDF")
    return mon, year


# ---------------------------------------------------------------------------
# Playwright flow
# ---------------------------------------------------------------------------

def _first_visible(page, selector: str, timeout: int = 8000):
    """Return the first matching locator that becomes visible, or None."""
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout)
        return loc
    except Exception:
        return None


def fetch_bill(config: Dict, sms_config: Dict, headed: bool = False,
               dry_run: bool = False, out_path: Optional[Path] = None) -> Optional[Path]:
    """Drive the browser to download the latest bill PDF. Returns the saved
    path, or None on --dry-run. Raises on any unrecoverable failure."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright not installed. Run:\n"
            "  pip install playwright && python3 -m playwright install chromium"
        )

    tm = config.get("tmobile", {})
    sel = tm.get("selectors", {})
    creds = load_credentials(tm.get("keychain_service", "tmobile-login"))
    Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)
    Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)

    def _dump(page, tag: str):
        try:
            page.screenshot(path=str(Path(DEBUG_DIR) / f"tmobile-{tag}.png"))
        except Exception:
            pass

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=not headed,
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            # Go straight to the bill page. With a valid persisted session this
            # loads the bill; otherwise T-Mobile redirects us to the sign-in form,
            # where the username field appears.
            print(f"[fetch] Opening bill page {tm.get('billing_url')}")
            page.goto(tm.get("billing_url"), wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)  # let any auth redirect settle

            if _first_visible(page, sel.get("username", ""), timeout=5000) is None:
                print("[fetch] Existing session is signed in")
            else:
                print("[fetch] Not signed in — logging in with password")
                login_started = time.time()
                u = _first_visible(page, sel.get("username", ""), timeout=10000)
                if u:
                    print("[fetch] Entering username")
                    u.fill(creds["username"])
                    nxt = _first_visible(page, sel.get("username_next", ""), timeout=4000)
                    if nxt:
                        nxt.click()

                # T-Mobile defaults to a passkey / Face ID step that cannot be
                # automated headlessly. If the "Log in with password" route is
                # offered, take it to reach the password form.
                use_pw = _first_visible(page, sel.get("use_password_link", ""), timeout=6000)
                if use_pw:
                    print("[fetch] Choosing 'Log in with password' (bypassing passkey)")
                    use_pw.click()

                pw = _first_visible(page, sel.get("password", ""), timeout=10000)
                if pw:
                    print("[fetch] Entering password")
                    pw.fill(creds["password"])
                    sub = _first_visible(page, sel.get("password_submit", ""), timeout=4000)
                    if sub:
                        sub.click()
                    else:
                        # Fall back to submitting the form via Enter when the
                        # exact button selector doesn't match.
                        pw.press("Enter")

                # --- 2FA, if prompted ---
                otp_box = _first_visible(page, sel.get("otp_input", ""), timeout=8000)
                if otp_box:
                    code = read_latest_otp(
                        sms_config.get("chat_db_path", "~/Library/Messages/chat.db"),
                        tm.get("otp_sender_pattern", "T-?Mobile"),
                        since_ts=login_started,
                        max_wait=int(tm.get("otp_max_wait_seconds", 150)),
                    )
                    if not code:
                        _dump(page, "otp-timeout")
                        raise RuntimeError(
                            "2FA code not received automatically. If your T-Mobile "
                            "texts don't land in Messages on this Mac, do a one-time "
                            "`--headed` login to seed the session.")
                    otp_box.fill(code)
                    osub = _first_visible(page, sel.get("otp_submit", ""), timeout=4000)
                    if osub:
                        osub.click()
                    else:
                        otp_box.press("Enter")

                # Back on an authenticated session — reload the bill page.
                page.wait_for_timeout(3000)
                print(f"[fetch] Opening bill page {tm.get('billing_url')}")
                page.goto(tm.get("billing_url"), wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)

            # Success is defined by actually reaching the download control on the
            # bill page — not by a guessed marker.
            opener = _first_visible(page, sel.get("bill_pdf_link", ""), timeout=20000)
            if not opener:
                _dump(page, "no-bill-page")
                raise RuntimeError(
                    "Could not reach the bill page — the session may have expired "
                    "or login failed. Re-seed with: "
                    "python3 src/fetch_tmobile_bill.py --headed")

            if dry_run:
                _dump(page, "dryrun-billing")
                print("[fetch] --dry-run: reached the bill page (download control present); "
                      "skipping download")
                return None

            # "Download my bill (PDF)" opens a modal asking summary vs detailed;
            # we want the summary version (matches the parser's expectations).
            opener.click()

            summary_btn = _first_visible(page, sel.get("bill_summary_button", ""), timeout=8000)
            if not summary_btn:
                _dump(page, "no-summary-button")
                raise RuntimeError("Could not find the 'Download summary bill' button")

            with page.expect_download(timeout=60000) as dl_info:
                summary_btn.click()
            download = dl_info.value

            if out_path is not None:
                # Explicit destination (testing/override): save exactly there.
                out_path.parent.mkdir(parents=True, exist_ok=True)
                download.save_as(str(out_path))
                print(f"[fetch] Saved bill to {out_path}")
                return out_path

            # Name by the bill's real statement month and de-dup, so T-Mobile's
            # "current bill" (which may still be last month's until the new one
            # issues) is never saved under the wrong month or processed twice.
            tmp = Path(DEBUG_DIR) / "tmobile-download.pdf"
            download.save_as(str(tmp))
            mon, year = bill_month_from_pdf(tmp)
            dest = _bills_dir(tm.get("download_dir", "bills")) / f"SummaryBill{mon}{year}.pdf"
            if dest.exists():
                print(f"[fetch] Downloaded bill is for {mon} {year}, already in "
                      f"bills/ — no new bill to fetch")
                tmp.unlink()
                return None
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), str(dest))
            print(f"[fetch] Saved new bill to {dest}")
            return dest
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch the latest T-Mobile bill PDF")
    parser.add_argument("--config", default="src/config.json")
    parser.add_argument("--headed", action="store_true",
                        help="Show the browser (use for the one-time seeding login)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log in and reach the billing page but do not download")
    parser.add_argument("--out", default=None, help="Override output PDF path")
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)
    sms_config = config.get("sms", {})

    try:
        out = fetch_bill(
            config, sms_config,
            headed=args.headed, dry_run=args.dry_run,
            out_path=Path(args.out) if args.out else None,
        )
        if out:
            print(f"✓ Bill downloaded: {out}")
        elif args.dry_run:
            print("✓ Dry run complete (no download)")
        else:
            print("✓ No new bill to download (current bill already on file)")
    except Exception as e:
        print(f"✗ Fetch failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
