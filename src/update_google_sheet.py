#!/usr/bin/env python3
"""
Google Sheets Integration for T-Mobile Bill Data
Updates Google Sheet with parsed bill data, creating a new tab for each month
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List
import re

try:
    from google.oauth2.credentials import Credentials
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("Google API libraries not installed. Installing...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "google-auth", "google-auth-oauthlib", "google-auth-httplib2", "google-api-python-client"])
    from google.oauth2.credentials import Credentials
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

from parse_tmobile_bill import TMobileBillParser


class GoogleSheetsUpdater:
    """Updates Google Sheets with T-Mobile bill data"""

    def __init__(self, config_path: str = "src/config.json"):
        # Load configuration
        with open(config_path, 'r') as f:
            self.config = json.load(f)

        self.sheet_id = self.config['google_sheet_id']
        self.phone_mapping = self.config['phone_to_name_mapping']
        self.service = None

    def authenticate(self, credentials_path: str = "credentials.json"):
        """Authenticate with Google Sheets API"""
        SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

        creds = None
        creds_file = Path(credentials_path)

        if creds_file.exists():
            # Try service account first
            try:
                creds = service_account.Credentials.from_service_account_file(
                    credentials_path, scopes=SCOPES)
                print("Authenticated using service account")
            except Exception:
                # Fall back to OAuth
                from google_auth_oauthlib.flow import InstalledAppFlow
                from google.auth.transport.requests import Request

                token_path = Path("token.json")
                if token_path.exists():
                    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

                if not creds or not creds.valid:
                    if creds and creds.expired and creds.refresh_token:
                        from google.auth.transport.requests import Request
                        creds.refresh(Request())
                    else:
                        flow = InstalledAppFlow.from_client_secrets_file(
                            credentials_path, SCOPES)
                        creds = flow.run_local_server(port=0)

                    # Save credentials
                    with open(token_path, 'w') as token:
                        token.write(creds.to_json())
                print("Authenticated using OAuth")
        else:
            raise FileNotFoundError(
                f"Credentials file not found: {credentials_path}\n"
                "Please follow these steps:\n"
                "1. Go to https://console.cloud.google.com/\n"
                "2. Create a new project or select existing\n"
                "3. Enable Google Sheets API\n"
                "4. Create credentials (Service Account or OAuth 2.0)\n"
                "5. Download and save as 'credentials.json' in project root"
            )

        self.service = build('sheets', 'v4', credentials=creds)

    def extract_bill_month(self, pdf_path: str) -> str:
        """Extract month and year from PDF to create tab name"""
        # Try to extract from filename first (e.g., SummaryBillMar2026.pdf)
        filename = Path(pdf_path).stem
        month_match = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(\d{4})', filename, re.IGNORECASE)

        if month_match:
            month_name = month_match.group(1).capitalize()
            year = month_match.group(2)
            # Convert to "Mar 26" format
            year_short = year[2:]  # Get last 2 digits
            return f"{month_name[:3]} {year_short}"

        # If not in filename, use current month
        now = datetime.now()
        return now.strftime("%b %y")

    def create_or_get_sheet(self, tab_name: str) -> int:
        """Create a new sheet tab or get existing sheet ID"""
        try:
            # Get all sheets
            sheet_metadata = self.service.spreadsheets().get(
                spreadsheetId=self.sheet_id
            ).execute()

            sheets = sheet_metadata.get('sheets', [])

            # Check if tab already exists
            for sheet in sheets:
                if sheet['properties']['title'] == tab_name:
                    print(f"Tab '{tab_name}' already exists, will update it")
                    return sheet['properties']['sheetId']

            # Create new sheet
            requests = [{
                'addSheet': {
                    'properties': {
                        'title': tab_name
                    }
                }
            }]

            response = self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={'requests': requests}
            ).execute()

            new_sheet_id = response['replies'][0]['addSheet']['properties']['sheetId']
            print(f"Created new tab: {tab_name}")
            return new_sheet_id

        except HttpError as error:
            print(f"Error creating/getting sheet: {error}")
            raise

    def format_sheet_header(self, tab_name: str):
        """Format the header row with proper styling"""
        requests = [
            # Format header row (bold, background color)
            {
                'repeatCell': {
                    'range': {
                        'sheetId': self.get_sheet_id(tab_name),
                        'startRowIndex': 0,
                        'endRowIndex': 1
                    },
                    'cell': {
                        'userEnteredFormat': {
                            'backgroundColor': {'red': 0.9, 'green': 0.9, 'blue': 0.9},
                            'textFormat': {'bold': True}
                        }
                    },
                    'fields': 'userEnteredFormat(backgroundColor,textFormat)'
                }
            },
            # Freeze header row
            {
                'updateSheetProperties': {
                    'properties': {
                        'sheetId': self.get_sheet_id(tab_name),
                        'gridProperties': {'frozenRowCount': 1}
                    },
                    'fields': 'gridProperties.frozenRowCount'
                }
            }
        ]

        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={'requests': requests}
        ).execute()

    def get_sheet_id(self, tab_name: str) -> int:
        """Get the sheet ID for a given tab name"""
        sheet_metadata = self.service.spreadsheets().get(
            spreadsheetId=self.sheet_id
        ).execute()

        for sheet in sheet_metadata.get('sheets', []):
            if sheet['properties']['title'] == tab_name:
                return sheet['properties']['sheetId']

        return None

    def update_sheet(self, bill_data: Dict, tab_name: str):
        """Update Google Sheet with bill data"""
        # Create or get sheet
        sheet_id = self.create_or_get_sheet(tab_name)

        # Prepare data rows
        values = [
            # Header row - added Payment Status column
            ['Name', 'Account', 'Equal portion of bill', 'Recurring Extras', 'Extras', 'Credit', 'Total per person', 'Payment Status', 'Notes']
        ]

        # Build per-owner Mobile Internet add-ons. Any Mobile Internet line whose
        # last4 has an owner mapping is folded into that owner's recurring extras
        # instead of getting its own row.
        from decimal import Decimal
        mi_owners = self.config.get('mobile_internet_owners', {})
        mi_extras_by_owner: Dict[str, Decimal] = {}
        for line in bill_data['lines']:
            if line['line_type'] != 'Mobile Internet':
                continue
            owner_last4 = mi_owners.get(line['last4'])
            if not owner_last4:
                continue
            mi_extras_by_owner[owner_last4] = (
                mi_extras_by_owner.get(owner_last4, Decimal('0')) + line['total']
            )

        # Data rows - map phone numbers to names
        for line in bill_data['lines']:
            if line['line_type'] == 'Account':
                continue
            # Skip Mobile Internet lines whose cost is folded into an owner row
            if line['line_type'] == 'Mobile Internet' and mi_owners.get(line['last4']):
                continue

            last4 = line['last4']
            name = self.phone_mapping.get(last4, f"Unknown ({last4})")

            mi_extra = mi_extras_by_owner.get(last4, Decimal('0'))
            equipment_amount = line['equipment'] + mi_extra
            total_amount = line.get('total_per_person', line['total']) + mi_extra

            equal_portion = f"${line.get('equal_portion', 0):.2f}"
            equipment = f"${equipment_amount:.2f}" if equipment_amount > 0 else ""
            one_time = f"${line['one_time_charges']:.2f}" if line['one_time_charges'] > 0 else ""
            credit = "$0.00"
            total = f"${total_amount:.2f}"
            payment_status = "Pending"  # Default to Pending, will be updated by Gmail monitor
            if line['is_removed']:
                notes = f"Removed — ${line['total']:.2f} split equally across active lines"
            elif mi_extra > 0:
                notes = f"Includes ${mi_extra:.2f} Mobile Internet"
            else:
                notes = ""

            values.append([
                name,
                last4,
                equal_portion,
                equipment,
                one_time,
                credit,
                total,
                payment_status,
                notes
            ])

        # Add totals row
        values.append([
            'Total',
            '',
            f"${bill_data['plans_total']:.2f}",
            f"${bill_data['equipment_total']:.2f}",
            f"${bill_data['one_time_total']:.2f}",
            '',
            f"${bill_data['total_due']:.2f}",
            '',
            ''
        ])

        # Add summary info (starting from column I to avoid interfering with data)
        values[0].extend(['', 'Bill Total:', f"${bill_data['total_due']:.2f}"])

        # Total payment due from others = Bill Total - account holder's share
        # (their voice-line total plus any Mobile Internet folded onto their row).
        account_holder_last4 = self.config.get('account_holder_last4')
        holder_line = next(
            (l for l in bill_data['lines']
             if l['line_type'] == 'Voice' and l['last4'] == account_holder_last4),
            None,
        )
        holder_share = Decimal('0')
        if holder_line:
            holder_share = (
                holder_line.get('total_per_person', holder_line['total'])
                + mi_extras_by_owner.get(account_holder_last4, Decimal('0'))
            )
        total_from_others = bill_data['total_due'] - holder_share

        if len(values) > 1:
            values[1].extend(['', 'Total Payment Due from Others', f"${total_from_others:.2f}"])

        # Add number of lines
        num_active_lines = len([l for l in bill_data['lines'] if l['line_type'] == 'Voice' and not l['is_removed']])
        if len(values) > 2:
            values[2].extend(['', 'Number of lines', str(num_active_lines)])

        # Update the sheet
        range_name = f"{tab_name}!A1"
        body = {'values': values}

        self.service.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=range_name,
            valueInputOption='USER_ENTERED',
            body=body
        ).execute()

        print(f"✓ Updated Google Sheet tab '{tab_name}' with {len(values)-1} rows")

        # Format the sheet
        self.format_sheet_header(tab_name)


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python update_google_sheet.py <path_to_bill.pdf> [credentials.json]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    credentials_path = sys.argv[2] if len(sys.argv) > 2 else "credentials.json"

    if not Path(pdf_path).exists():
        print(f"Error: PDF file not found: {pdf_path}")
        sys.exit(1)

    try:
        # Parse the PDF
        print(f"Parsing bill: {pdf_path}")
        parser = TMobileBillParser(pdf_path)
        bill_data = parser.parse()
        parser.print_summary()

        # Update Google Sheet
        print("\nConnecting to Google Sheets...")
        updater = GoogleSheetsUpdater()
        updater.authenticate(credentials_path)

        tab_name = updater.extract_bill_month(pdf_path)
        print(f"Updating tab: {tab_name}")

        updater.update_sheet(bill_data, tab_name)

        print(f"\n✓ Successfully updated Google Sheet!")
        print(f"  Sheet ID: {updater.sheet_id}")
        print(f"  Tab: {tab_name}")
        print(f"  URL: https://docs.google.com/spreadsheets/d/{updater.sheet_id}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
