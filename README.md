# Apotek

A pharmacist's workspace for investigating dated medicine-market evidence,
reviewing procurement opportunities and eventually measuring decision outcomes.

The local data foundation provides immutable source archiving, a persistent
DuckDB database, typed workbook ingestion and reproducible observation selection
with explicit identity, reconciliation and decision cutoffs (F1–F3).
The final product platform and hosting are open decisions.

## Direction and status

| Document | Purpose |
| --- | --- |
| [Approved direction](docs/direction.md) | Governs future product and analytical work |
| [Data foundation](docs/data-foundation.md) | Defines records, provenance, time, revisions and acceptance |
| [Development progress](docs/progress.md) | Tracks implemented work, dependencies and the next slice |
| [Development and delivery](docs/development.md) | Defines engineering checks and delivery safeguards |
| [F3 selection guide](docs/f3-selection.md) | Explains selection commands, evidence, revisions and operating limits |
| [E1 research guide](docs/e1-research.md) | Explains structured-source admission, frozen evaluation and the separate saved-results dashboard |
| [E2 research guide](docs/e2-research.md) | Defines dated substitution peers, bounded model comparisons and saved-result replay |
| [E3 research guide](docs/e3-research.md) | Defines advance-ABC proxy experiments, fall-aware queues, amount uncertainty and supported different-size peers |
| [E4 research guide](docs/e4-research.md) | Defines immutable assessments, separate outcomes and inventory scenarios |
| [E5 research guide](docs/e5-research.md) | Defines dated eligibility, freshness, tuning, monetary forecasts, persistence and financial rankings |
| [Training-data guide](docs/training-data.md) | Defines full-history export, typed features/labels, bounded reading and CatBoost inputs without fitting |

Older handovers and temporary-session-only plans do not govern new development.
The existing app is a working baseline, with its implemented behaviour documented
in [calculations.md](docs/calculations.md) and its current operating limits in
[deployment.md](docs/deployment.md).

## Current application

The current Danish Streamlit application imports Cito and LMS workbooks, builds a
deterministic review queue, compares available group alternatives and records
human judgements. Excel and review-session JSON downloads retain the applied
calculation context.

`historical-priority-v1` is an unvalidated screening method. Its ranking is not a
purchase recommendation or a demonstrated profit forecast. Existing eligibility
rules and UI choices are documented baselines for deliberate evaluation and
revision, not permanent requirements for the future product.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from
the repository root in PowerShell or a Unix shell:

```text
uv sync --locked --group dev
uv run streamlit run app.py
```

Open the local URL printed by Streamlit. Python 3.12 is selected by
`.python-version`. Only the server needs Python; viewers need a browser.
These commands do not establish a hosted instance or verified access controls.

The separate [local foundation entry point](docs/development.md#local-foundation-f1)
initializes private storage and registers original bytes.
[F3 selections](docs/f3-selection.md) require explicit imports, mappings, scope,
cutoff and independently reconciled evidence. The foundation does not change the
Streamlit workflow or persist its uploads and reviews.

The separate research dashboard displays explicitly selected, verified results:

```text
uv run streamlit run research_dashboard.py --server.address 127.0.0.1 --server.port 8503 -- <path-to-E1-run.json>
uv run streamlit run research_dashboard.py --server.address 127.0.0.1 --server.port 8513 -- --e2 <path-to-E2-result.json>
uv run streamlit run research_dashboard.py --server.address 127.0.0.1 --server.port 8514 -- --e3 <path-to-E3-index.json>
uv run streamlit run research_dashboard.py --server.address 127.0.0.1 --server.port 8515 -- --e4 <path-to-E4-index.json>
uv run streamlit run research_dashboard.py --server.address 127.0.0.1 --server.port 8516 -- --e5 <path-to-E5-index.json>
```

It reads saved artifacts only. It does not query the live foundation, fit models
or alter the existing application's reviews. See the
[E1 guide](docs/e1-research.md#saved-results-dashboard) and
[E2 guide](docs/e2-research.md) and [E3 guide](docs/e3-research.md) for their distinct datasets, exposure and comparison
boundaries. Explicit loopback binding keeps both dashboards local to this computer.

## Review a report

1. Upload **Cito-rapport** and **LMS-prishistorik**. Confirm the report's actual
   current pricing-period start; the latest history date does not establish it.
   Optionally enter received-at timestamps with timezone offsets.
2. Choose **Indlæs og kontroller** and inspect source dates, filters and issues.
   Eligible records without adequate matching history remain visible.
3. Set **Antal kandidater** or choose **Alle prioriterbare**. Search, sorting and
   queue size change the view without changing the underlying analysis or reviews.
4. Select a candidate and inspect its prices, gaps and found group alternatives.
   Optional labels identify products. Found current groups do not establish
   historical membership or a complete market; package AIP is not a unit-price
   comparison. Enable **Vis vurdering** to save a judgement and note.
5. Open **Eksport og sessionskopi** to download Excel and a JSON session copy.
   Only saved reviews appear in downloads. Download JSON before disconnecting or
   replacing the analysis; unfinished drafts are not included.

The existing app keeps uploads and reviews in the browser session's server-side
state. Restart or disconnection can lose them. JSON restoration requires matching
source bytes, confirmed period, availability metadata and analysis settings.
Excel is a review artifact, not a source-history backup.

## Current limits

Foundation selections are local evidence artifacts. Source schema support and
private historical availability remain bounded; accepted imports alone do not
establish analytical eligibility. Trained predictive models, automatic ordering,
quantity optimisation and realised-profit evaluation are not implemented.
See [progress](docs/progress.md) for verified status and remaining dependencies.

Keep original files, databases, derived business data and review copies in
approved private storage outside Git. Private-file checks are opt-in; default
tests use synthetic data. Software checks do not establish commercial benefit.
# Local pharmacist MVP

The trained-model workspace starts with `scripts/Start-Pharmacist.ps1 -ConfigFile
<private-config.json>`. It supports prepared market data, Cito-only upload,
same-target with/without-Cito review, the last two historical top-50 lists,
assessment-bound notes and PDF export. See [local operation](docs/deployment.md).
Private models, sources, configurations and results are installed by the operator
outside Git. This is a loopback-only diagnostic research tool, not an ordering system.
