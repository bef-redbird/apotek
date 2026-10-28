# Manual prospective amount comparison

This bounded adapter reuses six July 1, 2026 amount models and the unchanged E6 D
summer three-year calibrated probability reference. It never fits, recalibrates,
acquires market sources, opens the operating database or schedules work. The
three amount histories share the same eligible cases and probability-selected
rows within each information mode. No purchase quantities or savings are produced.

The private operation root is `../data/prospective-comparison`; `config.json`
pins the protocol. All commands below run from `bef-redbird` with the existing
locked environment. Do not initialise a second operation to replace a bad result.

```powershell
$comparison = '..\data\prospective-comparison'
uv run --no-sync python -m bef_redbird.research.prospective --root $comparison status
uv run --no-sync python -m bef_redbird.research.prospective --root $comparison verify
uv run --no-sync python -m bef_redbird.research.prospective --root $comparison replay
uv run --no-sync python -m bef_redbird.research.prospective --root $comparison report
```

`replay` performs retained-input inference, including all six amount and probability
components, calibrated routes, ranks and selected membership. Exact array equality
is required; it is exposed historical validation, never a prospective observation.
`status` and the viewer read saved evidence. `report` explicitly publishes saved
evaluations and the viewer index; it performs no inference. All clocks use UTC
instants with the decision convention expressed in Europe/Copenhagen.

## First enrolment and later manual freezes

The initial state is **not enrolled**. The supported local candidate is October
26 → November 9. Its proposed cutoff is November 4 at 12:00 Copenhagen, not a
confirmed final purchasing deadline. Its retained current-only features and
source receipts have been verified. The October 17 Cito report belongs to October
12 → October 26 and cannot support this forecast. The twelve candidate origins
are backed by retained official calendar evidence; the operating calendar is
unchanged. Calendar evidence establishes boundaries, not publication times.

The missing admission input is a current, dated declaration of actual prior
target-price exposure, identifying its actor and scope. Absence of a local outcome
file does not prove absence of exposure. `exposure.template.json` deliberately has
null values. Complete it truthfully, retain its bytes, and bind its hash, actual
check time and exposed status in a new `current-context.json` copied from
`current-context.template.json`. Do not use a later effective date as evidence
that future prices remain unknown. If any price outcome has already been seen,
declare exposure; the result cannot be a prospective primary.

```powershell
# Only with completed, evidence-bound context. The supplied template fails closed.
uv run --no-sync python -m bef_redbird.research.prospective --root $comparison prepare `
  --features ..\data\research-e4\features\2026-10-26-assessment.parquet `
  --context "$comparison\current-context.json"
# Use the exact prepared.json path printed by prepare.
uv run --no-sync python -m bef_redbird.research.prospective --root $comparison freeze `
  --prepared '<printed prepared.json path>'
uv run --no-sync python -m bef_redbird.research.prospective --root $comparison report
```

Later origins require a new causally assembled feature snapshot, source receipts,
feature-lineage receipt, current exposure declaration and valid calendar context.
The October snapshot is not reusable as a later origin. Receipt and publication
are separate; unknown publication stays unknown. Source receipts must cover every
declared feature-lineage dependency. Do not infer any timestamp from filenames or
modification times. Complete feature assembly and the forecast before the cutoff.
An earlier evidenced deadline requires a pre-recorded deviation, never a later one.

For a hybrid, use `information_mode: hybrid`, an E4 native actual report manifest
and its verified period-bound receipt in `advance_report` and
`advance_report_receipt`. Supported codes use advance components; absent or
unusable codes use same-history current components and the accepted probability
fallback. Final future official ABC is not an advance report. Missing reports
remain **advance unavailable**, not evidence about real advance information.

The first valid completed freeze per origin/mode is primary. Identical retries
verify and return the same result. Changed bytes or context make a linked
supplementary identity; performance cannot replace the primary. Staging directories
are never accepted publications. A writer lock causes a clear busy/interruption
failure; inspect its PID and stop the owning process before manually removing a
stale lock. Do not remove an active lock. Completed publications recover their
registration markers on retry. Keep interrupted evidence until recovery succeeds.

## Separate outcomes and interpretation

```powershell
uv run --no-sync python -m bef_redbird.research.prospective --root $comparison attach-outcome `
  --assessment '<this operation assessment.json>' `
  --official '<E4 official target snapshot.json>' `
  --receipt '<official outcome admission receipt.json>'
uv run --no-sync python -m bef_redbird.research.prospective --root $comparison report
```

The outcome admission receipt pins `artifact` and `evidence` and records `period`,
`basis: official_aip_ore`, `status: authoritative_resolved`, `received_utc`,
`published_utc` and explicit `precedence`. Corrected editions create linked outcome
versions. No download-time precedence is inferred. Wrong target, conflicting basis,
non-integer money, or pre-freeze official availability cannot be admitted as later
prospective evidence. Identity changes, gaps, unresolved target prices and missing
packages remain explicit; selected cases are never replaced. Attachments cannot
change inputs, predictions, ranks or selections.

Saved reports name their outcome versions and as-of date. They show equal-period
forecast and observed means, bias (forecast minus actual), absolute error and
period root mean squared error, coverage, fixed bins, own/common-bin membership,
tails, top ranks and a no-change reference. Raw forecasts are a sensitivity check;
supported next prices are nonnegative and their changes are coherent.

Six scheduled origins trigger a descriptive interim; twelve consecutive scheduled
origins trigger the reporting milestone. Misses and unknown outcomes stay on the
chronological grid. No replacement or favourable-period extension is allowed.
Temporal blocks of two and four use 2,000 draws and seed 20261028. Sparse evidence
does not receive a meaningful interval. Neither milestone establishes annual
stability, universal superiority, a promotion criterion or financial value.

## Saved viewer and recovery

```powershell
uv run --no-sync streamlit run research_dashboard.py `
  --server.address 127.0.0.1 --server.port 8521 --server.headless true `
  --browser.gatherUsageStats false -- --prospective "$comparison\index.json"
```

Open `http://127.0.0.1:8521`. Page loads only read checksum-verified saved artifacts.
Stop with Ctrl+C. Existing E4–E6 and volatility flags remain available separately.

```powershell
$restore = "$comparison\scratch\restore-manual"
uv run --no-sync python -m bef_redbird.research.prospective_recovery restore `
  --bundle "$comparison\recovery" --destination $restore
uv run --no-sync python -m bef_redbird.research.prospective_recovery replay `
  --bundle "$comparison\recovery" --destination $restore `
  --receipt "$comparison\restore-manual-receipt.json"
```

Choose unused restore and receipt paths. The capsule captures producer code and
operation artifacts, reuses hash-pinned shared models and retained inputs, and
contains no operating database. Replay blocks fitting, network and database access
and original operation/code reads. This is local recovery with the existing
interpreter; it is not fresh-machine or off-device proof. Preserve the shared
dependencies listed in the capsule and replay receipt.
