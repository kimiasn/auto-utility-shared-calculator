"""Real Chromium tests against synthetic pages, without external services."""
import importlib.util
import asyncio
from base64 import b64encode
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from utility_bills import (BillError, Credentials, PROVIDERS, collect_latest_bills,
                           extract_bill, extract_portal_bill, login, parse_bill, parse_portal_bill)
from tests.test_utility_bills import statement
from tests.test_portal_bills import DOMINION, DOMINION_HISTORY, GAS, WATER, ALEX, XFINITY
from renter_shares import Renter, RentalSettings


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Install requirements and Chromium")
class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch()
        self.page = await self.browser.new_page(accept_downloads=True)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def test_login_to_bill_selection_and_download(self):
        provider = replace(PROVIDERS[3], ready_selector="#statements", download_selector="a")
        history = ("<table id=statements>" + "".join(
            f"<tr><td>{statement(issued, amount)}</td><td><a href='data:application/pdf;base64,{b64encode(f'%PDF-1.4 synthetic statement {i}'.encode()).decode()}' download='bill.pdf'>Download</a></td></tr>"
            for i, (issued, amount) in enumerate([("09/15/2026", "500.00"), ("10/15/2026", "0.00")])) + "</table>")
        async def serve(route):
            url = route.request.url
            if url.split("?", 1)[0].endswith("/history"):
                await route.fulfill(content_type="text/html", body=history)
            else:
                await route.fulfill(content_type="text/html", body='''
                    <form action=/history><label>Email<input id=login-email></label>
                    <label>Password<input id=login-password type=password></label>
                    <button>Log into Portal</button></form>''')
        await self.page.route("https://myalexrenew.com/**", serve)
        await login(self.page, provider, Credentials("fixture-user", "fixture-password"))
        with TemporaryDirectory() as directory:
            bill = await extract_bill(self.page, provider, Path(directory))
            self.assertEqual(str(bill.amount), "0.00")
            self.assertEqual(bill.statement_date.isoformat(), "2026-10-15")
            self.assertEqual(bill.pdf_path.read_bytes(), b"%PDF-1.4 synthetic statement 1")

    async def test_single_detail_page(self):
        await self.page.set_content(f"<main>{statement()}</main>")
        with TemporaryDirectory() as directory:
            bill = await extract_bill(self.page, PROVIDERS[0], Path(directory))
            self.assertEqual(str(bill.amount), "123.45")

    async def test_american_water_landing_then_username_then_password(self):
        submitted = []
        async def serve(route):
            url = urlsplit(route.request.url)
            if url.hostname == "login.amwater.com":
                await route.fulfill(content_type="text/html", body='''
                    <button id=submitLoginButton onclick="location.href='https://auth.amwater.com/login'">LOGIN</button>
                ''')
            elif url.path == "/session":
                submitted.append(route.request.post_data_json)
                await route.fulfill(content_type="application/json", body='{}')
            elif url.hostname == "auth.amwater.com":
                await route.fulfill(content_type="text/html", body='''
                    <input name=identifier><input type=submit value=Next id=next>
                    <script>
                      document.querySelector('#next').onclick = () => {
                        const username = document.querySelector('[name=identifier]').value;
                        document.body.innerHTML = '<input type=password><input type=submit value="Sign In">';
                        document.querySelector('[type=submit]').onclick = async () => {
                          await fetch('/session', {method:'POST', body:JSON.stringify({username,
                            password:document.querySelector('[type=password]').value})});
                          location.href='https://mywaterv2.amwater.com/openidlogin';
                        };
                      };
                    </script>
                ''')
            else:
                await route.fulfill(content_type="text/html", body="<main>Fixture customer portal</main>")
        await self.page.route("https://*.amwater.com/**", serve)
        await login(self.page, PROVIDERS[2], Credentials("fixture-user", "fixture-password"))
        await self.page.wait_for_url("https://mywaterv2.amwater.com/openidlogin")
        self.assertEqual(submitted, [{"username": "fixture-user", "password": "fixture-password"}])

    async def test_washington_gas_waits_for_script_and_submits_once(self):
        submitted = []
        async def serve(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/login.js"):
                await asyncio.sleep(0.15)  # Visible form before deferred handler.
                await route.fulfill(content_type="text/javascript", body='''
                    document.querySelector('#btnlogin').addEventListener('click', async () => {
                      await fetch('default.aspx/updateState', {method:'POST',
                        body:JSON.stringify({username:document.querySelector('#txtLogin').value,
                                             password:document.querySelector('#txtpwd').value})});
                      document.querySelector('form').remove();
                    });
                    window.addEventListener('load', () => document.querySelector('#page_loader_custom').remove());
                ''')
            elif path.endswith("/default.aspx/updateState"):
                submitted.append(route.request.post_data_json)
                await route.fulfill(content_type="application/json", body='{}')
            else:
                await route.fulfill(content_type="text/html", body='''
                    <div id=page_loader_custom>Loading</div>
                    <form><input id=txtLogin><input id=txtpwd type=password>
                    <input id=btnlogin type=submit onclick="return false;" value="Log In"></form>
                    <script defer src="login.js"></script>
                ''')
        await self.page.route("https://my.washingtongas.com/**", serve)
        await login(self.page, PROVIDERS[1], Credentials("fixture-user", "fixture-password"))
        await self.page.locator("#txtLogin").wait_for(state="hidden")
        self.assertEqual(submitted, [{"username": "fixture-user", "password": "fixture-password"}])

    async def test_xfinity_username_then_password(self):
        submitted = []
        async def serve(route):
            path = urlsplit(route.request.url).path
            if path == "/session":
                submitted.append(route.request.post_data_json)
                await route.fulfill(content_type="application/json", body='{}')
            elif urlsplit(route.request.url).hostname == "login.xfinity.com":
                await route.fulfill(content_type="text/html", body='''
                    <input id=user><button id=sign_in>Let's go</button>
                    <script>
                    document.querySelector('#sign_in').onclick = () => {
                      const username = document.querySelector('#user').value;
                      document.body.innerHTML = '<input type=password><button id=sign_in>Sign in</button>';
                      document.querySelector('#sign_in').onclick = async () => {
                        await fetch('/session', {method:'POST', body:JSON.stringify({username,
                          password:document.querySelector('[type=password]').value})});
                        location.href='https://customer.xfinity.com/billing';
                      };
                    };
                    </script>
                ''')
            else:
                await route.fulfill(content_type="text/html", body="<main>Fixture billing</main>")
        await self.page.route("https://*.xfinity.com/**", serve)
        await login(self.page, PROVIDERS[4], Credentials("fixture-user", "fixture-password"))
        await self.page.wait_for_url("https://customer.xfinity.com/billing")
        self.assertEqual(submitted, [{"username": "fixture-user", "password": "fixture-password"}])

    async def test_provider_failure_preserves_other_result(self):
        providers = tuple(replace(p, ready_selector="body") for p in PROVIDERS[:2])
        credentials = {p.key: Credentials("fixture-user", "fixture-password") for p in providers}
        bill = parse_bill(providers[1].name, statement())
        with TemporaryDirectory() as directory, \
                patch("utility_bills.login", new=AsyncMock(side_effect=[BillError("Login failed"), None])), \
                patch("utility_bills.check_origin"), \
                patch("utility_bills.extract_bill", new=AsyncMock(return_value=bill)):
            report, email = await collect_latest_bills(credentials, providers=providers, output_dir=Path(directory))
        self.assertEqual(report.bills, [bill])
        self.assertEqual(report.errors, {providers[0].name: "Login failed"})
        self.assertIn("incomplete", email["Subject"])

    async def test_saved_xfinity_statement_skips_login_and_splits_renters(self):
        provider = PROVIDERS[4]
        bill = replace(parse_portal_bill(provider, XFINITY), source_note="Saved Xfinity statement")
        renters = RentalSettings((Renter("a", "A"), Renter("b", "B"), Renter("c", "C")))
        with TemporaryDirectory() as directory, patch("utility_bills.login", new=AsyncMock()) as login_mock:
            report, message = await collect_latest_bills({}, providers=(provider,),
                output_dir=Path(directory), renters=renters, xfinity_statement=bill)
        login_mock.assert_not_awaited()
        self.assertEqual(report.errors, {})
        self.assertEqual(report.bills[0].amount, bill.amount)
        self.assertIn("A: $20.00", message.get_content())
        self.assertIn("Saved Xfinity statement", message.get_content())

    async def test_xfinity_live_login_requires_explicit_opt_in(self):
        provider = PROVIDERS[4]
        bill = parse_portal_bill(provider, XFINITY)
        with TemporaryDirectory() as directory, \
                patch("utility_bills.login", new=AsyncMock()) as login_mock, \
                patch("utility_bills.extract_portal_bill", new=AsyncMock(return_value=bill)), \
                patch("utility_bills.load_default_xfinity_statement", side_effect=AssertionError("Live mode should not use cache")):
            report, _ = await collect_latest_bills({provider.key: Credentials("fixture-user", "fixture-password")},
                providers=(provider,), output_dir=Path(directory), xfinity_live=True)
        login_mock.assert_awaited_once()
        self.assertEqual(report.bills, [bill])

    async def test_default_saved_xfinity_keeps_other_provider_flow_and_order(self):
        dominion = replace(PROVIDERS[0], ready_selector="body")
        xfinity = PROVIDERS[4]
        gas_bill = parse_bill(dominion.name, statement())
        internet_bill = parse_portal_bill(xfinity, XFINITY)
        with TemporaryDirectory() as directory, \
                patch("utility_bills.load_default_xfinity_statement", return_value=internet_bill), \
                patch("utility_bills.login", new=AsyncMock()) as login_mock, \
                patch("utility_bills.check_origin"), \
                patch("utility_bills.extract_bill", new=AsyncMock(return_value=gas_bill)):
            report, _ = await collect_latest_bills({dominion.key: Credentials("fixture-user", "fixture-password")},
                providers=(dominion, xfinity), output_dir=Path(directory))
        login_mock.assert_awaited_once()
        self.assertEqual(login_mock.await_args.args[1].key, dominion.key)
        self.assertEqual(report.bills, [gas_bill, internet_bill])
        self.assertEqual(report.errors, {})

    async def test_interactive_login_failure_keeps_browser_for_recovery(self):
        provider = replace(PROVIDERS[1], ready_selector="body")
        manual = AsyncMock()
        bill = parse_bill(provider.name, statement())
        with TemporaryDirectory() as directory, \
                patch("utility_bills.login", new=AsyncMock(side_effect=BillError("Login request blocked"))), \
                patch("utility_bills.check_origin"), \
                patch("builtins.print"), \
                patch("utility_bills.extract_bill", new=AsyncMock(return_value=bill)):
            report, _ = await collect_latest_bills(
                {provider.key: Credentials("fixture-user", "fixture-password")},
                providers=(provider,), manual_step=manual, output_dir=Path(directory))
        manual.assert_awaited_once()
        self.assertEqual(report.bills, [bill])
        self.assertEqual(report.errors, {})

    async def test_provider_billing_navigation_and_amounts(self):
        expected = {"dominion": "11.22", "washington_gas": "12.34", "american_water": "23.45", "alexrenew": "34.56", "xfinity": "60.00"}
        entry_urls = ("https://myaccount.dominionenergy.com/portal/", "https://my.washingtongas.com/portal/Dashboard.aspx",
                      "https://mywaterv2.amwater.com/", "https://myalexrenew.com/", "https://customer.xfinity.com/billing")
        for provider, entry in zip(PROVIDERS, entry_urls):
            with self.subTest(provider=provider.key):
                await self.page.unroute("**/*")
                async def serve(route):
                    if provider.key == "dominion":
                        # A real SPA switches bodies on hash navigation.
                        import json
                        html = f'''<script>
                          const summary={json.dumps(DOMINION)}, historyText={json.dumps(DOMINION_HISTORY)};
                          function render() {{ document.querySelector('main').innerText = location.hash.includes('Billinghistory') ? historyText : summary; }}
                          window.addEventListener('hashchange',render);
                          window.addEventListener('DOMContentLoaded',render);
                        </script><a id=dtBilling>Billing</a><main></main>'''
                    else:
                        html = "<main>" + {"washington_gas": GAS, "american_water": WATER, "alexrenew": ALEX, "xfinity": XFINITY}[provider.key] + "</main>"
                        if provider.key == "washington_gas":
                            # No text-node spaces between table cells: the page's
                            # rendered text still separates the column values.
                            html = ('<a id=btnviewbill>VIEW BILL</a><table><tr>'
                                '<th>BILL DATE</th><th>BILL PERIOD START DATE</th>'
                                '<th>BILL PERIOD END DATE</th><th>BILL AMOUNT ($)</th></tr>'
                                '<tr><td>October 21, 2026</td><td>September 17, 2026</td>'
                                '<td>October 15, 2026</td><td>$12.34</td></tr></table>')
                        elif provider.key == "alexrenew":
                            html += '<p>Wastewater bill</p><a href="/billing" style="display:none">Billing</a>'
                    await route.fulfill(content_type="text/html", body=html)
                await self.page.route("**/*", serve)
                await self.page.goto(entry)
                bill = await extract_portal_bill(self.page, provider)
                self.assertEqual(str(bill.amount), expected[provider.key])
                self.assertIsNotNone(bill.period_start)
                self.assertIsNotNone(bill.period_end)


if __name__ == "__main__":
    unittest.main()
