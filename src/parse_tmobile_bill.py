#!/usr/bin/env python3
"""
T-Mobile Bill Parser
Extracts per-line costs from T-Mobile PDF bills
"""

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional
from decimal import Decimal

try:
    import pymupdf  # PyMuPDF (fitz)
except ImportError:
    print("PyMuPDF not installed. Installing...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pymupdf"])
    import pymupdf


class TMobileBillParser:
    """Parser for T-Mobile PDF bills"""

    def __init__(self, pdf_path: str):
        self.pdf_path = Path(pdf_path)
        self.bill_data = {
            'total_due': Decimal('0'),
            'plans_total': Decimal('0'),
            'equipment_total': Decimal('0'),
            'services_total': Decimal('0'),
            'one_time_total': Decimal('0'),
            'lines': []
        }

    def parse(self) -> Dict:
        """Parse the PDF and extract billing information"""
        doc = pymupdf.open(self.pdf_path)

        # Extract text from page 2 (index 1) where the summary is
        if len(doc) < 2:
            raise ValueError("PDF doesn't have enough pages")

        page = doc[1]  # Page 2 (0-indexed)
        text = page.get_text()

        # Parse the bill summary table
        self._parse_bill_summary(text)

        # Calculate equal portion of bill
        self._calculate_equal_portions()

        doc.close()

        return self.bill_data

    def _parse_bill_summary(self, text: str):
        """Parse the 'THIS BILL SUMMARY' section"""

        # Find the summary section
        summary_match = re.search(r'THIS BILL SUMMARY(.+?)DETAILED CHARGES', text, re.DOTALL)
        if not summary_match:
            raise ValueError("Could not find 'THIS BILL SUMMARY' section")

        summary_text = summary_match.group(1)

        # Parse totals line
        totals_match = re.search(
            r'Totals\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})',
            summary_text
        )

        if totals_match:
            self.bill_data['plans_total'] = Decimal(totals_match.group(1).replace(',', ''))
            self.bill_data['equipment_total'] = Decimal(totals_match.group(2).replace(',', ''))
            self.bill_data['services_total'] = Decimal(totals_match.group(3).replace(',', ''))
            self.bill_data['one_time_total'] = Decimal(totals_match.group(4).replace(',', ''))
            self.bill_data['total_due'] = Decimal(totals_match.group(5).replace(',', ''))

        # Parse individual lines
        # Pattern for lines like: (832) 768-4440 Voice $5.66 $9.84 - - $15.50
        # Also catches "Mobile Internet" rows (e.g. the mobile internet line).
        # Longest alternative goes first so the regex engine doesn't stop at "Mobile".
        # Trailing `- Removed` / `- New` / `- Changed` modifier on the phone row is optional
        # and captured loosely so the regex doesn't silently drop newly-added lines.
        line_pattern = r'\((\d{3})\)\s*(\d{3})-(\d{4})(?:\s*-\s*\w+)?\s+(Mobile\s+Internet|Voice|Data|Account)?\s+\$?([\d,]+\.\d{2}|-)?\s+\$?([\d,]+\.\d{2}|-)?\s+\$?([\d,]+\.\d{2}|-)?\s+\$?([\d,]+\.\d{2}|-)?\s+\$?([\d,]+\.\d{2})'

        for match in re.finditer(line_pattern, summary_text):
            area_code, prefix, last4, line_type_raw, plans, equipment, services, one_time, total = match.groups()

            phone = f"({area_code}) {prefix}-{last4}"
            line_type = re.sub(r'\s+', ' ', (line_type_raw or 'Voice')).strip()

            line_data = {
                'phone': phone,
                'last4': last4,
                'line_type': line_type,
                'plans': self._parse_amount(plans),
                'equipment': self._parse_amount(equipment),
                'services': self._parse_amount(services),
                'one_time_charges': self._parse_amount(one_time),
                'total': self._parse_amount(total),
                'is_removed': 'Removed' in match.group(0)
            }

            self.bill_data['lines'].append(line_data)

        # Parse Account line separately
        account_match = re.search(
            r'Account\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2}|-)?\s+\$?([\d,]+\.\d{2}|-)?\s+\$?([\d,]+\.\d{2}|-)?\s+\$?([\d,]+\.\d{2})',
            summary_text
        )

        if account_match:
            account_data = {
                'phone': 'Account',
                'last4': 'ACCT',
                'line_type': 'Account',
                'plans': self._parse_amount(account_match.group(1)),
                'equipment': self._parse_amount(account_match.group(2)),
                'services': self._parse_amount(account_match.group(3)),
                'one_time_charges': self._parse_amount(account_match.group(4)),
                'total': self._parse_amount(account_match.group(5)),
                'is_removed': False
            }
            # Insert at beginning
            self.bill_data['lines'].insert(0, account_data)

    def _parse_amount(self, amount_str: Optional[str]) -> Decimal:
        """Parse amount string to Decimal"""
        if not amount_str or amount_str == '-' or amount_str.strip() == '':
            return Decimal('0')

        # Remove $ and commas
        cleaned = amount_str.replace('$', '').replace(',', '').strip()
        return Decimal(cleaned) if cleaned else Decimal('0')

    def _calculate_equal_portions(self):
        """Calculate equal portion of bill for each active voice line.

        Removed voice lines have their per-line total folded into the shared
        pool so the burden splits across active lines.

        Mobile Internet lines stay on their own row at full price — their
        contribution to plans_total is subtracted from the pool so the voice
        lines don't subsidize them.
        """
        active_voice_lines = [
            line for line in self.bill_data['lines']
            if line['line_type'] == 'Voice' and not line['is_removed']
        ]
        removed_voice_lines = [
            line for line in self.bill_data['lines']
            if line['line_type'] == 'Voice' and line['is_removed']
        ]
        mobile_internet_lines = [
            line for line in self.bill_data['lines']
            if line['line_type'] == 'Mobile Internet'
        ]

        num_active_lines = len(active_voice_lines)
        unused_burden = sum((line['total'] for line in removed_voice_lines), Decimal('0'))
        mobile_internet_plans = sum((line['plans'] for line in mobile_internet_lines), Decimal('0'))

        if num_active_lines > 0:
            divisible_pool = self.bill_data['plans_total'] + unused_burden - mobile_internet_plans
            equal_portion = divisible_pool / num_active_lines

            for line in self.bill_data['lines']:
                if line['line_type'] == 'Voice' and not line['is_removed']:
                    line['equal_portion'] = equal_portion
                    line['total_per_person'] = (
                        equal_portion +
                        line['equipment'] +
                        line['one_time_charges']
                    )
                elif line['line_type'] == 'Voice' and line['is_removed']:
                    # Cost is redistributed; zero out the row so it doesn't double-bill.
                    line['equal_portion'] = Decimal('0')
                    line['total_per_person'] = Decimal('0')
                elif line['line_type'] == 'Mobile Internet':
                    # Charged in full to the mapped person; not split.
                    line['equal_portion'] = Decimal('0')
                    line['total_per_person'] = line['total']
                else:
                    line['equal_portion'] = Decimal('0')
                    line['total_per_person'] = line['total']

    def to_dict(self) -> Dict:
        """Return parsed data as dictionary"""
        return self.bill_data

    def print_summary(self):
        """Print a formatted summary of the bill"""
        print("\n" + "="*80)
        print(f"T-MOBILE BILL SUMMARY")
        print("="*80)
        print(f"\nTotal Due: ${self.bill_data['total_due']:.2f}")
        print(f"Plans Total: ${self.bill_data['plans_total']:.2f}")
        print(f"Equipment Total: ${self.bill_data['equipment_total']:.2f}")
        print(f"Services Total: ${self.bill_data['services_total']:.2f}")
        print(f"One-time Charges: ${self.bill_data['one_time_total']:.2f}")

        print("\n" + "-"*80)
        print(f"{'Phone':<18} {'Type':<10} {'Equal':<10} {'Equipment':<12} {'Extras':<10} {'Total':<10}")
        print(f"{'(Last 4)':<18} {'':<10} {'Portion':<10} {'':<12} {'':<10} {'Per Person':<10}")
        print("-"*80)

        for line in self.bill_data['lines']:
            phone_display = f"({line['last4']})" if line['last4'] != 'ACCT' else 'Account'

            equal = f"${line.get('equal_portion', 0):.2f}"
            equipment = f"${line['equipment']:.2f}"
            extras = f"${line['one_time_charges']:.2f}"
            total = f"${line.get('total_per_person', line['total']):.2f}"

            removed = " (Removed)" if line['is_removed'] else ""

            print(f"{phone_display:<18} {line['line_type']:<10} {equal:<10} {equipment:<12} {extras:<10} {total:<10}{removed}")

        print("="*80 + "\n")


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python parse_tmobile_bill.py <path_to_bill.pdf>")
        sys.exit(1)

    pdf_path = sys.argv[1]

    if not Path(pdf_path).exists():
        print(f"Error: File not found: {pdf_path}")
        sys.exit(1)

    try:
        parser = TMobileBillParser(pdf_path)
        bill_data = parser.parse()
        parser.print_summary()

        # Optionally output as JSON
        if len(sys.argv) > 2 and sys.argv[2] == '--json':
            import json
            # Convert Decimals to float for JSON serialization
            json_data = {
                'total_due': float(bill_data['total_due']),
                'plans_total': float(bill_data['plans_total']),
                'equipment_total': float(bill_data['equipment_total']),
                'services_total': float(bill_data['services_total']),
                'one_time_total': float(bill_data['one_time_total']),
                'lines': [
                    {
                        'phone': line['phone'],
                        'last4': line['last4'],
                        'line_type': line['line_type'],
                        'plans': float(line['plans']),
                        'equipment': float(line['equipment']),
                        'services': float(line['services']),
                        'one_time_charges': float(line['one_time_charges']),
                        'total': float(line['total']),
                        'equal_portion': float(line.get('equal_portion', 0)),
                        'total_per_person': float(line.get('total_per_person', line['total'])),
                        'is_removed': line['is_removed']
                    }
                    for line in bill_data['lines']
                ]
            }
            print("\nJSON Output:")
            print(json.dumps(json_data, indent=2))

    except Exception as e:
        print(f"Error parsing bill: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
