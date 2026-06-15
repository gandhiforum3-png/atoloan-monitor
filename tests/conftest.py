"""Shared pytest fixtures for the Wave 0 baseline regression net.

These build valid CURRENT-schema objects so the baseline tests can call the
pure-Python routing/threshold/urgency functions without Redis, K8s, or an API
key. No fake-redis fixture here on purpose — the Wave 0 baseline tests only
exercise pure sync functions; the async redis paths are out of scope.
"""

from datetime import datetime, timezone

import pytest

from agent.shared.models import DiagnosisResult, InfraEvent, SignalBundle  # noqa: F401


@pytest.fixture
def make_diagnosis():
    """Factory returning a valid CURRENT-schema DiagnosisResult with overridable kwargs."""

    def _make(**overrides) -> DiagnosisResult:
        defaults = dict(
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
        defaults.update(overrides)
        return DiagnosisResult(**defaults)

    return _make


@pytest.fixture
def make_event():
    """Factory returning a valid InfraEvent with an overridable event_type (and other fields)."""

    def _make(event_type: str = "node_not_ready", **overrides) -> InfraEvent:
        defaults = dict(
            source="test",
            domain="k8s",
            event_type=event_type,
            severity="warning",
            resource_id="node-1",
            timestamp=datetime.now(timezone.utc),
            raw_payload={},
            labels={},
        )
        defaults.update(overrides)
        return InfraEvent(**defaults)

    return _make
