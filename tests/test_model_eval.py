"""Tests for the model catalog and release-blocking eval harness."""

import pytest

from hcs_trader.agents.eval import (
    EvalCategory,
    EvalNotReady,
    assert_release_ready,
    run_eval,
)
from hcs_trader.model_catalog import (
    ModelNotAdmitted,
    admit,
    catalog,
    find,
    unverified_aliases,
)
from hcs_trader.model_manifest import ModelMetadata

_SHA = "a" * 64


def _metadata(**overrides: object) -> ModelMetadata:
    values: dict[str, object] = {
        "alias": "LFM2.5-350M-Q4_K_M",
        "sha256": _SHA,
        "license": "Apache-2.0",
        "quantization": "Q4_K_M",
        "runtime_version": "b12423",
    }
    values.update(overrides)
    return ModelMetadata.model_validate(values)


def test_catalog_has_bounded_entries_and_unverified_aliases() -> None:
    entries = catalog()
    assert entries
    assert all(entry.max_params_b <= 9 for entry in entries)
    assert "OpenJEV" in unverified_aliases()
    assert "System One JEV" in unverified_aliases()


def test_admit_rejects_disallowed_license() -> None:
    entry = find("LFM2.5-350M-Q4_K_M")
    assert entry is not None
    with pytest.raises(ModelNotAdmitted, match="license"):
        admit(entry, _metadata(license="GPL-3.0"))


def test_admit_rejects_alias_mismatch_and_missing_checksum() -> None:
    entry = find("LFM2.5-350M-Q4_K_M")
    assert entry is not None
    with pytest.raises(ModelNotAdmitted, match="alias"):
        admit(entry, _metadata(alias="Other"))
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="sha256"):
        _metadata(sha256="abc")


def test_admit_accepts_verified_metadata() -> None:
    entry = find("LFM2.5-350M-Q4_K_M")
    assert entry is not None
    admit(entry, _metadata())


def test_default_eval_passes() -> None:
    report = run_eval()
    assert report.passed
    assert report.ratio == 1.0
    assert all(result.passed for result in report.by_category(EvalCategory.INJECTION))
    assert_release_ready(report)


def test_failed_injection_case_blocks_release() -> None:
    from hcs_trader.agents.eval import EvalReport, EvalResult

    report = EvalReport(
        results=(EvalResult("broken", EvalCategory.INJECTION, False),),
        threshold=0.5,
    )
    with pytest.raises(EvalNotReady, match="Injection"):
        assert_release_ready(report)


def test_failed_ratio_blocks_release() -> None:
    from hcs_trader.agents.eval import EvalReport, EvalResult

    report = EvalReport(
        results=(
            EvalResult("ok", EvalCategory.SCHEMA, True),
            EvalResult("bad", EvalCategory.SCHEMA, False),
        ),
        threshold=0.9,
    )
    with pytest.raises(EvalNotReady, match="below"):
        assert_release_ready(report)


def test_case_exception_counts_as_failure() -> None:
    from hcs_trader.agents.eval import EvalCase

    def broken() -> bool:
        raise RuntimeError("case crashed")

    case = EvalCase("crash", EvalCategory.SCHEMA, broken)
    report = run_eval((case,))
    assert not report.passed
