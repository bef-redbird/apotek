# Bounded historical research

E1 evaluates package AIP forecasts using explicitly frozen evidence. It is separate
from the current pharmacist review application, the DuckDB viewer and the later
transaction/inventory workflow. Actual completion and checks belong in
[progress](progress.md); source and temporal invariants remain in the
[data contract](data-foundation.md#research-panels-and-evaluation).

## Structured-source admission

`lms-ftp-zip-v1` accepts an explicitly identified final ZIP with all required
members. It verifies ZIP integrity, exact member names, fixed-byte widths, CRLF,
SYSTEM counts and package/DrugID joins. The caller must supply final phase,
edition, a separately established tariff interval and a daily effective date
inside that interval. A daily release date does not create a new tariff period.

The documented current layout and the bounded observed older layout have separate
profile names. Layout compatibility does not establish a historical specification,
price basis, complete market population or historical availability. Unsupported
vintages fail admission; extension requires new native evidence and versioned
interpretation. A syntactically complete defective edition can still be incomplete
semantically. No directory-name rule automatically activates a replacement.

The adapter emits direct AIP, registered, consumer and reimbursement-basis prices,
plus current ordinary ABC and substitution groups. Reserved zeros are not prices.
Actual zero price tokens remain zero source assertions. AIP tax/fee context
requires a pinned, period-scoped reviewed definition; unrelated monetary contexts
remain separate. Other ZIP members are preserved and structurally checked without
claiming that all their business fields have been interpreted.

Use `source_audit` on every newly published import. Its independent LMS reader
reconstructs expected assertions from native bytes without calling the adapter.
Complete fidelity proof is necessary but does not establish source preference.
Record matching-period and matching-basis overlap, exceptions, population scope
and source semantics separately before freezing a research selection.

Acquisition preserves original paths/names, exact bytes, sizes, hashes and observed
receipt bounds. Server modification times and effective dates are distinct fields;
neither substitutes for evidenced publication. Preserve same-name changed bytes as
new receipts rather than overwriting an earlier original. Future collection must
retain those semantics; no scheduler is part of E1.

## Panel and identities

`python -m bef_redbird.research.panel` freezes an explicitly accepted selection
recipe from a closed foundation. It checks each full scoped source proof against
current persisted records before exporting any usable panel. The output includes
the selection recipe, native ZIPs, context evidence, independent reports, exact
observation/mapping hashes, row provenance, population exclusions and calendar.
A completed `dataset.json` is published only after the export succeeds. Existing
output directories are never overwritten.

The first policy selects positive supported fixed-price pharmacy AIP, per package
DKK, VAT and fees excluded. It retains every source row, with explicit exclusions
for other price contexts, hospital scope, zero/invalid prices and unsupported
lifecycle context. It does not infer a zero invoice price or deletion from these
states. Current ordinary classifications are features, not eligibility criteria.
Classified group features describe the eligible observed group members, not an
assertion of complete competition or supply.

Identity episodes are scoped to the exact selected catalogue. They continue only
across consecutive eligible tariff observations with matching package, DrugID and
creation evidence. Gaps, ineligible intervals and changed tokens start a new
episode. This conservative policy does not declare every creation reset a new
medicine or merge historical packages permanently. Current lifecycle age is known
only where creation binds to an admitted calendar start; earlier creation remains
left-censored. Supply is unknown without a separately accepted identity/coverage
selection; absent reports never become no disruption.

## Chronological comparison

The evaluator reads only the frozen dataset and verifies every file pin. It has
no connection to the foundation. Features use current and earlier observations
within the episode, plus current ordinary classification and group context.
Future labels are joined separately on actual calendar indices. Missing prices,
nonpositive prices, absent packages, changed identities and pending horizons remain
explicit unavailable outcomes. Methods share the same observed comparison cases.

Next-period direction uses exact integer-øre differences with zero tolerance.
Probability and log-return/price magnitude errors are separate measures. Reversal
conditions on an already observed strict rise: full reversal reaches the pre-rise
AIP within two following tariff periods; partial reversal falls below the current
AIP without reaching that reference. Both future prices must be observed for the
declared comparable target. This is not a pre-rise strategy or a profit estimate.

The fixed methods are no change, historical frequency/magnitude, an explicit
last-change rule and regularized linear probability/log-return regression. Reversal
uses no reversal, historical frequency and the same bounded learned approach.
Preprocessing and fitting admit only earlier examples whose target horizons have
matured at the current origin. The expanding algorithm is fixed before evaluation;
earlier matured validation/final-origin evidence may enter subsequent fits. The
final window is excluded from tuning, calibration selection and threshold choice.
The tuning budget is zero; no method is automatically promoted.

The default research configuration declares initial training, validation, final
evaluation and diagnostic tail intervals before fitting. Source-specific dates are
pinned in the private configuration and run. The Wednesday second-week noon cutoff
in Europe/Copenhagen is configurable and remains an operational assumption.
Reconstructed availability never claims actual historical publication or possession.

Results retain complete assessments, features, labels, predictions, fitted
coefficients/preprocessing, split dates, method/code/dependency versions, coverage,
calibration and per-period metrics. Confidence intervals resample blocks of tariff
periods; packages within a period are not independent temporal replications. Short
calendar coverage and conditional label attrition limit claims. A losing candidate
is a valid result and is retained. Procurement quantities and savings require
additional demand, stock, transaction and commercial evidence.

## Saved-results dashboard

The separate entrypoint reads explicitly selected completed run manifests. It
verifies pinned files, displays official frozen results separately from exploratory
filters, and marks incompatible run comparisons. Market-period performance is
distinct from development-run progress. It does not query DuckDB, load executable
models, resolve outcomes, train, schedule or promote methods.

Launch it with the project's existing environment and an accepted local manifest:

```powershell
uv run streamlit run research_dashboard.py --server.port 8503 -- <path-to-run.json>
```

Additional explicit run paths allow comparison. There is no automatic newest-run
selection. Missing, incomplete or corrupt artifacts remain visible failures.
Learning curves require actual training-window experiments and are not invented
from successive development runs. Holdout exposure/development lineage is recorded
by the producing workflow outside the immutable run; the dashboard can display it
but cannot enforce the researcher's later choices. A result used for subsequent
method development is no longer independent confirmation of that revised method.
