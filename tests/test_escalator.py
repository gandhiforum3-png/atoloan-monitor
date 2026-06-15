"""Baseline: urgency derivation against human_escalator._derive_urgency.

Pins the p1/p2/p3 mapping AND the two intentional fall-throughs
(node_network_unavailable, node_deleted are NOT in the k8s urgency map -> p3).

Post-D-13: _derive_urgency takes the urgency_map as an explicit second arg.
The k8s map is defined inline here (verbatim the original 4 entries).
"""

import pytest

from agent.skills.remediators.human_escalator import _derive_urgency

# The k8s urgency map — verbatim the original 4 entries (now lives in the
# k8s DomainConfig.urgency_map; defined inline here for the baseline test).
K8S_URGENCY_MAP = {
    "node_not_ready": "p1_immediate",
    "node_memory_pressure": "p2_within_15m",
    "node_disk_pressure": "p2_within_15m",
    "node_pid_pressure": "p2_within_15m",
}


@pytest.mark.parametrize(
    "event_type,expected",
    [
        ("node_not_ready", "p1_immediate"),
        ("node_memory_pressure", "p2_within_15m"),
        ("node_disk_pressure", "p2_within_15m"),
        ("node_pid_pressure", "p2_within_15m"),
        # CURRENT behavior — network_unavailable/node_deleted intentionally fall
        # through; must be preserved through D-13.
        ("node_network_unavailable", "p3_within_1h"),
        ("node_deleted", "p3_within_1h"),
    ],
)
def test_derive_urgency_single_signal(make_event, event_type, expected):
    assert _derive_urgency([make_event(event_type=event_type)], K8S_URGENCY_MAP) == expected


def test_p1_wins_regardless_of_order(make_event):
    signals = [
        make_event(event_type="node_memory_pressure"),
        make_event(event_type="node_not_ready"),
    ]
    assert _derive_urgency(signals, K8S_URGENCY_MAP) == "p1_immediate"


def test_empty_signals_fall_through(make_event):
    assert _derive_urgency([], K8S_URGENCY_MAP) == "p3_within_1h"


def test_empty_map_falls_through_to_p3(make_event):
    # Default-safe behavior: an empty urgency_map means everything falls through
    # to p3_within_1h, even a normally-p1 event.
    assert _derive_urgency([make_event(event_type="node_not_ready")], {}) == "p3_within_1h"
