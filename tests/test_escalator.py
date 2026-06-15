"""Baseline: urgency derivation against CURRENT human_escalator._derive_urgency.

Pins the p1/p2/p3 mapping AND the two intentional fall-throughs
(node_network_unavailable, node_deleted are NOT in _URGENCY_MAP -> p3).
"""

import pytest

from agent.skills.remediators.human_escalator import _derive_urgency


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
    assert _derive_urgency([make_event(event_type=event_type)]) == expected


def test_p1_wins_regardless_of_order(make_event):
    signals = [
        make_event(event_type="node_memory_pressure"),
        make_event(event_type="node_not_ready"),
    ]
    assert _derive_urgency(signals) == "p1_immediate"


def test_empty_signals_fall_through(make_event):
    assert _derive_urgency([]) == "p3_within_1h"
