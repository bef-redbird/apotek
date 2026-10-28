"""Explicit CatBoost Pool boundary. No estimator, fit, predict or model evaluation."""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from catboost import Pool

from .dataset import ResearchError, digest
from .training_contract import CATEGORIES, FEATURES, NUMERICS, atomic_json
from .training_reader import TrainingBundle, load_bundle


def category_token(value: str | None) -> str:
    """Injective encoding: native empty strings and literal sentinels remain distinct."""
    return "M:" if value is None else "V:" + value


def peak_rss_bytes() -> int:
    import sys

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                *[
                    (name, ctypes.c_size_t)
                    for name in (
                        "PeakWorkingSetSize",
                        "WorkingSetSize",
                        "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage",
                        "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage",
                        "PagefileUsage",
                        "PeakPagefileUsage",
                    )
                ],
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        ctypes.windll.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            raise OSError("process_memory_measurement_failed")
        return int(counters.PeakWorkingSetSize)
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * (
        1 if sys.platform == "darwin" else 1024
    )


@dataclass(frozen=True)
class PoolInput:
    pool: Pool
    row_keys: tuple[str, ...]
    exact_labels: tuple[int | str, ...]
    report: dict[str, Any]


def build_pool(
    bundle: TrainingBundle,
    *,
    start: str,
    end_exclusive: str,
    mature_before: str,
    target: str = "next_direction",
) -> PoolInput:
    started = time.monotonic()
    if target not in {"next_direction", "next_delta_ore", "reversal_category"}:
        raise ResearchError("unsupported_catboost_target")
    horizon = "reversal" if target == "reversal_category" else "next"
    selected = list(
        bundle.window(start, end_exclusive, target=horizon, mature_before=mature_before)
    )
    selected.sort(key=lambda pair: (pair[0]["period_index"], pair[0]["package_number"]))
    if not selected:
        raise ResearchError("empty_mature_catboost_window")
    data: dict[str, Any] = {}
    for name in FEATURES:
        values = [feature[name] for feature, _ in selected]
        # Floating NaN represents numerical missingness, never zero imputation.
        data[name] = (
            [category_token(value) for value in values]
            if name in CATEGORIES
            else pd.Series(values, dtype="float64")
        )
    frame = pd.DataFrame(data, columns=list(FEATURES))
    keys = tuple(feature["row_key"] for feature, _ in selected)
    labels = tuple(label[target] for _, label in selected)
    if any(label is None for label in labels) or len(set(keys)) != len(keys):
        raise ResearchError("catboost_label_alignment_or_maturity_changed")
    timestamps = [
        (feature["period_start"] - date(1970, 1, 1)).days * 86400 for feature, _ in selected
    ]
    pool = Pool(
        data=frame,
        label=list(labels),
        cat_features=list(CATEGORIES),
        feature_names=list(FEATURES),
        timestamp=timestamps,
        thread_count=2,
    )
    indices = [FEATURES.index(name) for name in CATEGORIES]
    if (
        pool.num_row() != len(keys)
        or pool.get_feature_names() != list(FEATURES)
        or pool.get_cat_feature_indices() != indices
    ):
        raise ResearchError("catboost_pool_contract_changed")
    actual_labels = list(pool.get_label())
    expected_labels = (
        list(labels)
        if target == "reversal_category"
        else list(np.asarray(labels, dtype=np.float32))
    )
    if actual_labels != expected_labels:
        raise ResearchError("catboost_pool_label_readback_changed")
    report = {
        "contract": "catboost-pool-readiness-v1",
        "dataset_manifest_sha256": digest(bundle.manifest_path),
        "target": target,
        "start": start,
        "end_exclusive": end_exclusive,
        "mature_before": mature_before,
        "actual_first_date": selected[0][0]["period_start"].isoformat(),
        "actual_last_date": selected[-1][0]["period_start"].isoformat(),
        "rows": len(keys),
        "feature_names": list(FEATURES),
        "categorical_names": list(CATEGORIES),
        "categorical_indices": indices,
        "label_readback": (
            "all aligned Pool labels checked against declared CatBoost "
            "float32 conversion or native class strings"
        ),
        "exact_integer_ore_labels": (
            "retained in keyed Parquet and PoolInput.exact_labels; Pool numeric labels use float32"
        ),
        "numeric_null_counts": {name: int(frame[name].isna().sum()) for name in NUMERICS},
        "elapsed_seconds": time.monotonic() - started,
        "process_peak_rss_bytes": peak_rss_bytes(),
        "memory_scope": (
            "process lifetime high-water RSS including verified bundle "
            "reading and Pool construction"
        ),
        "model_fitted": False,
        "predictions_produced": False,
    }
    return PoolInput(pool, keys, labels, report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end-exclusive", required=True)
    parser.add_argument("--mature-before", required=True)
    parser.add_argument("--target", default="next_direction")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = build_pool(
        load_bundle(args.dataset),
        start=args.start,
        end_exclusive=args.end_exclusive,
        mature_before=args.mature_before,
        target=args.target,
    )
    atomic_json(args.report, result.report)
    print(
        {
            "pool_rows": result.pool.num_row(),
            "process_peak_rss_bytes": result.report["process_peak_rss_bytes"],
        }
    )


if __name__ == "__main__":
    main()
