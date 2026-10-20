# Approved product direction

Status: approved direction; implementation status is maintained separately in
[progress.md](progress.md).

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

Build the smallest reliable system that supports this workflow. Add complexity
only when a real source, measured limitation or accepted use case requires it.
Existing design choices are revisable; evidence and explicit contracts govern
changes.

## Concrete workflow

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
workflow needs shared access, concurrent writes or scheduled processing.
PostgreSQL is a future operational-storage option; no hosting provider or frontend
framework is selected. A migration must preserve the domain contract and evidence,
but is not assumed to require zero adaptation.

Start evaluation with explicit rules and simple historical baselines. Existing
eligibility thresholds, `historical-priority-v1`, the A/B-to-C screen and supplied
scoring approaches are comparison candidates, not validated optimal policies.
Keep named methods reproducible; give semantic changes new versions.

CatBoost is an approved candidate for later evaluation, not a preselected winner
or a dependency of the source-data foundation. Compare methods on chronological
decision periods, with availability-correct inputs, separate future labels and
honest coverage. More old data does not automatically improve present decisions.
Model training or data backfills must not automatically replace a deployed method.

## Development sequence

| Stage | Deliverable | Completion evidence |
| --- | --- | --- |
| 1. Data foundation | Provenance, identity, period calendar, typed observations, imports and issue handling | Repeatable imports, explicit coverage/conflicts, preserved originals and recovery checks |
| 2. Decision workflow | Frozen analyses, complete package assessments, human reviews and later outcome checks | Cutoff exclusion, deterministic replay, preserved reviews and versioned outcomes |
| 3. Analytical evaluation | Baselines and candidate models on suitable historical windows | Chronological comparisons, uncertainty and downside reporting; no hindsight claims |
| 4. Operational enrichment | Sales, stock, expiry, supplier terms and purchase/receipt/invoice data | Verified joins, quantity/financial definitions and evaluated procurement effects |
| 5. Shared operation | Suitable product platform, hosting and operational database if needed | Access, durability, concurrency, migration and restore acceptance |

These are dependencies, not a requirement to finish collecting every historical
year before beginning evaluation. Current releases with hard-to-recover timing
can continue to be collected while the foundation is built.

## Boundaries and open evidence

- Upcoming ABC is a relative classification, not proof of an own-AIP increase.
  Cito is reported to be a purpose-built SQL export; its upstream query and
  availability timing are not established.
- Future FTP access does not establish archive depth, exact release timing,
  completeness or equivalence to the daily wholesaler disruption reports.
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
