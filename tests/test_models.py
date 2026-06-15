"""DiagnosisResult schema — post-D-01 (action_type opened to an open str).

D-01/D-02 (plan 01-05) intentionally FLIPPED the original baseline expectation:
the shared model's JSON schema no longer carries an action_type enum (it was a
closed Literal before). A novel action_type now validates at the model layer;
the boundary (threshold + safety_check) is what contains it. Confidence bounds
(ge=0.0, le=1.0) are unchanged.
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


def test_schema_action_type_has_no_enum():
    # FLIPPED by D-01: the prior baseline asserted `"enum" in ...`; the shared
    # model's action_type is now an open str, so its JSON schema must carry NO
    # enum key. Per-domain diagnosers re-inject their own enum into their tool
    # input_schema; the shared model stays open.
    schema = DiagnosisResult.model_json_schema()
    assert "enum" not in schema["properties"]["action_type"]
    assert schema["properties"]["action_type"]["type"] == "string"


def test_novel_action_type_validates():
    # Post-D-01: a novel action_type the old Literal forbade now constructs fine.
    result = DiagnosisResult(**_valid_kwargs(action_type="reboot_node"))
    assert result.action_type == "reboot_node"


def test_valid_construct_works():
    result = DiagnosisResult(**_valid_kwargs())
    assert result.action_type == "pod_restart"
    assert result.confidence == 0.9


def test_confidence_upper_bound_enforced():
    with pytest.raises(ValidationError):
        DiagnosisResult(**_valid_kwargs(confidence=1.5))
