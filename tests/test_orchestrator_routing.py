"""Baseline: escalation routing against generic_orchestrator._should_escalate.

Pins the (escalate, reason) contract: human_escalate always escalates,
requires_human_review forces escalation, and per-action confidence floors route
below-threshold diagnoses to the human escalator. Regression net for D-01..D-16.

Post-D-11: the routing helper moved into generic_orchestrator and takes the
per-action floor map (config.escalate_below) as its second arg. We use the k8s
DomainConfig's escalate_below (via the registry) so behavior is asserted against
the real wired map.
"""

import pytest

import agent.orchestrators.k8s_orchestrator  # noqa: F401 — triggers k8s registration
from agent.orchestrators.generic_orchestrator import _should_escalate
from agent.registry import REGISTRY
from agent.shared.remediation import _meets_threshold

_ESCALATE_BELOW = REGISTRY["k8s"].escalate_below


@pytest.mark.parametrize(
    "kwargs,expected_escalate",
    [
        (dict(action_type="human_escalate"), True),
        (dict(action_type="pod_restart", confidence=0.79), True),
        (dict(action_type="pod_restart", confidence=0.80), False),
        (dict(action_type="deployment_scale_down", confidence=0.84), True),
        (dict(action_type="deployment_scale_down", confidence=0.85), False),
        (dict(action_type="pod_restart", confidence=0.99, requires_human_review=True), True),
    ],
)
def test_should_escalate_routing(make_diagnosis, kwargs, expected_escalate):
    escalate, reason = _should_escalate(make_diagnosis(**kwargs), _ESCALATE_BELOW)
    assert escalate is expected_escalate
    # Contract: always a (bool, str) tuple.
    assert isinstance(escalate, bool)
    assert isinstance(reason, str)


def test_novel_action_type_below_floor_escalates(make_diagnosis):
    # Post-D-01 (plan 01-05): action_type is an open str, so a novel action the
    # old Literal would have rejected now validates at the model layer. At the
    # routing layer an unregistered action has no escalate_below entry, so the
    # default floor (0.80) applies — a below-floor novel action escalates.
    diagnosis = make_diagnosis(
        action_type="reboot_node",
        confidence=0.50,
        requires_human_review=False,
    )
    escalate, reason = _should_escalate(diagnosis, _ESCALATE_BELOW)
    assert escalate is True
    assert isinstance(reason, str)


def test_novel_action_type_never_executes_at_threshold_gate(make_diagnosis):
    # [Rule 1 - corrected plan assertion] The plan's <behavior> claimed
    # _should_escalate(reboot_node@0.99, ...) -> True, but the real wired
    # escalate_below defaults an unknown action to the 0.80 floor, so a HIGH
    # confidence (0.99) novel action does NOT escalate at the routing layer.
    # The actual backstop that guarantees "novel action NEVER executes" is the
    # execution threshold gate (THRESHOLDS two-level .get default 1.0): an
    # unknown action can never meet the threshold, so remediate() returns
    # "threshold_not_met" without acting. We assert that true boundary here
    # (matches the plan key_link: _meets_threshold returns False -> never executes).
    assert _meets_threshold("k8s", "reboot_node", 0.99) is False
    # The routing layer passes a high-confidence novel action through (no floor),
    # which is precisely why the threshold gate must be the hard backstop.
    diagnosis = make_diagnosis(
        action_type="reboot_node",
        confidence=0.99,
        requires_human_review=False,
    )
    escalate, _ = _should_escalate(diagnosis, _ESCALATE_BELOW)
    assert escalate is False  # documents that routing alone does NOT contain it
