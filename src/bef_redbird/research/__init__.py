"""Pinned offline research; independent of the existing application and UI."""

from typing import Any


def __getattr__(name: str) -> Any:
    # A read-only dashboard import must never load execution/model modules.
    if name in {"Dataset", "ResearchError", "load_dataset", "validate_dataset"}:
        from . import dataset

        return getattr(dataset, name)
    if name in {"EvaluationConfig", "evaluate"}:
        from . import evaluation

        return getattr(evaluation, name)
    if name in {"replay_fingerprint", "verify_run"}:
        from . import artifacts

        return getattr(artifacts, name)
    raise AttributeError(name)


__all__ = [
    "Dataset",
    "EvaluationConfig",
    "ResearchError",
    "evaluate",
    "load_dataset",
    "replay_fingerprint",
    "validate_dataset",
    "verify_run",
]
