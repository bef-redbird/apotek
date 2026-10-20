# Development progress

This file tracks execution of [the approved direction](direction.md) and
[data contract](data-foundation.md). It is the single progress record, not another
roadmap or specification. Approval is not implementation; implementation is not
validated commercial performance.

## Current position

- The existing application implements temporary-session workbook review and
  deterministic screening. Its contract remains in [calculations.md](calculations.md).
- F1 is implemented: local DuckDB storage, versioned migrations, immutable
  original-byte archiving and the six reference/provenance tables. Usage and
  recovery are documented in [development.md](development.md#local-foundation-f1).
- F2 is implemented and initiated: five typed observation tables, six versioned
  workbook adapters, transactional ingestion, explicit derivation reads, scoped
  issues and verified cold backup/restore. See the
  [support matrix and commands](development.md#typed-source-ingestion-f2).
- The foundation is independent of the current application. Its existing
  calculation definitions, uploads, session reviews and UI behaviour are unchanged.
- The final product platform is undecided; the current Streamlit app is a baseline.

## Milestones

| ID | Deliverable | Status | Evidence or completion requirement |
| --- | --- | --- | --- |
| D0 | Canonical direction, data contract and separate progress record | Complete | Consistency review, local links/anchors, content hygiene and documentation-only scope verified |
| F1 | Period/identity/provenance schema, immutable archive and versioned migrations | Complete | Six constrained tables, single-owner storage API/CLI, 43 synthetic foundation cases; verification below |
| F2 | Versioned source adapters and five typed observation tables | Complete | Bounded schema support, all five ingestion paths, synthetic and representative local imports, reviewed recovery and delivery gates below |
| F3 | Reconciliation, cutoff selection and recovery | Not started | Idempotence, revision scope, availability, frozen-evidence and restore acceptance |
| W1 | Frozen analysis, full package assessment, reviews and outcome checks | Not started | Four workflow tables; Wednesday inputs separated from later outcomes |
| E1 | Historical baselines and candidate-model evaluation | Not started | Chronological comparisons using supported input availability and honest coverage |
| O1 | Pharmacy transactions, inventory and supplier terms | Awaiting inputs | Verified dated joins and quantity/financial definitions |
| H1 | Shared operation and platform/hosting decision | Deferred until needed | Operational requirements, migration, access and durability acceptance |

Milestone status values: `Not started`, `In progress`, `Blocked`, `Complete`,
`Awaiting inputs`, `Deferred until needed`. Only completed, verified work is
`Complete`. Record a concrete dependency for `Blocked`; unrelated work can continue.

## Next implementation slice

The next bounded step, when requested, is F3's explicit versioned selection of
derivations and reconciliation within evidenced identity/coverage scopes. Start
with independently expected alternatives, conflicts and availability boundaries;
do not introduce a default latest-row query. F3 has not started.

Additional PDF, RTF or structured LMS adapters require their own schema evidence
and acceptance. They are optional source expansion beyond the completed bounded
F2 support, not a claim of universal historical coverage. FTP acquisition,
frozen analyses, model evaluation, frontend replacement and hosting remain later
work. Ingestion completion does not establish analytical or commercial readiness.

## Open dependencies

| Dependency | Affected capability | Work that can continue |
| --- | --- | --- |
| FTP access, release schemas and archive coverage | Live acquisition and verified structured-source preference | Local schema, archive, existing file adapters |
| Cito upstream definitions and availability timing | Reproduction and strict historical use of upcoming classifications | Source preservation, reconciliation and research with explicit limitations |
| Exact ordering cutoff and supplier charging evidence | Execution timing and realised-benefit calculations | Configurable assumptions and market-outcome evaluation |
| Irregular/truncated source values and uneven historical coverage | The affected observations and calculations | Valid observations with explicit issue scope |
| Sales, stock, expiry and commercial terms | Quantities, inventory risk and realised financial outcomes | Market history and prediction-quality evaluation |

Keep source-specific dates, file identifiers, audit counts and private evidence
locations in the local handover or approved private storage, not this public log.

## Updating this file

For each completed development slice:

1. Update the relevant milestone and the current position.
2. Add a short verification entry with the changed contract/component and actual
   checks. Link a commit/PR when one exists; never invent a reference.
3. State limitations and the next concrete slice.
4. Update direction or the data contract only if the accepted design changed.

Do not copy command output, conversations, private rows, personal data or a diary
of attempts here. Keep the current state readable; Git history preserves older
states. Never use this status file to override the approved contract.

## Latest verification

2026-10-20 — F2 typed source ingestion:

- Added checksummed migration 2 without rewriting F1. All five typed tables
  retain stable IDs, explicit import/delivery/mapping provenance, original and
  canonical identifiers, source locators, raw representations and field quality.
  Exact amounts use integer ore; unsupported precision is rejected without
  rounding. Historical prices cannot date current attributes; previous quoted
  prices do not create preceding tariff slots.
- Supported adapter versions are `cito-workbook-v1`, `lms-history-workbook-v1`,
  `price-changes-workbook-v1`, `introductions-workbook-v1`,
  `discontinued-workbook-v1` and `supply-workbook-v1`. The support matrix specifies
  XLS/XLSX schema boundaries, required context and unsupported cases.
- Added archive/register/import/summary/issue/retry commands and API, with
  immutable alternative derivations and explicit single-import observation reads.
  Optional delivery-scoped identities do not join different receipts. Accepted
  external mapping IDs are pinned; their evidence originals are verified too.
  Partial/invalid/conflicting coverage never becomes validated completeness.
- Demonstrated all six adapters in a persistent external synthetic workflow:
  21 independently expected assertions across all five types, identical retries,
  original-byte recovery and a cold backup/restoration comparison.
- Initialized a separate new private working database/archive and performed
  bounded representative imports of price-change, both introduction variants,
  stop/deletion and wholesaler reports. Identical retries, original-byte hashes,
  archive health and restored observations matched. No original was rewritten;
  no existing non-synthetic store was migrated and no history backfill was run.
  Detailed contexts, counts and evidence locations remain in private storage.
- Independently reviewed persistence, source identity and parser boundaries.
  Fixed reserved mapping-scope reuse, UUID spelling affecting identity, BIFF
  boolean/date coercion, reversed upcoming periods, and missing supply fields
  causing database failures. Scoped issue severity and metadata conflict locators
  were corrected. Regression tests cover the findings; final reviews have no
  unresolved findings.

- Final Windows Python 3.12 acceptance passed **508 synthetic tests**, including
  **203 foundation cases**. Six browser/private cases were deselected from that
  run; all **5 Chromium workflow tests** passed separately. The opt-in private
  application test was not run or counted as passed.
- Locked dependency synchronization and requirements export, Ruff check/format,
  mypy, pre-commit checks, tracked/staged content hygiene and `git diff --check`
  passed. Working-tree hygiene included all 67 tracked/new files rather than
  only the index. The wheel built outside Git contains both migrations, CLI,
  recovery helpers and all adapters. Pre-commit/pre-push hooks are installed.
- Synthetic recovery covered F1-to-F2 migration, repeated initialization,
  untouched original F1 backup, restored IDs/provenance, failed migration rollback,
  publication rollback/process termination, retry and missing/corrupt originals,
  including external mapping evidence. Current code reverified both demonstrated
  stores after the final identity fix.

Limits: only the exact workbook schemas in the support matrix are claimed.
Medicine-price PDFs, historical RTF schemas, older lifecycle variants, filtered
search exports and structured LMS members remain unsupported. Private Cito and
history workbooks exist but were not ingested in this F2 demonstration: applicable
report/calendar, mapping and price-context evidence was not established for them.
Their supported parser contracts have synthetic acceptance. Historical receipt/
publication timing and complete archive coverage remain unknown. Stop/deletion
date semantics retain explicit uncertainty; supply assertions do not infer
incidents or recovery. No Linux, remote CI, hosted or power-loss acceptance is
claimed. F3 reconciliation, replacement application, cutoff selection and frozen
evidence are absent by design.

## Earlier verification

2026-10-20 — F1 local foundation:

- Implemented `price_period`, `package`, `package_identifier`, `source_delivery`,
  `import_batch` and `import_issue`, with primary/foreign keys, scalar checks,
  application validation and a checksummed transactional migration ledger.
- Added explicit writer ownership, archive-relative SHA-256 objects, repeatable
  receipt/import registration, typed unknown/bounded timing and evidenced mapping
  scope/version. Original source bytes are never rewritten. DuckDB is the only
  new dependency; the lock and deployment requirements export agree.
- Verified 43 independently expected synthetic foundation cases: initialization,
  repeat migrations and failed-migration rollback; stable/restored identifiers;
  literal and canonical leading zeros; receipt versus interpretation identity;
  identical bytes in distinct contexts; exact original recovery; corrupt/missing
  objects; filesystem failure and orphan retry; unknown timing; scoped mappings;
  constraints, nested rollback and process-termination recovery.
- The full Windows Python 3.12 synthetic suite passed **348 tests**, with six
  browser/private cases deselected. All **5 Chromium workflow tests** passed
  separately. Existing application behaviour remains covered.
- Ruff check/format, mypy, pre-commit checks for tracked and new files,
  tracked/staged index hygiene and `git diff --check` passed. The content guard
  also inspected all **55 working-tree files**, including unstaged/new files,
  with no rejections. Built the wheel outside Git and confirmed its migration
  SQL and module entry point are included.
- Independent correctness review identified a rejected-import mapping conflict;
  it was fixed and covered by regression tests. No remaining review findings
  require a change to approved direction or the data contract.

Limits: acceptance used synthetic data only; no private originals or production
backup were verified. Database commits and filesystem publication are not atomic
together. Handled write failures and process termination are tested; power-loss/
OS-crash durability across both stores is not established. Verification reports
missing, corrupt, orphan and staging objects without destructive repair. Local
filesystems must support hard links and OS locks. Release completeness,
replacement semantics, active derivation selection and decision-cutoff enforcement
are retained declarations or later mechanisms, not implemented F1 capabilities.
No hosted deployment or remote CI result is claimed.
