from __future__ import annotations

from cctv_memory.contracts.vlm import VlmObservationOutput
from cctv_memory.services.model_output_validation import (
    ModelOutputValidationFailure,
    ModelOutputValidationResult,
    validate_json_model_output,
)


def _validate(
    raw: str,
) -> ModelOutputValidationResult[VlmObservationOutput] | ModelOutputValidationFailure:
    return validate_json_model_output(
        raw_response=raw,
        schema_type=VlmObservationOutput,
        forbidden_fields={"access_policy_id", "security_level", "camera_id"},
    )


def test_extracts_fenced_json_and_validates() -> None:
    result = _validate(
        '```json\n{"static":"s","dynamic":"d","tags":["person"],'
        '"quality":{"reason":"","score":0.8},"attr":{"alert":false}}\n```'
    )
    assert isinstance(result, ModelOutputValidationResult)
    assert result.value.static == "s"
    assert "strip_code_fence_start" in result.repair_actions


def test_strips_forbidden_fields_without_inventing_content() -> None:
    result = _validate(
        '{"static":"s","dynamic":"d","tags":[],'
        '"quality":{"reason":"","score":0.8},"attr":{"alert":false},'
        '"security_level":"restricted","camera_id":"cam_bad"}'
    )
    assert isinstance(result, ModelOutputValidationResult)
    assert result.validation_status == "repair_succeeded"
    assert result.repaired_payload.get("security_level") is None
    assert "strip_forbidden_field:camera_id" in result.repair_actions


def test_forbidden_field_repair_does_not_hide_missing_required_field() -> None:
    result = _validate(
        '{"static":"s","tags":[],"quality":{"reason":"","score":0.8},'
        '"attr":{"alert":false},"security_level":"restricted"}'
    )
    assert isinstance(result, ModelOutputValidationFailure)
    assert result.stage == "schema_validation_failed"
    assert result.repair_attempted is True
    assert any(err.get("loc") == ("dynamic",) for err in result.validation_errors)


def test_preserves_full_raw_response_on_parse_failure() -> None:
    raw = "not json at all with provider text"
    result = _validate(raw)
    assert isinstance(result, ModelOutputValidationFailure)
    assert result.stage == "json_parse_failed"
    assert result.raw_response == raw
