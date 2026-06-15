"""Baseline: threshold gating + target parsing.

Pins per-domain/per-action confidence thresholds (now in agent.shared.remediation)
and 'namespace/deployment' parsing (still in node_remediator). Must stay green after
every later extraction (D-03..D-16).
"""

import pytest

from agent.shared.remediation import (
    THRESHOLDS,
    PreflightResult,
    _log_action,
    _meets_threshold,
)
from agent.skills.remediators.node_remediator import _parse_target


def test_thresholds_current_values():
    assert THRESHOLDS["k8s"]["pod_restart"] == 0.80
    assert THRESHOLDS["k8s"]["deployment_scale_down"] == 0.85
    assert THRESHOLDS["k8s"]["human_escalate"] == 0.00


def test_thresholds_domain_keyed_shape():
    # THRESHOLDS is a nested per-domain dict: THRESHOLDS[domain][action] -> float.
    assert isinstance(THRESHOLDS, dict)
    assert isinstance(THRESHOLDS["k8s"], dict)
    assert all(isinstance(v, float) for v in THRESHOLDS["k8s"].values())


@pytest.mark.parametrize(
    "domain,action_type,confidence,expected",
    [
        ("k8s", "pod_restart", 0.80, True),
        ("k8s", "pod_restart", 0.79, False),
        ("k8s", "deployment_scale_down", 0.85, True),
        ("k8s", "deployment_scale_down", 0.84, False),
        # unknown action with valid domain -> .get(action, 1.0) -> can never meet
        ("k8s", "bogus_action", 0.99, False),
        # unknown domain -> .get(domain, {}).get(action, 1.0) -> can never meet, never raises
        ("bogus_domain", "pod_restart", 0.99, False),
    ],
)
def test_meets_threshold_matrix(domain, action_type, confidence, expected):
    assert _meets_threshold(domain, action_type, confidence) is expected


def test_log_action_is_importable_async():
    # _log_action now lives in shared; verify it is the coroutine function we expect.
    import inspect

    assert inspect.iscoroutinefunction(_log_action)


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
