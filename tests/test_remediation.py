"""Baseline: threshold gating + target parsing against CURRENT node_remediator.

Pins per-action confidence thresholds and 'namespace/deployment' parsing of the
unmodified code. Must stay green after every later extraction (D-03..D-16).
"""

import pytest

from agent.skills.remediators.node_remediator import (
    THRESHOLDS,
    PreflightResult,
    _meets_threshold,
    _parse_target,
)


def test_thresholds_current_values():
    assert THRESHOLDS["pod_restart"] == 0.80
    assert THRESHOLDS["deployment_scale_down"] == 0.85
    assert THRESHOLDS["human_escalate"] == 0.00


@pytest.mark.parametrize(
    "action_type,confidence,expected",
    [
        ("pod_restart", 0.80, True),
        ("pod_restart", 0.79, False),
        ("deployment_scale_down", 0.85, True),
        ("deployment_scale_down", 0.84, False),
        # unknown action -> THRESHOLDS.get(..., 1.0) -> can never meet
        ("bogus_action", 0.99, False),
    ],
)
def test_meets_threshold_matrix(action_type, confidence, expected):
    assert _meets_threshold(action_type, confidence) is expected


@pytest.mark.parametrize(
    "components,expected",
    [
        (["atoloan-backend-prod/atoloan-api"], ("atoloan-backend-prod", "atoloan-api")),
        (["no-slash-here"], None),
        ([], None),
    ],
)
def test_parse_target(components, expected):
    assert _parse_target(components) == expected


def test_preflight_result_shape():
    assert PreflightResult(False, "x").ok is False
    assert PreflightResult(True).reason == ""
