"""Baseline: safety floor against CURRENT agent.shared.safety.

safety.py is FROZEN — these must hold byte-for-byte after action_type is
opened in plan 01-05. The forbidden-operations set and the hard-stop raise
behavior are the permanent safety contract.
"""

import pytest

from agent.shared.safety import (
    FORBIDDEN_OPERATIONS,
    SafetyViolation,
    safety_check,
)


@pytest.mark.parametrize(
    "operation",
    [
        "delete_pod",
        "terminate_instance",
        "drop_table",
        "drain_node",
        "cordon_node",
    ],
)
def test_forbidden_operations_present(operation):
    assert operation in FORBIDDEN_OPERATIONS


def test_safe_operation_passes():
    assert safety_check("pod_restart", "ns/dep") is None


@pytest.mark.parametrize(
    "operation",
    ["delete_pod", "terminate_instance", "drain_node"],
)
def test_forbidden_operation_raises(operation):
    with pytest.raises(SafetyViolation):
        safety_check(operation, "x")
