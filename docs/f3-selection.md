# Explicit frozen source selection (F3)

F3 reconciles explicitly chosen source assertions into an immutable local
selection. It follows the [data foundation](data-foundation.md); actual delivery
status and verification belong in [progress.md](progress.md). This guide describes
the software contract, not completion of a private initialization or demonstrated
historical purchasing performance.

Migration 3 adds `frozen_selection`; migrations 1 and 2 and accepted observations
remain unchanged. A selection stores canonical configuration and its hash,
resolved field values and states, supporting observation/mapping IDs, competing
assertions, exclusions, coverage, pinned input records, an evidence hash and actual
production time. It is an F3 evidence artifact. Frozen analyses, package ranking,
human reviews and outcome checks remain the later W1 workflow.

## Prepare and verify evidence

Keep originals, database/archive, configurations, reports and detailed exports in
approved private storage outside Git. Use one controlled writer and close it
before an independent audit or cold copy. The foundation
[ownership and recovery rules](development.md#ownership-failures-and-recovery)
apply to F3 too.

1. Register unchanged originals and ingest through supported versioned adapters.
   Record evidenced report context, coverage and independent receipt/publication
   bounds. Do not derive those facts from filenames or modification times.
2. Establish mappings and interpretation evidence at their actual supported
   package/date/context scopes. A matching identifier alone does not establish
   historical package identity. Preserve the original mapping when selecting an
   evidenced alternative.
3. Audit a closed consistent database/archive with the independent raw-source
   reader. The auditor uses its own source expectations and converters rather
   than production adapters. Unsupported independent schemas are quarantined.

   ```powershell
   uv run python -m bef_redbird.foundation.source_audit --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" --output "$foundationRoot\source-audit.json"
   ```

   The optional `--original-manifest` is an external JSON object whose
   `source_hashes` keys are original file paths. With it, the auditor also verifies
   that delivery hashes occur among those unchanged originals. Detailed audit
   output is private and must use a new output file outside every Git worktree.
   Repeated `--import-id` arguments bound a complete audit to explicitly chosen
   imports; all their assertions, issues, mappings and source records are still
   reconciled. This avoids copying the entire historical database into memory.
   The [archive procedure](f4-archive.md) publishes per-file compressed reports
   through the same independent reader and verification gates.
   Exit status is `0` for a complete audit, `1` for reconciliation mismatches and
   `2` for argument, input or output failure. Check the saved report's
   `complete: true`, empty `mismatches` and empty
   `quarantined_observation_ids` before registering it. Console diagnostics contain
   aggregate status only; detailed exception information is confined to the
   external report when that report can be written.
4. Archive/register the successful JSON or gzip-compressed JSON report using the
   normal registration path. Pin its delivery ID in `verification_reports`. A later report can verify
   earlier sources retrospectively: fidelity auditing does not itself establish
   decision-time availability or source authority.

The selection gate requires explicit `complete: true` and rejects any supplied
report with mismatches or quarantined observations. Each admitted observation
must match its verified persisted-record hash. Both its original mapping and
selected mapping must match verified mapping hashes. Missing/stale hashes exclude
the affected evidence. Merely listing observation IDs or supplying a source-file
hash is insufficient.

The report also supplies verified per-import record hashes, expected/actual
assertion and issue counts, physical source-row accounting and an independent
schema description. Revision operations require this complete import proof and
verified hashes for every persisted member assertion. These checks establish
source fidelity; complete market coverage and authority need separate evidence.

## Selection configuration

The following is a schematic synthetic configuration. Replace the example UUIDs
with existing, independently verified records; its dates carry no operational
meaning. Store the real file outside Git.

```json
{
  "version": 1,
  "policy_version": "f3-selection-v1",
  "import_ids": ["00000000-0000-0000-0000-000000000001"],
  "mapping_ids": ["00000000-0000-0000-0000-000000000002"],
  "derivations": {
    "00000000-0000-0000-0000-000000000003": "00000000-0000-0000-0000-000000000001"
  },
  "scope": {
    "tables": ["price_observation"],
    "package_ids": ["00000000-0000-0000-0000-000000000004"],
    "periods": ["2026-01-05"],
    "dates": [],
    "fields": ["amount_ore"],
    "context": {"price_type": ["aip"], "price_role": ["effective_price"]}
  },
  "cutoff": "2026-01-14T12:00:00+00:00",
  "mode": "historical_replay",
  "availability_policy": "receipt",
  "source_preference": {"version": "no-preference-v1", "rules": []},
  "verification_reports": ["00000000-0000-0000-0000-000000000005"],
  "context_evidence": {
    "00000000-0000-0000-0000-000000000001": []
  },
  "revisions": [],
  "business_context": {"purpose": "synthetic selection example"}
}
```

`import_ids` pins the candidate derivations. `derivations` maps every selected
delivery ID to exactly one selected import; other pinned interpretations remain
visible exclusions. There is no default latest-import or latest-file preference.
Identical bytes do not make distinct receipts/report contexts interchangeable.

`mapping_ids` pins the candidate identity interpretations. Applicable interval
mappings require an evidenced observation date/period; scope mappings require
the actual delivery scope or the optional explicit `identity_scope`. Unknown
validity does not become timeless. Competing package identities remain unresolved.
Selection never rewrites an observation's original package or mapping.

The bounded `f3-identity-evidence-v1` evidence format further restricts a mapping
to its document's explicit `allowed_delivery_ids` and the matching entry's
`supporting_observation_ids`. A multi-year workbook receipt does not extend an
identity established for selected assertions to its older price columns. New
observations or re-parses require an explicitly evidenced catalogue revision.
Every delivery in `supporting_delivery_ids` is archive-verified and must satisfy
the selected availability policy. A scope label cannot extend that evidence to a
new receipt, and an early declaration cannot hide later supporting identity evidence.

`scope` requires explicit tables, package IDs, periods and dates. Empty period/date
lists select none of that kind. Optional `fields` and `context` narrow selection.
Supported fields are price `amount_ore`; the seven package attribute fields;
classification `abc_code` and coherent `group`; supply `reported_status`; and
lifecycle `event`. Context preserves price type, currency, package/unit and
tax/fee bases, price role, ordinary/dose classification and current/upcoming
phase, group namespace/scope, wholesaler namespace/identifier and event type.
Source, phase and coverage-kind filters are also supported.

`context_evidence` declares supporting delivery IDs for every pinned import.
Include external evidence used to establish its interpretation, such as price
basis or report context. An empty list is an explicit declaration that no
additional context-evidence delivery is used; it does not establish an unknown
fact. Supporting deliveries are archive-verified and availability-gated.
`business_context` records the purpose and explicit operational assumptions.

For a cohort drawn from large complete sources, configuration `version: 2` keeps
the same `f3-selection-v1` policy and adds a required `candidate_catalogue`:

```json
[{"namespace": "dk:varenummer", "canonical_identifier": "000007"}]
```

Pairs must be explicit, unique and already canonical. Every configured mapping
must belong to the catalogue. All rows for those pairs across the pinned imports
are retained, including other dates, tables and contexts, before the normal
selection gates run. Candidate membership does not establish package identity.
Aliases and competing mappings still require explicit evidence.

Version 2 pins whole source originals and complete independent verification
reports but freezes only candidate records individually. It records per-import,
per-table total/retained/omitted counts and verifies count drift on readback.
Omitted row values are not individually reproduced or rehashed. Retained rows,
mapping evidence, issues and all pinned archive bytes keep their exact checks.
Version 2 requires `revisions: []`; use version 1 for revision operations. Existing
version-1 configurations, identities, artifacts and policy behavior are unchanged.

A source-preference rule has exactly `scope`, ordered unique `sources`,
`evidence_delivery_id` and nonempty `source_locator`. Its evidence must be
available under the chosen policy. A rule applies only when all competing
assertions match its scope and all their sources occur in its ordered list.
Overlapping applicable rules are rejected. Equal admissible assertions preserve
corroboration without multiplying a market value; unresolved material differences
exclude that field.

## Modes and field decisions

| Mode | Availability and claim |
| --- | --- |
| `live` | Requires `receipt`; admissible bounds are no later than cutoff and selection start. Actual production time is frozen and `late` discloses completion after cutoff. |
| `historical_replay` | Requires established pre-cutoff bounds under explicit `receipt` or `publication` policy. Publication does not establish pharmacy possession or an actual past run. |
| `reconstructed_research` | Can use unknown/later availability with explicit disclosures. It cannot demonstrate historical decision availability or purchasing performance. |

Strict checks use the conservative latest plausible bound, including equality at
cutoff. They cover source delivery, chosen mapping availability, its evidence
delivery, interpretation context and authority/revision evidence. Import time
does not substitute for source availability. Cutoffs require timezone-aware
instants and are normalized to UTC; operational scheduling remains in
`Europe/Copenhagen`.

Resolution is field-aware: a sparse attribute assertion does not erase unrelated
supported fields. Present zero, missing, unreported, blank, withdrawn, invalid
and conflicting states remain distinct. Blocking source issues affect their
field/identifier/period scope. Undated history attributes cannot borrow dates
from neighboring prices and do not block otherwise supported dated prices.
Reported previous prices remain report-context quotes, and current groups never
supply upcoming membership. Lifecycle source codes remain evidence while
established event meaning is compared semantically.

Unknown tax/fee bases retain their exact source amounts in separate delivery
contexts with `usable: false` and reason `unknown_price_basis`. Shared unknownness
does not establish cross-source monetary comparability. All requested
package/time/table slots have coverage records, including no usable evidence;
absence never means zero price, no disruption or discontinuation.

## Explicit revisions

`revisions` contains archived JSON operation delivery IDs. Source delivery
replacement declarations alone never activate replacement. Each operation has
exactly the following shape; UUIDs and scope below are schematic.

```json
{
  "version": 1,
  "kind": "patch",
  "replacement_import": "00000000-0000-0000-0000-000000000001",
  "supersedes": ["00000000-0000-0000-0000-000000000006"],
  "scope": {
    "tables": ["price_observation"],
    "package_ids": ["00000000-0000-0000-0000-000000000004"],
    "periods": ["2026-01-05"],
    "dates": [],
    "fields": ["amount_ore"],
    "context": {"price_type": ["aip"], "price_role": ["effective_price"]}
  },
  "expected_observation_ids": ["00000000-0000-0000-0000-000000000007"],
  "expected_members": ["main"],
  "members": {"main": "00000000-0000-0000-0000-000000000001"},
  "source_locator": "explicit scoped correction evidence"
}
```

All referenced imports must be pinned active derivations. `complete_snapshot`
suppresses predecessor support throughout the declared scope, including omitted
keys; old rows cannot reappear through fallback. `patch` and explicit `changes`
affect only declared keys/fields with a reported new state. Missing/unreported
patch fields do nothing; blank/withdrawn are explicit states. Assertions outside
the operation scope retain their independent source status.

Application requires matching declared coverage, available evidence, exact raw
member scope inventory and complete admission of that inventory. A snapshot's
selected fields cannot be missing/unreported. Rejected assertion candidates,
blocking member issues (including skipped physical rows), incomplete fidelity
proof or unresolved identities refuse the operation and preserve predecessor
support. All failure reasons are retained. Unknown/partial/filtered coverage
never authorizes complete replacement.

Expected members must match distinct imports and distinct deliveries. A multi-file
release also needs declared member names, a common nonempty edition and matching
source, phase and report context. Operations follow explicit predecessor dependencies;
cycles and multiple operations claiming the same replacement are rejected.
List order and download time never supply precedence.

## API, CLI and recovery

```python
from bef_redbird.foundation import Store

with Store(database_path, archive_path) as store:
    selection = store.create_selection(configuration)
    saved = store.inspect_selection(selection["selection_id"])
```

The API is available on `bef_redbird.foundation.Store`. The same canonical
configuration returns the existing artifact after verification. New evidence,
mapping choices or semantic settings require a new configuration/selection.

```powershell
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" select --config "$foundationRoot\selection-config.json" --output "$foundationRoot\selection-detail.json"
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" selection <selection-id> --output "$foundationRoot\selection-readback.json"
```

Standard output contains IDs, hashes and aggregate counts. Optional detailed JSON
exports use new external files. Inspection checks configuration/evidence hashes,
frozen scalar metadata, archive bytes, pinned delivery/import/mapping/observation
records and issues. It does not recompute the selection from later imports.
Missing/corrupt originals or changed immutable records block inspection.

Before opening an existing private store with new migration code, close writers
and make a verified cold backup. Opening `Store` runs pending migrations. Follow
the [migration and recovery procedure](development.md#typed-source-ingestion-f2)
to restore into unused private destinations first, verify the archive and compare
preserved IDs, exact values, provenance and representative frozen artifacts.
Keep external manifests/configuration evidence with the backup. `backup` and
`restore` copy closed stores without opening/migrating the source; never delete
a WAL or overwrite damaged stores to force recovery. Database publication is
transactional; power-loss durability across database and archive is unproven.

## Bounded history support and limits

`lms-history-workbook-v2` supports an explicit subset of the established XLSX
history schema: one to five unique canonical six-digit `subset_identifiers` and
one or two unique ISO `subset_periods`. Every requested period must occur in the
original date headers. Its registration coverage must be `partial_extract` with
an exact `filter` object containing those same two lists. It reads selected AIP
rows and price cells at original coordinates, retaining unreported/invalid states
and undated attributes. V1 remains the full-scope reader; supplying subset keys
to V1 does not activate V2 semantics.

This is bounded schema support, not full archive ingestion. The independent
auditor supports the implemented report schemas and bounded history V2; Cito,
full history V1 and optional supply classifications lack an independent audit
contract here. Cito with unestablished current/upcoming dates stays excluded.
Two matching history vintages can corroborate within evidenced identity and
context; they do not establish replacement or source authority. If real same-slot
prices agree, conflict handling remains synthetic acceptance rather than an
invented private-source disagreement. FTP acquisition, ordering, analytical
features, reviews, outcome checks and commercial performance remain separate work.
