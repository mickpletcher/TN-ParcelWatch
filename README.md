# TN-ParcelWatch

A standalone Python 3.11+ project using only the standard library. Fetching, validation, exact owner comparison, Decimal tax totals, rendering, SMTP and status classification use no AI or model API. A single optional supervising agent launches one command and reads one bounded status line. No token usage was measured.

**Preview, not production-ready.** The current adapter targets Stewart County. The state ownership API returned `Token Required` in live testing, and the HTML tax adapter has only been tested with fixtures. Verify authorized live access, tax source coverage and results before scheduling runs or enabling email.

## Run the review demonstration

From this directory, on Linux or macOS with Python 3.11+ and timezone data:

```sh
python3 -m unittest discover -s tests -v
python3 run_supervised.py --config config.example.json --fixtures fixtures --output /tmp/stewart-demo/reports --state /tmp/stewart-demo/state
```

The synthetic example produces six matching owners and six nonzero balances totaling $193.00. All parcel IDs, owner names and balances in the demo are fictional. The example is fixture-only and cannot run against live sources. All reports are marked OFFLINE TEST. Full reports are written privately; stdout contains no owners, parcel IDs, addresses, URLs or email addresses.

No install, cloud subscription or model credential is needed. `watch.py` is the normal report entry point. `run_supervised.py` invokes it once, creates a fresh private output directory, suppresses subprocess logs, validates `summary.json`, and emits fewer than 512 bytes for the tested cases. A successful real finding is distinct from a failed lookup.

## Private configuration

Keep real config, report output, state, and credentials **outside this source directory and any repository**. Use a user-owned directory with mode 0700, config mode 0600 and umask 077. Do not attach real reports to public issues. The code creates report files with mode 0600. An existing overly permissive parent directory must be fixed by the operator.

Set expected owner names from your independently verified baseline. Never derive the expected owner from a fresh observation or automatically accept a new baseline.

Six `owners` and six `taxes` records are required. Unique local IDs may contain only lowercase letters, numbers and underscores. Ownership IDs must be Stewart GISLINK IDs beginning `081`; the query uses exact GISLINK and requires exactly one feature with nonempty string GISLINK and OWNER. OWNER is compared literally, without fuzzy matching, punctuation stripping, case folding or secondary-owner substitution.

If you have not verified a tax identity and detail URL, use `{"id":"tax_1","county":"Stewart","configured":false}` (and similarly for the other five). That produces UNKNOWN, never zero. The candidate tax adapter supports one HTML table with exact configured header order, exact parcel ID on every row, unique four-digit years and explicit two-decimal balances. It sums **all supplied year rows**, including older balances. `complete_history_verified` must only be true after proving the source includes the complete balance history without omitted pages. `required_year` must be reviewed for the current issued tax year; no current-year row means STALE. A new year is not assumed to have been issued January 1. Sources that need pagination, multiple receipts per year, dynamic JavaScript or other layouts require a tested adapter change; do not relax validation to make them pass.

The only currently allowed tax host is `tennesseetrustee.org`. It is a public candidate found during research, **not a verified live adapter**. The county landing page was readable; no actual tax record was retrieved. Adjust the allowlist only after verifying the original approved source. No payment, login, form submission, token harvesting, challenge solving or access-control bypass is implemented.

## Live read-only command

```sh
python3 run_supervised.py --config /private/stewart/config.local.json --output /private/stewart/reports --state /private/stewart/state
```

Dry run is the default; it writes local reports and sanitized local repair events. It sends no email and invokes no repair agent. Network GETs have a 20-second timeout and a 2 MB body limit. Transient network failures and HTTP 500/502/503/504 receive at most three attempts with 1s and 2s backoff. Access errors, rate limits, authentication challenges and parser failures are not retried. An access denial opens a per-run circuit so other checks on that adapter are marked unverifiable without further requests. Redirects are rejected rather than followed across unknown targets. The supervisor bounds the entire process at 15 minutes.

A six-tax grand total is null if any balance is unknown. A separately labelled known subtotal is retained. No stale cached observation is substituted. Retrieval timestamps are reported; upstream data currency is not established by the fetch time. The state assessor data is informational and may lag recorded ownership.

## Delivery for a later approved cutover

Transport code supports TLS SMTP on port 465, authentication from `STEWART_SMTP_USER` and `STEWART_SMTP_PASSWORD`, and a private `mail` object with `host`, `sender`, and exact `recipients`. It is dormant unless both `delivery_approved:true` and `--send` are present. Fixture runs cannot send. No credential or real recipient is in this bundle. 

SMTP acceptance is distinguished from inbox delivery: `smtp_accepted` means the server accepted the message, not that it arrived. A weekly America/Chicago ledger is marked pending **before** connecting. Existing accepted, pending, uncertain or partially refused attempts prevent another send that week. A process crash, timeout or partial acceptance must be reconciled manually; never delete the ledger and resend blindly. A persistent host/state directory and exclusive single launcher are required. There is no automatic resend or live SMTP test in this review.

## Monday execution and minimal supervisor

On an always-on Linux host such as a Proxmox VM, keep the real config separate from the Git checkout and restrict it to the runtime account. After a successful read-only trial, an authorized supervisor can run the command on Mondays using the `America/Chicago` time zone. No schedule is installed by this project.

Use `deploy/supervisor-task.txt` after replacing paths with the authorized host paths. The final one-command invocation should be frozen, including whether `--send` is authorized. On success the supervisor stops without reading reports, source pages, code or full logs and without another agent. See `docs/STATUS.md` for the fixed contract. Python handles retries and classification. AI usage is limited to the supervisor and appropriate failure repair; it is not zero for the overall agent-launched workflow.

## Repair handoff

`docs/REPAIR.md` describes the implemented local queue, stub demonstration, and the wiring needed for an authorized future GitHub repair destination. No external trigger, coding-agent invocation, PR, auto-merge or deployment was activated. A parser cannot safely repair itself by guessing changed field meanings.

Licensed under the [MIT License](LICENSE).
