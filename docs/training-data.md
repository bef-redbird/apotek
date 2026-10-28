# Full historical training data

`full-history-training-v1` is a separate, model-neutral boundary. The original E1
panel, reader, evaluator and saved results retain their existing contracts.
Preparation and input loading do not fit, predict, tune or evaluate a model.

The owner's later bounded E2 authorization permits actual local research under
its separately frozen protocol; it does not alter this accepted R1 contract or
automatically refresh this bundle. [The E2 guide](e2-research.md) describes its
dated group sidecar, feature-only prediction cohort, measured experiments and
saved-result replay. Existing group identifiers were available in FTP; validated
whole-period populations and comparable peer summaries were the missing step.
The separately authorized [E3 extension](e3-research.md) adds an audited
next-ABC proxy and supported scalar-size peer sidecars. Its predictor allowlist
admits only the next code, not detailed future rejection metadata. It leaves this
R1 contract, dictionary, labels and accepted bytes unchanged.

Use Python 3.12 and `uv sync --locked --group research`. Work with private output
directories outside Git, a stopped writer/viewer and a cold canonical database.
The producer opens that database read-only; it does not run publication again.

The private template declares `partitions` (an explicit measured bucket count), `exclusion_ledger`,
`recovery_references`, chronological nonoverlapping `windows`, `exposure`, and
`pool_window`. Each window has `name`, `start` and `end_exclusive` ISO dates.
The Pool window has `start`, `end_exclusive`, and `mature_before`; its dates must
fall inside a declared window. `pool_targets` defaults to `next_direction`,
`next_delta_ore` and `reversal_category`. Recovery references identify retained
precleanup recovery manifests/receipts and the completed lean migration receipt;
they must not depend on the final recovery manifest that will include this bundle.

After accepted FTP/supplier updates and canonical publication, refresh with the
same operating configuration and template:

```powershell
uv run --group research python -m bef_redbird.research.training `
  --operating-config <private-operating-config.json> `
  --template <private-training-template.json> --output-root <private-training-root>
```

The command discovers the explicit published dataset ID, derives a new immutable
version, exports every supported final package/price observation from 2008 onward,
verifies the complete bundle and constructs all three actual CatBoost Pools
sequentially. `current.json` is replaced only after every check succeeds. Earlier
versions are retained. An unchanged retry verifies completed partition pins and
resumes missing work. Changed source bytes, policies, code, dependencies or database
pins require a new version. Refresh performs a bounded full recomputation for a
new publication; it does not reuse stale future labels from an older publication.

For an explicitly selected dataset, the lower-level producer accepts `--config`
and `--output`. That config adds `database` and `dataset_id` to the template.
Its completed `dataset.json` establishes model-neutral export/readback; the
refresh pointer additionally establishes the measured CatBoost input check.

The bundle contains typed Parquet source, feature and separately keyed label
partitions; the actual calendar and assumed Wednesday second-week noon cutoffs;
per-period source/eligibility/label coverage; scoped source disagreements; and a
source catalogue with original content hashes, import/member/locator provenance,
canonical policy and proof hashes. A small deterministic `producer.zip` preserves
the producing source package, dependency declarations and lock. Native originals
remain in retained content-addressed archives, with pinned recovery references.

The pinned `contract.json` is the exact data dictionary: ordered predictor names,
Parquet types, units, categorical/numeric kind, null meanings, auxiliary fields,
future label definitions and maturity, and conversion policies. It also lists the
complete source/feature/label schemas. The reader requires the same versioned
dictionary, including when a modified dictionary has been given a new file hash.

Partitions restrict complete package histories before canonical aggregation and
identity windows. Raw package-key pruning is used only after an exhaustive check
that every selected identity, price and ordinary-classification scope supports
that key; otherwise canonical nullable-key partitioning is retained. SQL uses two
threads, an 8 GB engine limit and at most 12 GiB of
spill; these are engine limits, not an operating-system process cap. Python
construction and verification retain one package history and bounded record
batches, plus a compact set of package identifiers. Each exported source cursor's
row count must equal the source, feature and label Parquet row counts. Completion
is published only after all partitions pass. A stopped run retains atomic completed
checkpoints; partial files cannot become a current bundle.

The ordered feature allowlist consists of exact own AIP, three past integer-øre
changes, expanding within-episode frequencies/history length, evidenced lifecycle
age, current ordinary ABC, uncertainty/missingness and historical native ATC,
form, strength, package-size/unit, dispensing and distributor values. Native tokens
have no invented numeric scale. No latest attributes are carried backward. Opaque
package/drug/group identifiers and provenance are auxiliary keys, not model inputs.
The distributor code is intentionally admitted as a categorical description of
historical market affiliation; its predictive value has not been assessed.
Supply remains unavailable until a reliable dated identity/coverage join is
accepted; absence is never encoded as no disruption. Peer-count/price aggregates
were omitted from R1 because complete comparable populations had not then been
established. E2 now supplies a separately versioned, independently reconciled
dated LMS04 sidecar for its conservative equal-native-size/unit scope. It does
not silently add predictors to, or change the meaning of, this accepted bundle.

Eligibility requires supported positive fixed-price pharmacy AIP in DKK per
package, excluding VAT and dispensing fees, and supported identity/lifecycle and
dispensing context. Gaps, ineligible observations and identity changes split causal
episodes. Excluded origins remain in the bundle and coverage. Unequal ABC scopes
suppress the affected current ABC values and mark implicated ordinary groups
uncertain; group state concerns ABC evidence. TSP disagreements remain scoped
metadata. TSP is not a feature, and those disagreements do not discard valid AIP.

Next-period direction and magnitude use exact integer-øre differences with no
tolerance. Observed-rise reversal uses the immediately preceding eligible price
as reference and requires both following actual tariff observations in the same
episode: full reversal reaches that reference; partial reversal falls below the
rise price without reaching it; persistence does neither. Gaps, identity changes,
ineligibility, pending tails and non-rise origins remain explicit. Features contain
no future values. Labels retain horizon indices, actual maturity dates and status;
the window reader requires observed labels mature strictly before its cutoff.

Readback is independent of the live database:

```powershell
uv run --group research python -m bef_redbird.research.training_reader `
  --dataset <version/dataset.json> --report <private-readback.json>
```

`load_bundle(path, relocations={old_root: restored_root})` supports explicit
recovery relocation while checking the same external reference hashes. It checks
every pinned artifact, typed schema, source-feature alignment, historical
attributes/eligibility/episodes, exact labels, horizon maturity and period counts.
`TrainingBundle.iter_pairs()` streams all rows; `.window(...)` selects a declared
chronological eligible/mature window. No live database is opened.

For a Python consumer, run this from the repository root with the research group
installed (`uv run --group research python <your-script.py>`):

```python
import hashlib
import json
from pathlib import Path

from bef_redbird.research.training_reader import load_bundle

data_root = Path.cwd().parent / "data"
pointer = json.loads((data_root / "training/current.json").read_text())
manifest = Path(pointer["dataset"]["path"])
with manifest.open("rb") as stream:
    actual_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
if actual_sha256 != pointer["dataset"]["sha256"]:
    raise ValueError("Current training manifest changed")

bundle = load_bundle(manifest)  # verifies the frozen files and references
observed_eligible = 0
for feature, label in bundle.iter_pairs():
    if feature["eligible"] and label["next_status"] == "observed":
        observed_eligible += 1  # consume this pair, then discard it
print({"observed_eligible_pairs": observed_eligible})
```

The manifest is small; the loop does not retain the full feature/label rows.

The CatBoost adapter passes explicit feature names and categorical names/indices.
Missing categoricals use an injective `M:`/`V:` encoding, preserving native empty
strings and literal sentinels; numerical nulls remain NaN. Pool labels are read
back and checked in order. The dictionary's numeric adapter policy describes the
float64 columns passed to the constructor. CatBoost stores numerical features as
float32 internally, so large integer-øre values can be rounded inside a Pool;
the exact source/features remain in Parquet. Pool construction does not learn
quantization borders. CatBoost numerical labels also use float32; exact integer
labels remain in the keyed Parquet and `PoolInput.exact_labels`. This conversion
is explicit, including when the magnitude target would lose integer precision.
Reports record actual dates/rows, elapsed time and process peak working-set/RSS.
Full export streaming, the measured Pool window and eventual fitting memory are
separate claims. All available history remains reconstructed/exploratory, and
accepted E1 exposure stays declared. A future confirmation window requires a
fresh protocol; refresh never grants an untouched-data claim.

The Pool interface and missing-value policy follow the official
[Pool reference](https://catboost.ai/docs/en/concepts/python-reference_pool) and
[missing-value documentation](https://catboost.ai/docs/en/concepts/algorithm-missing-values-processing).
