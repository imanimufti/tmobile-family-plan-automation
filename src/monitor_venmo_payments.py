#!/usr/bin/env python3
"""
Unified Payment Monitor

Pulls payment notifications from multiple sources and updates the Google Sheet
payment status when a payment matches a person on the bill.

Sources:
  - venmo_email  Gmail search for Venmo "<name> paid you $X.XX" notifications
  - zelle_sms    macOS chat.db SMS from the bank ("<Name> sent you $X.XX using Zelle")
  - apple_cash   macOS chat.db Apple Cash peer payment balloons (amount in plist)
  - venmo_sms    macOS chat.db SMS from Venmo (currently no transaction texts; hook for future)
"""

import base64
import json
import os
import plistlib
import re
import sqlite3
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

try:
    from google.oauth2.credentials import Credentials
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("Google API libraries not installed. Installing...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "google-auth", "google-auth-oauthlib",
                           "google-auth-httplib2", "google-api-python-client"])
    from google.oauth2.credentials import Credentials
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError


APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01


# ---------------------------------------------------------------------------
# Payment sources
# ---------------------------------------------------------------------------

class PaymentSource(ABC):
    """A source produces payment records: {source, amount, sender_name, sender_phone, raw_id, subject, date}."""

    name: str = "abstract"

    @abstractmethod
    def fetch(self, days_back: int) -> List[Dict]:
        ...


class VenmoEmailSource(PaymentSource):
    """Searches Gmail for Venmo 'paid you' notifications."""

    name = "venmo_email"

    def __init__(self, gmail_service):
        self.gmail = gmail_service

    def fetch(self, days_back: int) -> List[Dict]:
        try:
            after_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y/%m/%d')
            query = f'from:venmo@venmo.com subject:"paid you" after:{after_date}'

            results = self.gmail.users().messages().list(userId='me', q=query).execute()
            messages = results.get('messages', [])
            print(f"[venmo_email] Found {len(messages)} Venmo notification(s)")

            payments = []
            for m in messages:
                parsed = self._parse_email(m['id'])
                if parsed:
                    payments.append(parsed)
            return payments
        except HttpError as err:
            print(f"[venmo_email] Error: {err}")
            return []

    def _parse_email(self, message_id: str) -> Optional[Dict]:
        try:
            message = self.gmail.users().messages().get(
                userId='me', id=message_id, format='full'
            ).execute()
            payload = message.get('payload', {})
            headers = payload.get('headers', [])

            subject = ''
            date = ''
            for h in headers:
                if h['name'] == 'Subject':
                    subject = h['value']
                elif h['name'] == 'Date':
                    date = h['value']

            pattern = r'(.+?)\s+paid you\s+\$?([\d,]+\.\d{2})'
            match = re.search(pattern, subject, re.IGNORECASE)
            if not match:
                body = _get_gmail_body(payload) or ''
                match = re.search(pattern, body, re.IGNORECASE)

            if not match:
                return None

            return {
                'source': self.name,
                'amount': float(match.group(2).replace(',', '')),
                'sender_name': match.group(1).strip(),
                'sender_phone': None,
                'raw_id': message_id,
                'subject': subject,
                'date': date,
            }
        except HttpError as err:
            print(f"[venmo_email] Error parsing {message_id}: {err}")
            return None


class SmsSource(PaymentSource):
    """Reads ~/Library/Messages/chat.db for payment notifications.

    Dispatches each row to a per-provider parser based on sender/balloon bundle.
    """

    name = "sms"

    def __init__(self, chat_db_path: str, sms_config: Dict):
        self.chat_db_path = os.path.expanduser(chat_db_path)
        self.zelle_senders = set(sms_config.get('zelle_senders', []))
        self.venmo_senders = set(sms_config.get('venmo_senders', []))

    def fetch(self, days_back: int) -> List[Dict]:
        if not os.path.exists(self.chat_db_path):
            print(f"[sms] chat.db not found at {self.chat_db_path} — skipping")
            return []

        cutoff_apple_ns = int((time.time() - days_back * 86400 - APPLE_EPOCH_OFFSET) * 1e9)

        try:
            con = sqlite3.connect(f"file:{self.chat_db_path}?mode=ro", uri=True)
        except sqlite3.Error as err:
            print(f"[sms] Cannot open chat.db (Full Disk Access required?): {err}")
            return []

        try:
            cur = con.cursor()
            cur.execute(
                """
                SELECT m.ROWID, h.id, m.is_from_me, m.text, m.balloon_bundle_id,
                       m.payload_data, m.date
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.rowid
                WHERE m.date > ?
                  AND m.is_from_me = 0
                ORDER BY m.date DESC
                """,
                (cutoff_apple_ns,),
            )
            rows = cur.fetchall()
        except sqlite3.Error as err:
            # FDA is sometimes denied at query time, not connect time.
            print(f"[sms] chat.db query failed (Full Disk Access required?): {err}")
            return []
        finally:
            con.close()

        payments: List[Dict] = []
        for rowid, sender, _from_me, text, bundle, payload, date_ns in rows:
            ts = self._apple_ns_to_iso(date_ns)
            parsed = None

            if bundle and 'PeerPayment' in bundle:
                parsed = self._parse_apple_cash(rowid, sender, payload, ts)
            elif sender and sender in self.zelle_senders and text:
                parsed = self._parse_zelle(rowid, sender, text, ts)
            elif sender and sender in self.venmo_senders and text:
                parsed = self._parse_venmo_sms(rowid, sender, text, ts)

            if parsed:
                payments.append(parsed)

        by_provider: Dict[str, int] = {}
        for p in payments:
            by_provider[p['source']] = by_provider.get(p['source'], 0) + 1
        if by_provider:
            summary = ", ".join(f"{k}: {v}" for k, v in by_provider.items())
            print(f"[sms] Found {len(payments)} payment(s): {summary}")
        else:
            print("[sms] No SMS payments matched")
        return payments

    @staticmethod
    def _apple_ns_to_iso(date_ns: Optional[int]) -> str:
        if not date_ns:
            return ''
        try:
            return datetime.fromtimestamp(date_ns / 1e9 + APPLE_EPOCH_OFFSET).isoformat()
        except Exception:
            return ''

    @staticmethod
    def _parse_zelle(rowid: int, sender: str, text: str, ts: str) -> Optional[Dict]:
        # "USC Credit Union: Yusuf Albazian sent you $5.00 using Zelle..."
        # "USC Credit U: Saim Sajjad sent you $34.60 using Zelle for 'T-mobile'..."
        pattern = r':\s*(.+?)\s+sent you\s+\$?([\d,]+(?:\.\d{2})?)\s+using Zelle'
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            return None
        return {
            'source': 'zelle_sms',
            'amount': float(m.group(2).replace(',', '')),
            'sender_name': m.group(1).strip(),
            'sender_phone': None,
            'raw_id': f"sms:{rowid}",
            'subject': text[:120],
            'date': ts,
        }

    @staticmethod
    def _parse_venmo_sms(rowid: int, sender: str, text: str, ts: str) -> Optional[Dict]:
        # Try several known Venmo SMS shapes. Whichever matches first wins.
        # Skip auth codes and welcome blurbs.
        if re.search(r'\bcode\b|never share|welcome to venmo', text, re.IGNORECASE):
            return None

        patterns = [
            r'(.+?)\s+paid you\s+\$?([\d,]+(?:\.\d{2})?)',                       # "X paid you $Y"
            r'you received\s+\$?([\d,]+(?:\.\d{2})?)\s+from\s+(.+?)(?:[.,(\n]|$)',# "You received $Y from X"
            r'\$?([\d,]+(?:\.\d{2})?)\s+(?:received )?from\s+(.+?)\s+(?:via|on)\s+Venmo',
        ]
        amount = name = None
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if not m:
                continue
            g1, g2 = m.group(1), m.group(2)
            # First pattern: (name, amount). Others: (amount, name).
            if re.match(r'^\$?[\d,]+', g1):
                amount, name = g1, g2
            else:
                name, amount = g1, g2
            break

        if not amount or not name:
            return None
        return {
            'source': 'venmo_sms',
            'amount': float(amount.replace(',', '').replace('$', '')),
            'sender_name': name.strip().lstrip('@'),
            'sender_phone': None,
            'raw_id': f"sms:{rowid}",
            'subject': text[:120],
            'date': ts,
        }

    @staticmethod
    def _parse_apple_cash(rowid: int, sender: str, payload: bytes, ts: str) -> Optional[Dict]:
        # Apple Cash peer-payment balloon: text is empty; amount lives in payload_data
        # as an NSKeyedArchiver plist. We pull the ldtext string ("Sent $X with Apple Cash.").
        if not payload or not sender:
            return None
        try:
            pl = plistlib.loads(payload)
        except Exception:
            return None

        objects = pl.get('$objects', []) if isinstance(pl, dict) else []
        ldtext = next(
            (o for o in objects if isinstance(o, str)
             and 'Apple' in o and 'Cash' in o and '$' in o),
            None,
        )
        if not ldtext:
            return None

        m = re.search(r'\$([\d,]+(?:\.\d{1,2})?)', ldtext)
        if not m:
            return None

        return {
            'source': 'apple_cash',
            'amount': float(m.group(1).replace(',', '')),
            'sender_name': None,
            'sender_phone': sender,
            'raw_id': f"sms:{rowid}",
            'subject': ldtext,
            'date': ts,
        }


def _get_gmail_body(payload: Dict) -> str:
    body = ""
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                break
            if part['mimeType'] == 'text/html' and not body and 'data' in part['body']:
                html = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                body = re.sub(r'<[^>]+>', '', html)
    elif 'body' in payload and 'data' in payload['body']:
        body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')
    return body


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class PaymentMonitor:
    """Monitors all configured payment sources and updates the Google Sheet."""

    SOURCE_CHOICES = ('venmo_email', 'sms', 'all')

    def __init__(self, config_path: str = "src/config.json"):
        with open(config_path, 'r') as f:
            self.config = json.load(f)

        self.sheet_id = self.config['google_sheet_id']
        self.phone_mapping = self.config.get('phone_to_name_mapping', {})
        self.sms_config = self.config.get('sms', {})
        self.sms_payer_aliases = self.sms_config.get('sms_payer_aliases', {})

        self.gmail_service = None
        self.sheets_service = None

    def authenticate(self, credentials_path: str = "credentials.json"):
        SCOPES = [
            'https://www.googleapis.com/auth/gmail.readonly',
            'https://www.googleapis.com/auth/spreadsheets',
        ]
        creds = None
        creds_file = Path(credentials_path)

        if not creds_file.exists():
            raise FileNotFoundError(
                f"Credentials file not found: {credentials_path}\n"
                "See README for setup steps."
            )

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

        self.gmail_service = build('gmail', 'v1', credentials=creds)
        self.sheets_service = build('sheets', 'v4', credentials=creds)

    # -- Sources --------------------------------------------------------

    def build_sources(self, selection: str) -> List[PaymentSource]:
        sources: List[PaymentSource] = []
        if selection in ('venmo_email', 'all'):
            if not self.gmail_service:
                print("Skipping venmo_email (Gmail not authenticated)")
            else:
                sources.append(VenmoEmailSource(self.gmail_service))
        if selection in ('sms', 'all'):
            chat_db = self.sms_config.get('chat_db_path', '~/Library/Messages/chat.db')
            sources.append(SmsSource(chat_db, self.sms_config))
        return sources

    def collect_payments(self, sources: List[PaymentSource], days_back: int) -> List[Dict]:
        payments: List[Dict] = []
        for src in sources:
            print(f"\nFetching from {src.name} (last {days_back} days)...")
            payments.extend(src.fetch(days_back))
        return payments

    # -- Sheet I/O ------------------------------------------------------

    def detect_current_tab(self) -> Optional[str]:
        """Pick the most recent monthly tab (e.g. 'May 26') from the sheet —
        the one whose <Mon> <YY> is <= today. Falls back to the closest
        prior month if the current month hasn't been processed yet.
        Returns None if no monthly tabs exist.
        """
        try:
            meta = self.sheets_service.spreadsheets().get(
                spreadsheetId=self.sheet_id
            ).execute()
        except HttpError as err:
            print(f"Cannot list tabs to auto-detect: {err}")
            return None

        month_pat = re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{2})$')
        monthly_tabs = []
        for sheet in meta.get('sheets', []):
            name = sheet['properties']['title']
            m = month_pat.match(name)
            if not m:
                continue
            mon_str, yr_str = m.groups()
            mon_num = datetime.strptime(mon_str, '%b').month
            yr_num = 2000 + int(yr_str)
            monthly_tabs.append((datetime(yr_num, mon_num, 1), name))

        if not monthly_tabs:
            return None

        today = datetime.now()
        not_future = [(d, n) for d, n in monthly_tabs if d <= today]
        pool = not_future if not_future else monthly_tabs
        pool.sort(reverse=True)
        return pool[0][1]

    def get_sheet_data(self, tab_name: str) -> List[Dict]:
        try:
            range_name = f"{tab_name}!A:H"
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id, range=range_name
            ).execute()
            values = result.get('values', [])
            if not values or len(values) < 2:
                print(f"No data found in tab '{tab_name}'")
                return []

            sheet_data = []
            for i, row in enumerate(values[1:], start=2):
                if len(row) < 7 or row[0] == 'Total':
                    continue
                total_str = row[6] if len(row) > 6 else '$0.00'
                payment_status = row[7] if len(row) > 7 else 'Pending'
                total_amount = float(total_str.replace('$', '').replace(',', '')) if total_str else 0.0
                sheet_data.append({
                    'row_index': i,
                    'name': row[0],
                    'account': row[1] if len(row) > 1 else '',
                    'total': total_amount,
                    'payment_status': payment_status,
                })
            return sheet_data
        except HttpError as err:
            print(f"Error reading sheet: {err}")
            return []

    def update_payment_status(self, tab_name: str, row_index: int, status: str = "Paid") -> bool:
        try:
            range_name = f"{tab_name}!H{row_index}"
            body = {'values': [[status]]}
            self.sheets_service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id, range=range_name,
                valueInputOption='USER_ENTERED', body=body,
            ).execute()
            print(f"  ✓ Updated row {row_index} to '{status}'")
            return True
        except HttpError as err:
            print(f"  ✗ Error updating sheet: {err}")
            return False

    # -- Matching -------------------------------------------------------

    def _resolve_sender_label(self, payment: Dict) -> Optional[str]:
        """Return a string to substring-match against the sheet name."""
        if payment.get('sender_name'):
            return payment['sender_name']

        phone = payment.get('sender_phone')
        if not phone:
            return None

        # Try sms_payer_aliases first (full or last-4 keys)
        if phone in self.sms_payer_aliases:
            return self.sms_payer_aliases[phone]
        last4 = re.sub(r'\D', '', phone)[-4:]
        if last4 and last4 in self.sms_payer_aliases:
            return self.sms_payer_aliases[last4]

        # Fall back to T-Mobile line mapping
        if last4 and last4 in self.phone_mapping:
            return self.phone_mapping[last4]
        return None

    def match_payment_to_person(self, payment: Dict, sheet_data: List[Dict]) -> Optional[Dict]:
        amount = payment['amount']
        sender_label = self._resolve_sender_label(payment)
        if not sender_label:
            return None

        sender_lower = sender_label.lower()
        for person in sheet_data:
            if person['payment_status'].lower() == 'paid':
                continue
            if abs(person['total'] - amount) >= 0.01:
                continue
            if person['name'].lower() in sender_lower:
                print(f"  ✓ Match: {person['name']} (${amount:.2f}) via {payment['source']}")
                print(f"    From: {sender_label}")
                return person
        return None

    # -- Top level ------------------------------------------------------

    def process_payments(self, tab_name: str, days_back: int = 7,
                         source_selection: str = 'all', dry_run: bool = False):
        sources = self.build_sources(source_selection)
        if not sources:
            print("No sources configured.")
            return

        payments = self.collect_payments(sources, days_back)
        if not payments:
            print("\nNo payment notifications found")
            return

        if dry_run or not self.sheets_service:
            print(f"\n{'='*60}\nDry-run: {len(payments)} payment(s) parsed\n{'='*60}")
            for p in payments:
                label = self._resolve_sender_label(p) or '(unresolved)'
                print(f"  [{p['source']:11}] ${p['amount']:>8.2f}  {label}  ({p['date']})")
            return

        print(f"\nReading Google Sheet tab '{tab_name}'...")
        sheet_data = self.get_sheet_data(tab_name)
        if not sheet_data:
            print("No pending payments in sheet")
            return

        print(f"\nMatching {len(payments)} payment(s) against {len(sheet_data)} person(s)...")
        matched = 0
        for p in payments:
            label = self._resolve_sender_label(p) or '(unresolved)'
            print(f"\nProcessing [{p['source']}]: ${p['amount']:.2f} from {label}")
            person = self.match_payment_to_person(p, sheet_data)
            if person:
                if self.update_payment_status(tab_name, person['row_index']):
                    matched += 1
                    person['payment_status'] = 'Paid'
            else:
                print("  ✗ No match (amount or name mismatch)")

        print(f"\n{'='*60}\nSummary: Updated {matched} payment(s)\n{'='*60}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Monitor payment sources (Venmo email, SMS) and update the Google Sheet')
    parser.add_argument('tab_name', nargs='?', default=None,
                        help='Sheet tab name (e.g., "Mar 26"). Omit to auto-detect the most recent monthly tab.')
    parser.add_argument('--days', type=int, default=7, help='Days to search back (default: 7)')
    parser.add_argument('--credentials', default='credentials.json', help='Path to credentials.json')
    parser.add_argument('--source', choices=PaymentMonitor.SOURCE_CHOICES, default='all',
                        help='Which source(s) to pull from')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print parsed payments without touching the sheet')
    parser.add_argument('--watch', action='store_true', help='Run continuously')
    parser.add_argument('--interval', type=int, default=300,
                        help='Check interval in seconds for --watch (default: 300)')
    args = parser.parse_args()

    try:
        monitor = PaymentMonitor()
        # SMS-only dry-run doesn't need Google auth (and can't auto-detect)
        needs_google = args.source != 'sms' or not args.dry_run
        if needs_google:
            monitor.authenticate(args.credentials)

        tab_name = args.tab_name
        if not tab_name:
            if not monitor.sheets_service:
                raise SystemExit("tab_name required when running without Google auth")
            tab_name = monitor.detect_current_tab()
            if not tab_name:
                raise SystemExit("Could not auto-detect any monthly tab in the sheet")
            print(f"Auto-detected current tab: '{tab_name}'")

        if args.watch:
            print(f"Watch mode: checking every {args.interval}s. Ctrl+C to stop.")
            while True:
                try:
                    monitor.process_payments(tab_name, args.days,
                                             args.source, args.dry_run)
                    print(f"\nWaiting {args.interval} seconds...")
                    time.sleep(args.interval)
                except KeyboardInterrupt:
                    print("\nStopping watch mode...")
                    break
        else:
            monitor.process_payments(tab_name, args.days,
                                     args.source, args.dry_run)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
