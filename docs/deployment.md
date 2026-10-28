# Local deployment

`pharmacist_app.py` is the trained-model pharmacist workspace. The operator supplies
a private JSON configuration outside Git with `root` (assessment storage),
`protocol_root` (retained-model operation) and `market` (path/SHA-256/size pin of a
prepared market index). The index binds causal features, supported history and any
verified Cito/outcome evidence to explicit origin-to-target price transitions.

Models, originals, prepared inputs and verified code pins are private prerequisites.
A repository checkout alone does not contain them. Retained absolute pins must
resolve to verified originals or separately verified recovery; do not rewrite frozen
protocol identities to make a new location pass validation. The preserved local
operator workspace retains acquisition, database maintenance and its own backup.
The viewer never opens the operating database.

From the repository root on Windows:

```powershell
.\scripts\Start-Pharmacist.ps1 -ConfigFile 'C:\Private\apotek\config.json'
.\scripts\Start-Pharmacist.ps1 -ConfigFile 'C:\Private\apotek\config.json' -Stop
.\scripts\Start-Pharmacist.ps1 -ConfigFile 'C:\Private\apotek\config.json' -Restart
```

The launcher installs the locked `inference` group in the private assessment root,
including CatBoost, SciPy, psutil and threadpoolctl, and binds only
`http://127.0.0.1:8530`. Duplicate launches reuse the verified process; startup errors
identify private logs. Python's exact patch is pinned in `.python-version`. Keep the
PC awake and the account signed in; Windows may be locked. No power settings,
signed-out collection, online hosting or public port are configured.

On another local operating system, install
`uv sync --locked --no-dev --group inference`, set `REDBIRD_ASSESSMENT_CONFIG` to the
private configuration, and run Streamlit with `--server.address 127.0.0.1` and
`--server.port 8530`. The base requirements export below omits inference dependencies.

Explicit analysis submission starts a serialized local worker. Durable status
supports reconnect/retry; identical requests reuse verified work and changed context
gets a separate identity. Failure preserves the last successful assessment. Notes
bind to assessment/package. PDF exports verify saved values without inference.
No training, automatic promotion, ordering or remote AI service is invoked.

## Earlier workbook baseline

The remaining sections describe the separate `app.py` baseline and its earlier
hosting proposal. They are not the trained-model workspace's deployment procedure;
remote operation remains outside this local delivery.

The same Streamlit process serves local and hosted use. There is no separate API,
database service, custom authentication or runtime AI dependency. Do not deploy
commercial source data until source-data terms and pharmacy approval cover that
host and account. A private GitHub repository is not processing permission.

## Community Cloud configuration

Streamlit Community Cloud supports private GitHub repositories. In the authorised
workspace, create an app from `bef-redbird/apotek`, the reviewed default branch
and `app.py`, selecting Python 3.12. Keep **Only specific people can view this app**
enabled and configure approved viewers through host-managed identity. Viewer
access does not require repository write permission. Do not make the app public
to work around account limits. The provider currently permits one private app;
verify the account's actual entitlement and sharing policy before deploying.

Community Cloud prioritizes the root `uv.lock` over `requirements.txt`.
The project disables default uv dependency groups so an ordinary sync installs
runtime dependencies only. Development and browser checks explicitly opt into
their groups. Check the cloud build log to confirm the selected installer.
See [dependency-file precedence](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies).

For hosts that use requirements files, the runtime `requirements.txt` is generated
from `uv.lock`:

```text
uv export --locked --no-dev --no-group browser --no-emit-project --format requirements-txt --output-file requirements.txt
```

It contains only locked runtime dependencies. `app.py` makes the local `src`
package importable when a host installs requirements without installing the
project. CI checks that this export remains current. Do not manually edit it.
For an approved process host that supports uv, use:

```text
uv sync --locked --no-dev --no-group browser
uv run --no-sync streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Place that process behind the host's authenticated TLS access boundary. A bind to
`0.0.0.0` is not authentication. Local launch defaults to the standard local URL;
do not expose it directly to the internet. The health endpoint is `/_stcore/health`.
Keep credentials in host configuration or ignored `.streamlit/secrets.toml`.

Before accepting a host:

- Verify unauthorised access in a signed-out browser and authorised viewer access
  without GitHub write rights, using synthetic data.
- Run two separate browser contexts with different uploads and notes; confirm
  isolation, reset behaviour and matching-only JSON restoration.
- Measure full-size import memory/time on that host with approved original data;
  verify cancellation, failed-import recovery and selection responsiveness.
- Verify upload and websocket/proxy limits accommodate the input; keep resource
  headroom for parser memory and concurrent sessions.

These are host acceptance checks, not claims that deployment has already occurred.
See [Streamlit sharing](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app)
and [deployment documentation](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app).

## Temporary storage and capacity

Each session owns one active parsed source pair, one analysis and its reviews.
There is no global upload cache, telemetry SDK, automatic remote download or
shared database. Changing N, notes or display settings does not reparse Excel.
Identical source hashes reuse the active parsed input. A failed import retains
the previous workspace and explicitly labels it. Replacing reviewed analysis
requires deliberate acknowledgement; download JSON before doing so.

Uploads and review state can disappear after restart or disconnection. Local
cloud disk is not durable storage. No backup/restore promise applies to source
workbooks: the JSON session contains review state and provenance, not raw history.
There is no forensic-erasure claim for process memory or provider disks.

The app accepts up to 128 MiB per file, at most 1 GiB total uncompressed XLSX ZIP
members, 5,000 ZIP members, 250,000 worksheet rows, 600 columns and 25 million
selected-sheet cells. It does not extract archive paths or follow workbook links.
Review notes are limited to 4,000 characters; review JSON to 2 MiB and 25,000
reviews in both export and restore. An oversized JSON copy is not offered for
download: reviews remain intact and Excel export stays available, but Excel
cannot restore the application session. Overlays are limited to
five found alternatives. N has a technical bound of one billion without allocation
proportional to N; all-rankable remains available. None of these limits silently
truncate analytic history. A limit violation is an explicit failed import.

The ~65 MB LMS workbook expands considerably. Development-machine measurements
are not a host capacity guarantee. Size the approved host after measuring its
own actual workbook import and expected concurrent sessions. The application
does not downsample observations to fit a small host.

## Private acceptance command

Set `REDBIRD_PRIVATE_CITO_PATH` and `REDBIRD_PRIVATE_LMS_PATH` to authorised absolute
paths outside Git. Optionally set `REDBIRD_PRIVATE_PERIOD` to an independently
confirmed ISO period date (the reference fixture test defaults to a 2026-09-28
test context, not an asserted date for a different report). Then run:

```text
uv run --group dev pytest -m private tests/private -q -s --tb=short --assert=plain
```

The check compares aggregate counts and a small source-cell sample using an
independent raw-cell pass, reports hashes, elapsed import time and process memory,
and verifies unchanged source hashes. Different hashes produce observed counts;
they do not inherit the reference fixture's expected shortlist. Missing paths skip
honestly. Never set private paths in CI or publish product-level failure output.
`--tb=short --assert=plain` reduces accidental diagnostic exposure; run only in approved private
development environments. Default CI uses wholly synthetic recipes.

Nonessential Streamlit telemetry is disabled. Unexpected UI faults show a safe
correlation code, not source values or local paths. Operational file logging is
off; no runtime/source artifacts belong in this repository.
