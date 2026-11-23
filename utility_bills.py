"""Read utility statements and compose an email; never send mail or make payments."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from getpass import getpass
import json
import os
from pathlib import Path
import re
from typing import Awaitable, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from renter_shares import RentalSettings, load_renters, format_renter_shares


class BillError(Exception):
    """A statement cannot be read reliably."""


@dataclass(frozen=True)
class Credentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    login_url: str
    hosts: tuple[str, ...]
    username: str
    password: str
    submit: str
    next_button: str | None = None
    # Set these after inspecting the authenticated portal. No guessed routes.
    billing_steps: tuple[str, ...] = ()
    ready_selector: str | None = None
    row_selector: str = "tr, [role=row]"
    download_selector: str | None = None
    service_address: str | None = None
    entry_button: str | None = None


PROVIDERS = (
    Provider("dominion", "Dominion Energy",
             "https://myaccount.dominionenergy.com/portal/#/login",
             ("myaccount.dominionenergy.com", "login.dominionenergy.com"),
             '#gigya-login-form input[name="username"]',
             '#gigya-login-form input[name="password"]',
             '#gigya-login-form input[type="submit"]'),
    Provider("washington_gas", "Washington Gas",
             "https://my.washingtongas.com/portal/", ("my.washingtongas.com",),
             "#txtLogin", "#txtpwd", "#btnlogin"),
    Provider("american_water", "American Water",
             # Enter via the customer landing page, not the separate app login
             # exposed at mywaterv2.amwater.com/#/login.
             "https://login.amwater.com/",
             ("mywaterv2.amwater.com", "auth.amwater.com", "login.amwater.com"),
             'input[name="identifier"]', 'input[type="password"]',
             'input[type="submit"]', next_button='input[type="submit"]',
             entry_button="#submitLoginButton"),
    Provider("alexrenew", "AlexRenew", "https://myalexrenew.com/signin",
             ("myalexrenew.com",), "#login-email", "#login-password",
             'button:has-text("Log into Portal")'),
    Provider("xfinity", "Xfinity Internet", "https://login.xfinity.com/login",
             ("login.xfinity.com", "customer.xfinity.com", "www.xfinity.com", "my.xfinity.com"),
             "#user", 'input[type="password"]', "#sign_in", next_button="#sign_in"),
)


@dataclass(frozen=True)
class Bill:
    provider: str
    statement_date: date | None
    period_start: date | None
    period_end: date | None
    amount: Decimal
    due_date: date | None = None
    pdf_path: Path | None = None
    service_address: str | None = None
    amount_label: str = "Statement amount due"
    source_note: str | None = None


@dataclass
class Report:
    bills: list[Bill] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


DATE = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})"


def parse_date(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y",
                "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise BillError("Unsupported or invalid statement date")


def _unique(pattern: str, text: str, label: str, optional: bool = False):
    matches = set(re.findall(pattern, text, flags=re.I))
    if not matches and optional:
        return None
    if len(matches) != 1:
        raise BillError(f"Missing or ambiguous {label}")
    return matches.pop()


def parse_bill(provider: str, text: str) -> Bill:
    """Parse one statement, not a dashboard balance or an entire bill history.

    Only explicitly labelled fields are accepted. Additional portal-specific
    labels can be added after checking a real statement.
    """
    text = re.sub(r"\s+", " ", text)
    issued = _unique(rf"(?:statement date|bill date|billing date)\s*:?\s*({DATE})", text, "statement date")
    period = _unique(rf"(?:service period|billing period|service dates|service from)\s*:?\s*({DATE})\s*(?:to|through|[-–—])\s*({DATE})", text, "service period")
    amount = _unique(r"(?:total amount due|amount due|total bill|bill amount)\s*:?\s*\$\s*(-?\d[\d,]*\.\d{2})(?!\d)", text, "bill amount")
    due = _unique(rf"(?:due date|payment due(?: date)?)\s*:?\s*({DATE})", text, "due date", optional=True)
    start, end = map(parse_date, period)
    if start > end:
        raise BillError("Service period ends before it starts")
    return Bill(provider, parse_date(issued), start, end,
                Decimal(amount.replace(",", "")), parse_date(due) if due else None)


def latest_index(texts: list[str]) -> int:
    """Select by issue date, even when rows are unsorted or latest is paid.

    Fail if any candidate statement lacks a date, or dates tie, rather than
    silently returning an older or different-account statement.
    """
    if not texts:
        raise BillError("No statements found")
    dates = [parse_date(_unique(rf"(?:statement date|bill date|billing date)\s*:?\s*({DATE})",
                               re.sub(r"\s+", " ", t), "statement date")) for t in texts]
    newest = max(dates)
    if dates.count(newest) != 1:
        raise BillError("Multiple latest statements; select one utility account")
    return dates.index(newest)


def check_origin(page, provider: Provider):
    url = urlsplit(page.url)
    if url.scheme != "https" or url.hostname not in provider.hosts:
        raise BillError("Unexpected portal origin; return to the utility account portal before continuing")


async def login(page, provider: Provider, credentials: Credentials):
    await page.goto(provider.login_url, wait_until="domcontentloaded")
    if provider.entry_button:
        await page.locator(provider.entry_button).wait_for(state="visible")
        check_origin(page, provider)
        await page.locator(provider.entry_button).click()
    if provider.key in ("washington_gas", "xfinity"):
        # These pages can expose inputs before their sign-in scripts finish.
        await page.wait_for_load_state("load")
    if provider.key == "washington_gas":
        # Its HTML submit handler returns false. default.js attaches the real
        # AJAX handler at document ready; the form can be visible before then.
        await page.locator("#page_loader_custom").wait_for(state="hidden")
    await page.locator(provider.username).wait_for(state="visible")
    check_origin(page, provider)
    await page.locator(provider.username).fill(credentials.username)
    if provider.next_button:
        await page.locator(provider.next_button).click()
    await page.locator(provider.password).wait_for(state="visible")
    check_origin(page, provider)
    await page.locator(provider.password).fill(credentials.password)
    if provider.key == "washington_gas":
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        try:
            # Observe the site's normal login request; don't submit a second
            # request ourselves or interfere with its CAPTCHA implementation.
            async with page.expect_request(
                lambda request: request.method == "POST"
                and urlsplit(request.url).hostname in provider.hosts
                and urlsplit(request.url).path.lower().endswith("/default.aspx/updatestate"),
                timeout=15000,
            ):
                await page.locator(provider.submit).click()
        except PlaywrightTimeoutError:
            raise BillError("Washington Gas did not start its login request; finish signing in in the browser") from None
        try:
            await page.locator(provider.username).wait_for(state="hidden", timeout=30000)
        except PlaywrightTimeoutError:
            raise BillError("Washington Gas received the login request but stayed on the login screen; check for a challenge or sign-in error") from None
    else:
        await page.locator(provider.submit).click()


async def ensure_statement_page(page, provider: Provider):
    check_origin(page, provider)
    if await page.locator(provider.username).is_visible():
        raise BillError("Still on the login screen; complete sign-in and open a statement before continuing")


MONEY = r"\$\s*(-?\d[\d,]*\.\d{2})(?!\d)"


def dollars(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def checked_period(start: date, end: date):
    if start > end:
        raise BillError("Service period ends before it starts")
    return start, end


def parse_portal_bill(provider: Provider, text: str, history: str | None = None) -> Bill:
    """Read formats verified on the authenticated utility portals.

    A missing issue date must not discard a bill with a known amount and period.
    Current charges and current amount due retain their portal labels.
    """
    text = re.sub(r"\s+", " ", text)
    issued = due = None
    if provider.key == "washington_gas":
        if not re.search(r"BILL DATE\s+BILL PERIOD START DATE\s+BILL PERIOD END DATE\s+BILL AMOUNT", text, re.I):
            raise BillError("Washington Gas bill statement table was not found")
        rows = re.findall(rf"({DATE})\s+({DATE})\s+({DATE})\s+{MONEY}", text, re.I)
        if not rows:
            raise BillError("Washington Gas bill amounts have not loaded")
        latest = max(parse_date(row[0]) for row in rows)
        selected = [row for row in rows if parse_date(row[0]) == latest]
        if len(selected) != 1:
            raise BillError("Multiple latest statements; select one utility account")
        issue, start, end, amount = selected[0]
        issued, start, end = map(parse_date, (issue, start, end))
        amount_label = "Statement bill amount"
    elif provider.key == "dominion":
        start, end = _unique(rf"Bill Period\s*:\s*({DATE})\s*[-–—]\s*({DATE})", text, "service period")
        start, end = map(parse_date, (start, end))
        amount = _unique(rf"Current Charges\s*{MONEY}", text, "current bill charges")
        due_text = _unique(rf"next payment of\s*{MONEY}\s+is due on\s+({DATE})", text, "due date", optional=True)
        due = parse_date(due_text[1]) if due_text else None
        if history:
            rows = re.findall(rf"({DATE})\s+({DATE})\s+\d+\s+{MONEY}\s+{MONEY}",
                              re.sub(r"\s+", " ", history), re.I)
            if rows:
                latest = max(parse_date(row[0]) for row in rows)
                selected = [row for row in rows if parse_date(row[0]) == latest]
                if len(selected) != 1 or dollars(selected[0][2]) != dollars(amount):
                    raise BillError("Dominion summary and latest statement history do not agree")
                issued, due = map(parse_date, selected[0][:2])
        amount_label = "Current bill charges"
    elif provider.key == "american_water":
        # The My Bill card shows a real amount due; Usage Quick View elsewhere
        # on the page can show a different usage-only current bill amount.
        card = _unique(r"My Bill\s+(.*?)View Current Bill", text, "My Bill card")
        amount = _unique(rf"Amount Due\s*:\s*{MONEY}", card, "amount due")
        due_text = _unique(rf"Due Date\s*:\s*({DATE})", card, "due date", optional=True)
        due = parse_date(due_text) if due_text else None
        full = re.findall(rf"Service Period\s*:\s*({DATE})\s*(?:to|[-–—])\s*({DATE})", card, re.I)
        if full:
            start, end = map(parse_date, _unique(rf"Service Period\s*:\s*({DATE})\s*(?:to|[-–—])\s*({DATE})", card, "service period"))
        else:
            start_md, end_md, year = _unique(
                r"Service Period\s*:\s*([A-Za-z]+\s+\d{1,2})\s+to\s+([A-Za-z]+\s+\d{1,2}),\s*(\d{4})",
                card, "service period")
            start, end = parse_date(f"{start_md}, {year}"), parse_date(f"{end_md}, {year}")
            if start > end:  # December-to-January notation uses the ending year.
                start = parse_date(f"{start_md}, {int(year) - 1}")
        amount_label = "Current amount due"
    elif provider.key == "alexrenew":
        start, end = _unique(rf"Residential Service Bill Period\s*:\s*({DATE})\s+to\s+({DATE})", text, "service period")
        start, end = map(parse_date, (start, end))
        amount = _unique(rf"Total Charges\s*{MONEY}", text, "current bill charges")
        due_text = _unique(rf"Due Date\s*:\s*({DATE})", history or "", "due date", optional=True)
        due = parse_date(due_text) if due_text else None
        amount_label = "Current bill charges"
    elif provider.key == "xfinity":
        # Only labelled fields are accepted. Missing service dates retain the
        # amount in the email but leave renter portions explicitly unallocated.
        amount = _unique(rf"total new charges\s*:?\s*{MONEY}", text, "current bill charges", optional=True)
        if amount is None:
            amount = _unique(rf"new charges\s*:?\s*{MONEY}", text, "current bill charges", optional=True)
        amount_label = "Current bill charges"
        if amount is None:
            amount = _unique(rf"(?:total amount due|amount due)\s*:?\s*{MONEY}", text, "amount due")
            amount_label = "Current amount due"
        period = _unique(rf"(?:billing period|service period|service from)\s*:?\s*({DATE})\s*(?:to|through|[-–—])\s*({DATE})",
                         text, "service period", optional=True)
        if period is None:
            period = _unique(rf"Regular monthly charges\s*({DATE})\s*[-–—]\s*({DATE})",
                             text, "service period", optional=True)
        start, end = map(parse_date, period) if period else (None, None)
        issued_text = _unique(rf"(?:statement date|bill date|billing date)\s*:?\s*({DATE})", text, "statement date", optional=True)
        issued = parse_date(issued_text) if issued_text else None
        due_text = _unique(rf"(?:due date|payment due date)\s*:?\s*({DATE})", text, "due date", optional=True)
        due = parse_date(due_text) if due_text else None
    else:
        raise BillError("No built-in billing adapter for this provider")
    if start is not None and end is not None:
        start, end = checked_period(start, end)
    return Bill(provider.name, issued, start, end, dollars(amount), due,
                service_address=provider.service_address, amount_label=amount_label)


async def portal_text(page, pattern: str) -> str:
    # Match the rendered text we actually parse. textContent-based locators can
    # concatenate adjacent table cells and lose the separators between dates.
    await page.wait_for_function(
        "pattern => new RegExp(pattern, 'i').test(document.body?.innerText || '')",
        arg=pattern,
    )
    return await page.locator("body").inner_text()


async def extract_portal_bill(page, provider: Provider) -> Bill:
    """Navigate read-only billing routes observed in each customer portal."""
    if provider.key == "xfinity":
        # Billing destination published in Xfinity's customer billing help.
        # After sign-in, open the account's bill details in interactive mode.
        await page.wait_for_url(lambda url: urlsplit(url).scheme == "https"
            and urlsplit(url).hostname in ("customer.xfinity.com", "www.xfinity.com", "my.xfinity.com"))
        check_origin(page, provider)
        await page.goto("https://customer.xfinity.com/billing/services/bill/current", wait_until="domcontentloaded")
        text = await portal_text(page, rf"(?:total new charges|new charges|total amount due|amount due)\s*:?\s*{MONEY}")
        check_origin(page, provider)
        return parse_portal_bill(provider, text)
    account_hosts = {"dominion": "myaccount.dominionenergy.com", "washington_gas": "my.washingtongas.com",
                     "american_water": "mywaterv2.amwater.com", "alexrenew": "myalexrenew.com"}
    expected = account_hosts[provider.key]
    # Wait through OAuth redirects; a transient identity-provider page is not
    # the final account origin and must not cause a premature extraction error.
    await page.wait_for_url(lambda url: urlsplit(url).scheme == "https" and urlsplit(url).hostname == expected)
    check_origin(page, provider)
    if provider.key == "dominion":
        await page.locator("#dtBilling").wait_for(state="visible")
        await page.goto("https://myaccount.dominionenergy.com/portal/#/ViewBill", wait_until="domcontentloaded")
        text = await portal_text(page, rf"Current Charges\s*{MONEY}")
        # Preserve period/amount before visiting the history table for issue date.
        await page.goto("https://myaccount.dominionenergy.com/portal/#/Billinghistory", wait_until="domcontentloaded")
        history = await portal_text(page, rf"{DATE}\s+{DATE}\s+\d+\s*\$")
    elif provider.key == "washington_gas":
        await page.locator("#btnviewbill").wait_for(state="visible")
        await page.goto("https://my.washingtongas.com/portal/BillingHistory.aspx", wait_until="domcontentloaded")
        text = await portal_text(page, rf"{DATE}\s+{DATE}\s+{DATE}\s*\$")
        history = None
    elif provider.key == "american_water":
        # The OAuth callback initializes this app's customer session. Preserve
        # that page rather than interrupting its redirect with a fresh root load.
        text = await portal_text(page, rf"Amount Due\s*:\s*{MONEY}")
        history = None
    else:
        # Reading this route directly avoids the dashboard's profile-review
        # dialog; it neither accepts that dialog nor changes profile information.
        # At smaller viewports the Billing nav link is in a closed mobile menu.
        # The bill card is a better sign that the authenticated app is ready.
        dashboard = await portal_text(page, r"Wastewater bill")
        await page.goto("https://myalexrenew.com/billing", wait_until="domcontentloaded")
        text = await portal_text(page, rf"Total Charges\s*{MONEY}")
        history = dashboard
    check_origin(page, provider)
    return parse_portal_bill(provider, text, history)


async def extract_bill(page, provider: Provider, output_dir: Path) -> Bill:
    """Read a statement detail or labelled statement rows from the current page."""
    if provider.ready_selector:
        await page.locator(provider.ready_selector).wait_for(state="visible")
    rows = page.locator(provider.row_selector)
    candidates = []
    texts = []
    for i in range(await rows.count()):
        row = rows.nth(i)
        if not await row.is_visible():
            continue
        text = await row.inner_text()
        if re.search(r"statement date|bill date|billing date", text, re.I):
            candidates.append(row)
            texts.append(text)
    if texts:
        index = latest_index(texts)
        source, text = candidates[index], texts[index]
    else:
        # Detail pages work without a history table; multiple dates fail closed.
        source, text = page.locator("body"), await page.locator("body").inner_text()
    bill = replace(parse_bill(provider.name, text), service_address=provider.service_address)
    if provider.download_selector:
        # Scope download to the selected statement, never the first history row.
        async with page.expect_download() as event:
            await source.locator(provider.download_selector).click()
        download = await event.value
        destination = output_dir / f"{provider.key}-{bill.statement_date.isoformat()}.pdf"
        await download.save_as(destination)
        destination.chmod(0o600)
        with destination.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                destination.unlink()
                raise BillError("Statement download was not a PDF")
        bill = replace(bill, pdf_path=destination)
    return bill


def compose_email(report: Report, renters: RentalSettings | None = None) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Latest utility bills" + (" — incomplete" if report.errors else "")
    lines = ["Here are the latest available utility statements.", ""]
    for bill in report.bills:
        lines += [bill.provider]
        if bill.service_address:
            lines.append(f"  Configured service address: {bill.service_address}")
        if bill.source_note:
            lines.append(f"  Source: {bill.source_note}")
        lines += [
                  f"  Statement date: {bill.statement_date.isoformat() if bill.statement_date else 'Not listed'}",
                  f"  Service period: {bill.period_start.isoformat() if bill.period_start else 'Not listed'} to {bill.period_end.isoformat() if bill.period_end else 'Not listed'}",
                  f"  {bill.amount_label}: ${bill.amount:,.2f}",
                  f"  Due date: {bill.due_date.isoformat() if bill.due_date else 'Not listed'}", ""]
    if report.bills:
        total = sum((bill.amount for bill in report.bills), Decimal("0"))
        lines += [f"Total statement amounts{' (partial)' if report.errors else ''}: ${total:,.2f}",
                  "Statement amounts may differ from current balances after payments.", ""]
    if report.errors:
        lines += ["Could not retrieve:"] + [f"  {name}: {error}" for name, error in report.errors.items()]
    if not report.bills:
        lines += ["No bill amounts or service periods were retrieved."]
    if renters and report.bills:
        lines += ["", format_renter_shares(report.bills, renters, partial=bool(report.errors))]
    message.set_content("\n".join(lines) + "\n")
    return message


ManualStep = Callable[[object, Provider], Awaitable[None]]


async def collect_latest_bills(credentials: dict[str, Credentials], *,
                               providers=PROVIDERS, headless=True,
                               manual_step: ManualStep | None = None,
                               output_dir=Path("output"),
                               renters: RentalSettings | None = None,
                               browser_channel: str | None = None,
                               xfinity_statement: Bill | None = None,
                               xfinity_live: bool = False) -> tuple[Report, EmailMessage]:
    """Log in separately to each provider and return bills plus an unsent email.

    manual_step can finish MFA and select an account in a visible browser.
    Built-in adapters read supported portals; explicit billing selector
    overrides retain the generic statement extractor.
    All contexts are ephemeral: passwords and sessions are not persisted.
    """
    if browser_channel not in (None, "chrome"):
        raise ValueError("Browser channel must be chrome or None for bundled Chromium")
    if xfinity_live and xfinity_statement is not None:
        raise ValueError("Choose live Xfinity login or a saved statement, not both")
    providers = tuple(providers)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = Report()
    to_fetch = []
    for provider in providers:
        if provider.key == "xfinity" and not xfinity_live:
            try:
                bill = xfinity_statement or load_default_xfinity_statement()
                report.bills.append(replace(bill, provider=provider.name,
                                            service_address=provider.service_address))
            except (ValueError, BillError) as error:
                report.errors[provider.name] = str(error)
            continue
        if provider.key not in credentials:
            report.errors[provider.name] = "Credentials not supplied"
        else:
            to_fetch.append(provider)
    if not to_fetch:
        return report, compose_email(report, renters)
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
    async with async_playwright() as playwright:
        launch_options = {"headless": headless}
        if browser_channel:
            launch_options["channel"] = browser_channel
        browser = await playwright.chromium.launch(**launch_options)
        try:
            for provider in to_fetch:
                context = await browser.new_context(accept_downloads=True)
                page = await context.new_page()
                page.set_default_timeout(30000)
                try:
                    try:
                        await login(page, provider, credentials[provider.key])
                    except (BillError, PlaywrightTimeoutError):
                        if not manual_step:
                            raise
                        # Keep the browser available for manual recovery instead
                        # of closing it when the site's login handler is blocked.
                        print(f"{provider.name}: automatic sign-in needs attention; finish sign-in in the open browser.")
                    if manual_step:
                        await manual_step(page, provider)
                    if provider.ready_selector or provider.billing_steps or provider.download_selector:
                        for selector in provider.billing_steps:
                            await page.locator(selector).click()
                        await ensure_statement_page(page, provider)
                        bill = await extract_bill(page, provider, output_dir)
                    else:
                        bill = await extract_portal_bill(page, provider)
                    report.bills.append(bill)
                except BillError as error:
                    report.errors[provider.name] = str(error)
                except Exception:
                    # Playwright exceptions can include sensitive page details.
                    report.errors[provider.name] = "Portal operation failed; check login, MFA, and billing selectors"
                finally:
                    await context.close()
        finally:
            await browser.close()
    order = {provider.name: i for i, provider in enumerate(providers)}
    report.bills.sort(key=lambda bill: order[bill.provider])
    return report, compose_email(report, renters)


def load_providers(path: Path | None):
    if path is None:
        return PROVIDERS
    overrides = json.loads(path.read_text())
    known = {p.key for p in PROVIDERS}
    if set(overrides) - known:
        raise ValueError("Unknown provider in configuration")
    allowed = {"billing_steps", "ready_selector", "row_selector", "download_selector"}
    result = []
    for provider in PROVIDERS:
        settings = overrides.get(provider.key, {})
        if set(settings) - allowed:
            raise ValueError("Only billing selectors can be overridden")
        result.append(replace(provider, **settings))
    return tuple(result)


def apply_property_settings(providers, path: Path):
    """Apply the same locally configured service address to every profile.

    This is account-selection guidance, not proof of the address on a statement.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        raise ValueError("Could not read property settings; check its path and JSON format") from None
    if (not isinstance(data, dict) or set(data) != {"service_address"}
            or not isinstance(data["service_address"], str)):
        raise ValueError("Property settings must contain a service_address string")
    service_address = " ".join(data["service_address"].split()) or None
    return tuple(replace(provider, service_address=service_address) for provider in providers)


def load_credentials(path: Path | None) -> dict[str, Credentials]:
    """Read a private JSON file without including its contents in errors."""
    if path is None:
        return {}
    try:
        with path.open() as stream:
            if os.name == "posix" and os.fstat(stream.fileno()).st_mode & 0o077:
                raise ValueError("Credentials file must be owner-only; run chmod 600 on it")
            data = json.load(stream)
    except (OSError, json.JSONDecodeError, UnicodeError):
        raise ValueError("Could not read credentials file; check its path and JSON format") from None
    if not isinstance(data, dict) or set(data) - {p.key for p in PROVIDERS}:
        raise ValueError("Credentials file must contain known provider keys")
    credentials = {}
    for key, entry in data.items():
        if (not isinstance(entry, dict) or set(entry) != {"username", "password"}
                or not all(isinstance(value, str) for value in entry.values())):
            raise ValueError("Each provider must have username and password strings")
        if not entry["username"] and not entry["password"]:
            continue
        if not entry["username"] or not entry["password"]:
            raise ValueError("Fill both username and password, or leave both empty")
        credentials[key] = Credentials(entry["username"], entry["password"])
    return credentials


def load_xfinity_statement(path: Path | None) -> Bill | None:
    """Explicit import when Xfinity only works in the user's normal browser."""
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ValueError("Could not read the saved Xfinity statement text") from None
    provider = next(p for p in PROVIDERS if p.key == "xfinity")
    bill = parse_portal_bill(provider, text)
    return replace(bill, source_note="Saved Xfinity statement; refresh this file when a new bill is issued")


def load_default_xfinity_statement(path: Path | None = None, *, today: date | None = None) -> Bill:
    """Avoid the known stalled login; never silently reuse an expired snapshot."""
    path = path or Path(__file__).resolve().parent / "output/statements/xfinity-current.txt"
    if not path.exists():
        raise BillError("Saved Xfinity statement is missing; save the current bill to output/statements/xfinity-current.txt or use --xfinity-statement. Automatic login is disabled because it stalls")
    bill = load_xfinity_statement(path)
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    if bill.period_end is None:
        raise BillError("Saved Xfinity statement needs a service period; refresh the statement file. Automatic login was not attempted")
    if bill.period_end < today:
        raise BillError("Saved Xfinity statement's service period has ended; refresh the statement file or explicitly select a historical bill with --xfinity-statement. Automatic login was not attempted")
    if bill.statement_date and bill.statement_date > today:
        raise BillError("Saved Xfinity statement has a future issue date; check the statement file")
    return bill


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interactive", action="store_true", help="Visible browser for MFA and statement navigation")
    parser.add_argument("--browser", choices=("chromium", "chrome"), default="chromium",
                        help="Bundled Chromium or installed Google Chrome")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--credentials", type=Path, help="Private JSON credentials file (defaults to credentials.json beside the script)")
    xfinity_options = parser.add_mutually_exclusive_group()
    xfinity_options.add_argument("--xfinity-statement", type=Path,
                        help="Use a saved Xfinity statement (defaults to output/statements/xfinity-current.txt beside the script)")
    xfinity_options.add_argument("--xfinity-live", action="store_true",
                        help="Explicitly attempt Xfinity's currently unreliable automated login")
    parser.add_argument("--property-file", type=Path,
                        default=Path(__file__).resolve().with_name("property.json"),
                        help="JSON file with the shared service_address for all profiles")
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--renters-file", type=Path, help="Renter names and occupancy dates (defaults to renters.json beside the script)")
    parser.add_argument("--provider", choices=[p.key for p in PROVIDERS], action="append")
    args = parser.parse_args()
    renters_path = args.renters_file
    if renters_path is None:
        default_renters = Path(__file__).resolve().with_name("renters.json")
        renters_path = default_renters if default_renters.exists() else None
    try:
        renters = load_renters(renters_path)
        xfinity_statement = load_xfinity_statement(args.xfinity_statement)
    except (ValueError, BillError) as error:
        parser.error(str(error))
    providers = tuple(p for p in load_providers(args.config) if not args.provider or p.key in args.provider)
    try:
        providers = apply_property_settings(providers, args.property_file)
    except ValueError as error:
        parser.error(str(error))
    credential_path = args.credentials
    if credential_path is None:
        default_path = Path(__file__).resolve().with_name("credentials.json")
        credential_path = default_path if default_path.exists() else None
    try:
        credentials = load_credentials(credential_path)
    except ValueError as error:
        parser.error(str(error))
    for provider in providers:
        if provider.key == "xfinity" and not args.xfinity_live:
            continue
        if provider.key in credentials:
            continue
        prefix = provider.key.upper()
        username = os.environ.get(f"{prefix}_USERNAME")
        password = os.environ.get(f"{prefix}_PASSWORD")
        if args.interactive:
            username = username or getpass(f"{provider.name} username: ")
            password = password or getpass(f"{provider.name} password: ")
        if username and password:
            credentials[provider.key] = Credentials(username, password)

    async def manual(page, provider):
        if provider.service_address:
            print(f"Select the account for service address: {provider.service_address}")
        print(f"{provider.name}: complete sign-in/MFA if requested and select ONE utility account. Leave the customer dashboard open; billing pages will be read automatically.")
        await asyncio.to_thread(input, "Press Enter when the account dashboard is ready: ")

    report, message = await collect_latest_bills(credentials, providers=providers,
        headless=not args.interactive, manual_step=manual if args.interactive else None,
        output_dir=args.output, renters=renters,
        browser_channel="chrome" if args.browser == "chrome" else None,
        xfinity_statement=xfinity_statement, xfinity_live=args.xfinity_live)
    destination = args.output / "utility-bills.eml"
    with open(destination, "w", encoding="utf-8",
              opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(message.as_string())
    print(f"Email draft saved to {destination}. Retrieved {len(report.bills)}/{len(providers)} providers.")
    for bill in report.bills:
        if bill.provider == "Xfinity Internet" and bill.source_note:
            print("Xfinity: used a saved statement; refresh the file when a new bill is issued.")
    for provider_name, error in report.errors.items():
        print(f"{provider_name}: {error}")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
