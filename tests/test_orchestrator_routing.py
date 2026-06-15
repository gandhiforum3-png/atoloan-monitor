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


# Note: do NOT test an unknown/novel action_type here — the CURRENT model is a
# Literal and pydantic rejects it; that case moves to plan 01-05 after D-01
# opens the action_type field.
