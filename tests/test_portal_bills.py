import unittest
from datetime import date
from decimal import Decimal
from utility_bills import BillError, PROVIDERS, Report, compose_email, parse_portal_bill, load_xfinity_statement
from pathlib import Path
from tempfile import TemporaryDirectory

DOMINION = """Bill Summary Bill Period: Sep 05, 2026 - Oct 06, 2026
Previous Balance $90.00 Current Charges $11.22 Total Account Balance $11.22
Your next payment of $11.22 is due on Nov 02, 2026"""
DOMINION_HISTORY = """Statement Date Due Date Billing Days Current Charges Total Account Balance
10/07/2026 11/02/2026 32 $11.22 $11.22
09/08/2026 10/05/2026 29 $90.00 $90.00"""
GAS = """BILL DATE BILL PERIOD START DATE BILL PERIOD END DATE BILL AMOUNT ($) VIEW BILL
September 18, 2026 August 18, 2026 September 16, 2026 $25.00
October 21, 2026 September 17, 2026 October 15, 2026 $12.34"""
WATER = """My Bill See Billing Comparison Amount Due: $23.45
Service Period: Sep 11 to Oct 09, 2026 Due Date: November 04, 2026
View Current Bill Make a Payment Usage Quick View Current Bill $9.00"""
ALEX = """Account Billing Summary Residential Service Bill Period:
9/10/2026 to 10/09/2026 View Rates View PDF File
Previous Bill Amount $99.00 Payments Received -$55.00 Total Charges $34.56"""
XFINITY = """Statement date: October 20, 2026
Service from September 20, 2026 to October 19, 2026
Total new charges $60.00 Amount due $80.00 Due date: November 10, 2026"""


class PortalParserTests(unittest.TestCase):
    def test_dominion_reads_current_charges_and_history_date(self):
        bill = parse_portal_bill(PROVIDERS[0], DOMINION, DOMINION_HISTORY)
        self.assertEqual(bill.amount, Decimal("11.22"))
        self.assertEqual(bill.statement_date, date(2026, 10, 7))
        self.assertEqual((bill.period_start, bill.period_end), (date(2026, 9, 5), date(2026, 10, 6)))
        self.assertEqual(bill.due_date, date(2026, 11, 2))

    def test_dominion_mismatched_history_does_not_mix_statements(self):
        with self.assertRaisesRegex(BillError, "do not agree"):
            parse_portal_bill(PROVIDERS[0], DOMINION, DOMINION_HISTORY.replace("11.22", "22.33"))

    def test_gas_selects_latest_row_without_requiring_repeated_labels(self):
        bill = parse_portal_bill(PROVIDERS[1], GAS)
        self.assertEqual(bill.amount, Decimal("12.34"))
        self.assertEqual(bill.statement_date, date(2026, 10, 21))
        self.assertEqual(bill.period_start, date(2026, 9, 17))
        self.assertEqual(bill.period_end, date(2026, 10, 15))

    def test_gas_zero_bill_is_preserved_and_duplicate_latest_rejected(self):
        self.assertEqual(parse_portal_bill(PROVIDERS[1], GAS.replace("12.34", "0.00")).amount, Decimal("0.00"))
        with self.assertRaisesRegex(BillError, "Multiple latest"):
            parse_portal_bill(PROVIDERS[1], GAS + " October 21, 2026 September 17, 2026 October 15, 2026 $99.00")

    def test_water_reads_bill_card_not_usage_estimate(self):
        bill = parse_portal_bill(PROVIDERS[2], WATER)
        self.assertEqual(bill.amount, Decimal("23.45"))
        self.assertEqual(bill.period_start, date(2026, 9, 11))
        self.assertEqual(bill.period_end, date(2026, 10, 9))
        self.assertEqual(bill.due_date, date(2026, 11, 4))
        self.assertIsNone(bill.statement_date)
        email = compose_email(Report([bill])).get_content()
        self.assertIn("Current amount due: $23.45", email)
        self.assertIn("Statement date: Not listed", email)

    def test_water_december_to_january(self):
        bill = parse_portal_bill(PROVIDERS[2], WATER.replace("Sep 11 to Oct 09, 2026", "Dec 11 to Jan 09, 2026"))
        self.assertEqual(bill.period_start, date(2025, 12, 11))
        self.assertEqual(bill.period_end, date(2026, 1, 9))

    def test_alex_current_charges_and_service_period(self):
        bill = parse_portal_bill(PROVIDERS[3], ALEX, "BALANCE DUE Due Date: Nov 5, 2026 $34.56")
        self.assertEqual(bill.amount, Decimal("34.56"))
        self.assertEqual(bill.period_start, date(2026, 9, 10))
        self.assertEqual(bill.period_end, date(2026, 10, 9))
        self.assertEqual(bill.due_date, date(2026, 11, 5))

    def test_unloaded_amount_is_not_zero(self):
        with self.assertRaises(BillError):
            parse_portal_bill(PROVIDERS[0], DOMINION.replace("Current Charges $11.22", "Current Charges $ --"))

    def test_xfinity_prefers_current_charges_over_balance(self):
        bill = parse_portal_bill(PROVIDERS[4], XFINITY)
        self.assertEqual(bill.amount, Decimal("60.00"))
        self.assertEqual(bill.period_start, date(2026, 9, 20))
        self.assertEqual(bill.period_end, date(2026, 10, 19))
        self.assertEqual(bill.statement_date, date(2026, 10, 20))

    def test_xfinity_missing_period_keeps_amount_without_inventing_dates(self):
        bill = parse_portal_bill(PROVIDERS[4], "Amount due $60.00")
        self.assertEqual(bill.amount, Decimal("60.00"))
        self.assertIsNone(bill.period_start)
        self.assertIsNone(bill.period_end)

    def test_xfinity_actual_statement_layout_and_saved_source(self):
        text = """Billing Date: Oct 20, 2026 | $60.00
        Previous Balance $50.00 Regular monthly charges $60.00
        New charges $60.00 Statement total: $ 60 00
        Regular monthly charges Oct 25, 2026 - Nov 24, 2026 $60.00 Internet
        My Xfinity plan $60.00 New charges $ 60 00"""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "statement.txt"
            path.write_text(text)
            bill = load_xfinity_statement(path)
        self.assertEqual(bill.amount, Decimal("60.00"))
        self.assertEqual(bill.period_start, date(2026, 10, 25))
        self.assertEqual(bill.period_end, date(2026, 11, 24))
        self.assertEqual(bill.statement_date, date(2026, 10, 20))
        self.assertIn("Saved Xfinity statement", bill.source_note)
        self.assertIsNone(load_xfinity_statement(None))


if __name__ == "__main__":
    unittest.main()
