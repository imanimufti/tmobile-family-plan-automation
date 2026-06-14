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
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

try:
    import pymupdf
except ImportError:
    print("PyMuPDF not installed. Installing...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pymupdf"])
    import pymupdf

import urllib.request

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.readonly',
]


def authenticate(credentials_path: str = "credentials.json"):
    """Return (sheets_service, creds). Mirrors GoogleSheetsUpdater.authenticate
    but exposes the raw Credentials so callers can hit the Drive export URL
    directly with a bearer token.
    """
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
            # Token was issued with a narrower scope set (e.g. before drive.readonly
            # was added) — force a fresh OAuth flow so the new scope is granted.
            if creds and not set(SCOPES).issubset(set(creds.scopes or [])):
                print("OAuth scopes changed (drive.readonly added) — re-authenticating...")
                token_path.unlink()
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    creds = None
            if not creds:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, 'w') as token:
                token.write(creds.to_json())
        print("Authenticated using OAuth")

    return build('sheets', 'v4', credentials=creds), creds


def fetch_tab_data(sheets, spreadsheet_id: str, tab_name: str) -> Dict:
    """Return gid, bill total, others-owe amount, and all data rows for the tab."""
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
        spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1:L25"
    ).execute()
    rows = result.get('values', [])
    if not rows:
        raise ValueError(f"Tab '{tab_name}' has no data — did Stage 2 run for it?")

    # Summary cells live in column L of the first two rows
    bill_total_raw = rows[0][11] if len(rows[0]) > 11 else ''
    others_owe_raw = rows[1][11] if len(rows) > 1 and len(rows[1]) > 11 else ''

    return {
        'gid': gid,
        'bill_total': bill_total_raw.replace('$', '').replace(',', '').strip(),
        'bill_total_display': bill_total_raw,
        'others_owe_display': others_owe_raw,
        'rows': rows,
    }


def export_tab_as_png(creds, spreadsheet_id: str, gid: int,
                      output_path: str, dpi: int = 180,
                      cell_range: str = 'A1:L15') -> None:
    """Fetch the target tab as PDF via Google's export endpoint, render to PNG
    with PyMuPDF, then crop to the actual content bbox so the result is a tight
    table image — not a letter-page with the table in the corner.

    Requires drive.readonly OAuth scope so the bearer token can authenticate.
    Landscape orientation prevents column-header truncation.
    """
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())

    params = {
        'format':       'pdf',
        'gid':          str(gid),
        'portrait':     'false',   # landscape — more horizontal room for column headers
        'size':         'letter',
        'fitw':         'true',
        'gridlines':    'true',
        'printtitle':   'false',
        'pagenumbers':  'false',
        'sheetnames':   'false',
        'range':        cell_range,
        'top_margin':   '0.25',
        'bottom_margin':'0.25',
        'left_margin':  '0.25',
        'right_margin': '0.25',
        'frozenrows':   '0',      # don't repeat the header on every page
        'frozencols':   '0',
    }
    query = '&'.join(f"{k}={v}" for k, v in params.items())
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?{query}"
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {creds.token}'})

    with urllib.request.urlopen(req) as resp:
        pdf_bytes = resp.read()

    doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    page = doc[0]

    # Find the bbox of actual content (text blocks) so we crop out the
    # letter-page whitespace below the table.
    blocks = page.get_text("blocks")
    if blocks:
        xs0 = [b[0] for b in blocks]
        ys0 = [b[1] for b in blocks]
        xs1 = [b[2] for b in blocks]
        ys1 = [b[3] for b in blocks]
        pad = 6  # points of breathing room around the table
        clip = pymupdf.Rect(
            max(0, min(xs0) - pad),
            max(0, min(ys0) - pad),
            min(page.rect.width, max(xs1) + pad),
            min(page.rect.height, max(ys1) + pad),
        )
    else:
        clip = page.rect

    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip)
    pix.save(output_path)
    doc.close()


def copy_image_to_clipboard(image_path: str) -> None:
    """Copy a PNG image to the macOS clipboard so ⌘V pastes it into apps."""
    abs_path = str(Path(image_path).resolve())
    script = f'set the clipboard to (read (POSIX file "{abs_path}") as «class PNGf»)'
    subprocess.run(['osascript', '-e', script], check=True)


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


def send_image_with_caption_via_applescript(
    image_path: str,
    caption: str,
    open_delay: float = 4.0,
    preview_delay: float = 2.5,
    caption_delay: float = 1.0,
) -> None:
    """Paste an image, then a text caption, into WhatsApp Desktop's media preview
    dialog, then send the combined message.

    WhatsApp Desktop's media-preview dialog focuses the caption field after
    paste, so we:
      1. Set image on the clipboard and ⌘V (preview opens)
      2. Swap clipboard to the caption text and ⌘V (fills the caption field)
      3. ⌘Return to send (Return alone usually adds a newline in the caption)
    """
    # 1. Image on clipboard
    copy_image_to_clipboard(image_path)

    # 2. Activate WhatsApp + paste image to open the preview dialog
    paste_image = f'''
    tell application "WhatsApp" to activate
    delay {open_delay}
    tell application "System Events"
        keystroke "v" using {{command down}}
    end tell
    '''
    subprocess.run(['osascript', '-e', paste_image], check=True)

    # Give the preview dialog time to render and focus the caption field
    time.sleep(preview_delay)

    # 3. Swap clipboard to caption text
    copy_to_clipboard(caption)

    # 4. Paste caption into focused caption field, then send with ⌘Return
    paste_and_send = f'''
    tell application "System Events"
        keystroke "v" using {{command down}}
        delay {caption_delay}
        keystroke return using {{command down}}
    end tell
    '''
    subprocess.run(['osascript', '-e', paste_and_send], check=True)


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
                        help='Open WhatsApp and copy each artifact to clipboard, but skip the auto-paste/Enter')
    parser.add_argument('--no-screenshot', action='store_true',
                        help='Skip the PNG breakdown image — send the text link message only')
    parser.add_argument('--render-only', action='store_true',
                        help='Render the PNG breakdown to a temp file and exit. Prints the path.')
    parser.add_argument('--open-delay', type=float, default=4.0,
                        help='Seconds to wait after launching WhatsApp before the first paste (default: 4.0)')
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

    sheets, creds = authenticate(args.credentials)
    data = fetch_tab_data(sheets, spreadsheet_id, args.tab_name)
    sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={data['gid']}"

    message = build_message(template, args.tab_name, data['bill_total'], sheet_url, methods)

    print("\n" + "=" * 60)
    print(message)
    print("=" * 60)

    # Export tab as PDF via Drive and render to PNG (default on)
    png_path: Optional[str] = None
    if not args.no_screenshot:
        slug = args.tab_name.replace(' ', '_')
        png_path = str(Path(tempfile.gettempdir()) / f"tmobile-{slug}.png")
        export_tab_as_png(creds, spreadsheet_id, data['gid'], png_path)
        print(f"\n✓ Screenshot exported from Google Sheets: {png_path}")

    if args.render_only:
        print("[render-only] Exiting before clipboard/WhatsApp.")
        return

    if args.dry_run:
        print("\n[dry-run] Clipboard not touched, WhatsApp not opened.")
        return

    open_whatsapp(group_invite_url)
    if group_invite_url:
        print(f"✓ Opened WhatsApp into the group ({group_invite_url})")
    else:
        print("✓ Opened WhatsApp Desktop (pick the group manually)")

    if png_path:
        # Single message: image with the text/link as its caption.
        if args.no_send:
            copy_image_to_clipboard(png_path)
            print("✓ Screenshot copied to clipboard")
            print("→ ⌘V to paste image (preview opens), type/paste your caption, ⌘Return to send")
            return
        print(f"→ Sending image + caption in ~{args.open_delay:.0f}s...")
        try:
            send_image_with_caption_via_applescript(
                png_path, message,
                open_delay=args.open_delay,
            )
            print("✓ Sent image with caption")
        except subprocess.CalledProcessError as e:
            print(f"✗ AppleScript send failed: {e}")
            print("  Image was on your clipboard; the caption text is now there.")
            print("  If this is the first run, grant Accessibility permission:")
            print("    System Settings → Privacy & Security → Accessibility")
            sys.exit(1)
    else:
        # Text-only fallback (--no-screenshot)
        copy_to_clipboard(message)
        print("✓ Text message copied to clipboard")
        if args.no_send:
            print("→ Paste with ⌘V, then Enter to send the link message")
            return
        print(f"→ Sending text message in ~{args.open_delay:.0f}s...")
        try:
            send_via_applescript(open_delay=args.open_delay, after_paste_delay=1.0)
            print("✓ Text message sent")
        except subprocess.CalledProcessError as e:
            print(f"✗ AppleScript send failed: {e}")
            print("  Text message is on your clipboard — paste manually with ⌘V + Enter.")
            sys.exit(1)


if __name__ == "__main__":
    main()
