# Inputs, calculations and review contracts

This reference describes the implemented temporary Streamlit review workspace.
Historical priority is a deterministic screening rule for human review, without
a validated purchasing-return or forecasting claim. Upcoming C does not establish
an own-price rise: a relative classification can also change with competitors.

## Input contracts

Cito requires six fields: `varenr`, `opbevkode`, `kostpris`, `subkode`,
`nysubkode` and `subgrp`. Headers are case/whitespace normalized; `ny subkode`
also maps to `nysubkode`. XLSX requires the `Table` sheet. Legacy BIFF XLS is
Cito-only and requires exactly one sheet whose first row contains all six
unambiguous headers. Optional Cito fields do not affect calculations.

LMS requires XLSX, the `Medicinpriser data` sheet, `Varenummer`, `Indikator`,
and date columns. Only indicator cells exactly equal to `AIP` supply prices.
Dates are Excel dates or `YYYYMMDD`; duplicate dates are rejected. Periods are
sorted chronologically. Unknown metadata outside the date block is ignored;
unknown or blank headings inside it are rejected. Recognized reference fields
may occur between dates. Optional medicine, strength, form, pack and company
labels are for display. Ambiguous headings and formula/error labels are ignored.

Required formulas need a values-only export; cached formula results are not
accepted. Macro/encrypted files are rejected. Originals are never rewritten.
Worksheet dimensions and named ranges do not define the parsed boundaries.
Upload/resource limits are described in [deployment.md](deployment.md).

Product IDs are normalized to six-digit strings, preserving leading zeros.
Blank group IDs stay missing and never join unrelated products into a group.
Identical normalized Cito six-field rows collapse with source-row references.
Conflicting rows, or an invalid row with an otherwise identifiable product ID,
exclude that product and mark affected group context as incomplete.
Blank storage is retained as unspecified; blank classifications are retained
but fail candidate eligibility.

Prices are integer øre. Numeric Excel cells use `Decimal(str(value))`; at most
`1e-7` øre of serialization noise is accepted. Text accepts Danish formats such
as `1234`, `1234,56` and `1.234,56`, excluding dot-decimal text. Negative,
nonfinite, excess-precision and over-limit amounts are invalid. Cito needs a
valid cost. Invalid or missing AIP observations become gaps; zero is retained
as source evidence but is not computable. Conflicting duplicate LMS observations
become gaps only at the affected product/period, even if later rows agree.

## Period, eligibility and rankability

The operator confirms the current Cito pricing-period start; it is not inferred
from the latest LMS date. Optional received-at timestamps require timezones.
Receipt, upload and price-period dates are distinct. Retrospective prices do
not prove that the same source version was available at an earlier decision.

The baseline filters valid Cito products in this order: exclude storage H/K;
require current A/B and upcoming C; require cost strictly below 100,000 øre
(DKK 1,000). Eligible products remain candidates even without rankable history.

Ranking additionally requires the confirmed period in the LMS calendar, a
positive AIP exactly at that period, exact equality between that AIP and Cito
cost, the full requested window length, the minimum valid-price count, and no
conflicting LMS observation inside the analysis window. Future and older
out-of-window conflicts do not block ranking. Failure reasons remain visible;
unrankable candidates never fill the ranked queue. Missing group is not itself
a ranking failure. A diagnostic older-period price match never changes the
confirmed period automatically.

Groups use valid members of the current Cito report before candidate filtering,
so H/K and other ineligible products remain available as found alternatives.
Group completeness and historical membership are not established. Package AIP
does not establish unit-price or dispensing-price comparability.

## Features and exact boundaries

Defaults are 26 lookback periods, at least 20 positive prices, a rise threshold
of `0.05`, low-price percentile `0.25`, at least two rises for repeated rises,
and five observable after-periods for standalone reversal-rate display.
Exports and the in-app method guide describe the applied settings.

Let W be the last requested number of loaded LMS periods at or before the
confirmed period, and S the positive valid prices in W. Missing periods/prices
are not filled. Current AIP exists only at the exact confirmed date.

- Valid-price count is `len(S)`; coverage is `len(S) / len(W)`, undefined if W is empty.
- Median is the middle value of S, or the mean of its two middle values; half-øre survive.
- Median distance in DKK is `(median_ore - current_aip_ore) / 100`.
- Price percentile is `(count(S < current) + 0.5 * count(S == current)) / len(S)`.
  A constant series has percentile 0.5. Median/percentile need their inputs.
- An adjacent pair needs two positive prices in consecutive W slots exactly
  14 days apart. Missing values and irregular calendar gaps are never bridged.
  Pair change is `new / previous - 1`; previous change is available only for
  such a pair ending at the confirmed period.
- A rise requires `(new - previous) / previous > rise_threshold`, strictly.
  Exactly 5% is excluded at the default threshold. Rise frequency is rise count
  divided by valid adjacent-pair count, undefined without such pairs.
- For a rise from A to B, C is observable only when the next W slot is positive
  and exactly 14 days after B. Reversal denominator counts only these events;
  a rise at the window end has no observable after-period.
- Any reversal is `C < B`; full reversal is `C <= A`. Each rate divides its
  count by the observable-event denominator, undefined when that denominator is
  zero. Below the display minimum, show counts and limited evidence.
- Next change is `C / B - 1`; giveback is `(B - C) / (B - A)`.
  Giveback is not clipped: it can be negative or greater than one.

Rise and low-percentile boundaries use exact integer ratios derived from
Decimal settings, before display rounding. All event prices must lie in W;
later observations never enter features or ranking. Retrospective chart
display can show later prices without changing the analysis.

## Ranking, identity and exports

`historical-priority-v1` assigns tier 0 to rankable products with percentile at
or below the low-price boundary, positive median distance, and enough rises.
Tier 1 meets the same low-price/distance conditions without enough rises.
Tier 2 contains the remaining rankable products. Sort by tier ascending,
median distance descending, percentile ascending, then normalized product ID
ascending; ranks start at one. There is no weighted score. Reversal metrics
are context and do not change this order; a group may contribute multiple rows.

Analysis identity hashes both source-byte SHA-256 values, confirmed period and
received-at context, canonical applied analysis settings, eligibility policy,
parser/feature/ranking versions and identity schema. Run ID and creation time
are separate. Current versions are `excel-v3-period-conflicts`,
`price-history-v1`, `historical-priority-v1`; identity/session schema is 1.

Top N (initially 50) slices the full ranking; all-rankable returns it whole.
N, search, sorting, inspected product and chart range do not change analysis
identity, ranks or saved reviews. Reviews belong to product plus analysis.
Only saved judgments enter downloads; unfinished drafts stay in the session.

Excel exports the canonical queue, explicit selection or all eligible candidates.
It includes the decision overview, candidate evidence, found group relations,
and method/data-control metadata: sources, hashes, applied context/settings,
versions, filter counts, queue/export scope and ordered exported IDs.
Source text is written as text, preventing spreadsheet formula/URL conversion.
JSON retains saved reviews, selection, inspection and queue preferences; restore
requires matching sources, analysis ID, context, settings, versions and schema.
It replaces review state and drafts. Neither Excel nor JSON is a source-history
backup, and sessions have no durable storage guarantee.
