# Approved product direction

Status: approved direction; implementation status is maintained separately in
[progress.md](progress.md).

The latest owner instruction authorizes implementation, reviewed commits/push and
local-only deployment of the assisted pharmacist MVP. This supersedes earlier
no-delivery restrictions in historical research entries below. Public hosting,
new model training/promotion, email sending and meeting slides remain outside scope.

This document governs new development. The [data foundation](data-foundation.md)
defines the approved data contract. Together they supersede earlier handovers,
platform prescriptions, scoring proposals and temporary-session-only plans.

The existing application is a working baseline. Its behaviour is documented in
[calculations.md](calculations.md), its current use in [README](../README.md), and
its operating limits in [deployment.md](deployment.md). Those descriptions remain
accurate until code changes; they do not restrict the future architecture.

## Purpose

Help a pharmacist make better, explainable procurement decisions from dated
market evidence, then measure the quality and practical value of those decisions.
Keep source facts, analytical estimates, human judgements and realised outcomes
distinct.

The immediate priority is a trusted historical market database and an engine
that can be evaluated across past periods, then maintained as new evidence
arrives. The existing week-to-week demo already supplies a useful baseline.
The first bounded historical evaluation and saved-results dashboard are complete.
The available supported FTP backfill and source-comparison investigation are
complete. The readiness milestone covers one lean working store, simple direct
supplier-file intake, a fully validated historical training bundle with a
compatible loader, and private cleanup and recovery. Its verified execution
status is in [progress.md](progress.md). After accepted E4, the owner has authorized
bounded [E5 research](e5-research.md): dated eligibility, fresher training,
controlled tuning, monetary uncertainty, persistence and financial rankings.
Earlier accepted experiments are reused rather than restarted.
The subsequent bounded [E6 research](e6-research.md) investigated
current-information-only ranking, consequences of the actual Cito code errors,
the Huber outer comparisons and three/six/full training history. Full-history
models were actually fitted and evaluated. Probability selection and the
three-year direction reference remain supported; older-history amount forecasts
have research merit with material cross-year limitations. Both frozen October
assessments remain preserved and operational promotion remains separate.
On 28 October the owner authorized scheduled FTP downloads under the existing
signed-in Windows account, configurable for a separate collector later. Collection
must remove the WinSCP dependency entirely and retain the PowerShell maintenance
entrypoint. This authorizes source acquisition, not scheduled training, promotion
or purchasing. Actual activation and verification are recorded in progress.
The owner's later 28 October clarification prioritizes an assisted local MVP for
the 29 October meeting, with a portable report the owner can email manually.
Cito is the only pharmacist-supplied assessment input; FTP and manually downloaded
supplier reports are maintained and ingested separately by the operator. Show the
next unknown period's prediction and prediction-ranked top-50 candidates with
actual outcomes for each of the last two completed transitions. Use period-bound
Cito evidence and retain current-only fallback, missing outcomes and retrospective
labels. A simple optional with/without-Cito comparison should use the same target
and market snapshot. Present clear visual evidence with technical detail available
on demand. Authenticated private browser access from the pharmacist's own computer
is a later phase; SQL access is not required. Defer the meeting deck while this
operating work is unresolved. This clarification records scope, not implementation
or operational model promotion.

Build the smallest reliable system that supports this workflow. Add complexity
only when a real source, measured limitation or accepted use case requires it.
Existing design choices are revisable; evidence and explicit contracts govern
changes.

## Historical research and operational workflow

Collect the available dated archive, audit its contents, ingest verified source
observations, then freeze reproducible historical datasets. Once the backlog is
processed, the same pipeline handles new releases, corrections and newly found
gaps. Acquisition completeness and successful parsing are separate acceptance
claims.

Evaluate methods by advancing through historical decision periods. Hide future
prices, classifications, group membership, outcomes and identities that are not
admitted as available at the simulated cutoff. The owner-defined advance-ABC
experiment admits previous/current prices and ABC plus next-period ABC, while
excluding ABC two or more periods ahead and unpublished next-period prices.
Predict next-period AIP value, its change and direction. Historical final next ABC
can be a labelled proxy for an advance report, with explicit availability and
accuracy assumptions; compare with the current-only baseline and later dated
advance evidence. E2 did not test this information lead.
Fit preprocessing and models only on eligible
earlier training data; respect the maturity of outcome labels. Unknown historical
publication timing permits explicitly reconstructed research, not a claim that
the pharmacy possessed those inputs at the time.

Research must produce complete, reproducible candidate assessments and later
outcome comparisons. A current-week pilot, Cito or a transaction feed is not a
prerequisite. The owner's explicit E3 authorization permits the separately frozen
advance-ABC comparison, fall-aware queues, valid monetary forecasts/uncertainty,
and supported different-size peers with origin-time fallback. Retain E2's accepted
history selection and baselines. The [E3 guide](e3-research.md) defines the new
boundary; earlier E2 magnitude/reversal results remain unchanged.
The executed E3 comparison supports a conditional next-ABC information gain,
with smaller peer/routing gains and a measured rise-versus-fall queue trade-off.
Incorrect advance codes can reverse gains; extreme monetary errors and weak
conditional interval coverage remain. Preserve the current-only comparator and
use genuinely dated prospective shadow evidence as the next performance check.
These findings do not authorize promotion or an operational purchase policy.
Freeze the protocol before comparative scores are inspected. Preserve accepted
E1 and R1 artifacts. No automatic retraining, promotion, purchasing, deployment,
commit, push or external upload is authorized.

The current operational assumption is a purchasing decision before the supplier's
Wednesday ordering cutoff in the second tariff week. Exact upcoming AIP normally
becomes available after that decision, on the last working day before the next
tariff period. Use actual dates and confirmed release evidence, including holidays.

1. Prepare candidates from information available before the decision cutoff.
2. Freeze the complete assessment, its evidence, settings and exclusions.
3. Let the pharmacist inspect the evidence and record a judgement.
4. Check price predictions against the subsequently published upcoming AIP.
5. When transaction data becomes available, evaluate receipt prices and actual
   financial outcomes separately.

Wednesday is a configurable working assumption, not a universal ordering rule.
Publication, official tariff start, ordering, receipt and invoicing are different
events. The reported receipt-based supplier charging arrangement remains an
explicit assumption until supported by terms or transactions. A price-rise
prediction alone cannot establish the quantity to buy or realised profit.

## Source strategy

FTP/LMS is the primary source for supported market facts and future routine
collection. Integrate its full available historical scope, including actual schema
vintages and release families, rather than extending only the first research
window. Inventory acquisition, interpreted fields and accepted coverage separately.
Native ZIP retention alone does not mean the full registry has been imported.

The routine website input is one manually downloaded supplier disruption report
every fourteen days. The archive downloader is not needed for that routine.
Pass that file to `scripts/Import-SupplierReport.ps1`; the supported workflow
validates its native context, retains its true intake receipt, independently
verifies the observations and publishes changed supplier pins. Known bytes and
unchanged pins do not require another full-history publication. A changed report
with unresolved revision precedence remains outside the selected facts.
Collect other PDF/Excel reports only for a named information gap in FTP that has
demonstrated value to the tool. Judge that need by useful fields and their meanings,
not exact equivalence of every printed description or display code in a report.

The owner accepts a working historical scope beginning in 2008. Target one lean
working database containing FTP facts, supplier disruptions and only evidenced
useful exceptions. Analytical deduplication alone does not justify retaining
redundant legacy observations in the live store. A reviewed migration may retire
their live copies once source coverage and dependencies are checked and earlier
accepted results remain reproducible from retained archival recovery material.
Keep that material outside the default operating and model paths; the existence
of unique 2007 archive data is not a reason to retain it in the working database.
Do not rewrite accepted evidence or discard its only recoverable copy. Actual
source schemas govern adapters; there is no universal format-change date.

The completed website/FTP comparison supports that collection policy within its
examined field, date and population scopes. Comparable modern AIP agreement is
not whole-history equivalence. Reimbursement-reference disagreements, unresolved
ABC context, malformed values and identity gaps retain their own scope; they do
not automatically invalidate otherwise supported AIP or authorize website
overrides. Keep the compact exception evidence and historical labels available
for a future useful feature decision.

Retrospective bulk price-history exports are excluded as the primary corpus and
as model inputs for this phase. Retain them for reproducibility and explicitly
labelled cross-checks only. Their irregular releases do not establish a complete
sequence of historical editions or decision-time availability.

Cito remains validation-only in the accepted historical foundation. E4 additionally
admits explicitly period-bound actual reports into separate frozen research
assessments, with current-only fallback and separately attached official outcomes.
This does not promote an operational model or establish prospective performance.
Keep reported current/upcoming classifications
separate and validate period binding against independently established periods.
High agreement with final ABC classifications does not prove early availability.
Future use of preliminary ABC requires evidence of the query's upstream inputs,
report periods and actual access timing; SQL access alone is insufficient.

Validate FTP schema, coverage, release timing and overlapping assertions before
granting a particular field/period scope authority. Complete snapshots can
establish unchanged-price denominators where change lists cannot. Primary-source
status does not resolve unknown correction precedence or historical timing.

Models consume one resolved, versioned dataset. Overlapping FTP/archive assertions,
alternative parsings and unchanged daily editions must not multiply training
examples. Retained raw evidence stays outside the default training path. Use a
small explicit selection policy for primary facts, permitted supplements and
unresolved conflicts; do not build parallel competing analytical databases.

## Approved foundation

- One local persistent DuckDB database, one controlled writing process, an archive
  of immutable originals, and frozen evidence for each analysis run.
- Separate source deliveries from parsing imports; retain provenance and
  interpretation versions.
- Store typed, dated observations for prices, package attributes, classifications,
  supply and lifecycle events.
- Select evidence using declared coverage, source authority and availability at
  the decision cutoff. Preserve disagreement and missingness.
- Import older history and future LMS releases through versioned adapters into
  the same domain model.
- Keep private originals, derived business data, databases, run artifacts and
  reviews outside Git.

The schema and acceptance requirements are in
[data-foundation.md](data-foundation.md). Progress and the next implementation
slice belong only in [progress.md](progress.md).

## Platform and analytical choices

The final product platform is open. Streamlit, the existing UI layout, its
temporary sessions and the current pair of Excel uploaders describe the shipped
baseline; they are not requirements for the future product. Preserve useful
behaviour when changing it deliberately, rather than treating old implementation
details as product policy.

Local DuckDB is the approved first data store. Choose hosting when an operational
workflow needs shared access or availability beyond the local machine. Scheduled
collection alone does not require hosting or a new database.
PostgreSQL is a future operational-storage option; no hosting provider or final
product frontend is selected. A migration must preserve the domain contract and evidence,
but is not assumed to require zero adaptation.

Start evaluation with explicit rules and simple historical baselines. Existing
eligibility thresholds, `historical-priority-v1`, the A/B-to-C screen and supplied
scoring approaches are comparison candidates, not validated optimal policies.
Keep named methods reproducible; give semantic changes new versions.

CatBoost is an approved candidate for bounded E2 evaluation, not a preselected winner
or a dependency of the source-data foundation. Compare methods on chronological
decision periods, with availability-correct inputs, separate future labels and
honest coverage. More old data does not automatically improve present decisions.
Model training or data backfills must not automatically replace a deployed method.

Dataset preparation uses a model-neutral typed feature/label boundary and a
separate bounded-memory reader. R1's accepted input-only construction remains
unchanged; E2 must measure actual fitting capacity and results separately.
Missing supply joins or uncertain
group populations limit those features; they do not block price-only research.
Full-history streaming, the measured input window and eventual fitting capacity
are separate acceptance claims.

E1 provides a separate local research dashboard for tracking saved runs and
performance across historical periods and successive method versions. Reuse the
existing Streamlit and plotting dependencies through a separate entrypoint; keep
evaluation code independent of the UI and preserve the existing application.
This is a bounded implementation choice, not a commitment to the final platform.

Read pinned E1–E6 result artifacts, without a connection to the live foundation or
model execution. Show baseline/candidate comparisons, coverage and missing
outcomes, split dates, price basis, availability assumptions and uncertainty.
Keep performance by tariff period separate from performance across development
runs. Show learning curves only when actual training-window experiments produced
them. Changes in datasets or eligible cases must not masquerade as model progress.

Keep the predefined evaluation results visible and label filtered views as
exploratory. Display holdout exposure/development status from the producing
workflow; a read-only dashboard cannot enforce the researcher's future choices.
Simplify the existing dashboard around a few relevant comparisons and plain
descriptions. Show observed better/worse results against a named baseline, mark
uncertainty or incompatible comparisons, and avoid an overall success badge when
results are mixed. Put detailed diagnostics behind an optional view. Do not add
training controls, automatic promotion or a new experiment-management service.

Acquisition and import maintenance use one resumable pipeline for backfill and
new releases. Build and test its scheduler integration, leaving the task disabled
until a later owner instruction. Keep scheduling separate from the dashboard and
from retraining; use the existing local tooling and protected credential setup.

Compare price-change probability and magnitude, persistence/reversal over declared
horizons, and supported classification, competition, lifecycle and supply signals.
Treat previously suggested reversal frequencies and financial benefits as
hypotheses to measure, not accepted facts or training targets.

Evaluate procurement policies separately from price forecasts. Inventory scenarios
must track quantities, purchase/receipt costs, sell-through, valuation, unsold
stock, expiry and applicable costs across periods. Do not count a paper inventory
gain again as sales profit. Without pharmacy inputs, disclose the demand and
commercial assumptions and report sensitivity; do not call simulated amounts
realised savings. Return/rebuy strategies require applicable credit/return terms,
and classification changes alone do not establish unsellable stock.
Include applicable stock price compensation, quantity caps, fees and adjustments
to return credits. An AIP fall is not automatically an equal net financial loss.
Keep public standard terms, the pharmacy's actual agreement and observed credits
distinct; do not apply current terms to historical periods without dated evidence.

Use rolling chronological evaluation and reserve prospective confirmation before
actual outcome exposure. A new date label does not make exposed history untouched.
Compare against simple rules and no-change/no-extra-purchase policies, include
coverage, uncertainty and downside, and report performance across market regimes.
Retraining and promotion are separate, versioned decisions supported by this
evidence. Do not add an online reward system or reinforcement learning before
there is a valid outcome definition and evidence it improves the simpler methods.

## Development sequence

| Stage | Deliverable | Completion evidence |
| --- | --- | --- |
| 1. Historical data foundation | Accepted F1–F4 plus full available FTP history, useful supplements and repeatable maintenance | Original-to-database reconciliation, one resolved analytical dataset, explicit coverage/conflicts, recovery and disabled scheduling acceptance |
| 2. Reproducible research and analytical evaluation | Frozen datasets/assessments, later outcome checks, baselines and candidate models, plus a separate local results dashboard | Chronological comparisons, deterministic replay, honest uncertainty/coverage and verified display of accepted results; no hindsight claims |
| 3. Pharmacist workflow | Reuse the evaluated engine for review, saved judgements and operational outcome monitoring | Cutoff exclusion, preserved reviews and versioned outcomes; no requirement to rebuild the existing demo first |
| 4. Operational enrichment | Sales, stock, expiry, supplier terms and purchase/receipt/invoice data | Verified joins, quantity/financial definitions and evaluated procurement effects |
| 5. Shared operation | Suitable product platform, hosting and operational database if needed | Access, durability, concurrency, migration and restore acceptance |

Audit and ingest the entire available historical corpus. Finish the current
FTP-primary foundation milestone before new model experiments; preserve accepted
E1 results and their original coverage. Missing or unsupported source evidence
must remain visible. Maintain genuine acquisition timestamps alongside the
backfill, without inventing historical receipt or publication times.

## Boundaries and open evidence

- Upcoming ABC is a relative classification, not proof of an own-AIP increase.
  Cito is reported to be a purpose-built SQL export. Evidence for one report's
  receipt and periods does not establish a general historical release schedule.
- FTP access and directory depth do not establish accepted historical coverage,
  exact release timing or equivalence to daily wholesaler disruption reports.
- Source coverage varies by period and report family. Unknown supply history
  cannot be encoded as no disruption.
- Exact operational cutoffs and supplier charging terms need evidence before
  they support execution or realised-benefit claims.
- Sales, stock, expiry and commercial terms are later inputs. Keep automatic
  ordering and clinical advice outside the current scope.
- No paid services, autonomous runners or new orchestration frameworks are part
  of this plan.

An unresolved fact blocks only the mechanism that requires it. Represent the
uncertainty and continue work whose inputs are supported.

## Maintaining direction

Update this document when the approved product direction changes; update the data
contract when its meanings or invariants change. Update progress when work moves
between states, with the verification needed to justify completion.

Do not maintain competing roadmaps or long running handovers. Local agent
instructions should point here. Keep historical rationale in version control or
an explicitly inactive private archive, not mixed into the active plan.

Keep the working environment understandable as the project grows. Maintain one
current operating entry point and a stable private folder structure for live data,
source evidence, accepted results and recovery. A new milestone must not create
another competing operational tree. Separate disposable checks and rehearsals
from retained evidence from the start, and review and remove or stage superseded
material before closing the work. Preserve immutable dependencies through an
explicit location map when consolidation changes a required path; folder age
alone never proves that its contents are disposable. Cleanup and a usable folder
structure are part of delivery, not deferred work for the owner.
