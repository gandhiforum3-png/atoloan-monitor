"""Baseline: DiagnosisResult schema against CURRENT agent.shared.models.

The CURRENT schema has an enum (action_type is a Literal). Plan 01-05 will
replace this test's enum assertion when action_type opens to str. Also pins
confidence bounds (ge=0.0, le=1.0).
"""

import pytest
from pydantic import ValidationError

from agent.shared.models import DiagnosisResult


def _valid_kwargs(**overrides):
    base = dict(
        action_type="pod_restart",
        confidence=0.9,
        requires_human_review=False,
        estimated_blast_radius="service",
        root_cause="x",
        affected_components=["ns/dep"],
        contributing_factors=[],
        confidence_reasoning="x",
        recommended_action="x",
    )
    base.update(overrides)
    return base


def test_current_schema_has_action_type_enum():
    schema = DiagnosisResult.model_json_schema()
    # CURRENT schema has enum (Literal); plan 01-05 will replace this assertion
    # when action_type opens to str.
    assert "enum" in schema["properties"]["action_type"]
    assert set(schema["properties"]["action_type"]["enum"]) == {
        "pod_restart",
        "deployment_scale_down",
        "human_escalate",
        "observe_only",
    }


def test_valid_construct_works():
    result = DiagnosisResult(**_valid_kwargs())
    assert result.action_type == "pod_restart"
    assert result.confidence == 0.9


def test_confidence_upper_bound_enforced():
    with pytest.raises(ValidationError):
        DiagnosisResult(**_valid_kwargs(confidence=1.5))
