"""Pure model-output parse, repair, and schema validation helpers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

TModel = TypeVar("TModel", bound=BaseModel)


@dataclass(frozen=True)
class ModelOutputValidationResult[TModel]:
    value: TModel
    raw_response: str
    parsed_payload: dict[str, Any]
    repaired_payload: dict[str, Any]
    validation_status: str
    repair_attempted: bool
    repair_succeeded: bool
    repair_actions: list[str] = field(default_factory=list)

    def to_attempt_detail(self) -> dict[str, Any]:
        return {
            "validation_status": self.validation_status,
            "repair_attempted": self.repair_attempted,
            "repair_succeeded": self.repair_succeeded,
            "repair_actions": list(self.repair_actions),
        }


@dataclass(frozen=True)
class ModelOutputValidationFailure:
    message: str
    raw_response: str
    stage: str
    parsed_payload: dict[str, Any] | None = None
    repaired_payload: dict[str, Any] | None = None
    validation_errors: list[dict[str, Any]] = field(default_factory=list)
    repair_attempted: bool = False
    repair_succeeded: bool = False
    repair_actions: list[str] = field(default_factory=list)

    def to_attempt_detail(self) -> dict[str, Any]:
        return {
            "validation_status": self.stage,
            "repair_attempted": self.repair_attempted,
            "repair_succeeded": self.repair_succeeded,
            "repair_actions": list(self.repair_actions),
            "validation_errors": list(self.validation_errors),
        }


def strip_code_fences(text: str) -> tuple[str, list[str]]:
    cleaned = text.strip()
    actions: list[str] = []
    if cleaned.startswith("```"):
        newline = cleaned.find("\n")
        if newline != -1:
            cleaned = cleaned[newline + 1 :]
            actions.append("strip_code_fence_start")
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[: -3]
            actions.append("strip_code_fence_end")
    return cleaned.strip(), actions


def extract_json_object(text: str) -> tuple[str, list[str]]:
    cleaned, actions = strip_code_fences(text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        extracted = cleaned[start : end + 1]
        if extracted != cleaned:
            actions.append("extract_json_object")
        return extracted, actions
    return cleaned, actions


def strip_forbidden_fields(
    payload: dict[str, Any], forbidden_fields: Iterable[str]
) -> tuple[dict[str, Any], list[str]]:
    forbidden = set(forbidden_fields)
    stripped = {key: value for key, value in payload.items() if key not in forbidden}
    removed = sorted(set(payload) - set(stripped))
    actions = [f"strip_forbidden_field:{field}" for field in removed]
    return stripped, actions


def validate_json_model_output[TModel](
    *,
    raw_response: str,
    schema_type: type[TModel],
    forbidden_fields: Iterable[str] = (),
) -> ModelOutputValidationResult[TModel] | ModelOutputValidationFailure:
    json_text, actions = extract_json_object(raw_response)
    try:
        parsed = json.loads(json_text)
    except (json.JSONDecodeError, ValueError) as exc:
        return ModelOutputValidationFailure(
            message=f"JSON parse failed: {type(exc).__name__}: {exc}",
            raw_response=raw_response,
            stage="json_parse_failed",
            repair_attempted=bool(actions),
            repair_succeeded=False,
            repair_actions=actions,
        )
    if not isinstance(parsed, dict):
        return ModelOutputValidationFailure(
            message="JSON payload must be an object",
            raw_response=raw_response,
            stage="json_shape_failed",
            parsed_payload={"value": parsed},
            repair_attempted=bool(actions),
            repair_succeeded=False,
            repair_actions=actions,
        )
    repaired, field_actions = strip_forbidden_fields(parsed, forbidden_fields)
    all_actions = [*actions, *field_actions]
    try:
        value = schema_type.model_validate(repaired)
    except ValidationError as exc:
        return ModelOutputValidationFailure(
            message="Schema validation failed",
            raw_response=raw_response,
            stage="schema_validation_failed",
            parsed_payload=parsed,
            repaired_payload=repaired,
            validation_errors=exc.errors(include_url=False),
            repair_attempted=bool(all_actions),
            repair_succeeded=False,
            repair_actions=all_actions,
        )
    return ModelOutputValidationResult(
        value=value,
        raw_response=raw_response,
        parsed_payload=parsed,
        repaired_payload=repaired,
        validation_status="repair_succeeded" if all_actions else "passed",
        repair_attempted=bool(all_actions),
        repair_succeeded=bool(all_actions),
        repair_actions=all_actions,
    )
