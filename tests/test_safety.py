"""Baseline: safety floor against CURRENT agent.shared.safety.

safety.py is FROZEN — these must hold byte-for-byte after action_type is
opened in plan 01-05. The forbidden-operations set and the hard-stop raise
behavior are the permanent safety contract.
"""

import pytest

from agent.shared.remediation import _meets_threshold
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


# --- Post-D-01 (plan 01-05) floor regressions ---
# These prove that opening DiagnosisResult.action_type from a closed Literal to
# an open str did NOT weaken the hard floor: FORBIDDEN_OPERATIONS still blocks
# every destructive op regardless of the now-open action_type, and the execution
# threshold gate still rejects any unknown action. safety.py itself is FROZEN
# (byte-unchanged this plan); these assertions pin the contract from the caller side.


@pytest.mark.parametrize(
    "operation",
    ["drain_node", "terminate_instance", "drop_table"],
)
def test_forbidden_ops_still_block_after_action_type_opened(operation):
    # The open action_type field does NOT let a forbidden op through the floor.
    with pytest.raises(SafetyViolation):
        safety_check(operation, "x")


def test_unknown_action_never_meets_threshold():
    # An open str lets a novel action validate at the model layer, but the
    # execution threshold gate (THRESHOLDS two-level .get default 1.0) means an
    # unknown action can NEVER meet the threshold — it never executes.
    assert _meets_threshold("k8s", "reboot_node", 0.99) is False
