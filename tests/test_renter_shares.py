from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from email import policy
from email.parser import Parser
import unittest

from renter_shares import Renter, RentalSettings, load_renters, split_bill
from utility_bills import Bill, Report, compose_email, main


def settings():
    return RentalSettings((Renter("a", "Full-period renter"),
        Renter("b", "September renter B", date(2026, 9, 14)),
        Renter("c", "September renter C", date(2026, 9, 14))))


def bill(amount="90.00", start=date(2026, 9, 1), end=date(2026, 9, 30)):
    return Bill("Test Utility", None, start, end, Decimal(amount))


class RenterSplitTests(unittest.TestCase):
    def assert_balanced(self, source, result):
        self.assertEqual(sum((p.amount for p in result.renters), result.owner_amount), source.amount)

    def test_september_fourteenth_counts_and_owner_covers_vacancies(self):
        source = bill()
        result = split_bill(source, settings())
        self.assertEqual(result.days, 30)
        self.assertEqual([p.occupied_days for p in result.renters], [30, 17, 17])
        self.assertEqual([p.amount for p in result.renters], [Decimal("30"), Decimal("17"), Decimal("17")])
        self.assertEqual(result.owner_amount, Decimal("26"))
        self.assert_balanced(source, result)

    def test_bill_before_move_in(self):
        source = bill("39", end=date(2026, 9, 13))
        result = split_bill(source, settings())
        self.assertEqual([p.amount for p in result.renters], [Decimal("13"), Decimal("0"), Decimal("0")])
        self.assertEqual(result.owner_amount, Decimal("26"))

    def test_bill_after_move_in_is_equal_three_way_split(self):
        source = bill("51", start=date(2026, 9, 14))
        result = split_bill(source, settings())
        self.assertEqual([p.amount for p in result.renters], [Decimal("17")] * 3)
        self.assertEqual(result.owner_amount, Decimal("0"))

    def test_rounding_distributes_pennies_without_losing_money(self):
        source = bill("1.00", start=date(2026, 9, 14))
        result = split_bill(source, settings())
        self.assertEqual([p.amount for p in result.renters], [Decimal("0.34"), Decimal("0.33"), Decimal("0.33")])
        self.assert_balanced(source, result)

    def test_credits_use_same_split_with_reversed_sign(self):
        positive = split_bill(bill("1.00"), settings())
        negative = split_bill(bill("-1.00"), settings())
        self.assertEqual([p.amount for p in negative.renters], [-p.amount for p in positive.renters])
        self.assertEqual(negative.owner_amount, -positive.owner_amount)
        self.assert_balanced(bill("-1.00"), negative)

    def test_single_day_and_exclusive_period_end(self):
        result = split_bill(bill("3.00", start=date(2026, 9, 14), end=date(2026, 9, 14)), settings())
        self.assertEqual([p.amount for p in result.renters], [Decimal("1")] * 3)
        result = split_bill(bill("39.00", end=date(2026, 9, 14)), replace(settings(), period_end_inclusive=False))
        self.assertEqual([p.occupied_days for p in result.renters], [13, 0, 0])
        self.assertEqual(result.owner_amount, Decimal("26"))

    def test_move_out_counts_as_occupied_and_leap_year(self):
        config = RentalSettings((Renter("a", "A", date(2024, 2, 29), date(2024, 2, 29)),), total_shares=1)
        result = split_bill(bill("29", date(2024, 2, 1), date(2024, 2, 29)), config)
        self.assertEqual(result.renters[0].amount, Decimal("1"))
        self.assertEqual(result.owner_amount, Decimal("28"))

    def test_vacant_bill_goes_to_owner(self):
        config = RentalSettings((Renter("future", "Future", date(2027, 1, 1)),))
        result = split_bill(bill(), config)
        self.assertEqual(result.renters[0].amount, Decimal("0"))
        self.assertEqual(result.owner_amount, Decimal("90"))

    def test_too_many_simultaneous_renters_rejected(self):
        config = replace(settings(), renters=settings().renters + (Renter("d", "D"),))
        with self.assertRaisesRegex(ValueError, "simultaneous"):
            split_bill(bill(), config)

    def test_historical_replacement_renters_can_share_one_slot(self):
        config = RentalSettings((Renter("a", "A", move_out=date(2026, 9, 13)),
                                 Renter("b", "B", move_in=date(2026, 9, 14))), total_shares=1)
        result = split_bill(bill("30"), config)
        self.assertEqual([p.amount for p in result.renters], [Decimal("13"), Decimal("17")])
        self.assertEqual(result.owner_amount, Decimal("0"))

    def test_invalid_amounts_or_service_period_are_rejected(self):
        for source in (replace(bill(), period_start=None), bill("NaN"), bill("1.001"),
                       bill(start=date(2026, 10, 1))):
            with self.assertRaises(ValueError):
                split_bill(source, settings())

    def test_email_includes_breakdown_owner_share_and_partial_status(self):
        message = compose_email(Report([bill()], {"Other Utility": "Unavailable"}), settings()).get_content()
        self.assertIn("September renter B: $17.00 (17/30 service days)", message)
        self.assertIn("Owner / vacant shares: $26.00", message)
        self.assertIn("Totals by renter (partial", message)
        self.assertIn("including fixed charges", message)

    def test_missing_period_does_not_fabricate_renter_portions(self):
        text = compose_email(Report([replace(bill(), period_start=None)]), settings()).get_content()
        self.assertIn("Not allocated: Service period is missing", text)
        self.assertIn("Totals by renter (partial", text)


class RenterFileTests(unittest.TestCase):
    def data(self):
        return {"total_shares": 3, "period_end_inclusive": True, "renters": [
            {"id": "a", "name": "A", "move_in": None, "move_out": None},
            {"id": "b", "name": "B", "move_in": "2026-09-14", "move_out": None}]}

    def test_load_names_and_dates(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "renters.json"
            path.write_text(json.dumps(self.data()))
            loaded = load_renters(path)
        self.assertIsNone(loaded.renters[0].move_in)
        self.assertEqual(loaded.renters[1].move_in, date(2026, 9, 14))
        self.assertEqual(loaded.total_shares, 3)
        self.assertIsNone(load_renters(None))

    def test_invalid_config(self):
        invalid = [dict(self.data(), total_shares=0), dict(self.data(), total_shares=True),
                   dict(self.data(), period_end_inclusive="true"), dict(self.data(), renters=[])]
        for update in ({"id": "a"}, {"move_in": "2026-02-30"}, {"move_in": "09/14/2026"},
                       {"move_in": "2026-09-14", "move_out": "2026-09-13"}):
            data = self.data()
            data["renters"][1].update(update)
            invalid.append(data)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "renters.json"
            for data in invalid:
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    load_renters(path)


class RenterCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_loads_renters_and_saves_email_with_portions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            rental_path = root / "renters.json"
            rental_path.write_text(json.dumps({"total_shares": 3, "period_end_inclusive": True,
                "renters": [{"id": r.id, "name": r.name, "move_in": r.move_in.isoformat() if r.move_in else None,
                             "move_out": None} for r in settings().renters]}))
            property_path = root / "property.json"
            property_path.write_text('{"service_address": ""}')
            captured = {}
            async def collect(*args, renters=None, **kwargs):
                captured["renters"] = renters
                captured["browser_channel"] = kwargs.get("browser_channel")
                report = Report([bill()])
                return report, compose_email(report, renters)
            with patch("sys.argv", ["utility_bills.py", "--renters-file", str(rental_path),
                        "--property-file", str(property_path), "--output", directory, "--browser", "chrome"]), \
                    patch("utility_bills.load_credentials", return_value={}), \
                    patch("utility_bills.collect_latest_bills", new=AsyncMock(side_effect=collect)), \
                    patch("builtins.print"):
                self.assertEqual(await main(), 0)
            self.assertEqual(captured["renters"].renters[1].move_in, date(2026, 9, 14))
            self.assertEqual(captured["browser_channel"], "chrome")
            message = Parser(policy=policy.default).parsestr((root / "utility-bills.eml").read_text())
            self.assertIn("September renter B: $17.00", message.get_content())
            self.assertIn("Owner / vacant shares: $26.00", message.get_content())


if __name__ == "__main__":
    unittest.main()
