import unittest
import json
import os
import io
from datetime import date
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from utility_bills import (BillError, Credentials, PROVIDERS, Report, check_origin,
                           apply_property_settings, compose_email, ensure_statement_page, extract_bill,
                           latest_index, load_credentials, login, main, parse_bill)


def statement(issued="10/15/2026", amount="123.45"):
    return (f"Statement Date: {issued}\nService Period: 09/10/2026 - 10/10/2026\n"
            f"Amount Due: ${amount}\nDue Date: October 30, 2026")


class ParsingTests(unittest.TestCase):
    def test_all_providers(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider.key):
                bill = parse_bill(provider.name, statement(amount="1,234.56"))
                self.assertEqual(bill.amount, Decimal("1234.56"))
                self.assertEqual(bill.period_start, date(2026, 9, 10))
                self.assertEqual(bill.period_end, date(2026, 10, 10))
                self.assertEqual(bill.due_date, date(2026, 10, 30))

    def test_latest_unsorted_including_paid_zero_bill(self):
        texts = [statement("09/15/2026"), statement("10/15/2026", "0.00"), statement("08/15/2026")]
        self.assertEqual(latest_index(texts), 1)
        self.assertEqual(parse_bill("Utility", texts[1]).amount, Decimal("0.00"))

    def test_missing_period_does_not_invent_one(self):
        with self.assertRaisesRegex(BillError, "service period"):
            parse_bill("Utility", "Statement Date: 10/15/2026 Amount Due: $45.00")

    def test_dashboard_balance_is_not_statement_amount(self):
        with self.assertRaisesRegex(BillError, "bill amount"):
            parse_bill("Utility", statement().replace("Amount Due", "Current Balance"))

    def test_invalid_or_reversed_dates(self):
        for text in (statement("02/30/2026"), statement().replace("09/10/2026", "11/10/2026")):
            with self.assertRaises(BillError):
                parse_bill("Utility", text)

    def test_duplicate_latest_and_undated_candidates_fail(self):
        for texts in ([statement(), statement()], [statement(), "Bill Date: unavailable"]):
            with self.assertRaises(BillError):
                latest_index(texts)

    def test_conflicting_amounts_fail(self):
        with self.assertRaisesRegex(BillError, "ambiguous bill amount"):
            parse_bill("Utility", statement() + " Total Amount Due: $99.00")

    def test_multiple_statement_detail_fails(self):
        with self.assertRaises(BillError):
            parse_bill("Utility", statement() + statement("09/15/2026"))

    def test_credit_and_optional_due_date(self):
        bill = parse_bill("Utility", statement(amount="-5.25").split("Due Date")[0])
        self.assertEqual(bill.amount, Decimal("-5.25"))
        self.assertIsNone(bill.due_date)

    def test_email_partial_failure_and_exact_decimal_total(self):
        bills = [parse_bill("Gas", statement(amount="0.10")), parse_bill("Water", statement(amount="0.20"))]
        msg = compose_email(Report(bills, {"Dominion": "Credentials not supplied"}))
        self.assertIn("incomplete", msg["Subject"])
        body = msg.get_content()
        self.assertIn("(partial): $0.30", body)
        self.assertIn("2026-09-10 to 2026-10-10", body)
        self.assertIn("Dominion: Credentials not supplied", body)
        self.assertIsNone(msg["To"])

    def test_empty_report_does_not_claim_zero_total(self):
        body = compose_email(Report(errors={"Gas": "Unavailable"})).get_content()
        self.assertIn("No bill amounts", body)
        self.assertNotIn("$0.00", body)

    def test_credentials_repr_redacted(self):
        self.assertNotIn("secret", repr(Credentials("secret-user", "secret-password")))


class CredentialsFileTests(unittest.TestCase):
    def write_file(self, directory, data):
        path = Path(directory) / "credentials.json"
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def test_loads_all_providers_and_preserves_special_characters(self):
        password = ' spaces "quotes" \\ backslash $value '
        with TemporaryDirectory() as directory:
            path = self.write_file(directory, {p.key: {"username": "user", "password": password} for p in PROVIDERS})
            loaded = load_credentials(path)
        self.assertEqual(len(loaded), len(PROVIDERS))
        self.assertTrue(all(c.password == password for c in loaded.values()))

    def test_empty_entries_are_skipped(self):
        with TemporaryDirectory() as directory:
            path = self.write_file(directory, {"dominion": {"username": "", "password": ""}})
            self.assertEqual(load_credentials(path), {})
        self.assertEqual(load_credentials(None), {})

    def test_rejects_invalid_shapes_without_echoing_secrets(self):
        for data in (["secret"], {"secret": {}}, {"dominion": {"username": "secret"}},
                     {"dominion": {"username": "secret", "password": 123}},
                     {"dominion": {"username": "secret", "password": ""}}):
            with self.subTest(data_type=type(data).__name__), TemporaryDirectory() as directory:
                with self.assertRaises(ValueError) as error:
                    load_credentials(self.write_file(directory, data))
                self.assertNotIn("secret", str(error.exception))

    def test_malformed_json_error_does_not_echo_contents(self):
        with TemporaryDirectory() as directory:
            path = self.write_file(directory, {})
            path.write_text('secret malformed JSON')
            with self.assertRaises(ValueError) as error:
                load_credentials(path)
            self.assertNotIn("secret", str(error.exception))

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_rejects_file_readable_by_other_users(self):
        with TemporaryDirectory() as directory:
            path = self.write_file(directory, {})
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "chmod 600"):
                load_credentials(path)


class PropertySettingsTests(unittest.IsolatedAsyncioTestCase):
    def test_shared_address_applies_to_all_profiles(self):
        address = "123 Example Street, Unit 4, Alexandria, VA 22314"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "property.json"
            path.write_text(json.dumps({"service_address": address}))
            configured = apply_property_settings(PROVIDERS, path)
        self.assertEqual(len(configured), len(PROVIDERS))
        self.assertTrue(all(p.service_address == address for p in configured))
        self.assertTrue(all(p.service_address is None for p in PROVIDERS))
        self.assertEqual([p.login_url for p in configured], [p.login_url for p in PROVIDERS])

    def test_blank_or_invalid_address_settings(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "property.json"
            path.write_text(json.dumps({"service_address": " "}))
            self.assertTrue(all(p.service_address is None for p in apply_property_settings(PROVIDERS, path)))
            for data in ({}, {"service_address": 42}, {"service_address": "street", "extra": "value"}, []):
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    apply_property_settings(PROVIDERS, path)

    async def test_extracted_bill_carries_configured_address_to_email(self):
        address = "123 Example Street, Alexandria, VA 22314"
        provider = replace(PROVIDERS[0], service_address=address)
        page = AsyncMock()
        rows, body = AsyncMock(), AsyncMock()
        rows.count.return_value = 0
        body.inner_text.return_value = statement()
        page.locator = lambda selector: body if selector == "body" else rows
        with TemporaryDirectory() as directory:
            bill = await extract_bill(page, provider, Path(directory))
        self.assertEqual(bill.service_address, address)
        self.assertIn(f"Configured service address: {address}", compose_email(Report([bill])).get_content())


class DraftSaveTests(unittest.IsolatedAsyncioTestCase):
    async def check_save(self, existing, report, expected_code):
        message = compose_email(report)
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "utility-bills.eml"
            property_file = Path(directory) / "property.json"
            property_file.write_text(json.dumps({"service_address": ""}))
            if existing:
                destination.write_text("Old draft " * 1000)
                destination.chmod(0o644)
            with patch("sys.argv", ["utility_bills.py", "--output", directory, "--property-file", str(property_file)]), \
                    patch("utility_bills.load_credentials", return_value={}), \
                    patch("utility_bills.load_renters", return_value=None), \
                    patch("utility_bills.collect_latest_bills", new=AsyncMock(return_value=(report, message))), \
                    patch("sys.stdout", new=io.StringIO()):
                code = await main()
            self.assertEqual(code, expected_code)
            self.assertEqual(destination.read_text(encoding="utf-8"), message.as_string())
            if os.name == "posix":
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    async def test_main_saves_new_draft(self):
        await self.check_save(False, Report([parse_bill("Gas", statement())]), 0)

    async def test_main_overwrites_draft_and_preserves_partial_failure_status(self):
        await self.check_save(True, Report(errors={"Gas": "Unavailable"}), 1)


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_login_selectors_and_multistep_flow(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider.key):
                page = AsyncMock()
                page.url = provider.login_url
                fields = {}
                def locator(selector):
                    return fields.setdefault(selector, AsyncMock())
                page.locator = locator
                page.expect_request = Mock(return_value=AsyncMock())
                await login(page, provider, Credentials("user", "password"))
                fields[provider.username].fill.assert_awaited_once_with("user")
                fields[provider.password].fill.assert_awaited_once_with("password")
                self.assertEqual(fields[provider.submit].click.await_count, 2 if provider.next_button else 1)
                if provider.entry_button:
                    fields[provider.entry_button].click.assert_awaited_once()

    async def test_unexpected_redirect_does_not_receive_credentials(self):
        provider = PROVIDERS[0]
        page = AsyncMock()
        page.url = "https://untrusted.example/"
        field = AsyncMock()
        page.locator = lambda selector: field
        with self.assertRaises(BillError):
            await login(page, provider, Credentials("user", "password"))
        field.fill.assert_not_awaited()

    async def test_login_screen_is_reported_before_statement_parsing(self):
        page = AsyncMock()
        page.url = PROVIDERS[1].login_url
        field = AsyncMock()
        field.is_visible.return_value = True
        page.locator = lambda selector: field
        with self.assertRaisesRegex(BillError, "Still on the login screen"):
            await ensure_statement_page(page, PROVIDERS[1])

    async def test_washington_gas_click_without_request_reports_failure(self):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        provider = PROVIDERS[1]
        page = AsyncMock()
        page.url = provider.login_url
        field = AsyncMock()
        page.locator = lambda selector: field
        event = AsyncMock()
        event.__aexit__.side_effect = PlaywrightTimeoutError("fixture timeout")
        page.expect_request = Mock(return_value=event)
        with self.assertRaisesRegex(BillError, "did not start its login request"):
            await login(page, provider, Credentials("fixture-user", "fixture-password"))
        field.click.assert_awaited_once()

    async def test_washington_gas_waits_for_login_screen_to_disappear(self):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        provider = PROVIDERS[1]
        page = AsyncMock()
        page.url = provider.login_url
        fields = {}
        def locator(selector):
            return fields.setdefault(selector, AsyncMock())
        page.locator = locator
        async def wait_for(**kwargs):
            if kwargs.get("state") == "hidden":
                raise PlaywrightTimeoutError("fixture login did not finish")
        locator(provider.username).wait_for.side_effect = wait_for
        page.expect_request = Mock(return_value=AsyncMock())
        with self.assertRaisesRegex(BillError, "received the login request but stayed"):
            await login(page, provider, Credentials("fixture-user", "fixture-password"))
        locator(provider.submit).click.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
