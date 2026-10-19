# Apotek

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
4. Select a row in the left candidate table. The right chart follows that candidate;
   **Vælg fra listen med tastatur** provides a keyboard fallback for the same visible rows. Optional
   medicine, strength, form, pack and company labels distinguish products.
   **Forrige kandidat / Næste kandidat** follow the filtered list in canonical order; table sorting
   changes only the table. **Sammenlign priser** shows the selected period's LMS AIP as
   **Kostpris fra LMS**, the product's own historical median, and **Afstand til median**
   on the right (median minus that LMS price). Missing LMS prices stay missing;
   Cito/LMS mismatch warnings remain visible. Check alternatives below the chart to compare their
   curves (up to five); uncheck to hide them. Only the selected candidate's found group
   is offered. **Vurder** on an alternative's row explicitly opens that eligible product.
   All found group members remain inspectable, including H/K and missing history.
   Current groups are not historical memberships or a verified complete market.
   Package AIP is not a unit-price or dispensing-price comparison.
5. Under **Gem din vurdering**, choose status, note and export inclusion. Drafts survive
   product changes in this session. **Gem vurdering** commits them to exports;
   **Gem og næste kandidat** saves and opens the next visible candidate. Only saved reviews
   appear in downloads. Replacing the analysis requires acknowledging saved work
   or unsaved drafts; restoring a JSON copy deliberately replaces both.
6. Open **Eksport og sessionskopi**. Export the canonical queue, explicit selection
   or all eligible candidates. Excel opens on **Indkøbsoverblik**, a concise decision
   sheet with names, reviews and prices. **Kandidater** retains full evidence,
   **Gruppeoverblik** retains all found alternatives, and **Metode og datakontrol**
   records the reproducible scope. Download JSON before disconnecting or replacing
   the analysis. Excel and JSON contain saved judgments, not unfinished drafts.

The workspace shows the candidate list on the left and price comparison on the right,
with matching panel heights and an assessment below both. The compact comparison is
designed for a laptop viewport when scrolled to the workspace; larger groups, warnings
and expanded keyboard controls need more space. The two panels stack on narrow screens;
previous/next navigation stays with the selected medicine. In-page links jump to
prices, assessment and explanations. Every found alternative is shown directly, with
product number, LMS company, package AIP and current/upcoming classes aligned in each row.
Shared medicine names and dosage descriptions stay in help and **Gruppedetaljer**;
the latter uses wrapping product cards rather than a wide table. Missing prices and
H/K storage markers remain on the rows; missing-history controls explain why no curve
can be shown. Chart legends use product number and company. **Flere grafvalg** holds
the chart's period range and retrospective options; **Filtre** holds group/review filters.
Import and export controls stay in disclosures.
Per-product comparison choices survive product changes; a prepared Excel download
survives inspection changes but is invalidated when saved reviews or its scope change.

Prices and monetary differences display two decimal places throughout the app and Excel.
Half-øre medians and differences retain their precision in calculations and exported
numeric cells; rounding is for display only (half-øre ties round away from zero).

At the bottom, **Sådan læses analysen** documents how the sources relate, candidate
selection, rankability checks, exact calculations and prioritization. It shows the
loaded analysis's applied settings and dates, not pending edits. Historical counts
and events remain available below the assessment. No calculation or schema version
changes are needed for this presentation-only layout and documentation change.

Everything is temporary and isolated to the browser session. Restart/disconnection
can lose uploads and notes. Restore JSON only after loading identical source bytes,
confirmed period, availability metadata and analysis settings. Excel is a review
artifact, not a source-history backup. Keep original files and review copies in
approved private storage outside this checkout.

## Input and calculation policies

[Calculation and data contracts](docs/calculations.md) describe the implemented
rules and their boundaries. The implementation retains
`historical-priority-v1`: low historical price position and repeated rises define
tiers; ties use median distance descending, percentile ascending, then product ID.
This has no validated commercial-performance claim. Current AIP must exist at the
declared period and exactly reconcile with Cito cost to receive a rank. The full
requested window and minimum observation count are required. Prices after the
declared period never enter its features or default chart.

Cito `.xlsx` requires `Table`; only the six named business fields matter. The
supplied original is BIFF `.xls`, so parser `excel-v3-period-conflicts` also supports exactly
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
candidate filter. Missing groups never join each other. Optional names, strength,
form, pack and company are display labels only; formulas/errors and ambiguous
optional headings are ignored for labels. These display additions do not change
calculation definitions, method versions, analysis identity or JSON compatibility.

Unrecognised optional LMS metadata before or after the date block is ignored.
Unknown/blank headings inside that block and malformed date-like headings remain errors;
recognised reference fields may be reordered between dates. Conflicting duplicate
AIP observations become explicit gaps only at the affected periods. A conflict
blocks ranking only inside the applied history window at or before the confirmed
period; future or older out-of-window conflicts cannot change that ranking.
Parser v3 changes analysis identity, so backups from older parser versions require
their original application revision and are not silently carried forward.

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
