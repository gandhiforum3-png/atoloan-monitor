"""
Hard-stop safety layer.

Every remediator must call safety_check() before making any API call.
This runs at the Python function boundary — it cannot be bypassed by
any orchestrator logic or LLM output.
"""

FORBIDDEN_OPERATIONS: frozenset[str] = frozenset([
    "delete_pod",
    "delete_deployment",
    "delete_namespace",
    "delete_node",
    "terminate_instance",
    "stop_instance",
    "drop_table",
    "truncate_table",
    "delete_database",
    "delete_secret",
    "delete_security_group",
    "revoke_iam",
    "delete_role",
    "force_delete",
    "drain_node",       # draining is destructive — escalate instead
    "cordon_node",      # scheduling change — escalate instead
])


class SafetyViolation(Exception):
    """Raised when a forbidden operation is attempted."""


def safety_check(operation: str, target: str) -> None:
    """
    Raise SafetyViolation unconditionally for any forbidden operation.

    This is not a policy check — it cannot be overridden by configuration,
    feature flags, or confidence scores.
    """
    if operation in FORBIDDEN_OPERATIONS:
        raise SafetyViolation(
            f"BLOCKED: '{operation}' on '{target}' is permanently forbidden. "
            "This check cannot be overridden."
        )
