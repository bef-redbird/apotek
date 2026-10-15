# bef-redbird — implementation handover

## Read this first

Build a working, professional browser application for pharmaceutical procurement **review**, using the real Excel input formats specified below. The application must help a pharmacist move from a prioritised candidate list to historical prices, current substitutes and a recorded judgement in one workspace. Retain Excel import and export.

**Repository:** `bef-redbird`
**Suggested application package:** `bef_redbird`
**Interface language:** Danish. Code, technical documentation and tests: English.
**Preferred starting implementation:** Python, Streamlit, Plotly, DuckDB and Excel adapters. These are design proposals, not an instruction to retain a weaker design; see the decision latitude below.
**First release:** deterministic calculations and transparent screening; no model required. Candidate count is user-editable, with 50 as the initial default, not a fixed limit.
**Deployment:** browser access, with a private hosted configuration and an equivalent local launch. No installation is required on a viewer's computer.

Implement the application, tests and operating instructions—not just a scaffold or a visual mock-up. Start with the real data contracts and calculation tests, then build the review workspace. Do not add an LLM, autonomous agent, ordering integration or speculative infrastructure.

No personal names, project deadlines, meeting references or delivery estimates belong in the application, repository or documentation. Pricing-period lengths and input observation dates are necessary domain data, not project schedules.

**Non-negotiable distinction:** reliable arithmetic, parsing and replay are achievable engineering requirements. Profitable purchases and a superior candidate shortlist are hypotheses that require evidence. Never imply that one proves the other.

---

## Decision latitude: outcomes and evidence over literal implementation

### Implemented interpretation

The first implementation retains the reference eligibility and
`historical-priority-v1` definitions. Temporary workspaces use typed per-session
memory and versioned JSON restoration; DuckDB/durable storage remain deferred.
The supplied original Cito file is genuine BIFF `.xls` with a differently named
sheet. Parser `excel-v2-biff` therefore additionally accepts a legacy workbook
with exactly one sheet whose first row has all six unambiguous required headers.
It rejects required-cell formulas, encryption and macro content. The XLSX `Table`
contract and LMS data-sheet/AIP contract are unchanged. No originals are converted
or rewritten. Synthetic XLSX/BIFF tests cover this departure independently.

Threshold classification uses exact integer-ratio cross multiplication, including
mid-rank percentiles; display ratios do not decide boundary membership. Duplicate
date headers are rejected as ambiguous. Excel group links use source group IDs
and normalized selected-product/group metadata rows, avoiding long concatenated
lists and Excel cell truncation. The queue input has a one-billion technical bound
and an independent all-rankable mode. See README.md for parsing policies and
docs/deployment.md for limits and host acceptance steps.

The supplied original hashes differ from section 3.3 but independently reproduce
its aggregate counts at the explicit 2026-09-28 test context. Default history gates
yield 366 rankable products. This is software verification, not a confirmed report
period for every Cito file or evidence of commercial performance. Private-host
access, resource and cancellation checks remain deployment-specific gates.

**Use this handover as an informed starting point, not as an infallible design.** Independently inspect the repository, supplied workbooks and relevant documentation. The proposed choices do not deserve deference merely because they are written down. Correct mistaken assumptions and improve the solution where that makes it simpler, clearer, safer or better supported.

Distinguish three kinds of instruction:

| Category | Authority | How to work with it |
|---|---|---|
| **Non-negotiable outcomes and safeguards** | Explicit user requirements and reliability/security invariants below. | Preserve them. Do not waive them because another design is faster or produces a more attractive result. A genuine conflict needs explicit resolution rather than a silent change. |
| **Confirmed input meanings and recorded observations** | The six-field contract, business explanations and fixture observations, subject to independent verification. | Use as the current factual baseline. If authoritative documentation or source inspection contradicts a statement, surface the evidence and correct it; never alter data or tests to force agreement with this document. Different source hashes can legitimately produce different counts. |
| **Proposed design and analytical defaults** | Suggested libraries, module layout, interfaces, UI arrangement, feature choices, thresholds, ranking method and future-model experiment. | Freely improve or replace them within scope. Make the reasoning inspectable, update the applicable tests/specification and use a new method/schema version when behaviour changes. No approval is needed for every reversible engineering choice; permissions and product scope still apply. |

### Non-negotiable requirements

- Work in the intended **`bef-redbird`** repository. Deliver a lean, professional browser review workspace with Danish UI, Excel import/export, candidate investigation, historical prices, current found group alternatives and recorded human review.
- The number of prioritised candidates is **editable in the UI**. Support a user-entered positive integer (not only fixed presets) and an explicit all-rankable view. Fifty is only the initial default. Queue size must not change calculations, eligibility, canonical ranking or saved product reviews.
- Preserve the six required Cito fields and their confirmed meanings. Additional discarded columns are not prerequisites. The stated baseline filter must remain reproducible. Do not silently broaden the purchasing policy or treat current group membership as historical fact.
- Preserve exact source values and identifiers, missingness, provenance and decision-time boundaries. No invented observations, future-data leakage, padded shortlists or undisclosed corrections. Uncertainty and data limitations remain visible.
- Keep predictions, historical comparisons and realised commercial outcomes distinct. No unvalidated profit claims, automatic orders, clinical advice or mandatory runtime LLM/agent.
- One underlying calculation result must agree with UI, charts and exports. Independent tests, deterministic replay, deliberate/versioned method changes and honest reporting of checks are required.
- Protect original data and user work. No private workbooks, derived business data, secrets, unnecessary logs or generated clutter in Git. Observe processing permissions; do not treat a private repository or local command execution as sufficient data-processing authorisation.
- Keep development reproducible, required checks meaningful, and merged/no-longer-needed branches cleaned locally and on GitHub after the safety checks. Do not destroy unmerged or dependent work to satisfy tidiness.

### Where to exercise judgement

This classification governs how to read the detailed reference sections, even where they use imperative example wording. Explicit safeguards and user requirements remain binding; particular tools or algorithms do not become non-negotiable merely because an example says “use”. Keep the root agent instructions concise and practical [R24].

The detailed stack, file tree, function signatures, exact layout proportions, persistence implementation, choice of test tools, feature list and lexicographic ranking are **reference solutions**. In particular, the proposed low-price/median-distance rule is an unvalidated hypothesis, not a requirement to preserve an inferior ranking. The future CatBoost target is a candidate experiment, not a commitment to train that exact model.

If you retain a named method, implement its published definition consistently. If you change its semantics, rename/version it and update the UI explanation, tests and export metadata together. Do not keep a familiar label while changing the mathematics behind it. Do not change the output merely to match reference counts or to make a failing test pass.

Prefer evidence such as independently checked source cells, documented API behaviour, a simpler equivalent implementation, benchmarked resource use, hand-calculated fixtures or genuinely held-out evaluation. A more complicated design or a more confident explanation is not evidence of improvement. New infrastructure, materially different access permissions or new commercial claims remain outside discretionary implementation changes.

Before implementation, briefly state in the task response which proposed choices you are retaining or revising. Do not create a separate planning-report file. Record material departures and supporting tests in the PR description and update maintained documentation; do not accumulate agent logs or repetitive decision documents. If an unresolved assumption affects only one mechanism, quarantine that mechanism and continue the reliable parts of the application.

---

## 1. Product purpose and commercial context

A pharmacy currently filters a recurring Cito export and manually investigates each remaining product's price history and substitutes. The application should reduce this repeated work and make the reasoning inspectable.

There are four motivating mechanisms:

| Mechanism | Meaning for this application | First-release treatment |
|---|---|---|
| Inventory bought before an AIP increase | The same eventual sale can benefit from a lower acquisition cost than buying after the increase. | Identify products worth investigating; show observed prices and historical signals. Do not promise the next price or realised profit. |
| Returns or inventory price compensation around a fall | Recovery depends on actual eligibility, pricing dates, caps, fees and commercial terms. | Excluded from financial calculations. Preserve an extension point, not a pretend implementation. |
| Temporary increases / sawtooth behaviour | Some increases reverse in the following pricing period; reversal size matters. | Compute separately defined historical statistics, including sample sizes. |
| Reduced sell-through after loss of price competitiveness | A change in relative price position may make inventory harder to sell. | Show current/upcoming classifications and current group context. No sales forecast without sales/availability data. |

A/B/C are **relative price classifications**, not product quality or investment grades. The current report uses the official classifications: A denotes the lowest-price position, B the permitted tolerance band, C outside that band. Read these fields as supplied. Do not reimplement regulatory band calculations from AIP per package.

An A/B→C transition can result from the candidate's own price increasing, a competitor becoming cheaper, or both. It is not a confirmed own-price increase.

The buying window can close **before exact forthcoming AIP is published**. The upcoming classification is available earlier. Consequently:

- `nysubkode` is legitimate information for a current review if received before the decision.
- The exact next-period AIP is normally unknown at that decision.
- Publication, effective pricing period and actual invoice pricing are different events.
- Do not encode a universal weekday-based ordering or invoicing rule. Supplier execution details are not verified inputs to this release.

Historical percentage claims from earlier discussion are not product constants. Calculate statistics from the selected data with explicit definitions. Do not hardcode any claimed overall reversal rate.

---

## 2. Scope and deliberate exclusions

### Required working capabilities

1. Upload and validate a Cito workbook and the LMS price-history workbook.
2. Confirm the report's current period; distinguish it from import time and history coverage.
3. Reproduce the existing eligibility filters and display the count after every step.
4. Produce a deterministic, explained **user-configurable Top N review queue (default 50)**, plus all eligible and unranked records.
5. Select a product and inspect its AIP history and current report group without leaving the workspace.
6. Overlay selected alternatives, with explicit missing-data and comparability warnings.
7. Adjust a small set of screening settings and deliberately apply them.
8. Record a review status and optional note for a product in this analysis.
9. Export the chosen Top N / all-rankable queue, selected products or all eligible products to a useful Excel workbook.
10. Preserve provenance, versioned settings and a reproducible run manifest.
11. Run privately in a browser; also run locally using the same code.
12. Exercise the entire workflow using genuinely synthetic fixtures without access to private source files.

### Out of scope

No automatic purchases, return execution, patient data, inventory optimisation, guaranteed-profit figures, email scheduling, public data scraping, invented API, clinical recommendations, chatbot, background training, multi-tenant platform, separate REST service or custom frontend framework.

No mandatory CatBoost dependency in the runtime environment. No framework for an imagined future: create small modules with clear boundaries and add complexity only when a real integration needs it.

---

## 3. Confirmed input contracts

The two source workbooks should be made available separately from the code repository in the actual Codex execution environment. Do not assume they are attached to a new task, inherited from an earlier chat, present in a worktree or included in a GitHub checkout. Their filenames are examples, not hardcoded upload requirements. A different valid filename must work. Section 3.4 describes safe access to originals.

### 3.1 Cito snapshot: `14day.xlsx`

Source: a purpose-built SQL export from Cito. The SQL itself is not available and is not required for the first implementation.

Worksheet: **`Table`**. Header row: **1**.

| Observed column | Exact header | Canonical field | Interpretation |
|---|---|---|---|
| A | `varenr` | `product_id` | Six-digit product/package identifier; canonical join key. |
| D | `opbevkode` | `storage_code` | Operational storage code. H and K are excluded by the initial purchasing policy. |
| E | `kostpris` | `current_cost_ore` | Cost for the **current** pricing period. Do not assume it is the upcoming AIP. |
| G | `subkode` | `current_code` | Current-period A/B/C classification. |
| H | `nysubkode` | `upcoming_code` | Classification for the **next** pricing period, before exact prices are released. |
| I | `subgrp` | `group_id` | Same substitution-group ID used by LMS. |

These are the **six required business columns**. Bind by normalised header, not by column letter. Accept harmless surrounding whitespace/case changes and the explicitly documented alias `ny subkode`; reject ambiguous duplicate canonical headers. Do not guess synonyms using fuzzy matching.

`tekst`, `tekst2` and `salgspris` are not required and must not become dependencies of filtering or ranking. Do not re-open already settled questions about `varenr`, H/K exclusions or the group-ID source.

The file does not establish transaction quantities, demand, available stock, a complete market universe or historical group membership. Repeated rows are not sales volumes.

**Period metadata is not reliably encoded in this worksheet.** Require explicit current-period confirmation in the import flow. The history's latest period can be a suggestion, not an automatically asserted report date. An optional user-supplied received-at timestamp is different from the system's upload timestamp.

### 3.2 LMS history: `dk_medicinpriser.xlsx`

The accessible history is a rolling Excel download updated by the publisher, not an API the application can currently call. The exact download URL and automation permissions are not supplied. Manual upload is the supported initial integration.

Use worksheet **`Medicinpriser data`**, not the much larger presentation sheet `Medicinpriser`. Header row: **1**.

| Observed column | Header | Treatment |
|---|---|---|
| D | `Varenummer` | Canonical six-digit `product_id`. |
| I | `Indikator` | Select exact indicator `AIP` only. |
| J onward | Date headings such as `20210809` | Parse as `%Y%m%d` pricing-period start dates. Number/order of columns can change. |
| A–C, E–H | `ATC`, `Lægemiddel`, `Indholdsstof`, `Pakning`, `Styrke`, `Form`, `Firma` | Optional reference/display metadata; not required by the MVP ranking. |

The inspected workbook contains four indicator rows per product: `AIP`, `AUP`, `DDD`, `AUP_pr_DDD`. These are **not duplicate AIP records**. Never mix them into one price history.

Optional names/package descriptions from this file may be used as secondary display labels to make the interface readable, if present. They must not affect eligibility or ranking; the application must work with product IDs alone. Do not infer originator status from a name or unusually high price.

**Important import trap:** the sample has an outdated named range ending before the actual data ends, as well as irrelevant broken defined names. Read the actual selected worksheet rows; do not use a named range as the data boundary. Check worksheet-dimension metadata as well; use the selected reader's documented dimension-reset/streaming behaviour if those bounds are stale. Broken names elsewhere are not a reason to reject valid source cells.

The input is about 65 MB compressed and has much larger expanded XML. Use a maintained streaming Excel reader and select the data sheet/AIP records. Avoid loading both large worksheets or melting every indicator into memory. Streaming reader behaviour is documented in [R7].

### 3.3 Verified reference facts for private acceptance testing

These observations were independently calculated from the supplied files. They are fixture-specific, **not runtime expectations** and not values to hardcode into application output.

| Check | Observed value |
|---|---:|
| Cito non-empty product rows, excluding header | 40,954 |
| Unique rows across the six required fields | 6,828 |
| Unique product IDs | 6,828 |
| After excluding H/K | 6,205 |
| After current A/B and upcoming C | 509 |
| After `kostpris < 1000` | **479** |
| Distinct candidate group IDs | 378 |
| History AIP product IDs | 16,991 |
| Observed period columns | 135 |
| Non-empty AIP observations | 1,442,961 |
| Candidate IDs with any AIP history | 478 |
| Candidates with AIP at the final observed period matching `kostpris` exactly | 461 |
| Mismatches among candidates with a price at that final period | 0 |

The observed history covers **2021-08-09 through 2026-09-28**. The 18 remaining candidates lack an AIP observation at that final period; one of those has no history at all. Missingness must remain visible. This match does not prove the contractual definition of cost for every future report.

The previously mentioned count of 462 is not reproduced by the stated filters. Do not invent an extra exclusion to obtain it. The baseline is 479 before history-quality gates; the number that can be ranked may be smaller.

Private fixture identification:

```text
14day.xlsx
bytes: 1682566
sha256: f30c17c0b85109630ac5a53d8c0dac0073dfd8cba8ea91d17d3c2eb1c47bcfcf

dk_medicinpriser.xlsx
bytes: 65329789
sha256: 9ee23e8a83a1d27f1cf60eafc4d2274406ab49f502a7b02238a2e30d1cb04dd8
```

Keep real workbooks, excerpts, product-level outputs and their metadata out of Git, public CI and issue attachments. A fixture manifest of aggregate counts/hashes is acceptable in this private repository. If files are absent from the coding environment, use synthetic fixtures, make private acceptance checks explicitly skip, and report that real-file execution has not been verified. Do not claim these observations are tests the new application has already passed.


### 3.4 Access to original workbooks during development

**Yes: real-file validation should use the original, unfiltered workbooks, when authorised.** Access is strongly recommended for real-format verification, not a licence to inspect unrelated files or a prerequisite for synthetic development. They let the implementer check the actual schema, hidden columns/rows, sheet layout, identifier formats, duplicate behaviour, missingness and full-size import constraints rather than reverse-engineering these solely from prose. They are private acceptance fixtures, not repository assets or a source of new mandatory business columns.

Provide the original LMS history download and the original Cito report before manual deduplication/filtering. A byte-for-byte read-only copy of an original is suitable. The previously shared files may already have hidden/removed fields or otherwise differ from an untouched export: the hashes and counts in section 3.3 describe those particular supplied bytes, **not every original**. Verify the actual files. Do not assert that an unseen original has been tested.

Keep originals immutable. Never save a workbook back over its source, unhide/resave it in place, repair it silently or strip data to make the importer pass. Required six-field calculations must work whether irrelevant columns are visible, hidden or absent. Hidden columns are not a confidentiality boundary: review what is actually in the workbook before granting access. Do not provide patient identifiers, credentials or unrelated personal data for this task. If authorised sanitisation is necessary, retain the original in the owner's approved storage and supply a clearly identified sanitised copy with a new hash; report the resulting validation limitation.

**Local coding setup:** prefer an authorised directory outside the Git checkout and grant only the read access needed by the actual Codex client/sandbox. Pass explicit absolute paths through application/test configuration, for example these **project-defined** environment variables:

```text
REDBIRD_PRIVATE_CITO_PATH=<absolute path to an authorised read-only Cito workbook>
REDBIRD_PRIVATE_LMS_PATH=<absolute path to an authorised read-only LMS workbook>
```

These variable names are this project's convention, not built-in Codex settings. Keep real values in the operator's environment or ignored machine-specific configuration; a tracked example may contain placeholders only. Confirm both files are readable from the process that will run the tests. Do not grant full-machine access or disable sandbox protections just to access two files. Worktrees may not contain files absent from Git; an external path avoids relying on an ignored directory being copied automatically. Verify actual client behaviour and permissions in the current documentation [R21-R23].

**Cloud coding setup:** a task has its own environment; a path on the operator's computer is not automatically available there [R21, R22]. Use the client's currently supported, approved private attachment/staging mechanism or an explicitly authorised private data source, then verify the resulting readable paths. Do not assume a generic upload/mount feature exists in every Codex client. Do not commit files to Git, Git LFS or public object storage as a transfer workaround. Reusable environments can capture their prepared filesystem [R21]; keep private workbooks out of shared/published environment images unless that persistence and audience are explicitly approved. Do not print expiring download links or credentials into task logs. If access is unavailable, continue with synthetic fixtures and mark private acceptance as not run.

**Processing permission is separate from file placement.** Approval should cover the chosen coding service and account, not only the future application host. Local parsing is not a guarantee that content shown to an AI assistant stays on the computer; avoid dumping workbook contents into model/tool transcripts. Prefer schema summaries, safe aggregates and necessary minimal diagnostics. Do not transmit data to new services to make inspection more convenient.

At startup, inventory just the two authorised files: readable path in private configuration, file size/hash, expected worksheets and required headers. Inspect locally with a parser; do not paste thousands of cells into the conversation. Run optional private tests against these paths, preserve originals and clean only derived temporary artifacts. Public/default CI uses synthetic recipes only. Missing access must produce an honest skipped/private-verification status, not fabricated acceptance evidence.

---

## 4. Data integrity: fail visibly, never improve appearances by guessing

### Identifiers

- Store product IDs as strings of exactly six digits, preserving leading zeros.
- Accept integral numeric Excel IDs within the six-digit range and left-pad them. Accept digit-only strings and an explicitly handled integral `.0` representation from a numeric conversion.
- Reject fractional, negative, overflowing or malformed IDs. Do not repair them by stripping arbitrary characters.
- Treat group IDs as their own identifiers, not as product IDs. Do not apply six-digit padding to group IDs.
- Missing group IDs never join to each other; otherwise unrelated products would become one fake group.

### Prices

- Preserve source financial values in integer øre after a documented cent-exact conversion. Use Decimal at the boundary. Harmless binary serialization noise may be normalised within a tightly tested tolerance; meaningful extra precision is flagged, not silently rounded.
- Derived ratios may use float64. Do not round a feature before sorting or testing thresholds. Display rounding is separate.
- Medians of an even sample can be half an øre: retain this precision for derived comparisons instead of altering input values.
- Handle numeric cells and a clearly specified Danish textual number format. Reject ambiguous textual formats rather than guessing a decimal separator.
- Missing, invalid, infinite and negative prices are not zero. A zero AIP is non-computable for this release and cannot enter return calculations or a prioritised queue.
- `kostpris < 1000` is strictly less than DKK 1,000; DKK 999.99 passes and DKK 1,000.00 does not. Validate this in cents.
- Keep `current_cost` and `current_aip` separate. Match the selected report period, not simply the latest price found for that product. On mismatch, show both and flag reconciliation. The conservative reference method withholds ranking pending explanation; a different documented policy may be justified as described in 6.3. Never silently overwrite either amount or present unmatched costs as verified AIP.

### Duplicates and schema changes

Collapse rows only if their six normalised required values are identical. Store duplicate counts and source row references in diagnostics. Conflicting required values for one product in one snapshot place that product in a data-issue queue and make its group context visibly incomplete. Do not select the first or last row arbitrarily.

If a required worksheet/header is missing, duplicate date headers conflict, or dates cannot be interpreted safely, reject the import with a concise actionable message. Optional or hidden columns do not break the parser. Read hidden rows as source data; an Excel view filter must not secretly become a business filter.

Reject unsupported/encrypted/macro-enabled files for this release. Inspect required cells for formulas: do not evaluate formulas, execute macros or trust potentially stale cached formula outputs. Formula cells in required source values need a values-only export. Irrelevant formulas elsewhere are ignored.

### Missing prices and time alignment

- Construct the observed period calendar from headers. Preserve dates with missing per-product prices.
- Do not forward-fill, backward-fill, interpolate or substitute zero.
- An adjacent-price movement requires both observations and the expected next pricing-period interval. A longer gap is not one ordinary period.
- If a header interval deviates from the normal 14-day rhythm, show a calendar warning and exclude ambiguous adjacent calculations. Do not silently invent a new calendar.
- Features for period `t` use dates `<= t`. Later prices from a newer workbook must not enter the ranking or the default decision-context chart.
- If the history is older than the selected Cito period, permit clearly labelled historical exploration but do not present a current, fully supported candidate ranking.
- If prices match an older period better, suggest investigating the date mismatch; never silently change the declared period.
- Import time is not source availability time. A newly downloaded retrospective workbook cannot prove when every value was first published or whether it was revised.

Use three actionable levels: **import blocked**, **record unranked**, **warning**. Do not turn every warning into a blocker, and do not bury critical problems in an expandable log.

---

## 5. Eligibility, group context and analysis identity

Run candidate filters against normalised Cito data in this order:

```text
all product rows
→ validate required values / identify conflicts
→ collapse exact six-field duplicates
→ exclude storage codes H and K
→ current_code in {A, B} and upcoming_code == C
→ current_cost < 1000 DKK
→ eligible candidates
→ apply history-quality checks for ranking, without deleting eligible records
```

Keep the full, valid unfiltered report as the group-reference population. H/K products, products costing more than the limit, and other code transitions must remain available in group context.

The UI must say **“Fundne alternativer i rapporten”**, unless independently verified complete source coverage becomes available. Do not say “all substitutes” or calculate market share from the row count. Show source group ID and explicitly state that membership is from the current snapshot, not historical membership.

Do not automatically remove originator products from group context. A later documented exclusion from a historical benchmark is a different operation from removing an actual competitor.

A candidate is a **product in a dated analysis**, not a permanently “good” medicine.

Define a content-derived `analysis_id` from both source hashes, declared current period, availability metadata, parser/schema version, feature version, ranking version and canonical applied **analysis settings** (eligibility and method parameters). Add a separate unique run timestamp/ID for repeated executions. Re-running identical inputs/analysis settings must yield identical numeric results, full rankings and explanations, even if exported workbook metadata contains a different creation timestamp.

Keep queue/display preferences separate. `top_n`, all-rankable mode, table search/sort, pagination, inspected product and chart zoom are not part of `analysis_id` and do not trigger feature/ranking recomputation. Record queue settings and the exact ordered exported product IDs in the export manifest/session state so a delivered shortlist remains reproducible. Review status/notes stay tied to product and analysis when the user changes N. A schema-versioned equivalent identity design is acceptable if it preserves these distinctions.

---

## 6. Initial feature and ranking specification

The following are **proposed analytical defaults**, not empirically optimised trading rules. Treat them as a transparent reference implementation, not as unquestionable product requirements. Codex may simplify or improve the feature set, rankability policy or ordering with a documented rationale and independent tests under the decision-latitude rules. Do not quietly replace them with a weighted “AI score.”

When using a definition or method below, implement it precisely and expose it. Changes to its meaning need a named/versioned method, consistent explanations and tests. Keep the original eligibility baseline available for comparison; no claim of greater commercial value without supporting evaluation. The **editable queue behaviour** in 6.5 is a required capability, independent of the chosen analytical method.

### 6.1 Applied settings

```toml
[eligibility]
excluded_storage_codes = ["H", "K"]
current_codes = ["A", "B"]
upcoming_codes = ["C"]
max_cost_dkk_exclusive = 1000

[features]
lookback_periods = 26
minimum_valid_prices = 20
rise_threshold = 0.05
low_price_percentile = 0.25
repeated_rise_minimum = 2
minimum_events_for_rate_display = 5

[queue]
mode = "top_n"                  # alternative: "all_rankable"
top_n = 50                     # positive integer editable in UI; no fixed list length
```

Validate settings: minimum observations cannot exceed the lookback; prices and sample thresholds must be sensible; percentile must lie in `[0,1]`. Expose a modest basic settings panel and put methodological thresholds under an advanced expander. Show “unvalidated screening settings”; do not encourage tuning to a desired outcome.

Queue preferences are separate from applied analysis settings. Changing N or switching to all-rankable mode immediately slices the existing canonical ranked result; it must not reload Excel, recalculate features or create a new analysis. An eligibility/method change still requires deliberate application.

The chart display range and candidate-table sort are also **not** ranking settings. Changing them must not silently create a new analysis or redefine the top-N membership. Retain canonical ranks in the table even when the visible rows are sorted differently.

### 6.2 Definitions

Let `P[t]` be the valid AIP at the confirmed current period. Let `W` contain the last `lookback_periods` scheduled periods through `t`, including `t`. Let `S` contain valid prices in `W`.

| Feature | Exact rule |
|---|---|
| Valid history / coverage | `n_valid = len(S)` and `coverage = n_valid / len(W)`; also show `len(W)` when the imported file is shorter than requested. |
| Current AIP | Exact valid observation at `t`; never “last known price” relabelled as current. |
| Previous change | `P[t] / P[t-1] - 1`, only for a valid adjacent pair. |
| Rolling median | Median of `S`, retaining derived precision. |
| Distance to own median | `median(S) - P[t]` in DKK; label **“Afstand til egen historiske median”**, not profit or forecast. |
| Current price percentile | Mid-rank: `(# values < P[t] + 0.5 × # values == P[t]) / n_valid`. Constant prices produce 0.5, not an extreme signal. |
| Significant rise | Adjacent prices within `W` where `P[k] / P[k-1] - 1 > rise_threshold`. At 5% exactly, it does not count. |
| Rise frequency | Rise count / valid adjacent pair count, with both counts available. |
| Any next-period reversal | A significant rise followed by `P[k+1] < P[k]`; all three dates must be valid, adjacent and inside `W`. |
| Full next-period reversal | A significant rise followed by `P[k+1] <= P[k-1]`, using the same valid-triple requirement. |
| Reversal denominator | Number of significant rises for which the following adjacent period is observed inside `W`, **not every rise**. |
| Reversal magnitude | For an eligible rise event: next change `P[k+1]/P[k]-1`, plus optional giveback `(P[k]-P[k+1])/(P[k]-P[k-1])`. A giveback above 1 is a fall below the starting price; do not clamp it. |

Evaluate price-event thresholds using integer/Decimal cross-multiplication, not a floating-point ratio comparison: an exact 5% rise must remain exactly on the excluded boundary. Convert configuration thresholds through a documented Decimal representation. Ratios may be floating-point values for display after the event has been classified.

If the reversal denominator is zero, output null—not 0%. Below the minimum event count, show a count such as “1 af 2 hændelser; begrænset grundlag” rather than a standalone authoritative percentage. Calculations remain accessible in details/export.

Do not add volatility, annualisation or vaguely defined cycle scores simply to fill the interface. These features are a sufficient starting proposal, not a ceiling: a justified replacement or additional statistic is welcome when its interpretation, input requirements, limits and independent tests are clear.

### 6.3 Reference rankability policy

The conservative reference method considers an eligible record rankable only if:

- ID and the six required fields are valid and unambiguous;
- current-period AIP exists and is positive;
- current Cito cost reconciles with current-period AIP under the declared cent-exact policy;
- the requested period window is available and has at least `minimum_valid_prices` observations;
- no fatal current-period alignment problem exists.

Other eligible records remain in **“Kan ikke prioriteres”**, with a reason and charts where possible. Do not silently lose them. Do not pad any requested queue size with invalid rows.

The particular minimum-history threshold and exact cost/AIP reconciliation gate are proposed policies, not proof that all other records are economically useless. A cost/AIP difference could reflect a verified difference in definitions rather than corrupt data. Codex may implement a more appropriate, explicitly labelled handling with evidence; it must preserve both amounts, keep required missing/current-period data visible and never treat acquisition cost as AIP by assertion. Fatal ambiguity must not become a confident rank simply to fill the queue.

### 6.4 Reference deterministic review priority v1

The reference method uses an explicit lexicographic order, not a hidden weighted sum:

1. Tier 0: percentile `<= low_price_percentile`, positive median distance, and at least `repeated_rise_minimum` significant historical rises.
2. Tier 1: percentile `<= low_price_percentile` and positive median distance, without the repeated-rise condition.
3. Tier 2: other rankable eligible records.
4. Within each tier: median distance descending, then percentile ascending, then canonical product ID ascending.

Suggested Danish reasons:

- Tier 0: “Lav historisk prisposition og gentagne prisstigninger.”
- Tier 1: “Lav historisk prisposition; begrænset gentagelsesmønster.”
- Tier 2: “Opfylder grundfilter; intet stærkt historisk lavprissignal.”

Append concrete values and separate risk/data warnings. Upcoming C is always visible; a low percentile does not cancel demand risk. Reversal statistics are contextual risk evidence in v1, not an unexplained penalty.

Version this method as `historical-priority-v1`. Its large-currency-gap ordering is a review preference, not expected return. Include a method note acknowledging that a low historic price may represent permanent price erosion.

Display product count and distinct group count. Provide a group-focus view and “all eligible” without silently enforcing one candidate per group. A group-diversified selection would be a separate, named method, not an undocumented side effect of queue size.

### 6.5 Editable candidate count — required behaviour

- Provide an accessible **“Antal kandidater”** integer control in the main workspace, initial value **50**. Offer convenient presets only as a supplement; users must be able to type another positive integer such as 17 or 80. Do not require a source-code/configuration edit, a restart or a re-upload.
- Define `requested_n >= 1`; reject empty, non-integer, zero and negative requests without discarding the last valid view. Let `m` be the number of rankable products and `effective_n = min(requested_n, m)`. There is no 50-product ceiling; N limits the displayed/exported queue, not the products eligible for analysis or group lookup. Parse and bound the control safely; do not allocate memory proportional to an arbitrarily large request. A justified technical input bound must not become a hidden business cap; all-rankable mode remains available.
- Provide an explicit **“Alle prioriterbare”** mode, separate from **“Alle kandidater”** (which includes unrankable eligible records) and **“Dataproblemer”**. With `m = 0`, show an empty state and issue access, not a fabricated ranked list. Do not overwrite the user's requested N merely because the current dataset is small.
- Use the first `effective_n` IDs from the full canonical ranking. With the same analysis, the Top 10 must be the prefix of the Top 50, and Top 50 the prefix of Top 100 when enough rows exist. Increasing N must never alter existing positions, exclusions or method calculations. Do not compute features only for the requested N.
- Show requested versus actual size, e.g. **“Viser 37 af 80 ønskede kandidater — 37 kan prioriteres.”** Labels, export choices and filenames must be generated from current settings rather than literal “Top 50” strings. Table pagination/page size is a different control and must not alter queue membership or exported row count.
- Keep search explicitly scoped. Suggested behaviour: search/filter the displayed queue after canonical top-N selection, without silently promoting lower-ranked products into it. Show the remaining visible count. Use the all-eligible view or separate product selector for a search across the whole analysis. Any alternative is acceptable only if the scope is labelled and exports are unambiguous.
- Changing N/mode affects only selection/view/export scope. It does not change `analysis_id`, clear notes, discard saved export selections or recompute rankings. If the currently inspected product falls outside the resized queue, retain it with an “outside current queue” indicator or move inspection explicitly without losing its saved review. Never attach its review to another row.
- Save N/mode in the review-session download and restore them alongside analysis-matched reviews. Persist as preferences in durable mode when available. Reuse the last valid choice within the workspace rather than resetting to 50 on every rerun.
- A top-N export uses the exact current canonical queue, not a hidden fixed 50, the current page, a table row offset or an incidental client-side sort/search. The selected-products export remains independent. Record requested N, effective N, queue mode, export scope and ordered exported product IDs in metadata. All-rankable mode records that mode and its actual count.

---

## 7. Professional UI specification

### 7.1 Visual direction

Build a restrained analytical workspace, not a startup landing page. No gradients, glowing cards, oversized hero text, emoji navigation, stock illustrations, chat pane, “AI-powered” slogans, confetti, invented performance metrics or ornamental dashboards.

Use a light neutral background, near-black text, fine dividers and one muted primary accent. Keep status colours subdued and always pair them with text. Use a system sans-serif stack and tabular numerals; no external font downloads. Prefer native Streamlit widgets and a small version-tested theme over brittle DOM-targeted CSS or a large component library.

Desktop-first, usable at ordinary laptop widths and on a larger monitor. At narrower widths, stack list/detail rather than compressing them into unreadable columns. Charts, source dates and warning text must remain legible at browser zoom. Empty and error states need the same care as the populated screen.

### 7.2 One workspace

The sketch is a starting layout, not a pixel-level mandate. Improve the arrangement if it makes the review flow clearer while retaining an editable candidate count and reliable linked selection.

```text
Header: product title | active period | data status | export

Compact filter/data rail  | Candidate list       | Selected product
                         | Top N / All / Issues  | identifiers and code badges
                         | search and group      | historical AIP chart
                         | review state          | selected alternatives
                         |                       | evidence / warnings / note
```

Avoid nested scroll boxes where possible. On a typical laptop, a collapsible sidebar plus roughly 40/60 list/detail columns is sufficient. The candidate list should remain in view during analysis; do not navigate to a new product page.

### 7.3 Import and data status

Two labelled uploaders: “Cito-rapport” and “LMS-prishistorik”. Show source filenames, hashes in details, detected date range and validation status. Show an explicit current-period selector and optional received-at information. Do not pre-populate today's date as the report period.

Have a deliberate **“Indlæs og kontroller”** / **“Anvend indstillinger”** action. Expensive imports and ranking do not run on every note edit, chart zoom or row selection. Uploading a new source invalidates the active analysis but must not silently erase unexported notes.

Show stages actually completed rather than a fabricated percentage/progress animation. On failure, retain the previous valid workspace, clearly mark the failed new import and prevent mixing its metadata with the old analysis.

At the top, show:

- confirmed current period and upcoming code period;
- history coverage through date;
- eligibility count, rankable count and issue count;
- one compact notice: “Prioritering til gennemgang — ikke dokumenteret købsgevinst.”

Do not label the newest available sample “current market data” when it is old relative to the calendar. Keep the loaded snapshot usable but visibly dated.

### 7.4 Candidate selection

Include “Antal kandidater” and an all-rankable mode near the queue; keep them separate from methodological settings. Implement all behaviours in 6.5. Use a supported Streamlit selectable dataframe [R1] or a tested equally lean selection control. Track selected products by `product_id` and analysis ID, never only by dataframe row offset. Map selection against the exact dataframe rendered on that rerun, reset stale row events when filtering/sorting changes and provide a keyboard-accessible product selector as a fallback. Test sort/filter selection explicitly.

Show a compact set of columns: canonical rank, product ID, optional display name, current cost, A→C/B→C, distance to own median and review status. Put detailed statistics in the right panel; do not force 15 columns into the list.

Selecting a row is not the same as marking it for export. Use an explicit saved selection/review control. Keep current inspection, export selection and review status separate.

### 7.5 Historical chart

- Candidate AIP first, with optional overlays from selected **found** group alternatives; do not enable a whole large group by default.
- Dates sorted ascending; step segments that reflect observed pricing periods rather than smoothed interpolation.
- Reindex series to the period calendar and include explicit nulls for missing values. `connectgaps=False` alone does not reveal absent rows that were never supplied to Plotly [R6].
- Do not extend a last-known price through an unobserved future period. A current-period extension is permitted only over a confirmed validity interval and must remain distinguishable from future pricing.
- Hover contains product ID, period date, observed AIP and source context.
- Default to decision-available dates. Later observations may be explored in a separately labelled retrospective view, with a shaded post-decision region; they never modify the saved ranking.
- “AIP pr. pakning (DKK)” is the axis label. Package-to-package overlays are not proof of unit-price or dispensing-price comparability. Display that caveat alongside group comparison.
- No fabricated next-period point. No official “cheapest” badge computed from these AIP overlays.
- Chart zoom/lookback for viewing is independent from applied feature lookback.

### 7.6 Group and review details

Group table comes from the unfiltered valid Cito snapshot. Include product ID, supplied codes, cost, storage exclusion marker and history status. Show the candidate itself distinctly; label missing history rather than dropping the alternative. Display “group completeness not verified”.

Status choices: `Ikke vurderet`, `Interessant`, `Fravalgt`, `Undersøg nærmere`; optional plain-text note. A status is a human judgement, not a supervised label for profitability. Notes are tied to product and analysis. Do not carry a judgement into a new period without explicit user action.

### 7.7 Export and restart behaviour

Users can export without losing their place or triggering a new analysis. If a hosted session is temporary, say clearly that data and notes can disappear after reset/disconnection. Never label session-only data as permanently saved. Persistent mode must visibly identify the active workspace and latest successful save.

---

## 8. Excel export contract

Use XlsxWriter [R8], or a justified equally safe writer, to export the **same saved AnalysisResult** consumed by the interface. No parallel spreadsheet calculation engine.

Export choices: current canonical Top N / all-rankable queue, explicitly selected products, all eligible products. Unrankable records remain in the all-eligible export with blank rank and reason. An empty selection produces an explanation, not an unrelated default export. Queue exports honour the currently requested size and section 6.5; never silently revert to 50 or export only the current table page.

Use the same AnalysisResult plus an explicit ExportScope/queue snapshot. Capture requested N, effective N, mode and exact exported IDs at download time so the workbook is reproducible even after the user resizes the on-screen queue. Notes are copied from the matching review state, not reread from an unrelated analysis.

Worksheets:

1. `Kandidater`: selected scope, ranks, six source fields, current AIP, features, quality flags, reasons, review statuses and notes.
2. `Gruppeoverblik`: found group members for exported candidates; group completeness caveat and history coverage. Deduplicate repeated group references without losing the relation to selected candidates.
3. `Metode og datakontrol`: run/analysis identity, source hashes/basenames, declared periods and availability metadata, applied analysis settings, queue/export scope (requested/actual counts and ordered IDs), method/schema versions, validation/filter counts and limitations.

Use numeric Excel cells for numeric values, text cells for six-digit IDs, readable Danish number/date formats, frozen headers, filters, sensible widths and restrained styling. Avoid large decorative title blocks and merged cells in data tables. Do not export the entire multi-million-observation history by default.

Disable automatic conversion of user strings into formulas and hyperlinks (`strings_to_formulas=False`, `strings_to_urls=False`), and explicitly write notes/identifiers as text. Test strings beginning with `=`, `+`, `-`, `@`, tabs and newlines. Exported input content must never execute as a spreadsheet formula.

Excel is a human-readable review artifact, not a full backup of the source history. Also provide a small **versioned review-session JSON download/import** containing analysis identity, applied analysis settings, queue preferences (N/mode), statuses, selection and notes—not raw historical workbooks. Restoring requires matching source hashes and declared periods; mismatches are blocked or explicitly mapped, never silently applied. JSON is validated data; do not use pickle/joblib for uploaded session files.

---

## 9. Lean architecture and storage

### 9.1 Preferred runtime stack

This is a supported starting proposal. Retain it unless a simpler or more reliable alternative is justified; the number of libraries and the exact persistence engine are not product goals. Do not introduce a custom frontend/backend split merely for style. Adapt the example commands/tests/docs when choices change.

Use one supported Python minor version consistently; prefer Python 3.12 unless deployment compatibility requires another supported version. Resolve and test actual compatible package releases, then commit `uv.lock`. Do not fabricate “latest” version numbers.

Runtime dependencies: Streamlit, Plotly, DuckDB, pandas/NumPy for bounded tabular transformations, openpyxl for streaming Excel import, XlsxWriter for export, and a safe XML dependency if required by the selected reader. Standard-library dataclasses, Decimal, pathlib, hashlib and logging are sufficient for most other needs.

Choose one tabular representation per boundary; do not add Polars, pandas and another dataframe engine for the same task. DuckDB is a local analytical store, not a separate service. No SQLAlchemy or ORM is required for the initial fixed schema.

Use `pyproject.toml` and `uv.lock` as the dependency source of truth. If a host requires `requirements.txt`, generate runtime-only requirements from the lockfile, document the command and check in CI that it is current. Never manually maintain two conflicting dependency lists. Locked syncing is documented in [R9].

### 9.2 Suggested repository shape

```text
bef-redbird/
  app.py                         # very small Streamlit entry point
  pyproject.toml
  uv.lock
  .python-version
  .gitignore
  .pre-commit-config.yaml
  AGENTS.md
  CODEX_HANDOVER.md               # this document; maintained canonical specification
  README.md                      # setup, run, workflow, limitations
  .streamlit/config.toml         # theme/server settings; no secrets
  .github/
    workflows/ci.yml
    pull_request_template.md
    dependabot.yml
  .agents/skills/
    redbird-data-integrity/SKILL.md
    redbird-ui-review/SKILL.md
    redbird-delivery/SKILL.md
  .codex/hooks.json               # only verified, reviewed hook configuration
  src/bef_redbird/
    __init__.py
    domain.py                    # settings, typed records, analysis result
    imports.py                   # two explicit Excel adapters; split if it grows
    validation.py
    features.py
    ranking.py
    service.py                   # application orchestration; no UI imports
    store.py                     # DuckDB workspace + narrow read/write methods
    export.py
    ui.py
    charts.py
  scripts/
    check_repo_hygiene.py
    prune_branches.py
    codex_session.py
    codex_stop.py
  tests/
    conftest.py
    fixtures/                    # small synthetic recipes, not private files
    unit/
    integration/
    ui/
    private/                     # opt-in tests reading external file paths
  docs/
    development.md               # checks, skills/hooks, PR/branch policy
    deployment.md                # privacy, persistence, resource constraints
```

The tree is a responsibility guide, not permission to generate empty files. Combine small modules where sensible; split only when responsibilities become unclear. Do not create a service layer per CRUD method, generic plugin registry or unused future-model class hierarchy.

### 9.3 Core interfaces

The names and number of functions are illustrative. Use small typed functions or cohesive classes along these lines:

```python
parse_cito(source, metadata) -> CitoSnapshot
parse_lms_history(source) -> HistoryDataset
validate_snapshot(snapshot, history, context) -> ValidationReport
build_analysis(snapshot, history, context, settings) -> AnalysisResult
get_candidate_detail(analysis, product_id, view_settings) -> CandidateDetail
export_analysis(analysis, review_state, export_scope) -> bytes
select_queue(analysis, queue_settings) -> QueueSelection  # pure prefix/scope selection; no re-ranking
```

`AnalysisResult` contains the **full** canonical ranked/unranked result, filter counts, provenance, analysis settings, feature/ranking versions and issues. Queue/view preferences are separate; slicing to N must not discard the underlying analysis. Charts and Excel derive from it. The calculation modules have no Streamlit imports or network access. Use structured issue codes rather than parsing warning prose to decide behaviour.

Preserve the source row/column provenance needed to explain a flagged value. Avoid raw workbook cells scattered through UI session state.

### 9.4 Storage modes

Separate temporary operation from durable operation behind a small workspace boundary. Temporary mode plus review-session export/restore is required; durable mode is an optional extension, not a reason to overbuild the initial app. If implemented, durable mode must satisfy the safeguards below. The reference configurations are:

**Temporary hosted mode (default):** isolated per-session in-memory DuckDB/workspace, never a module-global shared upload dataset. Store review state in the active workspace/session and support explicit review-session download. There is no claim of persistence after disconnect/restart.

**Single-workspace durable mode (optional implementation, explicit opt-in):** explicit `REDBIRD_DATA_DIR` outside the Git checkout, on approved persistent storage. Store content-addressed originals, normalised source versions, analyses and review events. Use transactions and a single application writer. A simple schema-version table plus tested migrations is sufficient; no migration framework is necessary yet.

Do not treat a path on an ephemeral cloud filesystem as durable. Community Cloud does not guarantee local-file persistence [R4]. A durable deployment requires a real persistent volume or later external storage, approved access and tested backup/restore.

Suggested logical records: `source_files`, `cito_snapshots`, `cito_items`, `history_versions`, `aip_observations`, `analysis_runs`, `review_events`. Prices are keyed by history source version, product and period; preserve revisions instead of overwriting the evidence behind a past run. Domain audit records are relevant business data, **not repository log files**.

Avoid a shared DuckDB connection used concurrently without synchronisation. Use a workspace lock/serial access with clearly scoped connections. Durable mode is one explicitly shared, access-controlled pharmacy workspace—not an accidental multi-customer database. DuckDB's concurrency model must be respected [R5].

### 9.5 Fortnightly operation and revisions

1. Upload the latest publisher workbook when available and the corresponding Cito snapshot.
2. Confirm the snapshot period and record actual known received-at information, if available.
3. Compare source hashes. Identical bytes must not trigger duplicate import or erase reviews.
4. Parse a new history version once. Read only relevant AIP data; do not reread Excel on every UI interaction.
5. Keep existing history coverage and old versions in durable mode even when older periods roll out of a new download. Do not silently overwrite a previously observed value with a publisher revision.
6. Analyse the selected snapshot with its declared information cutoff.
7. Save/export the review. A later history update can attach realised price outcomes to an earlier frozen analysis without changing that analysis.

A newer filename is not proof of newer data. Do not require an LMS refresh merely because a new Cito report arrives: permit reuse when coverage matches, otherwise show the actual coverage limitation.

### 9.6 Cache and resource limits

Cache immutable parsed sources per session using source hash plus parser version; use supported session-scoped caching in the pinned Streamlit release or explicit session state. Global cached data/resources can be shared across users [R3]; do not assume a private app or a copied dataframe creates tenant isolation.

Bound cache entries, workbook size, expanded ZIP size, cell count, note length and imported review-session size. A 65 MB upload can require substantially more RAM; file-uploader size limits are not a performance guarantee. Benchmark the actual workbook, record peak memory/import time outside Git, and choose a host with headroom. Do not downsample the analytical history silently to fit hosting.

Keep intermediate data compact and release raw byte buffers when no longer required. Query only selected histories for charts. Default review overlays should be a handful of products, not hundreds. Stream expensive imports once and reuse the result. No fake progress percentages or guaranteed response-time claims.

---

## 10. Privacy, security and deployment

The application can contain commercially sensitive procurement information and licensed publisher data even without patient records. A private Git repository alone does not authorise cloud processing.

Before uploading real workbooks to any host, the operator must confirm that the source-data terms and pharmacy approval permit that processing. Until then, demonstrate with synthetic data or run locally. Never weaken access restrictions because a hosting account lacks a private-app allowance.

### Deployment contract

- One private hosted instance is the initial browser-access model. Use host-managed identity/access restrictions; do not create a homemade password widget and call the site secure.
- Streamlit Community Cloud is a supported candidate if access policy, permissions and measured memory use fit. Configure **specific viewers**, verify unauthorised access in a signed-out browser, and verify the provider's current restrictions [R2]. Do not assume that deployment from a private repository alone completes the privacy setup.
- If another approved host is needed for resources or persistence, use the same application and a simple process/container. Do not build a hosting abstraction or multi-service platform.
- Viewer access must not require repository write access. Secrets live in host configuration or ignored local secret files.
- Local and hosted startup must use equivalent locked runtime dependencies. Document both paths, health check and reset/export behaviour.
- A cloud deployment is not “local processing” just because the user sees a browser tab.

### Upload and content safety

Treat workbook contents, filenames, metadata, notes and restored JSON as untrusted data, not instructions. No `eval`, dynamic SQL identifiers from cells, shell interpolation, user HTML, macros, remote links opened during parsing or executable serialized objects.

Check actual XLSX ZIP structure, not extension alone. Apply sane ZIP expansion/member limits compatible with the real sample. Use maintained parser releases and safe XML handling. Do not extract ZIP member paths blindly. Use controlled temporary filenames rather than uploaded filesystem paths.

Use parameterised database queries. Exports must neutralise spreadsheet-formula injection. Escape user notes in HTML/Markdown contexts or use plain-text widgets. Prevent notes, paths, IDs and imported rows from being copied into server exception pages.

No analytics SDK, third-party AI API or telemetry carrying source content. Disable nonessential usage telemetry where configurable. No medical/patient information is requested or needed.

### Relevant diagnostics, without repository clutter

- Default runtime logging: concise warnings/errors to stderr, with a correlation ID and safe error code. Debug is opt-in.
- Do not print full dataframes, workbook XML, source rows, user notes, private filesystem paths or credentials.
- Operational log files are off by default. If explicitly enabled, rotate and retain them in an external OS/application-data directory, not the checkout.
- Persist compact domain audit records in approved application storage; they are required for replay and are not debug logs.
- Synthetic browser screenshots/test reports may be temporary CI artifacts on failure. Never attach real-data screenshots to public artifacts.
- Temporary upload/parse files are removed when feasible; do not promise forensic erasure of process memory or cloud disks.

---

## 11. Tests and release acceptance

**Tests must check behaviour independently of the production calculation being tested.** Do not generate expected outputs by calling the same function twice or hardcode the real shortlist into the app.

### 11.1 Unit tests

Cover at minimum:

| Area | Required cases |
|---|---|
| IDs | Leading zeros, numeric integral IDs, malformed/fractional IDs, six-digit bounds, distinct group handling. |
| Schema | Reordered/hidden columns, optional missing fields, aliases, duplicate headers, wrong worksheet, stale named range. |
| Deduplication | Identical required rows collapse; conflicting price/code/group does not choose one silently. |
| Cost filter | 999.99 passes; 1000.00 fails; missing/negative values do not pass by accident. |
| Codes/storage | A→C and B→C pass; A→A/C→A do not; H/K excluded as candidates but retained as group context. |
| Price parsing | Numeric/string formats, tiny serialization noise, actual excessive precision, null/zero/nonfinite/error cells and formulas. |
| Time | Confirmed current period; stale history; future columns cannot change features; missing/irregular periods never become adjacent. |
| Statistics | Mid-rank ties, constant history, exact 5% boundary, valid-event denominators, small samples, full versus partial reversal. |
| Ranking | Stable ties and input order independence; analytical settings alter results only on apply; selected named method matches its documented independent expected results. |
| Editable queue | Default 50; custom N such as 1, 17, 80 and 100; all-rankable mode; N above available count; no rankable records; invalid inputs; prefix invariance; pagination/search do not redefine exported membership. |
| Identity | Source/analysis-setting/version changes alter analysis identity; N, all-rankable mode, table sort/search and chart zoom do not. Queue resizing preserves notes and saved export selections. |
| Groups | Unfiltered context; null groups do not coalesce; no inferred historical group membership. |
| Exports | IDs remain text, numbers remain numeric, formulas/URLs not executed, scope and ordered IDs exact for each requested N/all-rankable/selected/all-eligible export, UI numeric parity; N/mode restored in review-session import. |
| Review restore | Correct hashes restore; stale/mismatched source or analysis does not inherit old decisions. |
| Isolation | Two sessions with different uploads/notes cannot see or mutate one another's workspace. |

Hand-checkable examples to encode independently:

```text
100 → 120 → 118
one rise >5%; one eligible next observation; any reversal yes;
full reversal no; giveback 0.10.

100 → 120 → 100
any reversal yes; full reversal yes; giveback 1.00.

100 → 120 → 80
full reversal yes; giveback 2.00, not capped to 1.

100 → 105 → 100
no rise >5% at the default threshold; reversal denominator zero.

100 → 120 → missing → 90
one rise; no observed immediately following outcome for that rise;
do not call 90 a next-period reversal.

[100, 100, 100, 100], current 100
median gap 0; mid-rank percentile 0.5; no repeated-rise signal.

[80, 100, 120, 100], current 100
mid-rank percentile (1 + 0.5*2) / 4 = 0.5.
```

The sequences above use illustrative prices and consecutive calendar slots, not actual product recommendations.

### 11.2 Synthetic integration fixtures

Create small generated workbooks using the same headers and sheet names as the real sources. Include unrelated indicator rows, leading-zero IDs, duplicate/conflicting entries, hidden/reordered fields, missing periods, at least one flat and one oscillating price series, a missing history, a candidate above the cost limit and an excluded-storage competitor.

Generate fixtures in test temporary directories from tracked recipes or tiny text fixtures. Do not derive “synthetic” files by merely hiding names from actual business data. Include enough synthetic products to exercise several requested queue sizes including one above 50, plus a small-dataset case that must not pad the queue. Generate expected prefix IDs independently; do not derive the expected list using the production queue selector.

Run end-to-end: input → validation → ranking → selected-product detail → saved review → Excel/readback → review-session restore. Check all source-to-output counts and a selection of exact arithmetic by hand-authored expected values.

### 11.3 UI and browser testing

Use Streamlit AppTest where supported by the pinned version [R10]. Supplement with a focused Playwright browser test for actual file upload, row selection/sorting, chart changes and downloaded workbook content. Do not assume AppTest covers every interactive table/upload behaviour.

Use accessible labels rather than fragile generated CSS selectors. Test empty app, synthetic loaded app, invalid import, no candidates, short history, mismatch, fewer rankable products than requested, custom N changes, all-rankable mode, selected-product/review persistence after shrinking N, and two isolated browser contexts. Make sure a displayed product never silently differs from the selected row after sorting.

Review screenshots at normal laptop and wider desktop sizes. Check contrast, clipping, clear current-period labels, visible warnings and consistent number formatting. Keep screenshots outside Git; attach only synthetic-data artifacts to CI when useful.

### 11.4 Real-file acceptance

An opt-in command/test marker reads the two source paths from environment/configuration outside Git. With matching reference hashes, independently reproduce the counts in section 3.3 and verify a small product sample against source cells. With different source hashes, report observations rather than failing because market counts changed.

Exercise full-size import on the actual deployment class: count consistency, memory, cancellation/failure recovery and selection responsiveness. Passing a 20-row fixture does not establish capacity for the large workbook.

If licensed files cannot be used in automated CI, that is acceptable: retain strong synthetic CI and record a concise private-data verification outcome in a PR, with no product-level data attached.

### 11.5 Acceptance gates

The first release is ready only when:

- both schemas work without reformatting by the user;
- the eligibility baseline is reproduced on matching private inputs when available;
- current period is explicit and future prices cannot leak into ranking;
- unknown/missing values are neither hidden nor invented;
- editable Top N / all-rankable scope, detail view and exports agree, without fixed-50 assumptions or review loss;
- unfiltered alternatives remain inspectable;
- selected product/review state is stable across reruns and exports;
- temporary versus persistent storage is accurately communicated;
- source data is absent from Git, CI caches, logs and screenshots;
- private access and session isolation are tested on the selected host;
- a fresh checkout can install locked dependencies and start using documented commands;
- automated checks pass and any unperformed acceptance checks are disclosed;
- material improvements to proposed methods/designs are explained and versioned rather than silently diverging from labels or tests.

No test result may be described as a commercial-performance result unless it actually evaluates that outcome.

---

## 12. Coding standards and reproducible developer setup

Keep the code small, explicit and boring in the best sense. Prefer pure functions, small dataclasses, narrow interfaces and direct control flow. Use descriptive English identifiers, typed public boundaries and docstrings for non-obvious assumptions/formulas. Comments explain why, not what each ordinary line does.

No catch-all exceptions that return an empty successful result, global mutable dataframes, one giant `app.py`, copy-pasted feature formulas, hardcoded sample results or broad warning suppression. Catch known boundary errors, attach a structured issue and show a useful message. Unexpected faults fail visibly with a safe diagnostic ID.

Use Ruff for linting/formatting, pytest for tests and one type checker such as mypy. Do not add Black, isort and another linter for overlapping jobs already handled by Ruff. Keep dev/browser/ML dependencies separate from runtime. Use a small, reviewed test suite rather than maximising a coverage number while ignoring critical branches.

Expected setup/quality commands, once the real package configuration is implemented:

```bash
uv sync --locked --group dev
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
uv run ruff check .
uv run ruff format --check .
uv run mypy src/bef_redbird
uv run pytest
uv run python scripts/check_repo_hygiene.py --tracked
uv run streamlit run app.py
```

Provide platform-neutral commands usable in Windows PowerShell and common Unix shells. Do not require `make`, Docker, a GPU, shell aliases or a paid cloud account just to develop. A container is optional for deployment, not mandatory infrastructure.

Avoid redundant generated documentation: maintain this specification, a concise README and the small operational docs indicated in the tree. Update a changelog/release note only for meaningful released behaviour, not every edit. PR descriptions hold execution evidence; do not accumulate timestamped “progress”, “implementation summary” or agent transcript files.

---

## 13. Codex instructions, skills and hooks

### 13.1 `AGENTS.md`

Create a concise root `AGENTS.md` that points to this handover and gives the always-on rules. Codex discovers repository instructions through `AGENTS.md`; repo skills are separate task workflows [R11, R12]. Do not paste the entire handover into every instruction file.

Include this substance:

```markdown
# Repository instructions

Read CODEX_HANDOVER.md before changing behaviour, especially its distinction
between non-negotiable safeguards and revisable design proposals. Independently
verify assumptions; improve the design where justified rather than obeying a
weaker recommendation literally. Explain material changes and version changed
methods/tests together. Do not weaken evidence, privacy or product requirements.
Work only in the intended bef-redbird repository; inspect origin, branch,
worktree and existing changes.

Keep the first release lean: Streamlit review workspace, deterministic analysis,
Excel import/export, no runtime LLM or automatic orders. UI text is Danish.
The queue size is editable (default 50) with an all-rankable option. Resizing is
not re-ranking: preserve analysis identity, ordered results, reviews and notes;
exports use the requested scope and actual count, never a hidden fixed 50.

The six Cito business fields are varenr, opbevkode, kostpris, subkode,
nysubkode, subgrp. Never require discarded columns. Use the LMS data sheet
and only AIP indicator rows. Preserve six-digit IDs, missing observations,
source versions and point-in-time boundaries. A/B→C is not a known AIP rise.

Use the data-integrity skill for import/feature/ranking/export changes,
the UI-review skill for visible changes, and the delivery skill for GitHub work.
Run targeted tests while editing and the required quality gates before handoff.
Never claim checks passed without running them. Do not change tests merely to
make a changed result look correct.

Access authorised original workbooks read-only through explicit external paths;
do not assume earlier chat attachments or ignored files exist in this task.
Use synthetic fixtures when private access is unavailable and report that gap.
No real workbooks, exports, notes, databases, logs, model artifacts, caches,
agent histories or screenshots in Git. Relevant audit data lives outside Git.
Do not remove existing user changes, bypass permissions, or publish private data.

Use focused PRs. Delete merged, no-longer-needed branches locally and remotely
after checking PR state, branch tip and worktrees. Never bulk-force-delete
unmerged or merely old branches. No automatic merge from agent hooks.
```

### 13.2 Focused repository skills

The three skills below are the reference grouping. Combine or rename them if that reduces duplication while retaining data-integrity, UI-review and delivery coverage. Do not generate ceremonial skill files with no practical workflow. Create concise reusable instruction skills under `.agents/skills/`, each with YAML frontmatter `name` and `description` and a short body. This is the documented repository discovery location [R12]. Read any relevant installed skill before using it; do not invent availability or silently install remote skills with code execution permissions.

| Skill | Trigger | Required workflow |
|---|---|---|
| `redbird-data-integrity` | Changes to imports, validation, periods, features, ranking, persistence or Excel/session export. | Identify the affected contract; add an independently expected fixture; test missingness, temporal boundaries and deterministic results; verify source/export parity and provenance; run relevant unit/integration tests. |
| `redbird-ui-review` | Changes to layout, charts, tables, language or interaction. | Keep native controls and restrained visual language; test editable N/all-rankable mode, export scope and selected-ID stability; inspect normal/empty/error/missing-data states; verify chart facts and labels; run AppTest/browser checks and inspect synthetic screenshots outside Git. |
| `redbird-delivery` | Preparing changes for PR, dependency changes, merge or branch cleanup. | Inspect diff and origin; run quality/hygiene gates; write a factual PR; verify CI/merge state; clean eligible local/remote branches safely; report remaining limitations. |

Do not create a plugin distribution project, duplicate skill installers or large copied documentation bundles. Skills are developer instructions, not runtime dependencies.

### 13.3 Git hooks: fast checks at the right boundary

Use `pre-commit` with pinned reviewed revisions [R13]. Required pre-commit checks: Ruff lint/format, trailing whitespace/end-of-file/YAML checks, merge-conflict detection, private-key/secret checks, large-file checks and the repository-specific staged-content guard. Do not auto-stage fixes without review. Choose one reviewed secret scanner rather than stacking several redundant tools; scan only appropriate files and never print a discovered secret in full.

Use a pre-push stage for the fast deterministic test suite and type checks. Full browser/full-size tests can remain CI/private gates to avoid a costly test run after every keystroke. A skipped local hook does not remove the CI requirement.

`check_repo_hygiene.py` must inspect staged contents for commit checks and tracked contents for CI, not just extensions in the working directory. Reject forbidden paths, known private workbooks, large accidental binaries and source-data header signatures in disguised files where appropriate. Permit intentionally authored source/configuration and tiny synthetic text recipes. Do not treat a successful grep as proof that all sensitive data is absent; review the diff as well.

### 13.4 Codex lifecycle hooks: helpful, bounded and reviewed

Codex supports project-local `.codex/hooks.json`; hooks require trust review in supported clients [R14]. Verify the installed client's current event/schema support before adding configuration. Do not copy a different agent product's hook format or bypass trust checks. Git hooks and CI remain authoritative if lifecycle hooks are unavailable.

Implement two small scripts only if they add value:

**SessionStart:** report the repository/branch/worktree state and reminder to read `AGENTS.md`; warn about a wrong origin or dirty tree. Do not print source files, environment secrets or transcripts. No automatic fetching, installation, file deletion or network requests.

**Stop:** for relevant changed code, run bounded non-mutating quality checks and report failures. Honour `stop_hook_active` to avoid an endless continuation loop. Successful Stop output must follow the documented JSON contract. Do not write an agent progress log to the repository. This hook does not certify future commits or replace CI.

A minimal configuration shape, to use only after the referenced scripts exist and the installed client is verified:

```json
{
  "description": "Bounded repository checks; no automatic writes or delivery actions.",
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "uv run --no-sync python scripts/codex_session.py"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "uv run --no-sync python scripts/codex_stop.py"
          }
        ]
      }
    ]
  }
}
```

Launch from the repository root, or adapt paths using a **documented** client mechanism. Do not invent environment-variable names. Scripts must validate the actual Git root, handle missing dependencies gracefully and never implicitly install packages through a hook. Keep any small validation memo in the OS cache, not the checkout, or skip memoisation entirely.

No PostToolUse full-suite reruns, transcript archives, automatic PRs/merges, privilege changes or branch deletion in lifecycle hooks. Hooks are not a security sandbox and must not be presented as one.

---

## 14. GitHub workflow and branch hygiene

### 14.1 Repository setup

Repository name must be **`bef-redbird`**. Work in the provided repository/owner; inspect the remote before any write. Do not infer a GitHub owner from a personal name, create a duplicate repository or push to a similarly named repository.

Prefer a private repository. If repository creation, policy configuration or deployment access is unavailable, finish the local changes and provide the exact pending administrative action. Do not claim settings were applied when they were only recommended.

Configure, when authorised:

- protected default branch (normally `main`), required passing CI and review;
- no force pushes to the default branch;
- squash merge for focused changes;
- automatic deletion of merged PR head branches [R15];
- minimal GitHub Actions permissions, `contents: read` unless a specific job genuinely needs more;
- dependency-update PRs for Python and GitHub Actions, with tested compatibility before merging.

### 14.2 Change workflow

Use one short-lived branch per coherent change, e.g. `feat/price-history-import`, `fix/period-alignment`, `chore/quality-gates` or an existing repository naming convention. Avoid a permanent `develop` branch and empty planning branches.

Inspect existing changes first. Do not reset/stash/delete user work without authorisation. Implement a vertical slice, run targeted tests, inspect `git diff --check` and the staged diff, then open a focused PR. No unrelated formatting sweep or dependency churn.

The PR should state: behaviour changed, data/financial impact, tests actually run, screenshots if UI changed (synthetic only), migrations/rollback considerations and unresolved limitations. Do not paste verbose command logs, private source rows or autogenerated celebratory summaries.

Never self-approve changes with another tool identity, bypass failing required checks or use an admin override to force a merge. Agent reviews are helpful but not independent business approval.

### 14.3 CI

One straightforward workflow on `pull_request` and pushes to the default branch is enough. Use locked dependencies, Ruff, the selected type checker, synthetic unit/integration/AppTest tests and the tracked-file hygiene check. Add the small browser test with synthetic uploads. Include a Windows import/path smoke test if scripts claim Windows support.

Pin third-party actions to reviewed full commit SHAs with readable version comments and keep pins updated. Avoid privileged `pull_request_target` execution of untrusted code [R16]. Do not expose secrets or licensed data to PR jobs. Cache dependency downloads only—not uploaded datasets, DuckDB workspaces, reports or user sessions.

Keep CI output readable. Upload bounded synthetic screenshots/traces only on failure when they are useful; do not commit them. Retain meaningful test summaries, not an archive of every debug print. Cancel superseded PR runs when appropriate.

### 14.4 Delete merged branches locally and remotely

**Policy:** once a change is merged and the branch is no longer needed, remove its GitHub branch, local branch and stale remote-tracking reference. This applies to the branch, not to the merged code or PR history. A clean branch list is part of delivery.

GitHub automatic deletion handles the remote head when permitted; it does not clean every local clone. GitHub CLI also provides `gh pr merge --delete-branch` [R17], but verify the actual state after any operation.

The required outcome is safe local/remote cleanup, not a particular script. A small `scripts/prune_branches.py` with a **dry-run default** and explicit apply is a proposed convenience; use documented checked commands instead if simpler. Keep any helper narrowly scoped to the verified repository. Whether automated or manual, the procedure must:

1. Inspect `origin`, default branch, dirty state and all Git worktrees. Never act in an unrelated checkout.
2. Fetch/prune only as an explicit developer action; fail safely when offline or unauthenticated.
3. Exclude the default branch, protected/long-lived branches, current branch and branches checked out in any worktree.
4. Check the matching PR in this repository is merged into the intended base and the branch is no longer needed by an open/stacked PR.
5. Compare the candidate branch tip with the PR's merged head. If extra commits appeared after merge, retain it for review.
6. Remove the remote branch only after verifying the expected tip, using a lease/expected-ref safeguard where supported. Remote absence alone is not evidence that local work is disposable.
7. Delete the local branch with safe deletion where possible; prune stale remote-tracking references afterwards.
8. Report what was removed or retained and why, to stdout—not a persistent repository log.

**Squash-merge caveat:** the original branch commits may not be ancestors of the squash commit, so `git branch --merged` and `git branch -d` alone are insufficient. A narrowly scoped forced local ref deletion is permitted only after confirming a merged PR, exact unchanged head, no worktree use and no dependent work. Never run blanket `git branch -D` pipelines. Git documents the distinction between safe and forced deletion [R18].

**Obsolete but unmerged branches:** age is not proof of irrelevance. Require an explicit abandoned/superseded decision and check for unique unpreserved commits. Close/update the corresponding PR or issue. Do not irreversibly remove unique work merely to tidy the branch list. When evidence is incomplete, report the branch for human review.

Branch cleanup happens after confirmed merge, not after `--auto` merely schedules a merge. Scripts must not merge PRs, delete tags, prune another person's worktree, disable protections or run destructive `git clean` as a shortcut.

---

## 15. Clean-repository policy

Track application source, tests, small synthetic fixture recipes, documentation, lockfiles, reviewed configuration and GitHub/agent workflow instructions. Nothing else is needed to reproduce a build.

Keep these out of Git even in a private repository:

- real LMS/Cito workbooks and derived business datasets;
- Excel exports, user notes/review-session downloads and screenshots of real data;
- DuckDB/SQLite databases, WALs, Parquet caches and temporary parse files;
- runtime/debug logs, profiling dumps, stack-trace files and agent transcripts;
- coverage output, Playwright traces/reports, build directories and Python/tool caches;
- secrets, environment files, local paths and machine-specific settings;
- trained models and CatBoost's automatic training-output directory.

Suggested `.gitignore` baseline, to refine against the actual tree:

```gitignore
# Python, tooling, builds
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage*
htmlcov/
coverage.xml
build/
dist/
*.egg-info/
playwright-report/
test-results/

# Private input, runtime data and generated output
/data/
/runtime/
/uploads/
/exports/
/reports/
/artifacts/
/logs/
*.log
*.log.*
*.xlsx
*.xls
*.xlsm
*.xlsb
*.duckdb
*.duckdb.*
*.sqlite
*.sqlite3
*.db
*.db-wal
*.db-shm
*.parquet
*.arrow
*.pkl
*.pickle
*.joblib
*.cbm
catboost_info/
*.tmp
*.bak
~$*

# Secrets / machine-specific files
.env
.env.*
!.env.example
.streamlit/secrets.toml
.codex/config.toml
.codex/history*
.codex/sessions/
.DS_Store
Thumbs.db
```

Do not blanket-ignore all JSON/YAML/Markdown or all of `.codex`, `.agents` and `.github`: their reviewed source configuration belongs in Git. Fixture Excel files should be generated in temporary directories, so no broad workbook allowlist is necessary. `.gitignore` does not untrack a file already committed; the staged/tracked-content check is also required.

Prefer external data/cache directories by default, rather than filling the checkout with ignored junk. Cleanup utilities must target only app-created transient files. They must not delete relevant source archives, saved analyses or human review events as though those were disposable logs.

A small performance summary or business-method note is legitimate maintained documentation. A new timestamped report for every tool execution is not.

---

## 16. Future development and CatBoost plan

Preserve clear extension points, but do not implement these as dormant complexity in the first release.

### 16.1 Establish a dated evidence base

Accumulate original Cito snapshots, source availability, publisher history versions, analysis settings/rankings and user reviews in approved durable storage. Append later observed price outcomes to the frozen original analysis.

If older Cito reports become available, validate their schema, effective periods, received-at evidence and original-versus-reconstructed status. Never assume today's group assignments or final codes were available historically.

An updated five-year history can contain retrospective revisions or a changing product universe. Document this limitation in retrospective evaluation and examine survivorship bias. Preserve archived observations instead of silently retraining on a rewritten past.

### 16.2 First model experiment: price history only

A model can be tested without historical Cito snapshots, but it cannot learn past `nysubkode` relationships without those inputs.

CatBoost and the target below are hypotheses to test, not a mandated final method. A better justified target, feature set, simpler baseline or alternative tabular model is welcome. Keep the prediction question explicit, preserve the decision-time rules, and compare alternatives on held-out periods before making performance claims. Do not add an ML dependency to the initial runtime merely because this roadmap names one.

Suggested first target:

```text
y(product, t) = AIP(product, t+1) / AIP(product, t) - 1
```

Use only valid adjacent periods. Features are computed from prices `<= t`: recent returns, lagged prices/returns, rolling position, median distance, observation coverage and past reversal features whose outcomes were already known. Do not use full-series statistics, latest metadata labels as historical facts, or current group membership across the past.

A reasonable first experiment is a regularised CPU CatBoostRegressor, compared with zero return (“unchanged AIP”), recent-change and simple historical baselines [R19]. Avoid raw product-ID memorisation as the first route to apparent performance. No exhaustive search, per-product model fleet or training on UI reruns.

Train offline behind an optional `ml` dependency group. Set a seed, record environment/parameters and explicitly disable automatic training-file output in the checkout (`allow_writing_files=False`); put any authorised model artifacts in external versioned storage. Load only trusted, controlled model files, never arbitrary model uploads.

An alternative classification target such as `next_return > 0.05` must be separately defined, trained and calibrated. Changing the event threshold or forecast horizon changes the prediction task; changing a decision threshold does not. Do not present regression output as a probability.

### 16.3 Evaluation before displaying model output

- Split by **whole pricing periods**, not by random product rows. A row-wise TimeSeriesSplit call on a multi-product panel is not sufficient; build folds on distinct period dates and map them back to rows. Ordered splitting concepts are documented in [R20].
- For each fold, fit encoders/preprocessors only on training data. Do not compute aggregates over future rows.
- Include only training labels whose next-period outcome was actually available before the prediction cutoff. Purge overlapping/unavailable targets and handle horizon gaps explicitly.
- Keep a chronological validation set for hyperparameters/calibration and a later untouched test set. Do not tune the hand-built ranking on the final test either.
- Evaluate return error, material-rise detection, period-by-period precision/lift at explicitly reported review budgets N (including the default 50), false positives, data coverage and subgroup stability. For classification, evaluate calibration, not only AUC.
- Predeclare review budgets and selection metrics before the held-out test (or use validation data to choose them); do not pick the most flattering N after inspecting test results. Support configurable reporting budgets without changing the underlying model prediction/ranking. Any adaptive queue-sizing method would be a separately validated policy, not the ordinary UI count control.
- Use uncertainty estimates respecting period clustering, not millions of rows treated as independent evidence.
- Do not claim performance on A/B→C selections when historic upcoming codes were not available to reconstruct that population. Price-only results are price-only results.
- Do not report portfolio profit without acquisition timing, executable availability, demand, stock and recovery information. Price outcomes are not sales outcomes.

Enable model output in shadow mode first: separately labelled estimate, model/version/date window, missing-data behaviour and easy fallback to the deterministic method. Preserve the original rule ranking for comparison. Promotion requires documented out-of-sample value, operational stability and approval; no silent model replacement.

### 16.4 When historical Cito snapshots become available

Add point-in-time current/upcoming codes, transitions and date-specific group aggregates known before the decision. Keep publication/received times separate from effective dates. Historical availability must be demonstrated, not invented from the final published code.

Assess whether the model actually distinguishes own-price increases from competitor-driven reclassification. Retain the existing source adapters and AnalysisResult contract; add forecast fields rather than rewriting the application.

### 16.5 When sales and inventory become available

Move from “which products deserve review?” to “how much additional stock is economically reasonable?” Inputs include stock, incoming orders, sales, stockouts, expiry, actual acquisition costs and verified recovery terms. Use aggregate product-level data; patient identifiers are unnecessary for this problem.

Sales during stockouts are censored demand, not proof of no demand. Alternatives within one group compete for the same demand; do not allocate a full group forecast to each product independently.

A sell-through model, potentially CatBoost with quantile/multi-quantile objectives [R19], can support uncertainty-aware quantities. Define target, horizon and evaluation independently from the price model. Do not add a second model unless it solves a measured decision bottleneck.

Add an explicit inventory/cash-flow simulation compared with ordinary replenishment. Avoid double-counting a price gain, return recovery and avoided loss on the same pack. Existing stock gains are not automatically benefits caused by the app.

### 16.6 Other extensions, only when justified

- Automated download through a verified stable publisher link; respect authentication, licensing, revision detection and manual fallback. No API is required or assumed.
- More complete current group coverage from an authorised bulk source.
- Verified supplier return/price-credit terms with period-based cap accounting and documentary evidence.
- External persistent storage and stronger workspace access controls if operational usage requires them.
- Group-diversified ranking, original-product benchmark exclusions and unit-normalised comparisons only with clear definitions and necessary metadata.

No project deadlines or delivery estimates are part of this roadmap. Each extension is gated by data availability and evidence, not by adding features for appearance.

---

## 17. Suggested implementation order and handoff completion

1. Inspect repository state and authorised source files; distinguish confirmed requirements from proposed choices, state justified design improvements, and establish the environment, ignore rules and instruction files.
2. Build parsers/validation with synthetic fixtures; independently reproduce private sample checks when files are accessible.
3. Implement the chosen documented features, deterministic full ranking, editable queue selection, provenance and export with independent exact tests.
4. Build the professional list/detail workspace around the same AnalysisResult.
5. Add review-state save/restore including N/mode. Implement temporary mode correctly; add durable storage only if it improves the first release enough to justify its cost, otherwise retain the documented extension boundary.
6. Verify browser flow, source/export parity, session isolation and full-size resource use.
7. Complete CI, reviewed hooks, private deployment documentation and safe branch cleanup.

Use focused commits/PRs, but do not fragment one tightly coupled change into dozens of administrative artifacts. Keep existing user work intact.

At implementation handoff, report:

- working features and exact launch/deployment commands;
- automated checks actually run and their outcomes;
- whether real-file and hosted/private-access checks were performed;
- known data/method limitations, justified departures from proposed designs, and intentionally excluded functionality;
- repository/PR status and branch cleanup completed or awaiting verification.

Do not end with unsupported statements such as “production-ready”, “guaranteed accurate”, “fully validated profit ranking” or “all tests pass” unless the precise claim is supported. A trustworthy implementation states what it calculates, what remains unknown and where the user must still decide.

---

## 18. Primary technical references

These references support API/platform behaviour, not the commercial effectiveness of the proposed screening rules. Verify compatibility against the installed versions. The input schema/counts above come from the supplied workbooks; commercial timing and code interpretations come from the project requirements.

- **[R1] Streamlit selectable dataframe:** https://docs.streamlit.io/develop/api-reference/data/st.dataframe
- **[R2] Streamlit private sharing:** https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app
- **[R3] Streamlit cache scope and isolation considerations:** https://docs.streamlit.io/develop/api-reference/caching-and-state/st.cache_data
- **[R4] Streamlit local-file persistence caveat:** https://docs.streamlit.io/develop/concepts/connections/connecting-to-data
- **[R5] DuckDB concurrency:** https://duckdb.org/docs/current/connect/concurrency
- **[R6] Plotly line/gap behaviour:** https://plotly.com/python/line-charts/
- **[R7] openpyxl streaming/read-only mode:** https://openpyxl.readthedocs.io/en/stable/optimized.html
- **[R8] XlsxWriter workbook options:** https://xlsxwriter.readthedocs.io/workbook.html
- **[R9] uv locking and syncing:** https://docs.astral.sh/uv/concepts/projects/sync/
- **[R10] Streamlit AppTest:** https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest
- **[R11] Codex AGENTS.md:** https://developers.openai.com/codex/agent-configuration/agents-md
- **[R12] Codex repository skills:** https://developers.openai.com/codex/skills
- **[R13] pre-commit:** https://pre-commit.com/ ; Ruff integration: https://docs.astral.sh/ruff/integrations/
- **[R14] Codex lifecycle hooks and trust:** https://developers.openai.com/codex/hooks
- **[R15] GitHub automatic head-branch deletion:** https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-the-automatic-deletion-of-branches
- **[R16] GitHub Actions security:** https://docs.github.com/en/actions/reference/security/secure-use
- **[R17] GitHub CLI merge/branch deletion:** https://cli.github.com/manual/gh_pr_merge
- **[R18] Git safe/forced branch deletion:** https://git-scm.com/docs/git-branch
- **[R19] CatBoost regression and objectives:** https://catboost.ai/docs/en/concepts/python-reference_catboostregressor ; https://catboost.ai/docs/en/concepts/loss-functions-regression
- **[R20] Time-ordered validation:** https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html

- **[R21] Codex cloud environments and prepared filesystem:** https://developers.openai.com/codex/environments/cloud-environments
- **[R22] Codex environment modes and file availability:** https://developers.openai.com/codex/environments/modes
- **[R23] Codex local CLI and permissions:** https://developers.openai.com/codex/cli ; https://developers.openai.com/codex/permissions
- **[R24] Codex best practices and focused repository guidance:** https://developers.openai.com/codex/learn/best-practices
