# Offline E1 evaluator

The evaluator reads one explicitly selected, frozen research panel. It never
opens the foundation database, chooses sources, acquires data, schedules work or
promotes a model. The producing workflow must admit and reconcile the population
before calling it. All input and result artifacts belong outside Git.

```powershell
uv run python -m bef_redbird.research validate C:/Private/e1-panel/manifest.json
uv run python -m bef_redbird.research evaluate C:/Private/e1-panel/manifest.json C:/Private/e1-runs/run-001
uv run python -m bef_redbird.research verify C:/Private/e1-runs/run-001
```

Python callers can import `EvaluationConfig`, `evaluate`, `load_dataset`,
`validate_dataset` and `verify_run` from `bef_redbird.research`. Imports are lazy;
the separate saved-results dashboard does not import execution or fitting code.
`evaluate` requires a new output directory and optionally accepts `config`,
`lineage_path` and `development_parent` (the earlier run fingerprint whose
exposure informed development). There is no hyperparameter search.

## Frozen panel, `e1-panel-v1`

The UTF-8 JSON manifest requires:

| Field | Meaning |
| --- | --- |
| `schema_version` | `e1-panel-v1` |
| `dataset_id` | Nonempty producing selection/version ID |
| `price_context` | `price_type=aip`, `currency=DKK`, `price_basis=per_package`; `tax_basis` and `fee_basis` each explicitly `included` or `excluded` |
| `availability_mode` | `reconstructed_research`; E1v1 rejects strict replay until a versioned availability-proof contract exists |
| `periods` | Ordered objects with contiguous integer `index` from zero, ISO `start`, optional actual `end_exclusive` matching the next start |
| `files` | Every input file pinned by `role`, relative `path`, SHA-256 `sha256` and integer `size_bytes`; exactly one `panel` role |
| `population_definition` | Current-period admission and coverage definition, including known sampling limits |
| `identity_policy` | Versioned causal continuity/split policy |
| `provenance` | Nonempty object identifying audited source/interpretation/mapping selections and their evidence |

Every pinned file, including extra `provenance.jsonl.gz` and source-admission
evidence, is verified before computation and again before publication. Manifest
parsing, its SHA-256 and the saved input-manifest copy use one captured byte
sequence. The original manifest and all input pins are reverified after panel
parsing, so a concurrently changed recipe cannot bind old parsed context to a
new manifest hash. Paths
cannot escape the manifest directory. `panel` is CSV or CSV.gz, with these
required columns:

| Column | Contract |
| --- | --- |
| `period_index` | Canonical nonnegative integer in the declared calendar |
| `canonical_identifier` | Six-digit text, leading zeros retained |
| `identity_episode_id` | Nonempty for eligible rows; one package per episode; consecutive calendar observations; cannot resume eligibility after an ineligible interval |
| `amount_ore` | Nullable canonical nonnegative signed-64-bit integer; eligible values strictly positive; zero retained on excluded rows |
| `eligible` | Literal `true` or `false` |
| `exclusion_reason` | Empty when eligible, explicit state when excluded; must not impersonate `observed` or `not_observed_rise` |
| `abc` | Current ordinary `A`, `B`, `C`, `unknown` or empty |
| `group_id` | Current evidenced substitution-group ID or empty |
| `provenance_id` | Precise nonempty producing row-catalogue reference |

Optional columns are `lifecycle_age_periods` (nonnegative integer or empty),
`identity_state` (default unknown), `reported_supply_positive` (empty or `true`,
never `false`) and `supply_coverage_unknown` (`true` by default). An absent supply
report means unknown history, not no disruption. Source publication and input
admission are producer responsibilities: a schema validator cannot independently
prove that retrospective evidence was available at a historical ordering cutoff.

No future survival filter is applied. Every eligible current row is assessed;
all excluded current rows remain in the assessment stream and coverage. Gaps,
zero prices, unsupported basis, conflicts, unresolved identity, lifecycle
exclusions and pending horizons retain distinct states. The evaluator does not
carry prices across missing calendar observations.

## Frozen experiment

The default 47-period calendar uses initial training indices 0–23, validation
24–31, final evaluation 32–44 and diagnostic tails 45–46. JSON configuration may
explicitly replace these bounds and cutoff/bootstrap settings for a different
predeclared experiment; splits must be contiguous, nonoverlapping and exhaust
the calendar, with two periods after the final official origin. Configurations
are pinned in the result. Do not change them after viewing final results to
obtain a more favourable comparison.

The default cutoff is Wednesday in the second tariff week at 12:00
Europe/Copenhagen, including the local UTC offset. Noon is an assumption, not
evidence of supplier terms or historical publication. Cutoffs use the declared
actual start dates and must remain inside their tariff period.

Next-period direction is exact integer-ore fall/flat/rise with zero tolerance.
Magnitude targets are log price return and next AIP DKK. Reversal is a separate
conditional task after an already observed strict rise: within the next two
declared tariff periods, a full reversal reaches or falls below the pre-rise
price. Both future prices must be observed in the same identity episode, even
if the first future price already reversed. Partial reversal and persistence
remain descriptive categories; missing horizons are not negatives.

Each origin fits only examples whose outcomes have matured at that origin:
`j+1 <= t` for next-period targets and `j+2 <= t` for reversal. Features use
only their original current/past observations. The fixed expanding algorithm
continues to include earlier matured final-window examples during later final
origins. It never selects methods, tunes thresholds or fits calibration on the
final holdout. This evaluates a fixed sequential updating procedure.

Features are explicitly ordered in `FEATURE_NAMES`: current log AIP, three lag
log returns and missing flags, past episode rise/fall/flat frequencies, history
length/missingness, current lifecycle age/missingness, fixed current ordinary ABC
one-hot/unknown, current eligible group count and own/min AIP ratio/missingness,
and positive supply-report observed/unobserved/coverage flags. Group counts cover
only admitted same-basis panel members, not an asserted complete market.

The four next-period methods are no change, Dirichlet-smoothed historical
direction frequency with mean historical log return, repeat last observed
change, and a fixed ridge candidate. Reversal compares no reversal,
Beta-smoothed historical observed-rise reversal frequency, and fixed ridge.
Ridge fits three one-hot direction targets, log return, or binary reversal;
it uses training-only means and population scales, an unpenalized intercept and
`sum(squared errors) + 1 * sum(squared standardized coefficients)`. Scales below
1e-8 become one. With fewer than feature-count-plus-two matured rows it uses
the historical fallback and reports that fact. Probability predictions are
clipped/normalized; every magnitude method is bounded to ±log(100). Fixed
encodings require no fit. Sufficient statistics accumulate each mature row
once; fitting retains O(feature-count²) statistics rather than replaying dense
training matrices. Coefficients and transforms are JSON, never executable
pickle objects.

## Saved result, `e1-run-v1`

`run.json` is published only after all result files are complete. It pins the
exact input manifest, all its file pins, dataset declarations, experiment
definitions, ordered features, split indices/dates, methods, configuration,
code-file hashes, Python implementation/package versions, OS release/machine,
the actual Europe/Copenhagen timezone-data source/version/TZif hash and
completed/locked status. A source checkout additionally pins its `uv.lock`
SHA-256. Wheel environments explicitly report that the checkout lock is absent
and retain the relevant runtime package-version list; that is not a substitute
for the checkout's complete dependency lock.

| File | Contents |
| --- | --- |
| `input-manifest.json` | Exact original manifest bytes; original panel/provenance bytes remain in their frozen source bundle |
| `predictions.jsonl.gz` | Every current catalogue row, eligibility and provenance, causal feature vector, separately joined label states, each method's probability/direction/magnitude; reversal forecasts only after an observed rise |
| `models.jsonl.gz` | Per-origin/task training counts, latest mature training origin/horizon, fitted means/scales/intercepts/coefficients and fallback |
| `metrics.json` | Official aggregate and per-period scores, denominators, calibration bins, temporal-block uncertainty; initial/tail results explicitly separate |
| `coverage.json` | Current candidates, exclusions, label attrition and observed-rise counts by period, overall row counts |

All next-period methods share exactly the observed-label population; all reversal
methods share exactly the fully observed rise/horizon population. Metrics include
multiclass Brier (sum over classes, range 0–2), binary Brier, exact direction
accuracy, MAE log return, MAE AIP DKK and precision among the highest rise
probabilities in the top `ceil(n/10)` observed next-period cases per period
(canonical-identifier tie break). Ranking precision is conditional on observable
outcomes; it is not a complete-market procurement yield. Aggregates sum the
per-period numerators/denominators, including the selected-ranking denominator.
Empty outcomes yield empty metric objects and zero counts, never manufactured
accuracy. Calibration uses ten fixed bins, with probability one in the last bin.

Uncertainty uses fixed-seed circular moving blocks of tariff periods (default
length three, 1,000 replicates), preserving zero-label periods and weighting
scores by their metric denominators. Bounds are null with fewer than two observed
periods. These conditional, descriptive intervals do not treat packages as
independent temporal replications or prove performance in other market regimes.
No procurement savings or realised-profit claim is produced.

An external append-only, hash-chained `holdout-lineage.jsonl` records evaluation
intent before fitting/scoring, with a deterministic input/code/configuration
fingerprint. Completion binds that fingerprint to the final run fingerprint,
dates and optional development parent. When a parent is declared, an additional
`used_for_development` event marks that parent's exposure in the external log.
It is separate from the immutable bundle. Start/lock events can conservatively
remain after a failed evaluation/publication; do not erase exposure. A
dashboard cannot prevent a researcher from later tuning on displayed outcomes.
Subsequent development must disclose exposed parent runs and use a genuinely
new final window for independent confirmation. There is no automatic promotion.

`verify_run` streams and verifies every pinned result byte. Semantic replay
fingerprints include deterministic artifact hashes and omit only the actual
creation timestamp and external lineage-event hash. Code/dependency versions are
captured before fitting and reverified before publication. Gzip has fixed metadata, so
replaying unchanged code/configuration/input on the same pinned runtime platform,
dependency set and timezone data produces identical
artifact bytes in a new output directory. Source-file damage, partial lineage or
an existing output directory fails closed. Interrupted staging directories are
cleaned on handled errors; no incomplete result appears at the requested path.
