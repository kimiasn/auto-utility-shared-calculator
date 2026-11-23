import io
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from utility_bills import (Bill, BillError, Credentials, PROVIDERS, collect_latest_bills,
                           load_default_xfinity_statement, main)


TEXT = "Billing Date: Oct 20, 2026\nRegular monthly charges Oct 25, 2026 - Nov 24, 2026\nNew charges $60.00"


class DefaultStatementTests(unittest.TestCase):
    def test_valid_default_and_expired_default(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "statement.txt"
            path.write_text(TEXT)
            bill = load_default_xfinity_statement(path, today=date(2026, 10, 30))
            self.assertEqual(bill.amount, Decimal("60.00"))
            # The issue date can precede a future service period, as on Xfinity.
            self.assertEqual(load_default_xfinity_statement(path, today=date(2026, 10, 21)).amount, bill.amount)
            with self.assertRaisesRegex(BillError, "service period has ended"):
                load_default_xfinity_statement(path, today=date(2026, 11, 25))

    def test_missing_statement_or_period_gives_clear_refresh_error(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "statement.txt"
            with self.assertRaisesRegex(BillError, "statement is missing"):
                load_default_xfinity_statement(path, today=date(2026, 10, 30))
            path.write_text("New charges $60.00")
            with self.assertRaisesRegex(BillError, "needs a service period"):
                load_default_xfinity_statement(path, today=date(2026, 10, 30))

    def test_future_issue_date_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "statement.txt"
            path.write_text(TEXT)
            with self.assertRaisesRegex(BillError, "future issue date"):
                load_default_xfinity_statement(path, today=date(2026, 10, 19))


class DefaultCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_uses_saved_bill_without_starting_browser(self):
        provider = next(p for p in PROVIDERS if p.key == "xfinity")
        saved = Bill(provider.name, date(2026, 10, 20), date(2026, 10, 25), date(2026, 11, 24), Decimal("60"))
        with TemporaryDirectory() as directory, \
                patch("utility_bills.load_default_xfinity_statement", return_value=saved), \
                patch("playwright.async_api.async_playwright", side_effect=AssertionError("Browser must not start")), \
                patch("utility_bills.login", new=AsyncMock()) as login:
            report, message = await collect_latest_bills({provider.key: Credentials("fixture-user", "fixture-password")},
                providers=(provider,), output_dir=Path(directory))
        login.assert_not_awaited()
        self.assertEqual(report.bills, [saved])
        self.assertEqual(report.errors, {})
        self.assertIn("$60.00", message.get_content())

    async def test_missing_or_expired_default_never_falls_back_to_live_login(self):
        provider = next(p for p in PROVIDERS if p.key == "xfinity")
        for error in ("Saved Xfinity statement is missing", "Saved Xfinity statement's service period has ended"):
            with TemporaryDirectory() as directory, \
                    patch("utility_bills.load_default_xfinity_statement", side_effect=BillError(error)), \
                    patch("playwright.async_api.async_playwright", side_effect=AssertionError("Browser must not start")):
                report, _ = await collect_latest_bills({provider.key: Credentials("fixture-user", "fixture-password")},
                    providers=(provider,), output_dir=Path(directory))
            self.assertEqual(report.bills, [])
            self.assertEqual(report.errors[provider.name], error)

    async def test_cli_live_and_saved_options_are_mutually_exclusive(self):
        with patch("sys.argv", ["utility_bills.py", "--xfinity-live", "--xfinity-statement", "statement.txt"]), \
                patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                await main()
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
