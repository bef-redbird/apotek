# Current application: deployment and operating limits

## Local trained-model workspace

`pharmacist_app.py` is the assisted pharmacist MVP. It reads an operator-published,
hash-bound market index and saved assessments. Cito is its only file upload. The
legacy `app.py` remains the separate deterministic workbook baseline.

Set `REDBIRD_ASSESSMENT_CONFIG` to a private JSON configuration outside Git containing
`root` (private assessment storage), `protocol_root` (the retained prospective
comparison operation) and `market` (path/SHA-256/size pin of a market index). Each
index period binds origin, target, causal features and optional official outcome,
period-bound Cito and price-history artifacts. The operator prepares these inputs;
the viewer never opens the live database. Do not commit actual configurations.

On Windows, run `scripts/Start-Pharmacist.ps1 -ConfigFile <private-config.json>`.
It installs the locked `inference` group, including CatBoost, SciPy, psutil and
threadpoolctl, and binds only `127.0.0.1:8530`. Repeated launches reuse the verified
process. Add `-Stop` or `-Restart` for that same process; occupied unrelated ports
fail clearly. Keep the PC awake and signed in; Windows may be locked. This does
not configure signed-out collection or change power settings.

For another local operating system, use `uv sync --locked --no-dev --group inference`
and `uv run --no-sync streamlit run pharmacist_app.py --server.address 127.0.0.1
--server.port 8530 --server.headless true --browser.gatherUsageStats false` with
the environment variable set. The legacy requirements export does not install
the additional inference group; it is not the pharmacist launcher environment.

Analysis starts only on explicit submission, in a separate local Python process.
Jobs retain actual stage names, source/model/context identities, full predictions,
current-only comparison, exclusions and subsequent outcome attachment. Identical
requests reuse verified work; changed input gets a separate identity. Native OS
locking prevents simultaneous inference. Browser reconnection reads job status;
interrupted jobs can be retried. Notes are stored per assessment and package.
PDF exports verify saved predictions and retain a checksum receipt. All artifacts,
uploads, notes and logs remain outside Git. The last successful assessment survives
a failed new job. Source data are not sent to a runtime AI service.

These are diagnostic research reviews, not prospective enrolment, orders, quantity
recommendations or pharmacy profit forecasts. The next-period view supports missing
Cito; historical final ABC is never passed off as a received advance report. The
retained three-year amount model is the display reference; six/full-history estimates
are optional sensitivity views. No unsupported monetary interval is shown.

## Earlier workbook baseline

The following sections describe the separate `app.py` baseline and are not the
deployment instructions for the trained-model workspace above.

This document describes the existing Streamlit application. It does not select
the future product platform, hosting provider or persistent-storage architecture.
New work follows [direction.md](direction.md) and
[data-foundation.md](data-foundation.md); implementation status is in
[progress.md](progress.md).

The current Streamlit process serves both local and hosted use. It has no
separate API, database service, custom authentication or runtime AI dependency.

## Local use

From the repository root with Python 3.12 and uv:

```text
uv sync --locked --group dev
uv run streamlit run app.py
```

Use the local URL printed by Streamlit. Keep authorised originals and review
copies outside Git. Local use does not require choosing a hosting provider.

## Hosting the current application

Choose a host only when shared operation is needed and source-data terms and
pharmacy approval cover that host and account. A private GitHub repository does
not establish permission to process its users' source data.

For an approved process host that supports uv:

```text
uv sync --locked --no-dev --no-group browser
uv run --no-sync streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Place that process behind the host's authenticated TLS access boundary. A bind
to `0.0.0.0` is not authentication. Do not expose the process directly to the
internet. Its health endpoint is `/_stcore/health`. Keep credentials in host
configuration or ignored `.streamlit/secrets.toml`.

For hosts that install requirements files, the runtime `requirements.txt` is
generated from `uv.lock`:

```text
uv export --locked --no-dev --no-group browser --no-emit-project --format requirements-txt --output-file requirements.txt
```

It contains only locked runtime dependencies. `app.py` makes the local `src`
package importable when a host installs requirements without installing the
project. CI checks that this export remains current; do not manually edit it.
Confirm the host actually uses the intended lock/export and Python version.

Before accepting a host:

- Verify signed-out access is denied and approved viewers can use the app
  without repository write permission, using synthetic data.
- Run separate browser contexts with different uploads and notes; confirm
  isolation, reset behaviour and matching-only JSON restoration.
- Measure full-size import memory/time with approved originals; verify
  failed-import recovery and selection responsiveness.
- Check upload, websocket/proxy and concurrent-session capacity on that host.

These are acceptance requirements, not evidence of an existing deployment.

## Temporary storage and capacity

Each current session owns one active parsed source pair, one analysis and its
reviews. There is no global upload cache, automatic remote download or shared
database. Changing queue size, notes or display settings does not reparse Excel.
Identical source hashes reuse the active parsed input. A failed import retains
and explicitly labels the previous workspace. Replacing reviewed analysis
requires deliberate acknowledgement.

Uploads and reviews can disappear after restart or disconnection. Host-local
disk is not a durability guarantee. Download JSON before replacing an analysis;
it preserves saved review state and provenance, not raw source history. There
is no forensic-erasure claim for process memory or provider disks.

Current implementation limits:

- 128 MiB per uploaded file; 1 GiB total uncompressed XLSX ZIP members;
  5,000 ZIP members.
- 250,000 worksheet rows, 600 columns and 25 million selected-sheet cells.
- 4,000 characters per review note; 2 MiB and 25,000 reviews per JSON export
  or restore.
- Five found alternatives overlaid on the selected candidate's chart.
- A technical queue-size bound of one billion, without allocation proportional
  to that number; all-rankable remains available.

The app does not extract archive paths or follow workbook links. Limits do not
silently truncate analytic history: an input violation fails explicitly.
Oversized JSON is not offered for download; reviews remain intact and Excel
export remains available, but Excel cannot restore the session.

These are current software limits, not future product ceilings. Measure parser
memory and expected concurrency before sizing a host; local measurements are
not a capacity guarantee. The app does not downsample history to fit a host.

## Private acceptance

Set `REDBIRD_PRIVATE_CITO_PATH` and `REDBIRD_PRIVATE_LMS_PATH` to authorised
absolute paths outside Git. Set `REDBIRD_PRIVATE_PERIOD` to the independently
confirmed ISO period date for the report being checked; do not treat a test's
default context as proof of a different report's date.

```text
uv run --group dev pytest -m private tests/private -q -s --tb=short --assert=plain
```

The check compares aggregate counts and a small source-cell sample using an
independent raw-cell pass, reports hashes, import time and process memory, and
verifies unchanged source hashes. Different hashes produce observed counts;
they do not inherit a reference fixture's expected shortlist. Missing paths
skip honestly. Never set private paths in CI or publish product-level failures.
Run only in an approved private development environment.

Nonessential Streamlit telemetry is disabled. Unexpected UI faults show a safe
correlation code instead of source values or local paths. Operational file
logging is off. Default CI uses synthetic data; runtime/source artifacts do
not belong in this repository.
