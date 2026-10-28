# Approved data foundation

Status: approved design, not a claim of implemented persistence. This contract
specifies the data design for [the current direction](direction.md); status is in
[progress.md](progress.md). The existing session-only application is described
separately in [calculations.md](calculations.md).

## Storage boundary

Use one local persistent DuckDB database with one process controlling writes.
Keep immutable original bytes and frozen run artifacts in approved private
storage outside Git. Store archive-relative locations and content hashes, with
the archive root supplied by configuration.

There is one import path: archive/register, parse, validate, reconcile, then
publish accepted observations transactionally. Format adapters own file-specific
details; business calculations consume the shared domain model.

Acquisition manifests are input inventories, not the foundation database or
sources of verified effective dates. Routine inputs are verified FTP releases and
one directly supplied disruption-report file; they do not require the historical
website downloader. Keep historical acquisition context for reproduction. Never
recursively treat databases, backups, synthetic evidence or comparison collections
as market inputs. Preserve receipt context when registering deliveries.

Keep manual-file relocations in a separate private location map with original and
current paths and verified hashes. Moving an original is not a new publication
or historical receipt. Historical manifests and frozen provenance remain intact;
current audit input lists may resolve the new physical locations explicitly.

No separate database service, feature-store service or general event framework
is needed for this stage. Research can read consistent published snapshots;
independent processes must not casually share the live writable database file.

## Record identity and shared conventions

- Stable internal IDs identify packages, deliveries, imports, observations and
  runs. Preserve them across migrations and restores.
- External package identifiers are text. Preserve original representations and
  canonical six-digit varenummer values, including leading zeros.
- An observation represents one source assertion, not a globally unique market
  fact. Each has `observation_id`, `import_id`, `source_locator`, the source
  identifier and mapping reference, effective context and quality state.
- Source locators identify a sheet/cell or row, PDF page/record, RTF
  paragraph/record, or LMS member/record/field. Exact originals remain recoverable.
- Delivery and import metadata supply source revision and availability context;
  do not duplicate guessed publication times into every row.
- Use `DATE` for business dates and half-open validity intervals. Store precise
  event instants in UTC; retain source timezone/precision metadata and schedule
  local decisions in `Europe/Copenhagen`.
- Store exact per-package monetary amounts in integer ore (`BIGINT`) when their
  source precision permits it. Preserve zero as evidence; zero is not a valid
  divisor. Negative, malformed and nonfinite prices are issues.
- Do not silently round higher-precision amounts. Introduce an explicitly
  specified exact decimal representation when such a source field is needed.
  Per-package and per-unit values, currency and tax/fee bases are distinct.
- Canonical native price interpretation requires an evidence-pinned, versioned
  value policy in addition to monetary context. Keep exact native amounts,
  including zero. Unexplained zero AIP, registered, consumer or hospital prices
  have `zero_value_unresolved` state and are unusable; this does not mean missing
  or free. Explicitly evidenced, date-scoped dispensing and calculation rules
  may establish `no_fixed_price` or `not_calculated`; contradictory positive
  amounts have `price_context_conflict`. Rules require supported, unambiguous
  references from the same selected snapshot and phase, exact codes and reviewed
  descriptions. TSP and non-reimbursable-component zero remain published reference
  or component facts, without an entitlement inference. Unit and DDD quantities
  remain independent. Native uncertainty never reactivates a suppressed legacy
  fallback or rewrites frozen legacy decisions. Policy evidence, its exact hash
  and the analytical read contract are pinned by each new publication.
  Native field-state maps are sparse: an absent entry records no exception.
  Treat it as present only when the field is supported and its value is non-null;
  explicit blank, invalid or ambiguous states still take precedence.
- Core business values belong in typed columns. Small versioned manifests,
  configuration and presence/quality metadata may use JSON.
- Missing, unreported, explicitly blank/withdrawn, invalid and conflicting values
  must remain distinguishable. Nullable columns alone do not encode that meaning;
  retain field-presence metadata and structured issues.

Enforce ordinary primary/foreign keys, required fields and scalar checks in the
database. Validate temporal overlaps, identity ambiguity, release completeness
and cross-record rules in the import/selection layer.

## Reference and provenance tables

| Table | Grain | Essential fields and relationships |
| --- | --- | --- |
| `price_period` | One official tariff period | `period_start` key, `period_end_exclusive`; calendar coverage is independent of whether a source file was collected |
| `package` | One identified medicine package | Stable `package_id`; mutable descriptions belong in observations |
| `package_identifier` | One evidenced external-identifier mapping | `mapping_id`, `package_id`, namespace, identifier value, applicable interval/scope, evidence import/locator, mapping version and availability evidence |
| `source_delivery` | One receipt of a source file/release in a declared context | `delivery_id`, source, original filename/URI, content hash, archive-relative location, receipt/publication evidence, edition/release key, phase, report context, coverage and replacement manifest |
| `import_batch` | One versioned interpretation of a delivery | `import_id`, `delivery_id`, parser/schema/mapping versions, interpretation-context hash, processing/completion times, validation status and counts |
| `import_issue` | One validation or interpretation problem | `issue_id`, import, source locator, affected identifier/field/period, issue code, severity and blocking scope |

Identifier mappings can be unresolved or limited to evidenced periods. An
unknown validity interval is not proof of validity across the entire archive.
Do not merge by name or silently relink old observations when a mapping changes.
Retain mapping revisions; frozen runs pin the mappings actually used.

Identical bytes can have distinct receipt contexts, so a content hash is not a
delivery's availability or edition identity. Repeating the same delivery,
parser and interpretation is idempotent. A new parser creates an alternative
derivation; the active selection must never count both derivations twice.

Delivery context must identify the report's actual current/target periods where
known, not infer them from a folder, filename, modification time or latest price.
Retain conflicts between declared and observed context as issues.

## Typed market observations

| Table | Grain | Essential business fields |
| --- | --- | --- |
| `price_observation` | One price assertion for a package in a stated context | Package/mapping, price type, amount, currency/basis, effective period where established, report period and price role |
| `package_observation` | A coherent package-attribute record supplied by one source | Name, form, strength, pack description, company, storage code, ATC when provided, observed/effective context and field presence |
| `classification_observation` | One package's reported classification/group context for a period | Period, classification context, ABC code, group namespace/code, presence and quality |
| `supply_observation` | One dated package/wholesaler supply-status assertion | Observation date, wholesaler identifier/namespace and reported status |
| `lifecycle_event` | One asserted introduction, deactivation or permanent deletion | Event type, effective date, original source code and interpretation status |

All five tables use the shared observation provenance above. A valid resolved
view selects at most one applicable value per business key and context while
retaining the supporting assertions and conflicts.

### Prices

AIP, registered price, consumer/dispensing price and printed reimbursement basis
(TSP) are explicit different price types. A consumer-price definition does not
establish the tax basis of reimbursement amounts. Reported percentage changes
must retain their price basis; do not treat them as AIP changes. Derive
analytical AIP changes from the selected AIP amounts.

Distinguish `effective_price` from `reported_previous_price`. An old price quoted
on a change list is not automatically a price for the preceding tariff period.
When its effective period is unknown, retain its report context and exclude it
from a fabricated historical price slot.

Missing calendar slots stay missing. A latest-known value does not establish an
unobserved price or extend validity beyond the supported interval.

A change list is not a complete package-price snapshot. Reconstructing unchanged
intervals requires an independently evidenced complete change-feed contract,
an initial price anchor, continuous applicable release coverage, supported identity
and lifecycle intervals, and correction semantics. Without those facts, do not
carry the last observed AIP forward or treat absence as no change. If those facts
are established, introduce an explicitly versioned reconstruction method and
document its contract before use. Keep its derived values and supporting intervals
distinct from directly reported price assertions; never fill gaps in raw evidence.

### Attributes and classifications

A current description/company/pack attribute cannot be backdated merely because
the same workbook contains older prices. Attribute selection is field-aware:
choosing the latest sparse row must not erase unrelated supported fields.

Cito current and upcoming ABC become separate observations for their respective
periods. Its current group applies only to the current report context. Upcoming
ABC is a reported classification, not a model prediction or a price type.

Scope group identifiers by issuing namespace, classification context and
applicable period/edition. Distinguish ordinary and dose-dispensing
classifications. Do not invent historical or upcoming membership from current
membership. Unknown group IDs must not join unrelated packages.

### Supply and lifecycle

Repeated wholesaler reports are dated observations, not independent incidents.
Lack of an entry is not proof of recovery. Lack of archive coverage is not a
zero-disruption observation. Derive incident episodes only under a separately
defined method with adequate observation coverage.

Direct supplier intake validates native headers and the report's native ISO-week
sheet window independently of its filename. Preserve each row's effective date;
the report window, row date, intake instant and unknown publication time are
different facts. Unsupported layouts/dates require review, not a guessed period.
Coverage remains `unknown`, and positive assertions are not carried forward into
unobserved days or periods. Preserve wholesaler namespace/code and source-local
identity; an unproved FTP identity join must remain unavailable.

Retain immutable report bytes and a genuine first direct-intake timestamp. An
identical retry reuses that receipt; newly presenting already accepted historical
bytes can register a new receipt while reusing their verified analytical import.
Changed same-name bytes are a new version. Independently reconcile complete
persisted assertion inventories before adding import/proof pins. Manage those
pins automatically in the canonical policy and evidence list. Overlapping revised
reports without established precedence remain verified evidence pending review,
not a silent union or overwrite. Publish only when supplier pins differ from the
current publication; do not trigger training refresh from supplier intake.

Stop/deactivation and permanent deletion are distinct events. A report can
contain a historical stop date alongside a current deletion date. Preserve both
when supported, with the availability of the source that reports them. Unknown
historical event semantics remain explicit; do not infer separate dates from
formats that do not provide them.

## Coverage, revisions and source selection

Delivery manifests describe source family, release phase, covered fields,
periods/dates, population/filter and whether the delivery is a complete snapshot,
change feed, explicit patch or partial extract. A filtered brand export is not a
market snapshot. Multi-file LMS releases need a release key and the expected
members for their schema/phase.

Replacement semantics require evidence:

- A validated complete replacement supersedes the previous snapshot inside its
  declared scope. An omitted old row must not reappear through row-wise fallback.
  Omission does not itself create a discontinuation, availability or zero-price
  fact.
- An explicit patch affects only its declared keys and fields. Missing a field
  is different from explicitly withdrawing it.
- Missing release members or rejected records block the affected scope or leave
  explicit unknown/excluded keys. They must not accidentally enact deletions or
  erase valid evidence.
- Unestablished correction semantics remain unresolved; the latest download time
  is not a source-authority rule.

Keep source preference in a small versioned selection configuration, scoped by
field/price type, period, context, coverage and release phase. Preserve
corroborating evidence without multiplying market observations. Material
unresolved conflicts exclude the affected calculation and remain visible.

Accepted source observations are immutable. Corrections, re-parsing and mapping
changes create traceable revisions. Issues identify their blocking scope so one
bad cell does not automatically invalidate unrelated usable evidence.

Large-source research selections may explicitly retain a bounded catalogue of
canonical identifier/namespace pairs. Catalogue membership establishes neither
identity nor availability. Retain every candidate assertion across pinned
imports before applying the existing field, temporal, mapping and verification
gates, including candidate rows outside the requested business scope. Pin whole
originals and complete independent reports, freeze retained rows exactly, and
disclose total/retained/omitted counts. Omitted row values are not individually
frozen or verified during readback. This retention mode does not support revision
operations; complete-revision requirements remain unchanged for full retention.

## Availability and temporal views

Keep these clocks independent:

| Clock | Meaning |
| --- | --- |
| Effective date/period | When a fact applies |
| Published time | When that source edition became available, if established |
| Received time | When this operation obtained it |
| Imported time | When software interpreted it |
| Decision cutoff | Latest admissible input availability for a run |
| Completed time | When the analysis was actually produced |
| Evaluated time | When a subsequent outcome check was made |

Retain publication/receipt precision, evidence basis and uncertainty bounds.
Unknown is not midnight. For strict cutoff eligibility, the latest plausible
availability must be no later than the cutoff. A backfilled history workbook
does not prove historical availability of its present revision.

Support:

- `live`: an actually produced analysis using the operation's available inputs.
  Record completion time and expose lateness against its ordering deadline.
- `historical_replay`: a retrospective calculation using established pre-cutoff
  availability. Record whether the policy relies on proven receipt or supported
  publication; publication alone does not prove pharmacy possession or a past run.
- `reconstructed_research`: useful retrospective history where the relevant
  source revision's pre-cutoff availability is not established. Do not describe
  its results as demonstrated historical purchasing performance.

A best-reconstructed-history view may use later evidence. Decision-input views
must apply the run's availability policy. Identity, package attributes and group
membership are subject to the same restrictions as prices.

The official tariff period stays separate from expected publication and supplier
charging rules. Store operational cutoff/lead-time/receipt-pricing assumptions in
the run configuration, with their basis. Refine them when terms and transaction
data arrive.

## Frozen decision workflow

Introduce these tables when implementing the workflow after the import foundation:

| Table | Grain | Essential fields |
| --- | --- | --- |
| `analysis_run` | One frozen analysis execution | Run ID, current/target periods, cutoff, mode/availability policy, created/completed times, configuration, parser/selection/feature/method versions, input/feature manifest locations and hashes |
| `analysis_item` | One package assessed in a run | Run/package key, eligibility, rankability, structured exclusions, rank, method outputs and optional typed predictions |
| `review_event` | One saved human judgement | Event ID, run/package, timestamp, reviewer, status and note |
| `outcome_check` | One version of one later outcome assessment | Check ID, run/package, horizon, evaluation method/version, supporting observation IDs, price change, status, evaluated time and optional superseded check |

Retain every assessed package, not just a displayed shortlist. Keep deterministic
scores distinct from calibrated probabilities; a rule-only run has no invented
probability.

A frozen input/feature snapshot contains the exact selected observation and
mapping IDs, values, derived features, coverage and exclusions used. Its manifest
pins source interpretations, selection configuration, method/code versions and
hashes. Filenames or source hashes alone do not freeze source selection.

The Wednesday review reads this immutable result. Queue size, sorting, filtering
and chart preferences do not change its identity or erase reviews. Reviews append
events and never modify original scores or features.

Friday's confirmed upcoming AIP can support an outcome check, never a Wednesday
feature. Outcome checks are append-only, including pending, completed and corrected
results. Each has its own ID; an unchanged evaluation-method version does not mean
the underlying outcome revision is unchanged.

Missing/conflicting outcome prices yield an explicit unevaluable state, not a
flat price or a failed prediction. An AIP change is not realised profit, and a
review is not an order. Purchase, receipt, invoice, stock and sales records remain
later, separately dated inputs.

## Backfills, models and migration

Older files go through the same process regardless of arrival order. Add history,
fill supported gaps and publish a new dataset selection. Recompute affected
derived features for new runs; preserve prior runs and outcome-check revisions.

### Historical archive acceptance

Before bulk interpretation, catalogue every selected original by observed format,
schema variant, report family and supported period context. Separate missing
archive links, missing listed periods, failed acquisition, unsupported schemas,
unresolved periods, rejected records and verified observations. A downloaded
file or a year/week label cannot establish complete market or calendar coverage.

Maintain per-file record accounting: physical candidate records, accepted,
rejected, duplicate and non-record material, with locators and explicit reasons.
Detect repeated PDF headers, wrapped records, omitted pages/sections, RTF table
boundaries and Excel cell-type differences. Establish variants from the source;
do not silently treat an unrecognised layout as an empty report.

For every newly published derivation, independently compare all emitted
assertions, identifiers, exact amounts, dates, missingness and provenance with
the originals. The verifier must not call the production adapter or reuse its
normalised rows as expected results. Check coverage and omissions as well as
value equality. Inspect representative rendered/source records across every
schema variant and anomalies; spot checks supplement exhaustive reconciliation.
Persist discrepancy reports and the accepted audit fingerprint outside Git.

Cross-check overlapping publications and change lists where their price bases,
periods and populations match. Corroboration is additional evidence, not licence
to silently overwrite disagreements. Do not forward-fill missing prices, infer
deletions from absence, or infer no disruption outside covered supply history.
Matching hashes across manual and downloaded copies permit byte reuse, but do
not merge distinct delivery contexts or invent earlier availability.

Stage and validate interpretations before publishing their accepted scope.
If an existing import path stores typed rows before independent verification,
keep them ineligible for analytical selection until the audit gate passes.
Schema uncertainty must block only its affected scope; every unsupported file
remains accounted for and cannot be counted as an accepted import.

Backfill and maintenance share versioned adapters, idempotent receipt/import
identity, checkpoints and revision handling. Later releases or re-parsing create
new evidence and selections; they do not rewrite frozen runs. Extend the existing
foundation only where an observed source contract requires it, using a migration
when necessary rather than editing applied migrations.

The approved source strategy excludes retrospective bulk history exports from
this corpus's model inputs and limits Cito to validation. Preserve earlier accepted
interpretations and demonstrations for reproduction without requiring every copy
to remain in the live database. The owner accepts a working history from 2008.
A reviewed migration may retire redundant legacy live records after verifying
useful field coverage, remaining references and reproduction from retained archival
recovery material. Publish a new selection for the reduced working set; never
rewrite an accepted observation or frozen run to conceal the change. Supplier
disruptions and evidenced useful gaps remain eligible supplements. Build the
reduced database separately, compare all retained typed values and provenance,
verify its new canonical publication, then switch operating pointers through a
resumable journal. Archive closure must include both persisted deliveries and all
retained acquisition versions/receipts. A failed copy/publication must leave the
previous operating store usable. Keep immutable originals and earlier frozen
results recoverable outside the default working/model path.

### FTP-primary integration and analytical deduplication

FTP acquisition stores original bytes and release context before interpretation.
Inventory all available historical release families and member layouts at a
declared cutoff, including daily editions, preliminary/final phases and documented
corrections or supplements. Account for every discovered release and member as
accepted, duplicate, unavailable or unsupported with a reason. Distinguish raw
preservation, structural validation, interpreted fields and analytical eligibility;
retaining a complete ZIP does not establish complete semantic ingestion.

Version adapters for actual schemas, encodings and period-specific meanings.
Daily editions are not automatically tariff periods. Keep release, effective,
publication and receipt dates distinct; later specifications cannot silently date
or reinterpret older fields. Reuse the current store and typed contracts, adding
only evidenced extensions through versioned migrations when necessary.

Prefer verified FTP assertions within declared field/period/context coverage.
Allow legacy or supplementary facts only through explicit supported scopes.
Conflicts and unknown correction precedence remain visible; fallback cannot
resurrect a package omitted by a validated complete replacement. Preserve unique
archive history, preliminary evidence and supply observations where FTP does not
establish equivalent coverage. Absence of a disruption report is not a negative
observation.

Historical audit reports retain their original verification scope. Compatibility
with a recognized older producer must not invent a baseline for a table that
producer did not audit. Preserve every recorded assertion and metadata check.
Where an older proof omitted calendar rows, explicitly disclose that omission,
require all referenced periods to exist, and freeze their current context
separately for later change detection. This does not establish past calendar
availability or expand the original audit's claim.

A lean copy may inherit the accepted parent's original-byte native proof only
through a locked, single-use publication path. Before publication, independently
compare every retained table value with the accepted parent, check schema and
retention predicates, pin the closed destination bytes and unchanged native input,
code and analytical read contracts. The publication must still verify its newly
selected facts and fail if any binding changes. A stopped native verification or
an unverified copy alone cannot authorize operating cutover.

Expose one canonical analytical selection: at most one selected value per package,
effective date/period, field and full business context (including price basis and
classification phase). Evidence may corroborate that value without creating extra
training rows. Keep daily source editions and alternative derivations as provenance;
the declared model grain determines examples. Physical byte deduplication does not
merge distinct receipt contexts. Preserve all evidence required by frozen runs
outside the default model input path.

Native registry storage may share exact row bytes and ordered member versions
between editions. Each accepted edition still binds its complete member inventory,
source ordinals, original archive, acquisition receipt and interpretation profile.
Independent byte readers reconcile every new member's typed values. Reusing an
unchanged member proof requires its exact hash and profile; final dataset validation
also checks the persisted values and ordering. Physical deduplication is not an
identity merge or an assertion of historical availability.

Versioned native framing may preserve embedded line breaks inside documented
fixed-width text fields or a complete final record without its line ending.
Exact widths, external boundaries and control-file counts remain mandatory;
never pad, truncate or repair source values to obtain acceptance. A structurally
inconsistent release can supply independently verified partial reference members,
with expected and actual counts retained. Derived ZIP members keep their exact
parent/member locator and parent receipt provenance. They are not separate FTP
downloads, complete replacement snapshots or default canonical coverage.
A preliminary control catalogue may disagree with the actual members. Admit only
the explicitly supported variant as partial evidence, retain both inventories,
and keep absent declarations unknown rather than inventing expected counts.
Reserved price slots are not monetary observations. Equal record counts do not
establish member substitutions or complete replacement coverage. A new parser
version creates a separate derivation; prior proofs and their exact producer
remain available for reproduction.
An explicitly reviewed loose-file alternative may coexist with a valid ZIP.
Its exact component hashes, native control file and review evidence must be pinned;
retain differing counts and assertions separately. Sharing a directory or filename
does not establish equivalence or correction precedence.
A reviewed alternative whose bytes cannot be interpreted safely may be retained
without typed assertions. Pin its exact scope and concrete reason; preserve the
malformed or empty original without padding, repair or an inferred deletion.

Fields whose historical scale, encoding or business meaning is unsupported retain
their native tokens and explicit states. Structural compatibility alone does not
authorize the latest definition across an earlier era. Reference relations retain
their own natural grain instead of multiplying package examples. Canonical
publication pins supported profile/date/field/context scopes, member choices,
corroborating editions, unresolved conflicts and supplementary evidence. Missing
or conflicted periods break identity episodes. Native package identifiers alone
never establish continuity.

The default analytical grain is an exact tariff-start snapshot. A daily edition
with a later effective date cannot be relabelled as that start. Reimbursement
amounts may change within a tariff period: a start snapshot does not establish a
constant value or authorize carry-forward. Daily editions remain separate evidence.
Exact source descriptors can be selected without inventing a numeric scale or
expanding historical codes using a newer definition; those conversions need their
own evidenced scope.

Backfill and incremental collection use the same transactional, resumable path.
Validate retries, late releases, corrected same-name files and rejected partial
releases without changing accepted selections. The 28 October owner instruction
permits scheduled download-only acquisition under the existing signed-in Windows
account; later unattended-account operation remains a separate setup. Preserve
PowerShell as the entrypoint while removing the active WinSCP dependency entirely.
Historical WinSCP acquisition evidence remains readable and immutable. Provision
replacement credentials locally, never by inspecting or exporting the old login.
Imports must not trigger model fitting or promotion. Accepted bounded research
does not authorize scheduled training or promotion.

Candidate models consume versioned feature snapshots with future labels joined
separately for training/evaluation. Use chronological decision-period splits and
an appropriate separation for the prediction horizon. Training data, feature
definitions and model artifacts are pinned. Neither an import nor retraining
automatically promotes a model.

Research datasets must declare feature and label horizons, calendar/population
coverage, availability mode and policy, source/interpretation/mapping selections
and hashes. Separate reconstructed source history from proven historical replay.
Chronological evaluation must hide future outcomes from features, preprocessing,
tuning and model selection, including labels not yet mature at a training cutoff.
Unsupported outcomes stay unevaluable. These artifacts can use the existing
selection contract without requiring a new UI or a feature-store service.

The bounded E1 panel selection may freeze an explicit observation catalogue
outside the database. It must verify complete independent source proofs against
the persisted records, preserve each selected observation and mapping hash,
retain native originals and context evidence, and pin its selection policy and
producer. A research identity episode links only its listed observations. It is
not a permanent package merge or an amendment of earlier F3 selections. Gaps,
identity changes and ineligible intervals split episodes causally; new evidence
requires a new selection rather than changing the frozen panel.

### Research panels and evaluation

E6 changes only declared research components under a separate frozen protocol.
Older fitting cohorts preserve origin eligibility, disappeared packages, exact
labels and causal historical feature lookback. Sparse sidecars join from the
origin population with explicit missingness. Full-history capacity failures have
no predictive score. The actual Cito correction diagnostic changes only accepted
comparable incorrect codes; final codes do not establish early availability.
Current-only monetary ranking must exclude advance outputs throughout its
population. Robust amount forecasts remain distinct from arithmetic expectations,
and price exposure remains distinct from executable quantities or pharmacy profit.
The E6 panel retains 4,105,846 eligible origins from 14 January 2008 through
12 October 2026. Independent keyed checks verify all 2,953,646 older 2008–2021
rows against accepted source/sidecar definitions; exact compatible E5 reuse covers
2022 onward. Pre-2019 advance fields remain missing. Recency weighting changes
training weights only, with age measured from the fitting cutoff and mean-one
normalization; evaluation weighting remains unchanged. Later-known official
outcomes are not silently substituted into the pinned E5 comparison snapshot.

E5 adds dated role-specific company and lagged-behaviour recommendation policies.
Recommendation, training-target and peer-population exclusions are separate;
source observations and full supported market peers are preserved by default.
Historical names must resolve through their dated registry context. Policy
thresholds use development evidence only; missing history retains an explicit
state. Comparisons on common retained cases distinguish easier populations from
model improvement.

The [E5 guide](e5-research.md) specifies component-scoped freshness and tuning,
separate fit/early-stop/calibration/evaluation blocks, distinct median/mean/robust/
quantile interpretations and an unconditional two-period joint-persistence label.
Longer-horizon outputs never admit later features. Raw and nonnegative adjusted
forecasts remain coherent. Monetary rankings, integer-capital allocation and
shared-demand scenarios retain unknown selected outcomes and separate cash,
valuation and credits. E5 assessments have separate identities and do not modify
accepted E4 inputs, models, ranks or selections.

E3 admits only the immediately following tariff period's ABC as an additional
feature, separately from current ABC. Historical final ABC is a conditional
advance-report proxy, not publication-timing or Cito-accuracy evidence. Its
price-independent left join cannot change origin candidates. Preserve missing,
unsupported and ambiguous classifications; detailed next-identity rejection
states remain audit-only. Only the code and generic missingness enter prediction.
Future prices, groups, attributes, existence states and later ABC remain excluded.
The [E3 guide](e3-research.md) specifies common prediction-only error scenarios,
origin-time routing, defensible scalar size ratios, constrained monetary forecasts,
earlier residual calibration and frozen risk-aware queues. Final-source missingness
may convey later coverage unavailable to an early report; disclose that limitation.
Keep outcomes separately keyed and unknown selected candidates unreplaced.
Reconcile comparison keys, target dates and exact outcomes before publishing the
saved-only E2/E3 display. E2 scientific artifacts remain immutable.
Broader scalar-quantity support is a distinct, potentially smaller supported set
than equal-token peers; report their intersection and union, not just the broader
row count. A broader model retains equal-peer inputs and adds supported ratios.
Routing applies only to the declared direction comparisons. Do not imply a tested
monetary routing policy or a tested combination of a different classifier and queue.
Report empirical interval coverage within fixed history/price/peer strata as well
as overall; near-nominal aggregate coverage can conceal poor subgroup coverage.

E2 adds a separate dated LMS04 sidecar, never a rewrite of R1. Aggregate complete
native group-period populations before joining package partitions. Source group
codes are period-scoped; LMS01, LMS05, LMS07 and dose relations are separate.
Conservative AIP comparisons require identical supported native size/unit tokens,
complete member/identity/price joins and own-package exclusion. Preserve unknown
scopes and valid own-price observations. Exact-token comparability does not grant
numeric unit conversion or project current dispensing-price ABC rules backwards.
The [E2 guide](e2-research.md) specifies the separate scientific and prediction
cohort boundary. Freeze eligible candidates and rankings without labels, then
attach outcomes without replacement; preserve unknown, pending and exit-related
states in full and selected-population coverage.

E2's independent acceptance accounts for every claimed source member and sidecar
row, with separate native extraction and aggregate recomputation. Model comparison
proof binds exact candidate keys, observed-outcome keys and outcome values for
each task and split. Compatible target names or date labels alone do not make two
saved runs comparable. Dashboard reading remains pinned JSON only; executable
models, source selection and training are outside the viewing path.

`full-history-training-v1` is a separate model-neutral boundary from the bounded
E1 panel. Freeze typed Parquet source/feature partitions, separately keyed future
labels, actual calendar/cutoffs, chronological windows/exposure, coverage,
exclusions, source catalogue, recovery references and exact producer code. Read
the simplified canonical dataset from 2008 onward without implying that every
year, package or field is eligible. Complete package histories stay within one
partition; construction and full readback use bounded batches and package-sized
state. The independent consumer opens frozen artifacts, not the live database.

Use exact integer-øre AIP and deltas, causal within-episode lags/frequencies,
supported same-period native attributes and scoped ordinary-classification
evidence. Gaps, ineligibility and identity changes split episodes. Next-period
direction/magnitude and observed-rise reversal/persistence labels remain separate,
with actual horizon dates and explicit gap/ineligible/pending states. Reversal
requires both following observations; window loading requires labels mature
strictly before its cutoff. Missing supply identity joins and unestablished peer
populations stay unavailable rather than blocking eligible AIP rows. TSP is not
an AIP feature; unresolved ABC scopes suppress only their dependent evidence.

Keep an ordered numeric/categorical allowlist with units and missingness rules.
Identifiers, provenance, exclusions and future labels are auxiliary, never model
features. CatBoost input construction must verify actual names, categorical
indices, row alignment and label readback for declared windows without fitting.
Native categorical missingness has an injective adapter; numerical nulls remain
missing. Preserve exact labels outside CatBoost's explicit numeric conversion.
Separate full-history stream readability, measured Pool input size and eventual
fitting capacity. Learned encoding, scaling, imputation and quantization belong
inside later training folds.

Refresh creates a new immutable version for changed source/policy/code/runtime
pins, resumes only matching completed checkpoints and updates the current pointer
after full readback and declared input checks. It does not automatically fit or
promote a method. Verify the current pointer's pinned dataset-manifest hash before
loading or backing up its selected version. A restored frozen bundle uses exact
per-file reference relocations; later refresh uses a separate mutable runtime
config, training template and reviewed recovery metadata within the restored
workspace. Final cold recovery captures the current store, required
metadata, matching code/scripts and frozen training bundle. A separate unused
restore must verify bytes, unchanged schema, an offline idempotent native retry
and current supplier pins, and the complete relocated training reader before
acceptance. Dispose of the validation restore only after its receipt is retained;
the accepted cold bundle and precleanup recovery remain separate.
The accepted R1 bundle remains an input-only artifact. The owner now permits
bounded E3 fitting, validation selection and evaluation after E2 under a separately frozen
protocol. Automatic promotion remains unauthorized.
Earlier exposed evaluation periods cannot be relabelled as an untouched holdout.

Declare the quantity and population being estimated before model fitting. Select
one reconciled value per package, period and monetary context; corroborating
sources and alternative derivations do not multiply training rows. AIP procurement
targets require AIP evidence. Consumer ESP remains a separate analytical quantity.

Choose candidates using only the inputs admitted at that decision cutoff. Report
coverage and exclusions by period, including entrants, exits and unresolved identity.
Do not choose packages because they survive into the final period or appear in a
future change list. Sparse changed-price observations support only explicitly
conditional results; they do not establish whole-market change probabilities or
the unchanged-price denominator. Compare methods on the same eligible cases and
report any differences in their usable populations.

Next-period change prediction and reversal prediction have different triggers.
A rise into the next period is an unknown outcome for the former. The latter may
condition on a rise only once that rise is observable under its input policy.
Specify reference price, full/partial reversal, tolerance, horizon and denominator.
Descriptive analysis conditioned on a later rise is not a pre-rise decision rule.

Keep pending horizons, missing/conflicting prices, unresolved identity and lifecycle
censoring separate. Do not encode them as unchanged prices or prediction failures.
Define zero-denominator handling and exact price-change labels explicitly. Missing
features need an explicit policy; fit imputation, encodings, preprocessing, feature
selection and calibration only inside the earlier training/validation windows.

Freeze chronological development and untouched later evaluation periods before
tuning. Walk forward; remove training examples whose outcome horizon has not
matured at the training cutoff. Use the actual tariff calendar, not guessed week
arithmetic. Apart from the explicitly declared E3 advance-ABC proxy, reconstructed
availability does not admit future-period inputs.
Later evidence used to reconstruct historical identity or context must be pinned,
scoped and disclosed; it cannot silently supply future attributes or select survivors.

Include appropriate no-change, historical-frequency/magnitude and explicit rule
baselines. Evaluate a bounded learned candidate when viable. For probability
forecasts report a proper probability score and calibration; for magnitude report
error in declared units; assess direction and ranking where meaningful. Report
per-period coverage, label attrition, variation and uncertainty rather than relying
on aggregate accuracy. Package rows within a period are not independent temporal
replications. Limit claims to the available evaluation periods and population.

Pin dataset/selection IDs, feature and label definitions, code/dependency versions,
split dates, seeds, tuning choices, model artifacts, predictions and metrics. Keep
the final holdout out of method selection, calibration and threshold choice. If a
holdout result leads to a change, record it as used development evidence and obtain
a genuinely new evaluation window before claiming independent confirmation.
Failure to beat a baseline is a valid research result, not permission to alter the
test set or discard the losing comparison. Promotion remains a separate decision.

The research dashboard consumes explicitly selected accepted run manifests and
their pinned predictions, recorded outcome checks and metrics. It does not query
the live foundation, load executable model objects, fit models or resolve later
outcomes. Publish run artifacts coherently before exposing a completed result;
pending, failed, missing, incompatible or corrupt material must remain explicit.

Keep official frozen metrics separate from exploratory filtering. Comparable-run
views must disclose differences in target, horizon, availability, split dates and
eligible cases. Training fit, training-window learning curves and performance on
unseen periods are different quantities. Display final-holdout results only after
the producing workflow locks its evaluation. Record holdout exposure and later
development lineage outside the immutable result; a display cannot enforce that
future tuning respects the holdout. Do not choose thresholds or a promoted model
interactively from final-holdout outcomes.

Later sales, stock, expiry and transaction tables attach through stable package
and pharmacy/site identifiers. Add the pharmacy/site dimension when these records
exist rather than building a multi-tenant service now.

Use ordinary versioned schema migrations with a schema-version record. Prefer
portable types and explicit queries. PostgreSQL migration or a different frontend
must preserve the meanings, keys and evidence contracts; expect deliberate SQL
and operational adaptation.

Back up the database, originals, identity/selection configuration and run artifacts
together. Raw files can rebuild source interpretations with their versions; they
cannot recreate human reviews. Restore acceptance includes preserved identifiers,
amounts, provenance, reviews and representative frozen results.

## Acceptance before analytical use

E4 adds a research-file lifecycle outside the operating database. An intake pins
original bytes, source locator, actual acquisition, separately evidenced reported
receipt and current/target periods; conflicting codes and corrected reports remain
explicit. Actual-report retrospective, conditional historical proxy, genuine
prospective-pending and synthetic modes are distinct. Current ABC is never replaced
by advance ABC. Origin candidate populations include excluded/unavailable rows.

Assessment inputs, models, earlier-mature calibration, ranks and selections are
immutable. Later official outcome checks have separate content identities and
version lineage; a later check does not itself establish source-edition precedence.
Package/DrugID/creation/exit continuity is checked before attaching prices. Retry
identities include the producer version. E4's source maintenance appends original
evidence without rewriting the accepted R1/E1–E3 foundation or results.

The scenario ledger conserves stock, outstanding orders and acquisition basis
after credits, sales, returns and write-offs. Cash flow and terminal inventory
valuation remain separate; the conditional median forecast is not expected profit.
Public modern supplier terms support hypothetical assumptions only. Pharmacy
exports can later provide dated demand, lot, receipt-price and credit inputs through
these explicit contracts; no ERP connection or automatic ordering is implied.

| Scenario | Required result |
| --- | --- |
| Retry an identical delivery/import | No duplicate active market observations or lost reviews |
| Reparse with a new adapter version | Traceable alternative derivation; prior runs reproduce |
| Read a short numeric package identifier | Canonical leading zeros survive |
| Read an old stop date with a later deletion | Separate events; no invented early availability |
| Encounter malformed/truncated price text | Structured issue and calculation gap; no guessed amount |
| Add Friday prices after Wednesday cutoff | Outcome use allowed; decision-input use excluded |
| Reconstruct AIP across an unsupported change-feed interval | Unknown period, not an automatically carried price |
| Select candidates from future changes or final-period survivors | Invalid decision cohort; descriptive conditioning must be explicit |
| Train before an example's outcome horizon matures | Example excluded from that training cutoff |
| Evaluate with missing or censored outcomes | Explicit unavailable label and reported attrition, not a flat price |
| Add later history after freezing an evaluation | Earlier inputs, predictions and reported results remain reproducible |
| Lack historical supply coverage | Unknown coverage, never a synthetic zero |
| Replace a complete snapshot or patch one field | Exact scope respected; no resurrected or unrelated erased assertions |
| Receive an incomplete release | Affected scope blocked/unknown; no accidental replacement |
| Encounter ambiguous identity or stale report context | Quarantined/limited scope, not a guessed current package |
| Backfill older history or correct mappings | New runs may differ; frozen old runs do not |
| Receive Cito with uncertain timing | Preserved evidence, excluded where strict availability is unproven |
| Change queue size or save a review | Original analysis features, ranks and evidence remain unchanged |
| Correct a later outcome | New check ID; previous check remains recoverable |
| Restore or migrate | Identifiers, values, evidence links, reviews and selected run results agree |

Use independently expected synthetic cases for default tests. Authorised private
files remain read-only external acceptance evidence. Passing ingestion tests
establishes data/software behaviour, not commercial effectiveness.
