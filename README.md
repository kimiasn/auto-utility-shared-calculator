# Utility bill email draft

Python function for Dominion Energy, Washington Gas, American Water, AlexRenew,
and Xfinity Internet.
Returns each latest statement's amount, statement date, service period, and due date,
and an `EmailMessage`. No email is sent and no payment controls are used.

## Setup and run

```sh
cd /Users/kniasar/workspace/utility-bills
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers"
.venv/bin/python -m playwright install chromium
.venv/bin/python utility_bills.py --interactive
```

To use installed Google Chrome instead of bundled Chromium, add `--browser chrome`.
This uses a separate temporary browser context; it does not load your normal Chrome
profile or save login sessions. Xfinity waits for page load before filling its ID.

Use `--provider dominion` (or `washington_gas`, `american_water`, `alexrenew`, `xfinity`)
to run a single provider. Repeat the option to choose several.
### Shared service address

Set `service_address` in `property.json` beside the script to your full property
address, including any unit number, city, state, and ZIP code. All provider
profiles load this one value automatically. For example (fictional address):

```json
{
  "service_address": "123 Example Street, Unit 4, Alexandria, VA 22314"
}
```

The interactive prompt reminds you which property account to select, and each
collected bill carries this setting into the email as `Configured service address`.
This value is locally configured; the collector does not yet verify it against
the portal, automatically select an account by address, or update addresses at the
utilities. Continue checking the selected account's address in the browser.
An empty value omits the address from prompts and drafts.

The property file is ignored by Git. Use `--property-file /path/to/property.json`
for another file. When calling the function directly, use
`apply_property_settings(PROVIDERS, Path("property.json"))` and pass the resulting
profiles as `providers` to `collect_latest_bills`.

### Credentials file

Fill in the empty username and password strings in `credentials.json` beside
the script. It is loaded automatically and takes precedence over environment
variables and prompts. Leave both fields empty for providers you have not filled
in yet; interactive mode will prompt for them if selected.

This file stores passwords as plaintext, is ignored by Git, and must have
owner-only permissions (`chmod 600 credentials.json` on macOS/Linux).
Use `--credentials /path/to/private-file.json` to load a different file.
JSON passwords containing a double quote or backslash must escape those
characters as `\"` or `\\` respectively.

Username and password prompts are hidden. Alternatively, provide
`DOMINION_USERNAME`/`DOMINION_PASSWORD`, `WASHINGTON_GAS_USERNAME`/`WASHINGTON_GAS_PASSWORD`,
`AMERICAN_WATER_USERNAME`/`AMERICAN_WATER_PASSWORD`, `ALEXRENEW_USERNAME`/`ALEXRENEW_PASSWORD`,
and `XFINITY_USERNAME`/`XFINITY_PASSWORD`
through your local environment. Keep real credentials out of source code and chat.

Xfinity uses a saved statement by default because its automated login repeatedly
gets stuck at the ID step. The usual command does **not** open a Xfinity browser
or ask for its credentials. It automatically reads
`output/statements/xfinity-current.txt` beside the script, while the other
providers continue using their normal login flows.
The Xfinity bill format was verified through the existing normal Chrome session:
Billing Date, New charges, and the dates under Regular monthly charges. Automatic
Xfinity sign-in remains blocked at the ID step in fresh automation sessions, even
with installed Chrome. No special flag is needed for the saved-statement default:

```sh
.venv/bin/python utility_bills.py --interactive
```

The supplied text file contains the bill fields verified from the current Xfinity
statement in normal Chrome. The default skips Xfinity login only, keeps the other
provider flows, and includes the imported amount in the email and renter split.
It is a snapshot, not a live refresh: replace its Billing Date, date range, and New
charges from the next statement before reusing it for a later bill. The email
identifies its saved-statement source. If the default file is missing, lacks service
dates, has a future issue date, or its service period has ended, Xfinity is reported
as needing attention, with no automatic live-login fallback. This check uses the
America/New_York date. The file remains a snapshot and cannot detect a newer bill
issued before its service period ends.

Use `--xfinity-statement /path/to/statement.txt` to explicitly choose another saved
statement, including a historical bill. Only use `--xfinity-live` if you intentionally
want to retry automated login; these options are mutually exclusive. Live mode uses
the `xfinity` entry in `credentials.json`, starts at `https://login.xfinity.com/login`,
and attempts the ID and password steps before reading
`https://customer.xfinity.com/billing/services/bill/current`. If that flow is blocked,
interactive mode keeps its browser available for manual recovery. No normal Chrome
cookies or sessions are copied or stored by the collector.
When dates are unavailable, it retains the bill amount and reports renter portions
as unallocated. Xfinity amounts and confirmed service periods feed the same renter
split and email totals as other providers.

### Renters and prorated portions

Edit `renters.json` beside the script to enter renter names and move-in dates.
The provided file has three equal shares: one renter present for the full period
(`move_in: null`) and two who moved in on `2026-09-14`. Replace the placeholder
names with your renters' names. The file is private locally and ignored by Git.
It loads automatically with the usual run command; `--renters-file /path/to/file.json`
can select another file.

Each renter pays a fixed one-third of each service day's bill cost only for days
they occupied the property. The owner covers vacant shares. The full-period renter
continues paying one-third before the other renters arrive. The entire bill amount,
including fixed charges, is spread evenly over the bill's actual service period.
The bill's issue date and payment due date do not determine occupied days.

Move-in and move-out days count as occupied. Both service-period endpoints count
by default; set `period_end_inclusive` to `false` if your chosen convention excludes
the ending date. A null move-in means present from before every bill's start date;
a null move-out means still living there. Dates use `YYYY-MM-DD`.

For a $90 September 1–30 bill, the full-period renter pays $30; each September 14
renter pays $17 for 17 occupied days; the owner covers $26. Renter portions plus
the owner portion always equal the bill, including credits. Portions round once
per bill using largest remainders; equal penny ties follow renters-file order,
then the owner. Totals sum those rounded bill portions.

To add a renter, add an entry with a unique `id`, a `name`, `move_in`, and `move_out`.
Keep `total_shares` at 3 for this three-share property, including historical replacement
renters. The calculator rejects more simultaneous renters than available shares.
Change `total_shares` only if you intend to change the split for every bill being
calculated. Missing service periods are reported as unallocated, and incomplete
reports mark renter totals as partial.

The email includes each bill's renter portions, occupied-day counts, owner portion,
and totals per person. To use the calculator directly, call
`split_bill(bill, load_renters(Path("renters.json")))` from `renter_shares`.

Interactive mode opens a browser for login, MFA, and account selection. Complete
any challenge yourself, choose one account, and leave its dashboard open.
Then press Enter in the terminal; the script navigates to billing pages itself.
CAPTCHA and MFA are not bypassed. Login itself
can require manual recovery during a multistep flow; timeouts and reported login
problems leave the browser available in interactive mode. Washington Gas waits for
page initialization and checks that the normal Log In click starts its AJAX request.
It then waits for the login form to disappear before showing the statement prompt.
If a portal blocks that request or shows a challenge, finish signing in yourself.
Press Enter only after the customer account dashboard is visible. Continuing on a login screen
reports a sign-in error instead of trying to parse that screen as a bill.
American Water opens `https://login.amwater.com/`, clicks its customer LOGIN
button, and fills the username-first authentication form followed by the password.

The draft is saved to `output/utility-bills.eml` with owner-only permissions. The
directory and credentials file are not uploaded anywhere. Credentials are submitted
only to the selected utility login pages. Browser contexts
are isolated per provider and discarded after each attempt. Downloaded statements
and the email draft contain private bill data; `output/` is ignored by Git.

## Function

```python
from utility_bills import Credentials, collect_latest_bills

report, email = await collect_latest_bills(
    {"dominion": Credentials(username, password)},
    providers=verified_providers,
)
body = email.get_content()
```

`report.errors` contains provider-specific failures; successfully collected bills
remain available. The draft explicitly marks partial results. Totals sum statement
amounts, which can differ from current balances after payment.

## Bill extraction

The built-in adapters use customer billing pages inspected on October 30, 2026:

| Provider | Amount source | Service period source |
| --- | --- | --- |
| Dominion | Current Charges in Bill Summary, checked against the latest history row | Bill Period in Bill Summary |
| Washington Gas | Bill Amount in the latest dated statement row | Bill Period Start/End Date columns |
| American Water | Amount Due in the My Bill card | Service Period in the same card |
| AlexRenew | Total Charges in Account Billing Summary | Residential Service Bill Period |
| Xfinity Internet | New charges in the current statement | Dates under Regular monthly charges |

The adapters wait for amounts to finish loading instead of reading placeholders.
They preserve the amount's meaning in the draft (current charges, statement bill
amount, or current amount due). Dashboard usage estimates and previous balances
are not substitutes for bill amounts. Missing issue dates are shown as `Not listed`
instead of discarding known amounts and service periods. Dominion uses its billing
history to obtain the statement date; Washington Gas reads it from the bill table.

Interactive mode still requires you to complete MFA and select the correct account.
Use `--interactive` for the verified workflow; Washington Gas may reject a
background/headless login even when the same credentials work in a visible browser.
The configured address is a reminder, not an automatic account selector. The default
adapters extract bill details; automatic PDF archiving is available through the
custom selector flow below. No PDF contents are parsed by these adapters.

### Optional custom selectors

To use the generic extraction flow instead, pass `--config selectors.json` with
overrides such as
the following **illustrative selectors**, replaced with observed selectors:

```json
{
  "dominion": {
    "billing_steps": ["a:has-text('Bill History')"],
    "ready_selector": "#statement-history",
    "row_selector": ".statement-card",
    "download_selector": "a:has-text('Download PDF')"
  }
}
```

Setting `ready_selector`, `billing_steps`, or `download_selector` opts that provider
into the generic extractor instead of its built-in adapter. Only billing selectors
can be overridden; login destinations remain fixed.

Extraction accepts explicitly labelled fields (`Statement Date`, `Bill Date`,
`Billing Period`, `Service Period`, `Amount Due`, etc.) using US dates with four-digit
years or ISO dates. A typical accepted statement is:

```text
Statement Date: 10/15/2026
Service Period: 09/10/2026 - 10/10/2026
Amount Due: $123.45
Due Date: October 30, 2026
```

It selects the greatest statement date from labelled statement rows, regardless
of row order or paid status. Tied latest dates fail rather than combining accounts.
History pagination must be configured/navigated to include the latest statement.
In this optional generic flow, tables with labels only in their column headers need
additional mapping. Missing/ambiguous fields fail instead of inventing dates
or substituting the dashboard balance. PDF-only statements and scanned PDFs need
an additional extraction adapter; optional PDF downloading currently archives the
selected statement, without parsing its contents.

## Tests

```sh
.venv/bin/python -m unittest discover -v
```

Unit tests cover date/amount parsing, latest selection, partial failures, secrets
in object representations, redirect checks, and each provider's login flow using
mocked fields. Browser tests exercise login, unsorted bill selection, and download
against synthetic local responses intercepted in real Chromium. They require
Playwright and its Chromium binary and do not authenticate to utility accounts.
If you open a new terminal, export `PLAYWRIGHT_BROWSERS_PATH` as shown above before
running the tests or collector, since the browser installation is local to this project.
