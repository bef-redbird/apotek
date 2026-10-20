# Development and delivery

Read [direction.md](direction.md) before new development and
[data-foundation.md](data-foundation.md) for data/workflow changes. The approved
plan governs future work. [calculations.md](calculations.md) specifies the
implemented baseline; keep named behaviour reproducible and version deliberate
changes. [progress.md](progress.md) is the separate execution-status record.

Use Python 3.12 and uv from the repository root. The committed `pyproject.toml`
and `uv.lock` define the environment. Never commit private inputs or generated
workbooks; generate synthetic fixtures in test temporary directories.

Personal coding-agent instructions, skills and configuration stay local and are
ignored by Git. The content guard also rejects them if force-staged. Shared
direction and contracts belong in the documents above; delivery guidance lives here.

```text
uv sync --locked --group dev --group browser
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
uv run ruff check .
uv run ruff format --check .
uv run mypy src/bef_redbird
uv run pytest -m "not browser and not private"
uv run playwright install chromium
uv run pytest -m browser tests/browser
uv run python scripts/check_repo_hygiene.py --tracked
git diff --check
```

On Linux CI, install browser OS dependencies using `playwright install --with-deps
chromium`. Browser tests use generated synthetic workbooks. Private checks are
opt-in and need authorised external file paths; never provide them to public CI.
Do not describe skipped private-file, full-size or hosted-access checks as passed.

Pre-commit runs pinned Ruff, whitespace/YAML/conflict, detect-secrets (including
private-key detection) and large-file
checks plus the staged-content guard. Pre-push runs deterministic synthetic tests
and type checking. Hooks do not auto-stage fixes, merge, publish or delete branches.
CI runs the authoritative locked checks, Chromium workflow and Windows smoke.
Failed browser jobs retain only their synthetic PNG screenshots for three days;
uploaded workbooks, exports, HTML dumps and private data are never CI artifacts.
CI actions use full upstream commit SHAs with tag comments;
dependency updates still require compatibility testing and diff review.

The hygiene guard reads Git index blobs, including when the working-tree file
differs. `--staged` checks added/changed index content; `--tracked` checks the entire
index. It rejects generated/private paths, workbook/binary artifacts, large files,
private keys and tabular source-header signatures hidden in text files. Failures
print reason codes rather than filenames, secret values or source rows. Review
the local diff to identify rejected files. This heuristic cannot prove privacy:
inspect every staged diff and never use the guard as permission to upload data.
Authored configuration, Markdown and small synthetic fixture recipes are allowed.
Intentional new artifact types require a reviewed narrow policy change.

## Local foundation (F1)

`bef_redbird.foundation` is separate from the session application. Install the
locked environment above, then choose a local private directory outside every
Git worktree. These PowerShell examples use an operator-chosen root:

```powershell
$foundationRoot = "C:\Private\apotek-foundation"
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" init
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" register "$foundationRoot\incoming\example.txt" --source example --receipt-key receipt-001
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" verify
```

The same module works on Unix with appropriate absolute paths. `init` is
repeatable; opening `Store` also applies pending migrations. There is no default
data location. Storage, source files and optional metadata manifests inside Git
worktrees are rejected. The archive is separate from the database file.

`register` returns a delivery ID. Supply a stable `--receipt-key` for the actual
receipt context within `--source`. Reusing that pair with identical bytes and
metadata returns the same ID; changed metadata or bytes are rejected. A different
receipt context requires a different key even when its bytes are identical.
Do not generate a fresh receipt key on each retry. Original filenames are
recorded as labels, not interpreted as dates or release evidence.

Optional `--metadata` reads an external JSON object with `original_uri`,
`received`, `published`, `edition`, `phase`, `current_period`, `target_period`,
`coverage` and/or `replacement`. Supply only established information. Period
references must already exist in the official calendar through `add_period`.
Coverage/replacement manifests require a positive integer `version`; omitted
manifests become `{"version":1,"kind":"unknown"}`. F1 retains these declarations;
it does not validate release completeness or enact replacements.

Each known timing object contains `lower`, `upper` (ISO timestamps with offsets),
`precision` (`instant`, `day` or `range`), `basis` and `source_timezone`.
Instant bounds are equal; other bounds are increasing and conservatively cover
the possible event time. Supply date-only evidence as bounds with its source
timezone, never a fabricated midnight instant. `{"precision":"unknown"}` has
no bounds. Bounds are stored in UTC with precision, timezone and basis retained.
Publication, receipt, registration, import and effective dates remain distinct.
Omitted publication/receipt times stay unknown; file modification time is unused.

Reference-data setup uses the Python API. This synthetic example illustrates grouping
reference writes and completing an interpretation, without parsing a source:

```python
from datetime import date
from pathlib import Path
from bef_redbird.foundation import Store

root = Path("C:/Private/apotek-foundation")
with Store(root / "foundation.duckdb", root / "originals") as store:
    store.add_period(date(2026, 1, 5), date(2026, 1, 19))
    delivery = store.register_source(
        root / "incoming" / "example.txt",
        source="example",
        receipt_key="receipt-001",
    )
    with store.transaction():
        batch = store.begin_import(
            delivery,
            parser_version="example-v1",
            schema_version="example-fields-v1",
            mapping_version="identity-v1",
        )
        # Retain the package ID when retrying. Never merge packages by name.
        package = store.create_package("2cd42d91-ab20-4f4d-9e0f-7595c91458a0")
        store.add_identifier(
            package,
            namespace="dk:varenummer",
            identifier_value="7",
            evidence_import_id=batch,
            source_locator="record:1",
            mapping_version="identity-v1",
            validity_kind="interval",
            valid_from=date(2026, 1, 5),
            valid_to=date(2026, 1, 19),
        )
        store.finish_import(
            batch,
            status="accepted",
            record_count=1,
            accepted_count=1,
            rejected_count=0,
        )
```

`create_package()` can allocate a UUID, or reuse a supplied UUID for an already
identified package. `dk:varenummer` stores the exact source text plus a six-digit
canonical value (`"7"` becomes `"000007"`). Numeric Python values are rejected.
Other namespaces preserve their supplied text unchanged.

Mappings carry an evidence import/locator, version and separate availability
evidence. `validity_kind="interval"` requires both half-open date bounds;
`"scope"` requires an evidenced `scope_key`; `"unknown"` establishes no general
validity. `package_id=None` retains an unresolved mapping. Contradictory overlapping
scopes in the same mapping version are rejected; failed/rejected import evidence
does not block later mappings. Revisions retain old records. `resolve_identifier`
requires a mapping version and date and/or scope, uses only accepted import
evidence, and never treats unknown validity as timeless. It is a reference lookup,
not the decision-cutoff selection engine planned for F3.

Import identity includes delivery, parser/schema/mapping versions and a canonical
hash of interpretation context. Repeating them returns the same import, including
its terminal status. A different interpretation creates an alternative record;
F1 has no active observation selection. Counts must be nonnegative and partition
the total. Complete with `accepted`, `rejected` or `failed`; completion is immutable.
`add_issue` records a locator, code, severity and explicit blocking scope
(`none`, `field`, `identifier`, `period`, `import`) with the relevant target.
An import-level blocking issue prevents accepted completion. Narrower issues
remain available to future adapters and selection logic.

Use `get_record(table, key)` to inspect a reference record and
`original_path(delivery_id)` to verify and retrieve its original. Direct SQL writes
are unsupported: scalar checks and foreign keys are enforced in DuckDB, while
overlaps, immutable retries and cross-record rules belong to `Store`.

### Ownership, failures and recovery

Use one local filesystem supporting hard links and OS file locks. Each `Store`
holds exclusive database and archive locks for its lifetime, with one owning
thread. A second writer fails immediately. Persistent `.writer` files contain no
business data; their OS locks release on close or process termination. Never
delete a lock file to force access. The writer boundary follows DuckDB's
[single-process writing model](https://duckdb.org/docs/current/connect/concurrency).
Independent readers should use a closed database copy, not the live writer file.

Migrations are ordered SQL files plus a schema-version ledger. Normalized SQL
checksums reject modified, missing or unknown migration history. Pending schema
changes and ledger entries share a database transaction; failures roll them back.
Add a new migration instead of modifying an applied one. Internal IDs are stored,
not regenerated during migrations or restores.

Archive paths are `sha256/<first-two-hex-digits>/<sha256>` relative to the configured
root. Registration streams unchanged bytes into staging, flushes and syncs the
file, then publishes using a hard link that cannot overwrite an existing object.
Existing objects are hash/size verified. Only then is the delivery inserted.
No supported operation rewrites an archived object or private original.

Filesystem and database writes are **not one atomic transaction**:

- Failure before archive publication leaves no delivery. Handled failures clean
  their staging file; abrupt termination can leave a `.staging` file.
- Publication followed by SQL failure/rollback can leave an unreferenced object.
  Retrying the same source/receipt reuses verified bytes. `verify` reports orphan
  and staging counts; it never deletes them or treats them as evidence.
- An interrupted database transaction rolls back. A separately committed
  `running` import remains visible: retry the same identity and finish its work
  transactionally, or explicitly finish it as failed. Terminal imports stay fixed.
- Missing referenced files block access and import. Re-registering the exact
  original with the original receipt metadata can recreate a missing object.
  Corrupt existing objects are never overwritten, including on retry.

The tested interruption guarantee covers process termination and injected write
failures. It does not establish power-loss/OS-crash durability across the archive
and database: archive directory metadata is not explicitly synced. After an
unclean system shutdown, run `verify` and restore missing/corrupt originals from
verified backup before continuing. Even with clean shutdown, external file
changes are detectable damage, not supported edits.

For a simple backup, stop and close all writers, then copy the database, complete
archive and any external identity/configuration manifests together. Do not copy
only a live `.duckdb` file or delete its WAL. Restore to a new private directory,
open with the matching migration/code version and run `verify`. Relative archive
locations permit relocation. Synthetic acceptance verifies restored IDs and
provenance; private-data backup operations and later frozen-run/review restoration
are not claimed by F1. Preserve corrupt bytes separately for inspection and restore
from a known-good backup; there is no automatic destructive repair or garbage
collection.

## Typed source ingestion (F2)

Migration 2 adds `price_observation`, `package_observation`,
`classification_observation`, `supply_observation` and `lifecycle_event`.
Migration 1 is unchanged. Each assertion pins its import, delivery, package and
mapping IDs, original and canonical identifier, source locator, typed business
values, raw source representations, presence and quality. There is no combined
“latest” query. `Store.observations(import_id, table)` requires one accepted
derivation and verifies its archived original and mapping-evidence originals
before returning private rows. The same verification protects identical retries.

### Source-support matrix

Support means the specific schema below, not every file with that extension.
Headers, metadata anchors and strict container checks form part of each adapter
version. All examples/tests in Git are invented; sample inventories and acceptance
logs remain external.

| Adapter | Version | Supported schema | Assertions and required context | Boundaries |
| --- | --- | --- | --- | --- |
| `cito` | `cito-workbook-v1` | XLSX `Table`; BIFF XLS with one unambiguous six-field sheet; normalized baseline headers | Price, storage attributes, separate current/upcoming ABC; delivery current/target periods, price type/bases, classification context and group namespace | Other XLSX sheet names unsupported; current group never copied to upcoming; upstream cost/query meaning must be supplied explicitly |
| `lms-history` | `lms-history-workbook-v1` | XLSX `Medicinpriser data`, exact AIP rows, Excel-date/YYYYMMDD headings | AIP prices at column dates; optional current attributes; explicit tax/fee basis | No inferred calendar slots; unknown attribute period remains unknown; non-AIP rows supply no prices |
| `price-changes` | `price-changes-workbook-v1` | XLSX new/old AIP and registered-price columns, fixed header row and anchored tariff/run metadata | Package attributes; separate effective and reported previous prices; delivery current period and tax/fee basis | Declared tariff mismatch rejects import; run date is not publication; prior price effective period remains unknown; reported percentage retained raw, never used as AIP change |
| `introductions` | `introductions-workbook-v1` | XLSX existing/new formulation titles, header row 5/6 and two identifier punctuation variants | Attributes, AIP/registered prices, introduction; current period must match explicit tariff metadata; tax/fee basis | Introduction meaning follows report title/tariff date; no guessed publication or separate old lifecycle event |
| `discontinued` | `discontinued-workbook-v1` | XLSX separate stop and permanent-deletion date columns, supported newline/header escaping | Current/undated attributes and separate source-reported lifecycle dates; optional evidenced current period | Date-versus-announcement semantics remain `unknown`, quality `incomplete`; no historical date assigned to current attributes; blank dates do not create events |
| `supply` | `supply-workbook-v1` | BIFF XLS or XLSX exact package/wholesaler/effective-date report signature | Attributes and dated wholesaler assertions; namespace, `reported_status="reported_disruption"` and date basis | No incident, recovery or classification inference; namespace is source-scoped unless authoritative identity evidence exists |

Workbook locators are `sheet:<sheet>!<cell>` or `sheet:<sheet>!row:<number>`.
Metadata conflicts identify the anchored source cell. Raw date serials and
calendar system are retained when decoding BIFF reports. Filenames, sheet names
containing dates, folders and modification times never supply report context.

Medicine-price PDFs, historical RTF variants, older lifecycle layouts, filtered
search exports and structured LMS release members are **not supported** by these
versions. Available samples establish those families' existence, not validated
equivalent schemas. PDF extraction/layout and RTF family-specific record semantics
remain unverified; structured LMS samples/specification vintages require
member/encoding/release validation. No FTP acquisition or archive backfill is run.

### Commands and context

Use the F1 registration/timing metadata above. All input/context/output paths must
be outside Git. Add only evidenced official periods; the importer never invents
period ends. These illustrative dates/paths are synthetic:

```powershell
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" adapters
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" period 2026-01-05 2026-01-19
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" period 2026-01-19 2026-02-02
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" import "$foundationRoot\incoming\example.xlsx" --source example --receipt-key receipt-001 --metadata "$foundationRoot\delivery.json" --adapter cito --version cito-workbook-v1 --mapping-version source-local-v1 --mapping-policy delivery --context "$foundationRoot\context.json"
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" summary <import-id>
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" issues <import-id> --output "$foundationRoot\private-issues.json"
```

For this Cito example, `delivery.json` supplies `current_period` and `target_period`
plus only established coverage/timing. `context.json` contains:

```json
{
  "price_type": "aip",
  "currency": "DKK",
  "price_basis": "per_package",
  "tax_basis": "unknown",
  "fee_basis": "unknown",
  "classification_context": "ordinary",
  "group_namespace": "example:current-groups"
}
```

The example's price/classification interpretation needs independent source
evidence in real use. Price type accepts `aip`, `registered`, `consumer`;
basis accepts `per_package`, `per_unit`; classification context accepts `ordinary`,
`dose`. Do not declare a known tax/fee basis without evidence.
History, price-change and introduction schemas establish AIP/registered DKK
per-package amounts themselves; their context requires `tax_basis` and `fee_basis`.
Supply requires `wholesaler_namespace`, `reported_status` as above, and either
`date_basis="source_effective_date"` or an ISO `observation_date` with a nonempty
`observation_date_basis`. The former dates the assertion by its reported effective
date; it does not establish the report, publication or receipt date. Lifecycle
needs no adapter options (`{}`); current context comes only from delivery metadata.

`import` archives/registers then interprets. `ingest <delivery-id>` accepts the
same adapter/version/mapping/context arguments and reuses the archived original.
Repeat the identical command/receipt/context to retry. CLI output contains IDs,
counts and issue codes, never source rows or paths. `issues --output` exclusively
creates a new external file with detailed locators; it never overwrites files.
The Python equivalents are `Store.ingest(...)`, `Store.import_summary(import_id)`,
`Store.issues(import_id)` and `Store.observations(import_id, table)`.

### Identity, validation and recovery

The default `mapping_policy="existing"` uses accepted mappings at the explicitly
supplied `mapping_scope` or the assertion's established effective date, with an
explicit `mapping_version`. Unknown validity is never global. The exact eligible
mapping catalogue IDs are pinned in interpretation context. Added mapping
evidence produces an alternative import; prior assertions retain their IDs and
mapping references. Ambiguous or unresolved mappings reject affected assertions.

Explicit `mapping_policy="delivery"` initializes source-local package identities,
scoped to that delivery only, without matching names or joining receipts. Stable
package IDs derive from delivery and canonical source identifier. This is useful
for first local ingestion where cross-delivery mapping evidence is absent.
Reserved delivery scopes cannot be reused by another delivery. Later mapping
work creates alternative derivations; it cannot silently relink accepted rows.

Parser version, typed schema version, mapping version, all adapter options,
mapping policy/scope and the applicable mapping catalogue are included in import
identity. Delivery context remains separate. Unsupported parser versions fail;
new parser implementations need new versions. A different receipt key can retain
identical bytes as a distinct delivery. No automatic selection combines alternatives.

Parsing finishes before publication. Assertions, new source-local identities,
issues and accepted completion commit together. Expected fatal format/context
failures become rejected imports with no published assertions. Unexpected IO,
process or database failures roll back publication and leave a `running`
interpretation that an identical retry resumes. Terminal results are immutable.
Correct context through a new interpretation; correct delivery declarations with
a separately identified receipt/revision, never by editing earlier metadata.

Counts partition **assertion candidates**, not physical spreadsheet rows.
One row can produce several observations. Invalid amounts retain raw evidence,
a null typed amount, `invalid` presence and scoped issues; they are not usable
prices. Skipped invalid identifier rows are represented by issues, not fabricated
assertions. `coverage_assessment="incomplete"` identifies rejected/invalid/conflicting
scope; `declaration_only` never means independently verified market completeness.
Unknown coverage stays unknown. Supported coverage kinds are `unknown`,
`complete_snapshot`, `changes`, `patch`, `partial_extract`; optional `family` and
`record_count` declarations are checked. An explicit filter contradicting a
complete snapshot rejects publication. Replacement application belongs to F3.

Exact amounts have no rounding/noise tolerance in F2. Zero remains evidence.
Missing, unreported, blank, withdrawn, invalid and conflicting states are distinct;
blank is never automatically interpreted as withdrawal. Conflicting source rows
remain separate with affected fields marked. Issues use canonical identifiers
where valid, and source locators retain the exact original position. No conflict
is resolved by last-row order.

Before opening any existing non-synthetic store with new migrations, close writers
and create a cold backup using the matching code or the migration-free command:

```powershell
uv run python -m bef_redbird.foundation --database "$foundationRoot\foundation.duckdb" --archive "$foundationRoot\originals" backup "$foundationRoot\backup-before-upgrade"
uv run python -m bef_redbird.foundation --database "$foundationRoot\restore-test\foundation.duckdb" --archive "$foundationRoot\restore-test\originals" restore "$foundationRoot\backup-before-upgrade"
uv run python -m bef_redbird.foundation --database "$foundationRoot\restore-test\foundation.duckdb" --archive "$foundationRoot\restore-test\originals" init
uv run python -m bef_redbird.foundation --database "$foundationRoot\restore-test\foundation.duckdb" --archive "$foundationRoot\restore-test\originals" verify
```

`backup` never opens/migrates the source database, requires writer locks and a
closed/checkpointed store without a WAL, and verifies copied byte hashes.
`restore` verifies all backup members before copying to unused destinations.
Then `verify` checks database/archive references; copied-byte equality alone does
not establish original archive health. Preserve external configuration/evidence
files alongside the backup if needed. Test upgraded/restored IDs, representative
observations and original bytes before using the migrated store. Never delete a
WAL or overwrite a failed/corrupt store to force recovery. These guarantees cover
tested process interruption and cold copies, not power-loss durability.

## Reviewing application changes

For data-foundation work, use the acceptance scenarios in
[data-foundation.md](data-foundation.md). Test delivery/import identity, revision
scope, availability cutoffs, missingness, frozen evidence and recovery using
independently expected synthetic examples. Source-format expansion must not
silently change the meaning of an existing adapter or analysis.

For changes to the existing app's imports, calculations, ranking or exports,
read the affected contract in [calculations.md](calculations.md). Write
independently expected examples before
changing semantics: integer-ore boundaries, missing calendar slots, exact 5% rises
and observable reversal denominators. Verify affected supported workbook formats without
requiring optional fields. Test future-data exclusion and deterministic full ranking
separately from queue slicing. Compare Excel readback with the same `AnalysisResult`;
test formula injection and matching-only JSON restore. Version changed method
definitions and update their tests and explanations together. Authorised originals
remain read-only external fixtures; report unavailable private evidence explicitly.

For UI changes to the existing app, retain the selection and review-state
guarantees described in [the review workflow](../README.md#review-a-report).
Use native accessible controls
and restrained styling. Exercise N above and below 50, all-rankable, sorting, search,
selection and resizing with saved reviews. Inspect empty, failed-import, short-history
and missing-data states. Check chart gaps, period labels and unfiltered found group
alternatives. Run Streamlit AppTest and real-browser upload/download checks; inspect
synthetic screenshots at laptop and desktop widths outside Git.

These checks describe the current implementation. A future platform needs
equivalent evidence for its workflow and data contracts, not Streamlit-specific
tests. Existing visual layouts and screening thresholds remain revisable under
the approved direction.

## Pull requests and acceptance

Update [progress.md](progress.md) when a development slice changes state. Include
the implemented component, actual verification, unresolved dependencies and next
slice. Keep private audit evidence and verbose logs outside Git. Update canonical
direction/contracts when approved decisions change; do not create competing
handover specifications.

Before a PR, inspect origin, status, staged and unstaged diffs, branch and worktrees.
Run the relevant checks and record factual results in the PR template. Explain
material method changes, data impact and any migration/rollback. Do not self-approve
through another identity, bypass required checks or force an administrative merge.
The owner authorizes normal squash merge once all code reviews for the current
PR head are complete, findings are fixed and resolved, and both required CI
checks pass. Completed code reviews count toward this workflow; a separate formal
GitHub approving review is not required. The configured approval count is zero,
with strict Linux/Windows checks, enforced branch protection and force-push
restrictions retained. Re-review changed code after fixes; never infer acceptance
from an unfinished review or successful checks on an older head. Host access and
permission to process private inputs remain separate deployment gates.

## Safe branch cleanup

Cleanup starts only after confirmed merge, not after auto-merge is scheduled.
These commands inspect state; run from this verified repository:

```text
git remote -v
git status --short
git worktree list --porcelain
git branch --show-current
gh repo view --json nameWithOwner,defaultBranchRef
git fetch --prune origin
gh pr view <number> --json state,mergedAt,baseRefName,headRefName,headRefOid
git rev-parse <branch>
git ls-remote --heads origin refs/heads/<branch>
gh pr list --state open --json number,baseRefName,headRefName
```

Fetch is an explicit network action; stop safely on authentication/offline errors.
Verify the matching PR belongs to this repository and merged into the intended
default base. Retain default/protected/long-lived branches, the current branch,
every worktree branch, branches with open/stacked dependants and branches whose
tip differs from the PR's merged head. Remote absence does not prove local work
is disposable. Inspect dirty work and never reset, clean or remove worktrees as
a cleanup shortcut.

After verifying the remote tip equals the expected merged head, remove only that
remote ref with an expected-value lease (replace placeholders explicitly):

```text
git push --force-with-lease=refs/heads/<branch>:<expected-head-sha> origin :refs/heads/<branch>
git branch -d <branch>
git fetch --prune origin
```

Switch to the default branch before local deletion, after protecting any dirty
work. Squash merging can make `branch -d` refuse even genuinely merged work.
Use a narrowly scoped `git branch -D <branch>` only after independently verifying
the merged PR, unchanged exact head, no worktree use and no dependent or unique
unpreserved work. Never use blanket deletion pipelines. An obsolete unmerged
branch additionally needs an explicit abandoned/superseded decision; age is not
evidence. Recheck local/remote refs and report removed/retained branches and
reasons to the operator, without creating repository cleanup logs.
