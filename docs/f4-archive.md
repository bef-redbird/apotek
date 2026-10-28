# Historical and exceptional archive acquisition

This preserves the accepted F4 acquisition contract for explicit historical or
exceptional work. Routine operation uses FTP plus the direct supplier-file
command in [development.md](development.md); it does not require a downloader
manifest or corpus plan. Earlier frozen selections remain recoverable in the
retained pre-cleanup archive, even when their dependencies leave the live store.

This procedure extends the F1–F3 store and its immutable source evidence. Actual
initialization and acceptance status belong in [progress.md](progress.md).
Detailed inventories, operating paths, source contracts and proofs are private
external artifacts. This document is an acquisition procedure, not a claim that
every historical market period is available.

## Source contracts

Use the downloader manifest to choose originals. The corpus command verifies
each listed version's bytes, SHA-256, native container and acquisition receipts.
It does not discover inputs by recursively scanning the data directory. Distinct
receipts remain separate deliveries even when their immutable objects match.
Receipt count, unique archived-body count and interpretation count are separate
measures. Full base ingestion can coexist with separately retained alternative
derivations. Only the explicitly chosen receipt and derivation supply selected
observations for each file.

The external ingestion plan pins the acquisition manifest hash and accounts for
every version exactly once with an ingest or exclude disposition and evidence.
It supplies the inspected adapter version, interpretation context, primary
receipt, source coverage and evidenced calendar. Archive labels and filenames
cannot establish these facts. A newly identified report family is recorded in
the plan without rewriting the acquisition history.

Additional supported contracts are:

| Version | Source meaning and boundaries |
| --- | --- |
| `medicine-prices-pdf-v1` | Native printed catalogue with package attributes, consumer ESP per unit/package, separate TSP reimbursement basis, and ordinary ABC. Original page/span coordinates and wrapped tokens are retained. There is no printed AIP or explicit substitution-group identity. An unestablished tariff period stays undated. |
| `price-changes-rtf-v1` | Inspected native paragraph/tab-stop schemas, including contextual headings and wrapped records. New and quoted prior amounts remain distinct; a prior quotation does not establish its effective period. |
| `introductions-rtf-v1` | Both printed introduction families, with their actual company/formulation context and field-presence differences. Missing columns cannot become blank reported cells. |
| `discontinued-rtf-v1` | Historical printed lifecycle lists. Unestablished event meaning or dates remain explicit unknowns. |
| `introductions-workbook-v2` | Observed tariff/run metadata positions and heading variants beyond the preserved V1 contract. |
| `discontinued-workbook-v2` | Historical alternate headers and unlabelled metadata dates; an unlabelled date is not promoted to tariff context. |
| `price-changes-workbook-v2` | Preserves the V1 source contract and adds an explicit, evidenced basis override for dated effective AIP only. |
| `introductions-workbook-v3` | Preserves the archive-layout V2 source contract and adds the same bounded effective-AIP basis override. |

The earlier workbook adapters remain available. Excel readers preserve native
cell types, number formats and formula/cache distinctions without running macros
or links. RTF uses structured text and its encoding/group state. PDF ingestion
requires native text/geometry; unsupported structures produce scoped issues.

Price tax and fee bases require source evidence at the applicable vintage.
Consumer ESP evidence does not establish TSP tax treatment. The optional PDF
`reimbursement_tax_basis` context separates them; omitting it retains the
declared common tax basis. Printed money is never converted through a pricing
formula. Unknown bases preserve exact amounts but remain unusable under the F3
monetary-comparability gate.

### Bounded effective AIP context

The two new workbook versions require `price_basis_override` in the adapter
context. Earlier versions retain their existing behavior and reject this reserved
key. The ordinary context must keep both `tax_basis` and `fee_basis` as `unknown`;
the override cannot establish a common basis for every price in a report.

The override has exactly these fields:

| Fields | Required values or meaning |
| --- | --- |
| `version` | Integer `1`. |
| `price_type`, `price_role` | `aip`, `effective_price`. |
| `currency`, `price_basis` | `DKK`, `per_package`. |
| `tax_basis`, `fee_basis` | Both `excluded`. |
| `effective_period_start_from`, `effective_period_start_through` | Ordered, canonical ISO dates; inclusive bounds on the source-evidenced effective period's start. |
| `evidence_delivery_id`, `evidence_sha256`, `evidence_locator` | Canonical UUID of an archived definition delivery, its lowercase SHA-256, and a nonempty locator for the supporting source passage. |

The caller must separately review the native AIP heading and an applicable
archived definition establishing that the printed AIP operand excludes VAT and
ordinary consumer dispensing fees. Record the definition, reviewed date bounds
and reasoning in the external plan and evidence. Schema validation alone does
not prove that meaning or its applicability. No particular definition, archive
identity or date ceiling is embedded in the adapter.

Only effective AIP assertions with an established period inside those bounds
receive the two known basis labels. Registered prices, undated prior quotations
and AIP outside the bounds retain their unknown bases. Amounts, field states,
source dates, raw provenance, locators, record counts and issues are preserved;
blank or invalid amounts do not become usable prices. No pricing formula is
evaluated.

Independent reconciliation includes the referenced definition delivery in its
scoped snapshot and verifies its archived bytes against the declared hash.
Selection must also pin that exact delivery in each affected import's
`context_evidence`; the declared hash must match. Existing archive-integrity,
identity and strict/research availability gates still apply. A definition's
effective date does not establish its historical publication or availability.

## Ingest and independently reconcile

Resolve the explicit operating database/archive and accepted plan from the
private operating manifests. Close writers before a backup, independent audit
or restore. Preserve external plans, configurations, code and proofs with the
verified cold backup. Restore to unused locations before a schema upgrade.

These PowerShell variables are operator-selected external locations:

```powershell
$manifest = "$privateRoot\acquisition\manifest.json"
$sourceRoot = "$privateRoot\incoming"
$plan = "$privateRoot\operations\ingestion-plan.json"
$journal = "$privateRoot\operations\ingestion-journal.jsonl"
$database = "$privateRoot\store\foundation.duckdb"
$archive = "$privateRoot\store\originals"
$proofs = "$privateRoot\evidence\source-audit-v1"

uv run --locked python -m bef_redbird.foundation.corpus --manifest "$manifest" --source-root "$sourceRoot" inventory --output "$privateRoot\operations\inventory.json"
uv run --locked python -m bef_redbird.foundation.corpus --manifest "$manifest" --source-root "$sourceRoot" ingest --database "$database" --archive "$archive" --plan "$plan" --journal "$journal" --limit 50
uv run --locked python -m bef_redbird.foundation.corpus --manifest "$manifest" --source-root "$sourceRoot" reconcile --database "$database" --archive "$archive" --plan "$plan" --journal "$journal" --output-root "$proofs"
```

The limit counts new checkpoints, so repeat the ingest command to continue.
Every import uses the existing register/parse/validate path and atomic
publication. Unchanged retries preserve IDs and terminal imports. A deliberate
new parser version or interpretation creates a separate derivation. A newly
recorded receipt for identical bytes adds evidence without multiplying the
chosen observations.

A newly evidenced AIP basis is an alternative derivation. Preserve the accepted
base plan, imports, observations, journal and proofs. Publish the new reader and
context through a separate supplemental plan and journal, retaining existing
receipt identity and source metadata. Independently reconcile the new imports
before selecting them. A changed auditor fingerprint requires fresh proofs in a
new namespace for imports that will be used with that reader.

Retain both histories in the final evidence manifest. Selection explicitly
chooses one derivation per delivery. Establish identity supports for the new
observation IDs and include their definition dependencies; old observation
supports cannot silently transfer.
Do not choose the latest import automatically or combine fields from alternative
derivations as an implicit fallback. Earlier selections remain frozen.

Corpus ingestion uses a 256 MB automatic checkpoint threshold to amortize
database checkpoints across files. Every import still commits separately
through DuckDB's normal durability/recovery path, and normal store closure
checkpoints the database.
Recovery tests cover a committed WAL larger than the default threshold followed
by process exit before the external journal append. This does not establish
power-loss durability. See DuckDB's
[checkpoint configuration](https://duckdb.org/docs/stable/configuration/overview).

The append-only, flushed journal is a checkpoint, not analytical authority.
An interruption after database commit can be retried through the same IDs.
A torn journal is rejected explicitly: preserve it and restart with a new
journal through the same idempotent ingestion path. Never delete a WAL, replace
an original or overwrite a database to make a retry succeed.

Reconciliation reads every persisted assertion and independently derives the
expected contents from the originals. Its readers do not call production
adapters. Per-file evidence includes rejected records, missing/invalid fields,
physical/non-record accounting, mappings, issues, locators and persisted-record
hashes. Reports are compressed JSON, published atomically to new paths.
Reusing a report requires unchanged scoped database records, archive bytes and
auditor code. Changed evidence requires a new proof directory.

A successful import alone cannot make an observation analytically eligible.
Inspect failed proofs against originals, fix parser defects, and retain genuine
source defects with their affected scope. Continue unaffected families.
Distinguish absent archive links, absent periods, empty reports, failed
acquisitions, unsupported schemas and rejected records. Missing supplier history
never establishes absence of disruption.

## Publish a new research selection

Use [F3 selection](f3-selection.md): register successful independent reports,
pin exact derivations and supporting context, establish identity only over
evidenced scopes, and save a new explicit configuration. The selector accepts
both ordinary and gzip-compressed audit reports through the same verification
gates. Inspect and reproduce the resulting frozen artifact.

Keep reconstructed research separate from strict historical replay. A genuine
acquisition receipt obtained today does not prove historical availability.
Unknown publication timing can be disclosed in reconstructed research without
weakening replay. Unknown effective periods, unresolved identity and unknown
price bases keep their existing exclusions.

A bounded baseline-package cohort establishes identity only for the evidenced
packages and periods. Consumer package ESP observations in a reconstructed
selection are not AIP observations or proof of historical operational access.
Preserve strict selections even when availability exclusions leave them empty;
never relabel reconstructed evidence as strict replay to improve coverage.
Retain earlier frozen selections alongside new cohort results.

Use accepted research windows for chronological baselines and candidate-model
comparisons with future inputs hidden and later outcomes kept separate. Evaluate
persistence/reversal and supported competition, classification, lifecycle and
supply signals within their evidenced scopes. Broader identity coverage and model
quality require their own evidence. Inventory-policy comparisons additionally
require explicit quantity, demand, sell-through, expiry, cost and supplier-term
assumptions; ingestion and price predictions do not establish realised benefit.

Do not select retrospective bulk-history exports as new archive model inputs.
Preserve old F3 imports and frozen examples. Cito remains validation-only, and
neither Cito nor FTP access is a prerequisite for this routine.

For each new release or correction: preserve its bytes and actual receipt,
refresh the manifest and scoped source audit, version any changed contract,
ingest and reconcile, then publish a new selection. Do not infer replacement
from a newer download, deletion from omission, group identity from a name, or
prices by forward filling. Old selections remain frozen.

Before refreshing acquisition metadata, retain a byte-exact, immutable copy of
the accepted manifest alongside its inventory, plan, journal and proofs. Record
that snapshot's path and hash in the operating manifest. Reproducing the accepted
run uses that receipt snapshot; it must not depend on the downloader's mutable
current manifest. A later release receives a new reviewed plan and journal, and
new or refreshed proofs in an explicitly named namespace. Preserve earlier
plans and receipt snapshots rather than editing them to describe the new run.

## Schema and recovery

Migration 4 adds the distinct reimbursement-basis price type. Migration 5
rebuilds its foreign-key references under the final table name. Applied
migrations are not edited. After pending migrations commit, initialization
performs a normal checkpoint before exposing the writer.

This addresses observed DuckDB transactional table-rebuild and renamed-child
foreign-key failures ([WAL replay](https://github.com/duckdb/duckdb/issues/22124),
[foreign-key references](https://github.com/duckdb/duckdb/issues/24648)).
Process interruption during ordinary ingestion is covered by retry acceptance.
An interruption during schema reconstruction before its checkpoint requires
inspection and the verified pre-migration backup; automatic power-loss recovery
is not claimed.

After backfill, close the writer, verify every archived original, reproduce
frozen selections and compare all protected baseline records. Make a cold
backup including external evidence/configurations, restore to unused locations,
and repeat these checks. Preserve a damaged or incomplete store for diagnosis;
never force recovery by removing its WAL.

The recovery set is explicit: database, immutable archived bodies, receipt aliases,
required plans/configurations, independently verified proofs, frozen results and
matching runtime/review evidence. A whole private data directory is not the
recovery contract. Verify the selected external-file manifest and original aliases
as well as the cold database/archive copy; preserve earlier evidence and backups.

A clean restore also needs the acquisition paths expected by the unchanged
manifest. From the verified cold alias index, copy each acquisition alias into a
new unused restore-local source tree. Use independent copies, preserve receipt
identity, verify every size/hash/container signature through the existing inventory
loader, and compare inventories after removing only each file's physical path.
Do not manufacture new receipts or alter archived originals.

Before restored maintenance, independently copy and hash-verify the accepted base
and supplemental journals into new restore-local writable paths. Bind commands to
the restored database/archive, recovered manifest/plans and these copied journals;
use fresh proof namespaces. Corpus checkpoint appends and adjacent failure logs
must never write inside immutable cold evidence. Keep the cold plans read-only.

Use an already prepared, verified external Python/dependency environment matching
the recovered lock, with the interpreter outside the cold bundle. Set inherited
`PYTHONDONTWRITEBYTECODE=1` and `PYTHONPATH` to the recovered source directory, pass
`-B`, and verify that the imported corpus module belongs to that recovered source.
Do not run `uv` inside the recovered repository or create a cold `.venv`. The
reviewed private maintenance reference provides the exact command recipe; this
procedure adds no environment installation workflow. Static recipes and reviews
belong in the pre-cold recovery plan when selected. Actual alias/journal copy plans,
logs, inventory comparisons and receipts belong in the post-recovery addendum.

After the unused restore passes, finalize documentation and run the actual final
delivery checks against unchanged covered files before operating publication.
Retain late delivery receipts/logs, final documentation, root review and the
published operating pointer in a separate immutable hashed completion addendum.
Bind the earlier backup/external manifest and actual restore acceptance there;
never edit the verified cold manifest to imply those later artifacts were restored.
