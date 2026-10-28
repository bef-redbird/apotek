"""Independent scope, exact-money and temporal examples for opt-in AIP basis."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from bef_redbird.foundation import Store, Timing
from bef_redbird.foundation.adapters import reports
from bef_redbird.foundation.ingestion import adapter_reader
from bef_redbird.foundation.source_audit import expected_report, reconcile, snapshot_imports
from tests.test_foundation_reports import changes_rows, intro_rows, workbook

FIRST = "2026-02-02"
LAST = "2026-03-02"
SYNTHETIC_EVIDENCE = str(uuid5(NAMESPACE_URL, "invented-aip-scope-definition"))


def profile(**changes: Any) -> dict[str, Any]:
    result = {
        "version": 1,
        "price_type": "aip",
        "price_role": "effective_price",
        "currency": "DKK",
        "price_basis": "per_package",
        "tax_basis": "excluded",
        "fee_basis": "excluded",
        "effective_period_start_from": FIRST,
        "effective_period_start_through": LAST,
        "evidence_delivery_id": SYNTHETIC_EVIDENCE,
        "evidence_sha256": "e" * 64,
        "evidence_locator": "section:invented-reviewed-price-basis",
    }
    result.update(changes)
    return result


def context(period: str, **changes: Any) -> dict[str, Any]:
    return {
        "current_period": period,
        "tax_basis": "unknown",
        "fee_basis": "unknown",
        "price_basis_override": profile(**changes),
    }


def recipe(adapter: str, period: str, amount: Any = "90.071.992.547.409,93") -> bytes:
    printed = date.fromisoformat(period).strftime("%d-%m-%Y")
    if adapter == "price-changes":
        return workbook(changes_rows(tariff=printed, new=amount))
    rows = intro_rows()
    rows[1][9] = "Takstdato: " + printed
    rows[-1][10] = amount
    return workbook(rows)


def version(adapter: str) -> str:
    return {
        "price-changes": "price-changes-workbook-v2",
        "introductions": "introductions-workbook-v3",
    }[adapter]


def portable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


@pytest.mark.parametrize("adapter", ["price-changes", "introductions"])
@pytest.mark.parametrize(
    "period,eligible",
    [
        ("2026-02-01", False),
        (FIRST, True),
        ("2026-02-16", True),
        (LAST, True),
        ("2026-03-03", False),
    ],
)
def test_only_current_aip_within_inclusive_source_period_bounds(
    adapter: str, period: str, eligible: bool
) -> None:
    data = recipe(adapter, period)
    parsed = adapter_reader(adapter, version(adapter))(data, context(period))
    expected = expected_report(data, adapter, context(period), parser_version=version(adapter))
    prices = [a for a in parsed.assertions if a.table == "price_observation"]
    native = [a for a in expected["assertions"] if a["table"] == "price_observation"]
    assert prices[0].values["amount_ore"] == native[0]["values"]["amount_ore"] == 9007199254740993
    for actual, independent in zip(prices, native, strict=True):
        intended = (
            "excluded"
            if eligible
            and actual.values["price_type"] == "aip"
            and actual.values["price_role"] == "effective_price"
            else "unknown"
        )
        assert actual.values["tax_basis"] == actual.values["fee_basis"] == intended
        assert independent["values"]["tax_basis"] == independent["values"]["fee_basis"] == intended
        assert portable(actual.values) == independent["values"]
        if actual.values["price_role"] == "reported_previous_price":
            assert actual.effective_period is None and independent["effective_period"] is None


@pytest.mark.parametrize("adapter", ["price-changes", "introductions"])
@pytest.mark.parametrize(
    "amount,state,ore", [(0, "present", 0), (None, "blank", None), ("1,001", "invalid", None)]
)
def test_override_preserves_zero_blank_invalid_and_all_native_provenance(
    adapter: str, amount: Any, state: str, ore: int | None
) -> None:
    data = recipe(adapter, FIRST, amount)
    base_context = {"current_period": FIRST, "tax_basis": "unknown", "fee_basis": "unknown"}
    old = {
        "price-changes": reports.parse_price_changes,
        "introductions": reports.parse_introductions_v2,
    }[adapter](data, base_context)
    new = adapter_reader(adapter, version(adapter))(data, context(FIRST))
    assert new.records == old.records and new.issues == old.issues
    assert len(new.assertions) == len(old.assertions)
    changed = 0
    for original, upgraded in zip(old.assertions, new.assertions, strict=True):
        before, after = asdict(original), asdict(upgraded)
        if (
            original.table == "price_observation"
            and original.values["price_type"] == "aip"
            and original.values["price_role"] == "effective_price"
        ):
            assert upgraded.presence == {"amount_ore": state}
            assert upgraded.values["amount_ore"] == ore
            assert (
                after["values"].pop("tax_basis") == after["values"].pop("fee_basis") == "excluded"
            )
            assert (
                before["values"].pop("tax_basis") == before["values"].pop("fee_basis") == "unknown"
            )
            changed += 1
        assert before == after
    assert changed == 1


BAD_PROFILE_CHANGES = [
    {"version": True},
    {"version": 2},
    {"price_type": "registered"},
    {"price_type": "consumer"},
    {"price_role": "reported_previous_price"},
    {"currency": "EUR"},
    {"price_basis": "per_unit"},
    {"tax_basis": "unknown"},
    {"fee_basis": "includes_prescription_and_preparation_fees"},
    {"effective_period_start_from": "2026-2-2"},
    {"effective_period_start_from": "20260202"},
    {"effective_period_start_from": "2026-W06-1"},
    {"effective_period_start_from": "2026-02-30"},
    {"effective_period_start_from": None},
    {"effective_period_start_through": "2026-01-01"},
    {"evidence_delivery_id": "not-a-uuid"},
    {"evidence_delivery_id": SYNTHETIC_EVIDENCE.upper()},
    {"evidence_sha256": "g" * 64},
    {"evidence_sha256": "A" * 64},
    {"evidence_sha256": "e" * 63},
    {"evidence_locator": "  "},
    {"evidence_locator": None},
    {"unreviewed_extra": True},
]


@pytest.mark.parametrize("change", BAD_PROFILE_CHANGES)
@pytest.mark.parametrize("adapter", ["price-changes", "introductions"])
def test_both_readers_independently_reject_invalid_declared_scope(
    adapter: str, change: dict[str, Any]
) -> None:
    data, declared = recipe(adapter, FIRST), context(FIRST, **change)
    with pytest.raises(ValueError):
        adapter_reader(adapter, version(adapter))(data, declared)
    with pytest.raises(ValueError):
        expected_report(data, adapter, declared, parser_version=version(adapter))


@pytest.mark.parametrize("fallback", ["excluded", "vat_included", None])
@pytest.mark.parametrize("field", ["tax_basis", "fee_basis"])
def test_known_or_missing_fallback_cannot_relabel_registered_or_prior(
    fallback: Any, field: str
) -> None:
    declared = context(FIRST)
    declared[field] = fallback
    with pytest.raises(ValueError):
        reports.parse_price_changes_v2(recipe("price-changes", FIRST), declared)
    with pytest.raises(ValueError):
        expected_report(
            recipe("price-changes", FIRST),
            "price-changes",
            declared,
            parser_version="price-changes-workbook-v2",
        )


@pytest.mark.parametrize(
    "adapter,old_version",
    [
        ("price-changes", "price-changes-workbook-v1"),
        ("introductions", "introductions-workbook-v2"),
    ],
)
def test_new_version_requires_profile_and_old_version_rejects_reserved_key(
    adapter: str, old_version: str
) -> None:
    data = recipe(adapter, FIRST)
    missing = {"current_period": FIRST, "tax_basis": "unknown", "fee_basis": "unknown"}
    with pytest.raises(ValueError):
        adapter_reader(adapter, version(adapter))(data, missing)
    with pytest.raises(ValueError):
        expected_report(data, adapter, missing, parser_version=version(adapter))
    with pytest.raises(ValueError):
        adapter_reader(adapter, old_version)(data, context(FIRST))
    with pytest.raises(ValueError):
        expected_report(data, adapter, context(FIRST), parser_version=old_version)
    # Previously supported declared bases retain their exact old meaning.
    known = {**missing, "tax_basis": "excluded", "fee_basis": "excluded"}
    old = adapter_reader(adapter, old_version)(data, known)
    assert all(
        a.values["tax_basis"] == a.values["fee_basis"] == "excluded"
        for a in old.assertions
        if a.table == "price_observation"
    )


def test_api_bounds_are_reviewed_caller_data_not_private_archive_constants() -> None:
    invented_future = "2037-04-06"
    declared = context(
        invented_future,
        effective_period_start_from=invented_future,
        effective_period_start_through=invented_future,
    )
    parsed = reports.parse_price_changes_v2(recipe("price-changes", invented_future), declared)
    assert parsed.assertions[1].values["tax_basis"] == "excluded"
    # This proves schema applicability only, not that this invented definition exists.


def test_independent_expectation_does_not_call_any_production_basis_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("production reader called")

    monkeypatch.setattr(reports, "parse_price_changes_v2", forbidden)
    monkeypatch.setattr(reports, "parse_price_changes", forbidden)
    result = expected_report(
        recipe("price-changes", FIRST),
        "price-changes",
        context(FIRST),
        parser_version="price-changes-workbook-v2",
    )
    assert result["assertions"][1]["values"]["tax_basis"] == "excluded"


def timing(day: int) -> Timing:
    instant = datetime(2026, 2, day, 9, tzinfo=UTC)
    return Timing(instant, instant, "instant", "invented evidence", "UTC")


def setup_store(
    tmp_path: Path, *, wrong_hash: bool = False
) -> tuple[Path, Path, str, str, str, Path]:
    database, archive = tmp_path / "synthetic.duckdb", tmp_path / "originals"
    definition = tmp_path / "synthetic-definition.json"
    definition.write_text('{"meaning":"invented reviewed AIP basis"}', encoding="utf-8")
    source = tmp_path / "report.xlsx"
    source.write_bytes(recipe("price-changes", FIRST))
    with Store(database, archive) as store:
        start = date.fromisoformat(FIRST)
        store.add_period(start, start + timedelta(days=14))
        evidence_id = store.register_source(
            definition, source="synthetic-basis", receipt_key="definition", received=timing(3)
        )
        delivery_id = store.register_source(
            source,
            source="synthetic-change",
            receipt_key="report",
            current_period=start,
            received=timing(1),
        )
        declared = context(
            FIRST,
            evidence_delivery_id=evidence_id,
            evidence_sha256="0" * 64
            if wrong_hash
            else hashlib.sha256(definition.read_bytes()).hexdigest(),
        )
        declared.pop("current_period")
        result = store.ingest(
            delivery_id,
            adapter="price-changes",
            version="price-changes-workbook-v2",
            mapping_version="invented-basis-v1",
            mapping_policy="delivery",
            context=declared,
        )
        assert result["validation_status"] == "accepted"
        import_id = result["import_id"]
    return database, archive, import_id, delivery_id, evidence_id, source


def test_scoped_audit_includes_definition_bytes_and_exact_persisted_assertions(
    tmp_path: Path,
) -> None:
    database, archive, iid, _, evidence, original = setup_store(tmp_path)
    scoped = snapshot_imports(database, [iid])
    assert scoped["source_delivery"]["count"] == 2
    assert evidence in {r["delivery_id"] for r in scoped["source_delivery"]["rows"]}
    result = reconcile(database, archive, [original], import_ids=[iid])
    assert result["complete"] and not result["mismatches"]
    assert len(result["verified_observation_ids"]) == 5
    assert result["imports"][0]["basis_context_evidence"]["delivery_id"] == evidence


def test_false_evidence_hash_is_quarantined_not_treated_as_definition_proof(tmp_path: Path) -> None:
    database, archive, iid, _, _, original = setup_store(tmp_path, wrong_hash=True)
    report = reconcile(database, archive, [original], import_ids=[iid])
    assert not report["complete"]
    assert report["imports"][0]["status"] == "quarantined"
    assert report["imports"][0]["reason"] == "independent_price_basis_evidence_pin_mismatch"
    assert not report["verified_observation_ids"]


@pytest.mark.parametrize("mode", ["historical_replay", "reconstructed_research"])
@pytest.mark.parametrize("configuration_version", [1, 2])
def test_definition_availability_remains_a_selection_gate(
    tmp_path: Path, mode: str, configuration_version: int
) -> None:
    database, archive, iid, delivery, evidence, original = setup_store(tmp_path)
    report = reconcile(database, archive, [original], import_ids=[iid])
    proof = tmp_path / "independent-report.json"
    proof.write_text(json.dumps(report), encoding="utf-8")
    with Store(database, archive) as store:
        proof_id = store.register_source(
            proof, source="synthetic-independent-proof", receipt_key="proof", received=timing(3)
        )
        mapping = store._connection.execute(
            "SELECT mapping_id, package_id FROM package_identifier WHERE evidence_import_id=?",
            [iid],
        ).fetchone()
        assert mapping is not None
        config: dict[str, Any] = {
            "version": configuration_version,
            "policy_version": "f3-selection-v1",
            "import_ids": [iid],
            "mapping_ids": [str(mapping[0])],
            "derivations": {delivery: iid},
            "scope": {
                "package_ids": [str(mapping[1])],
                "periods": [FIRST],
                "dates": [],
                "tables": ["price_observation"],
                "fields": ["amount_ore"],
                "context": {"price_type": ["aip"], "price_role": ["effective_price"]},
            },
            "cutoff": "2026-02-02T12:00:00+00:00",
            "mode": mode,
            "availability_policy": "receipt",
            "source_preference": {"version": "none-v1", "rules": []},
            "verification_reports": [proof_id],
            "context_evidence": {iid: [evidence]},
            "revisions": [],
            "business_context": {"purpose": "invented temporal basis boundary"},
        }
        if configuration_version == 2:
            config["candidate_catalogue"] = [
                {"namespace": "dk:varenummer", "canonical_identifier": "000123"}
            ]
        missing = copy.deepcopy(config)
        missing["context_evidence"][iid] = []
        with pytest.raises(ValueError, match="must be pinned"):
            store.create_selection(missing)
        selected = store.create_selection(config)
        if mode == "historical_replay":
            assert selected["evidence"]["result"] == []
        else:
            assert [(r["value"], r["usable"]) for r in selected["evidence"]["result"]] == [
                (9007199254740993, True)
            ]
            assert any(
                d["id"] == evidence and d["reason"] == "after_cutoff"
                for d in selected["evidence"]["availability_disclosures"]
            )
        assert store.inspect_selection(selected["selection_id"]) == selected


def test_selection_rejects_a_declared_hash_which_differs_from_pinned_delivery(
    tmp_path: Path,
) -> None:
    database, archive, iid, delivery, evidence, _ = setup_store(tmp_path, wrong_hash=True)
    proof = tmp_path / "untrusted-empty-proof.json"
    proof.write_text(
        json.dumps(
            {
                "version": 1,
                "complete": True,
                "mismatches": [],
                "quarantined_observation_ids": [],
                "verified_observation_ids": [],
                "observation_hashes": {},
            }
        ),
        encoding="utf-8",
    )
    with Store(database, archive) as store:
        proof_id = store.register_source(proof, source="synthetic-empty-proof", receipt_key="proof")
        mapping = store._connection.execute(
            "SELECT mapping_id, package_id FROM package_identifier WHERE evidence_import_id=?",
            [iid],
        ).fetchone()
        assert mapping is not None
        config = {
            "version": 1,
            "policy_version": "f3-selection-v1",
            "import_ids": [iid],
            "mapping_ids": [str(mapping[0])],
            "derivations": {delivery: iid},
            "scope": {
                "package_ids": [str(mapping[1])],
                "periods": [FIRST],
                "dates": [],
                "tables": ["price_observation"],
            },
            "cutoff": "2026-02-02T12:00:00+00:00",
            "mode": "reconstructed_research",
            "availability_policy": "receipt",
            "source_preference": {"version": "none-v1", "rules": []},
            "verification_reports": [proof_id],
            "context_evidence": {iid: [evidence]},
            "revisions": [],
            "business_context": {"purpose": "reject context pin mismatch before any selection"},
        }
        with pytest.raises(ValueError, match="hash differs"):
            store.create_selection(config)
