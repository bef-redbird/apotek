# Apotek

A pharmacist's workspace for investigating dated medicine-market evidence,
reviewing procurement opportunities and eventually measuring decision outcomes.

The first local data-foundation slice (F1) provides immutable source archiving,
one persistent DuckDB database and reference/provenance records. Source adapters,
market observations and reproducible analyses tied to a decision cutoff remain
later slices. The final product platform and hosting are open decisions.

## Direction and status

| Document | Purpose |
| --- | --- |
| [Approved direction](docs/direction.md) | Governs future product and analytical work |
| [Data foundation](docs/data-foundation.md) | Defines records, provenance, time, revisions and acceptance |
| [Development progress](docs/progress.md) | Tracks implemented work, dependencies and the next slice |
| [Development and delivery](docs/development.md) | Defines engineering checks and delivery safeguards |

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
initializes private storage and registers original bytes. It does not change the
Streamlit workflow or persist its uploads and reviews.

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

No durable database, historical-file ingestion pipeline, trained predictive model,
automatic ordering, quantity optimisation or realised-profit evaluation is
implemented. See [progress](docs/progress.md) for the approved development sequence.

Keep original files, databases, derived business data and review copies in
approved private storage outside Git. Private-file checks are opt-in; default
tests use synthetic data. Software checks do not establish commercial benefit.
