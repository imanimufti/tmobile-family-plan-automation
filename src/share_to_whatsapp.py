#!/usr/bin/env python3
"""
Stage 5 — Share the breakdown link to WhatsApp.

Reads the bill total for a given month tab from the Google Sheet, builds a
gid-anchored URL pointing straight at that tab, formats a message from the
configured template (including payment-method bullets), copies it to the
macOS clipboard, and opens WhatsApp Desktop so you can paste + send.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    from google.oauth2.credentials import Credentials
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("Google API libraries not installed. Installing...")
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "google-auth", "google-auth-oauthlib",
                           "google-auth-httplib2", "google-api-python-client"])
    from google.oauth2.credentials import Credentials
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError


SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/spreadsheets',
]


def authenticate(credentials_path: str = "credentials.json"):
    """Return a Sheets service. Mirrors GoogleSheetsUpdater.authenticate."""
    creds_file = Path(credentials_path)
    if not creds_file.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {credentials_path}\nSee README for setup steps."
        )

    creds = None
    try:
        creds = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=SCOPES)
        print("Authenticated using service account")
    except Exception:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request

        token_path = Path("token.json")
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, 'w') as token:
                token.write(creds.to_json())
        print("Authenticated using OAuth")

    return build('sheets', 'v4', credentials=creds)


def fetch_tab_gid_and_total(sheets, spreadsheet_id: str, tab_name: str) -> Tuple[int, str]:
    """Return (gid, bill_total_string) for the given tab."""
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    gid = next(
        (s['properties']['sheetId']
         for s in meta.get('sheets', [])
         if s['properties']['title'] == tab_name),
        None,
    )
    if gid is None:
        raise ValueError(f"Tab '{tab_name}' not found in spreadsheet")

    result = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{tab_name}!L1"
    ).execute()
    values = result.get('values', [])
    if not values or not values[0]:
        raise ValueError(f"Cell {tab_name}!L1 is empty — did Stage 2 run for this tab?")

    raw = values[0][0]
    total = raw.replace('$', '').replace(',', '').strip()
    return gid, total


def render_payment_methods(methods: Dict[str, str]) -> str:
    return "\n".join(f"• {label}: {handle}" for label, handle in methods.items())


def build_message(template: str, tab_name: str, total: str,
                  sheet_url: str, methods: Dict[str, str]) -> str:
    return template.format(
        tab_name=tab_name,
        total=total,
        sheet_url=sheet_url,
        payment_methods=render_payment_methods(methods),
    )


def copy_to_clipboard(text: str) -> None:
    proc = subprocess.run(['pbcopy'], input=text.encode('utf-8'), check=True)
    if proc.returncode != 0:
        raise RuntimeError("pbcopy failed")


def open_whatsapp(group_invite_url: Optional[str]) -> None:
    if group_invite_url:
        subprocess.run(['open', group_invite_url], check=True)
    else:
        subprocess.run(['open', '-a', 'WhatsApp'], check=True)


def send_via_applescript(open_delay: float = 4.0, after_paste_delay: float = 1.0) -> None:
    """Drive WhatsApp Desktop to paste from clipboard and press Enter.

    Requires Accessibility permission for the terminal/process running this
    script (System Settings → Privacy & Security → Accessibility). The first
    run will surface a permission prompt; subsequent runs are silent.
    """
    script = f'''
    tell application "WhatsApp" to activate
    delay {open_delay}
    tell application "System Events"
        keystroke "v" using {{command down}}
        delay {after_paste_delay}
        keystroke return
    end tell
    '''
    subprocess.run(['osascript', '-e', script], check=True)


def main():
    parser = argparse.ArgumentParser(
        description='Share the Google Sheet breakdown for a given month to WhatsApp')
    parser.add_argument('tab_name', help='Sheet tab name (e.g. "Mar 26")')
    parser.add_argument('--credentials', default='credentials.json',
                        help='Path to credentials.json')
    parser.add_argument('--config', default='src/config.json',
                        help='Path to config.json')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print the rendered message + URL only; do not touch clipboard or open WhatsApp')
    parser.add_argument('--no-send', action='store_true',
                        help='Open WhatsApp and copy message to clipboard, but skip the auto-paste/Enter — leaves you to send manually')
    parser.add_argument('--open-delay', type=float, default=4.0,
                        help='Seconds to wait after launching WhatsApp before pasting (default: 4.0)')
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = json.load(f)

    spreadsheet_id = config['google_sheet_id']
    wa = config.get('whatsapp', {})
    template = wa.get('message_template')
    methods = wa.get('payment_methods', {})
    group_invite_url = wa.get('group_invite_url') or None

    if not template:
        print("Error: whatsapp.message_template is missing from config.json")
        sys.exit(1)

    sheets = authenticate(args.credentials)
    gid, total = fetch_tab_gid_and_total(sheets, spreadsheet_id, args.tab_name)
    sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={gid}"

    message = build_message(template, args.tab_name, total, sheet_url, methods)

    print("\n" + "=" * 60)
    print(message)
    print("=" * 60)

    if args.dry_run:
        print("\n[dry-run] Clipboard not touched, WhatsApp not opened.")
        return

    copy_to_clipboard(message)
    print("\n✓ Message copied to clipboard")

    open_whatsapp(group_invite_url)
    if group_invite_url:
        print(f"✓ Opened WhatsApp into the group ({group_invite_url})")
    else:
        print("✓ Opened WhatsApp Desktop (pick the group manually)")

    if args.no_send:
        print("→ Paste with ⌘V, then press Enter")
        return

    print(f"→ Auto-sending in ~{args.open_delay:.0f}s via AppleScript...")
    try:
        send_via_applescript(open_delay=args.open_delay)
        print("✓ Sent to WhatsApp")
    except subprocess.CalledProcessError as e:
        print(f"✗ AppleScript send failed: {e}")
        print("  Falling back to manual paste — message is on your clipboard.")
        print("  If this is the first run, grant Accessibility permission to your terminal:")
        print("    System Settings → Privacy & Security → Accessibility")
        sys.exit(1)


if __name__ == "__main__":
    main()
