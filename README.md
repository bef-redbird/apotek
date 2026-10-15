# bef-redbird

A Danish browser workspace for pharmaceutical procurement **review**. Import a
Cito snapshot and LMS price history, confirm the report period, investigate a
deterministic queue, compare found group alternatives and record a human review.
Excel and review-session JSON downloads retain the applied calculation context.
Historical priority is an unvalidated screening rule, not a purchase recommendation
or a demonstrated profit forecast.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from
the repository root in PowerShell or a Unix shell:

```text
uv sync --locked --group dev
uv run streamlit run app.py
```

Open the local URL printed by Streamlit. Python 3.12 is selected by
`.python-version`. Only the server needs Python; viewers need a browser.
See [private deployment](docs/deployment.md) and [development checks](docs/development.md).
No hosted instance or access-control verification is implied by these commands.

## Review a report

1. Upload **Cito-rapport** and **LMS-prishistorik**. Select the report's actual
   current pricing-period start; the latest history date does not establish it.
   Optionally enter received-at timestamps with timezone offsets.
2. Choose **Indlæs og kontroller**. Read the filter counts, source dates and issues.
   H/K, current A/B → upcoming C, and cost strictly below DKK 1,000 define the
   baseline. Eligible records without adequate matching history remain visible.
3. Type any positive **Antal kandidater** (initially 50), or choose **Alle
   prioriterbare**. This slices the existing full ranking and preserves reviews.
   Search filters the chosen scope; it does not promote products into the queue.
4. Select a row or use the product selector. Inspect the observed AIP chart and
   **Fundne alternativer i rapporten**. Current report groups are not historical
   memberships or a verified complete market. Package AIP is not a unit-price
   or dispensing-price comparison.
5. Save a review status and plain-text note; mark export selections separately.
   Export the canonical queue, explicit selection or all eligible candidates.
   Download a JSON session copy before disconnecting or replacing an analysis.

On desktop, the candidate list stays beside the selected product's price-history,
alternatives and review tabs. Narrow screens stack the two panels.

Everything is temporary and isolated to the browser session. Restart/disconnection
can lose uploads and notes. Restore JSON only after loading identical source bytes,
confirmed period, availability metadata and analysis settings. Excel is a review
artifact, not a source-history backup. Keep original files and review copies in
approved private storage outside this checkout.

## Input and calculation policies

`CODEX_HANDOVER.md` is the maintained specification. The implementation retains
`historical-priority-v1`: low historical price position and repeated rises define
tiers; ties use median distance descending, percentile ascending, then product ID.
This has no validated commercial-performance claim. Current AIP must exist at the
declared period and exactly reconcile with Cito cost to receive a rank. The full
requested window and minimum observation count are required. Prices after the
declared period never enter its features or default chart.

Cito `.xlsx` requires `Table`; only the six named business fields matter. The
supplied original is BIFF `.xls`, so parser `excel-v2-biff` also supports exactly
one legacy sheet whose first row contains all six unambiguous headers, regardless
of its name. It inspects BIFF formula, encryption and macro records before
reading values; required formulas demand a values-only export. XLSX retains its
explicit sheet contract. Legacy support is Cito-only. No originals are rewritten.

LMS requires `.xlsx`, `Medicinpriser data`, exact `AIP` indicator rows and valid
date headings. Streaming ignores named ranges and resets worksheet dimensions.
Duplicate date headers are rejected as ambiguous, even if source values happen
to match. Duplicate identical six-field Cito rows collapse with source row references;
conflicts remove the product from calculation and flag affected group context.
Blank storage is retained as unspecified; blank classifications never pass the
candidate filter. Missing groups never join each other. Optional names are display
labels only.

Numeric Excel prices are converted with `Decimal(str(value))` to integer øre.
At most `1e-7` øre of serialization noise is accepted. Danish text prices accept
`1234`, `1234,56` and `1.234,56`; dot-decimal text is rejected. Meaningful excess
precision, negative/nonfinite values and Excel errors are invalid. Zero AIP remains
visible but is non-computable. Medians retain half-øre precision. Event thresholds
use Decimal cross-multiplication: exactly 5% is excluded by the default `>5%` rule.
Missing prices and irregular calendar gaps never become adjacent returns.

The app uses typed in-memory session records instead of DuckDB: this avoids an
unused persistence layer in temporary-only mode. Source hashes, versions and
applied settings determine analysis identity; run IDs and timestamps are separate.
Queue size, search, sorting and chart range do not change analysis identity.
Received-at timestamps are operator evidence, not proof that each retrospective
price was published or unrevised at decision time.

## Limits and excluded work

No durable database, LLM, model training, automatic ordering, scraping, demand
forecast, return compensation or clinical advice is implemented. Hosting and
processing permissions must be confirmed before real data is uploaded to a host.
Upload/resource limits, privacy controls and operational validation are documented
in [deployment.md](docs/deployment.md). Private workbook tests are opt-in; synthetic
tests work without any private data. Test success establishes software behaviour,
not profitable purchasing.
