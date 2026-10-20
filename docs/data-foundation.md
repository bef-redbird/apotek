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

AIP, registered price and consumer/dispensing price are explicit different price
types. Reported percentage changes must retain their price basis; do not treat
them as AIP changes. Derive analytical AIP changes from the selected AIP amounts.

Distinguish `effective_price` from `reported_previous_price`. An old price quoted
on a change list is not automatically a price for the preceding tariff period.
When its effective period is unknown, retain its report context and exclude it
from a fabricated historical price slot.

Missing calendar slots stay missing. A latest-known value does not establish an
unobserved price or extend validity beyond the supported interval.

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

FTP acquisition stores original bytes and release context before interpretation.
Version adapters for actual file schemas/encodings and compare overlapping
periods before preferring a new source. A newer structured feed does not erase
earlier preliminary evidence or guarantee equivalent supply-history coverage.

Candidate models consume versioned feature snapshots with future labels joined
separately for training/evaluation. Use chronological decision-period splits and
an appropriate separation for the prediction horizon. Training data, feature
definitions and model artifacts are pinned. Neither an import nor retraining
automatically promotes a model.

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

| Scenario | Required result |
| --- | --- |
| Retry an identical delivery/import | No duplicate active market observations or lost reviews |
| Reparse with a new adapter version | Traceable alternative derivation; prior runs reproduce |
| Read a short numeric package identifier | Canonical leading zeros survive |
| Read an old stop date with a later deletion | Separate events; no invented early availability |
| Encounter malformed/truncated price text | Structured issue and calculation gap; no guessed amount |
| Add Friday prices after Wednesday cutoff | Outcome use allowed; decision-input use excluded |
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
