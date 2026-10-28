# Bounded historical group research

E2 is the owner-authorized successor to the accepted E1 evaluation and R1
input-only bundle. It permits the declared local research fits and comparisons,
without scheduling, automatic retraining, model promotion, ordering or deployment.
E1 and `full-history-training-v1` retain their original meanings and bytes.
Execution status belongs in [progress.md](progress.md); private measured results,
source references and counts remain outside Git.

## Data and group boundary

LMS group identifiers already existed. Validating dated group populations and
comparable peer features was the missing analytical work. The
`lms04-equal-native-size-peers-v1` sidecar assembles
complete selected LMS04 package populations for each actual tariff start before
joining back to the package-history partitions. LMS01 drug groups, LMS05 nonfixed
price groups, LMS07 reimbursement groups and dose contexts remain separate.

The conservative price comparison uses identical nonempty native package-size
and unit tokens within the same dated LMS04 group. It preserves exact AIP per
package, excluding VAT and consumer dispensing fees. It does not convert physical
quantities, compare differently sized packs, infer clinical equivalence or
reconstruct ABC. The retained technical specification is modern; historical
descriptor support comes from the accepted versioned profile admission and
independently reopened native records, not a projection of modern ABC rules.

Current [official ABC guidance](https://laegemiddelstyrelsen.dk/da/tilskud/priser/laegemiddelgrupper-substitutionsgrupper/)
concerns dispensing price per unit. AIP, dispensing
price per package, dispensing price per unit and reimbursement references remain
different quantities. The original bundle's supported ordinary ABC is retained
as an observed attribute, with its existing scoped exclusions.

Peer counts, distinct evidenced distributor codes, minimum, exact twice-median,
spread, own gap and observed previous peer-change shares exclude the focal
package. A group-period must have complete native members and valid identity,
price and size/unit joins; disputed or unsupported scopes remain unknown. A
zero-peer comparable stratum is distinguished from incomplete membership; native
group size separately distinguishes a true source singleton from mixed sizes.
No count is a sales share. Distributor codes do not establish supplier-disruption
coverage or consolidate legal entities.

Full claimed sidecar coverage is reconciled to retained native members and a
separate whole-population SQL recomputation. This validates the admitted dated
descriptors and equal-token price populations; it does not establish physical
unit conversion or complete clinical substitution across differently sized packs.

## Scientific boundary

`protocol.json` is frozen before comparative model scores, with exact input,
producer and dependency hashes. Development descriptive estimates are explicitly
recorded separately. History screening uses earlier validation only; fixed
annual evaluation fits use strictly mature earlier labels. All existing history
is retrospective and exploratory; E1 exposure is not erased. A future calendar
reservation is conditional on actual exposure and publication evidence.

The primary score is equal-period multiclass Brier (lower is better), with
package-weighted scores, calibration, rise ranking, fall exposure and coverage
reported separately. Comparison intervals use paired tariff-period differences
and temporal blocks. Package rows are not independent market experiments.

The feature-only candidate path does not consult future labels. It saves all
eligible candidates and the fixed top-decile membership before attaching later
outcomes. Missing selected outcomes are never replaced, unchanged prices or
prediction failures. Observed shortlist performance, unknown fractions and
sensitivity bounds retain their distinct denominators. Ties use a declared
seeded policy, with a uniform-selection expectation for comparison.

The fixed comparisons cover own-price history, supported package attributes and
ABC, then the same inputs plus group relationships. Logistic preprocessing is
learned on the fitting rows only. CatBoost quantization and categorical encoding
are fitted locally on that same earlier population. No Codex/OpenAI inference
is involved. The amount regressor uses MAE loss for DKK MAE reporting: its median
forecast is not an expected monetary gain. Reversal starts only after an already
observed rise and requires the accepted two-period maturity definition.

The executed comparisons favour group information on the primary probability
score, including the matched population with actual comparable peers. Gains are
not universal: zero-peer and unsupported cases, calibration, fall exposure and
missing shortlisted outcomes need separate attention. CatBoost outperforms the
tested simple methods on that score. Three recent fitting years improve the
earlier validation result over one; the expanding-history attempt hit its frozen
time limit and has no predictive score. Feature lookback did not change with the
fitting window. The magnitude improvement is modest and extreme errors remain;
reversal probability scores improve while calibration is worse. These are
retrospective research findings, not a promotion or financial-benefit claim.

The private owner report and acceptance index retain measured values, all trials,
source limitations and verification receipts. The conditional future-confirmation
reservation fixes a candidate, comparators, observation horizon and stopping rule;
activation needs separate authorization and evidenced pre-outcome exposure. No
calendar extension or future prediction was performed in E2.

## Reproduction and viewing

The results view explains each task beside its saved scores: the question,
historical test and practical interpretation. The optional score and method
guide distinguishes probability error from accuracy, price error from savings,
and observed shortlist outcomes from unknown outcomes. Method explanations
separate own history, package attributes and comparable-peer inputs. These are
presentation explanations; viewing does not change the accepted comparisons.
Legends, method selectors and comparison tables use readable presentation names,
while raw method identifiers remain unchanged in saved files and filtering.
Each metric chart states the question it answers and the favorable direction.
The visible task guide states that E2 tested current-period ABC only; advance
ABC for the next period was not an E2 input.

For later pharmacist review, keep a reported upcoming ABC separate from the
current ABC used by the accepted model. Record its source, receipt time and
target period. A relative ABC transition does not by itself establish an own-AIP
change or future sales. SQL access supplies an acquisition route, not proof of
the upstream calculation, early availability or forecast accuracy. Any use as a
new predictive input needs its own dated feature contract and evaluation; do not
substitute upcoming ABC into the model's current-ABC input.

The owner-defined next research question assumes a one-period ABC information
lead: prior and current prices/ABC plus advance next-period ABC may be inputs;
ABC two or more periods ahead is excluded. Unpublished next-period AIP remains
an outcome to predict: next AIP value, its DKK change from current AIP and the
direction of that change, evaluated by advancing historical decision origins.
Historical recorded next-period ABC can be a separately
labelled proxy scenario for that advance signal, with the assumption that it
matches the early report and sensitivity to missing/incorrect codes. It does
not establish historical early availability. Compare this extra feature with
the current-only baseline on the same cases, then verify using genuinely dated
advance snapshots. This follow-up was not fitted or evaluated in E2; it now has
its own separately authorized [E3 protocol](e3-research.md), without changing E2.

The saved research dashboard does not yet generate a live procurement queue.
A future review view can combine frozen price forecasts, separately identified
early ABC evidence, supported supply information and pharmacist stock/demand
context. Candidate priority, suggested quantities and measured financial benefit
are separate outputs with different evidence requirements.

Run from the application repository with Python 3.12:

```powershell
uv sync --locked --group dev --group research --group browser

# Byte-checked recovery and captured-producer replay into unused destinations.
$cycle = Get-Date -Format 'yyyyMMdd-HHmmss'
uv run --no-sync python -m bef_redbird.research.research_recovery restore `
  --bundle ..\data\recovery\research-e2-v1 `
  --destination "..\data\research-e2-replay-$cycle" `
  --receipt "..\data\research-e2-replay-$cycle-receipt.json"

# Read-only dashboard. Bind explicitly to this computer; no firewall access is needed.
uv run --no-sync streamlit run research_dashboard.py `
  --server.address 127.0.0.1 --server.port 8513 `
  --browser.gatherUsageStats false -- `
  --e2 ..\data\research-e2\run-v1\saved\result-reviewed.json
```

The dashboard reads only the explicitly pinned saved JSON summaries. Viewing
never selects live sources, opens executable model files or triggers training.
E1 remains available through its existing entry point.

Direct `e2_experiment replay --root <run>` replaces that run's `replay.json`
verification receipt. Predictions and outcomes stay unchanged, but accepted
receipt bytes should remain pinned. The restore command above runs captured
producer replay on its own disposable copy and preserves the accepted run.

For a fresh scientific reproduction, use the captured producer and the same
pinned inputs. This is an intentional new set of fits, not part of viewing or
replaying results. Run from the application repository and choose unused paths:

```powershell
$accepted = Get-Content ..\data\research-e2\run-v1\protocol.json -Raw | ConvertFrom-Json
$pointer = Get-Content ..\data\training\current.json -Raw | ConvertFrom-Json
if ($pointer.dataset.sha256 -ne $accepted.dataset.sha256) {
  throw 'Current training pointer differs from the accepted experiment'
}
foreach ($pin in @($accepted.dataset, $accepted.groups, $accepted.producer)) {
  if ((Get-FileHash -LiteralPath $pin.path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.sha256) {
    throw 'Accepted input or producer bytes changed'
  }
}
@'
import importlib.metadata, json
from pathlib import Path
p = json.loads(Path('../data/research-e2/run-v1/protocol.json').read_text())
assert all(importlib.metadata.version(k) == v for k, v in p['dependencies'].items()), 'Dependency versions differ'
'@ | uv run --no-sync python -
if ($LASTEXITCODE -ne 0) { throw 'Restore the captured dependency lock before fitting' }
$cycle = Get-Date -Format 'yyyyMMdd-HHmmss'
$reproductionRoot = "..\data\research-e2-reproduction-$cycle"
$producerCopy = "$reproductionRoot\producer"
$newRun = "$reproductionRoot\run-v1"
if (Test-Path -LiteralPath $reproductionRoot) {
  throw 'Reproduction destinations must be unused'
}
Expand-Archive -LiteralPath $accepted.producer.path -DestinationPath $producerCopy
$priorPythonPath = $env:PYTHONPATH
try {
  $env:PYTHONPATH = (Resolve-Path -LiteralPath "$producerCopy\src").Path
  uv run --no-sync python -m bef_redbird.research.e2_experiment freeze `
    --root $newRun --training-pointer ..\data\training\current.json `
    --groups $accepted.groups.path
  if ($LASTEXITCODE -ne 0) { throw 'Protocol initialization failed' }
  uv run --no-sync python -m bef_redbird.research.e2_experiment run --root $newRun
  if ($LASTEXITCODE -ne 0) { throw 'Experiment execution failed' }
} finally {
  $env:PYTHONPATH = $priorPythonPath
}
```

Use the captured dependency lock and recorded Python/package versions for exact
version reproduction; the explicit check above rejects changed model dependencies.
The initial `uv sync --locked` command applies the repository's current lock, so
compare it with the captured lock before reproducing after later dependency
updates. Current source changes require their own protocol binding.
The original completed directory is immutable research evidence, not a target
for overwriting trials. Every attempted scientific fit, including failures,
remains in the trial ledger. Resource-limited expanding-history trials must not
be described as successful full-history fitting.

Research recovery copies only the explicitly inventoried research payload and
producer. It verifies the retained training/source dependencies without another
complete database copy. Recovery and replay are local checks with the existing
interpreter, not evidence of fresh-machine or off-device protection.

Sales, stock, expiry and supplier terms are still needed to assess procurement
quantities and financial benefit. Unknown historical source publication timing
prevents claiming proven historical Wednesday operational readiness.
