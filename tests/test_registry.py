"""Registry construction + k8s wiring assertions.

Importing agent.orchestrators.k8s_orchestrator registers the k8s domain. These
assertions pin the runtime keys (stream/group), both threshold dicts kept
separate (escalate_below carries the 1.1 sentinels, distinct from THRESHOLDS),
the exact 4-entry urgency map (the node_network_unavailable gap preserved), and
that every wired callable is present.
"""

import agent.orchestrators.k8s_orchestrator  # noqa: F401 — triggers registration

from agent.registry import REGISTRY


def test_k8s_domain_registered():
    assert "k8s" in REGISTRY


def test_k8s_runtime_keys_preserved():
    cfg = REGISTRY["k8s"]
    # Runtime keys must stay byte-identical or Redis consumer offsets strand.
    assert cfg.stream == "events:k8s"
    assert cfg.consumer_group == "k8s-orchestrator"


def test_k8s_escalate_below_sentinels():
    cfg = REGISTRY["k8s"]
    # escalate_below (routing) is distinct from THRESHOLDS (execution gate) — it
    # carries the 1.1 "always escalate" sentinels. Do NOT merge the two dicts.
    assert cfg.escalate_below["pod_restart"] == 0.80
    assert cfg.escalate_below["deployment_scale_down"] == 0.85
    assert cfg.escalate_below["human_escalate"] == 1.1
    assert cfg.escalate_below["observe_only"] == 1.1


def test_k8s_urgency_map_exact_with_gap_preserved():
    cfg = REGISTRY["k8s"]
    assert cfg.urgency_map["node_not_ready"] == "p1_immediate"
    assert cfg.urgency_map["node_memory_pressure"] == "p2_within_15m"
    assert cfg.urgency_map["node_disk_pressure"] == "p2_within_15m"
    assert cfg.urgency_map["node_pid_pressure"] == "p2_within_15m"
    # The intentional gap — these fall through to p3_within_1h, never mapped.
    assert "node_network_unavailable" not in cfg.urgency_map
    assert "node_deleted" not in cfg.urgency_map
    # Exactly the original 4 entries, no new ones filled in.
    assert len(cfg.urgency_map) == 4


def test_k8s_callables_wired():
    cfg = REGISTRY["k8s"]
    assert callable(cfg.observer)
    assert callable(cfg.diagnose)
    assert callable(cfg.remediate)
    assert callable(cfg.run_incident)
    assert callable(cfg.context_fetcher)
