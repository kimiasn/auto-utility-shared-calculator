"""Prorate fixed equal utility shares by occupied days, with exact cent totals."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path


@dataclass(frozen=True)
class Renter:
    id: str
    name: str
    move_in: date | None = None
    move_out: date | None = None  # Last occupied day, inclusive.


@dataclass(frozen=True)
class RentalSettings:
    renters: tuple[Renter, ...]
    total_shares: int = 3
    period_end_inclusive: bool = True


@dataclass(frozen=True)
class Portion:
    renter_id: str
    name: str
    amount: Decimal
    occupied_days: int


@dataclass(frozen=True)
class BillSplit:
    days: int
    renters: tuple[Portion, ...]
    owner_amount: Decimal


def load_renters(path: Path | None) -> RentalSettings | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Could not read renters file; check its path and JSON format") from None
    if (not isinstance(data, dict) or set(data) != {"total_shares", "period_end_inclusive", "renters"}
            or type(data["total_shares"]) is not int or data["total_shares"] < 1
            or type(data["period_end_inclusive"]) is not bool
            or not isinstance(data["renters"], list) or not data["renters"]):
        raise ValueError("Renters file needs positive total_shares, period_end_inclusive, and a nonempty renters list")
    renters = []
    for entry in data["renters"]:
        if (not isinstance(entry, dict) or set(entry) != {"id", "name", "move_in", "move_out"}
                or not all(isinstance(entry[k], str) and entry[k].strip() for k in ("id", "name"))):
            raise ValueError("Each renter needs an id, name, move_in, and move_out")
        dates = []
        for key in ("move_in", "move_out"):
            value = entry[key]
            if value is None:
                dates.append(None)
                continue
            try:
                if not isinstance(value, str) or len(value) != 10:
                    raise ValueError()
                dates.append(date.fromisoformat(value))
            except ValueError:
                raise ValueError("Renter dates must be YYYY-MM-DD or null") from None
        if dates[0] and dates[1] and dates[0] > dates[1]:
            raise ValueError("A renter's move_out cannot precede move_in")
        renters.append(Renter(entry["id"].strip(), " ".join(entry["name"].split()), *dates))
    if len({r.id for r in renters}) != len(renters):
        raise ValueError("Renter ids must be unique")
    return RentalSettings(tuple(renters), data["total_shares"], data["period_end_inclusive"])


def split_bill(bill, settings: RentalSettings) -> BillSplit:
    """Each renter owes 1/total_shares for occupied days; owner covers vacancies.

    Spread the whole bill evenly over service days. Round once per bill using
    largest remainders so renter portions plus the owner portion equal the bill.
    Equal penny ties follow renters-file order, then the owner. Credits use the
    same allocation with reversed signs.
    """
    if bill.period_start is None or bill.period_end is None:
        raise ValueError("Service period is missing; renter portions were not calculated")
    if type(settings.total_shares) is not int or settings.total_shares < 1 or not settings.renters:
        raise ValueError("A positive share count and renters are required")
    if len({r.id for r in settings.renters}) != len(settings.renters):
        raise ValueError("Renter ids must be unique")
    start = bill.period_start.toordinal()
    end = bill.period_end.toordinal() + int(settings.period_end_inclusive)
    days = end - start
    if days <= 0:
        raise ValueError("Service period must contain at least one day")
    cents = bill.amount * 100
    if not cents.is_finite() or cents != cents.to_integral_value():
        raise ValueError("Bill amount must be a finite number of cents")
    occupied = []
    events = {}
    for renter in settings.renters:
        if renter.move_in and renter.move_out and renter.move_in > renter.move_out:
            raise ValueError("A renter's move_out cannot precede move_in")
        first = max(start, renter.move_in.toordinal() if renter.move_in else start)
        last = min(end, renter.move_out.toordinal() + 1 if renter.move_out else end)
        count = max(0, last - first)
        occupied.append(count)
        if count:
            events[first] = events.get(first, 0) + 1
            events[last] = events.get(last, 0) - 1
    active = 0
    for day in sorted(events):
        active += events[day]
        if active > settings.total_shares:
            raise ValueError("More simultaneous renters than total_shares")
    absolute = abs(int(cents))
    exact = [Fraction(absolute * count, days * settings.total_shares) for count in occupied]
    exact.append(Fraction(absolute) - sum(exact))
    rounded = [value.numerator // value.denominator for value in exact]
    remainder = absolute - sum(rounded)
    order = sorted(range(len(exact)), key=lambda i: (-(exact[i] - rounded[i]), i))
    for index in order[:remainder]:
        rounded[index] += 1
    sign = -1 if cents < 0 else 1
    amounts = [Decimal(value * sign) / 100 for value in rounded]
    portions = tuple(Portion(renter.id, renter.name, amounts[i], occupied[i])
                     for i, renter in enumerate(settings.renters))
    return BillSplit(days, portions, amounts[-1])


def format_renter_shares(bills, settings: RentalSettings, partial: bool = False) -> str:
    lines = ["Renter portions", f"Each renter pays 1/{settings.total_shares} of the daily cost for occupied days; owner covers vacant shares.",
             "Move-in and move-out days count as occupied. " +
             ("Both service-period endpoints count." if settings.period_end_inclusive else "The service-period end date is excluded."),
             "The entire bill is spread evenly over service days, including fixed charges.", ""]
    totals = {r.id: Decimal("0") for r in settings.renters}
    owner_total = Decimal("0")
    for bill in bills:
        lines.append(bill.provider)
        try:
            split = split_bill(bill, settings)
        except ValueError as error:
            lines.extend([f"  Not allocated: {error}", ""])
            partial = True
            continue
        for portion in split.renters:
            totals[portion.renter_id] += portion.amount
            lines.append(f"  {portion.name}: ${portion.amount:,.2f} ({portion.occupied_days}/{split.days} service days)")
        owner_total += split.owner_amount
        lines.extend([f"  Owner / vacant shares: ${split.owner_amount:,.2f}", ""])
    lines.append("Totals by person" + (" (partial; available and allocated bills only)" if partial else ""))
    lines.extend(f"  {r.name}: ${totals[r.id]:,.2f}" for r in settings.renters)
    lines.extend([f"  Owner / vacant shares: ${owner_total:,.2f}",
                  "Amounts are rounded per bill; any one-cent ties follow renters-file order."])
    return "\n".join(lines) + "\n"
