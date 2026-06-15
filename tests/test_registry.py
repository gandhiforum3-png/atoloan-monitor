"""Registry construction + k8s wiring assertions.

Importing agent.orchestrators.k8s_orchestrator registers the k8s domain. These
assertions pin the runtime keys (stream/group), both threshold dicts kept
separate (escalate_below carries the 1.1 sentinels, distinct from THRESHOLDS),
the exact 4-entry urgency map (the node_network_unavailable gap preserved), and
that every wired callable is present.
"""

import importlib.util
import os

import pytest

import agent.orchestrators.k8s_orchestrator  # noqa: F401 — triggers registration

from agent.registry import REGISTRY


def _load_run_local():
    """Import scripts/run_local.py as a module so we can unit-test parse_monitors.

    scripts/ isn't a package; load it by file path. The module's top-level
    `sys.path.insert(...)` + registration imports run on load, which also
    guarantees the k8s domain is registered.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "scripts", "run_local.py")
    spec = importlib.util.spec_from_file_location("run_local", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_local = _load_run_local()


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


# --- run_local.py --monitors parsing (D-14) -------------------------------


def test_parse_monitors_none_returns_all_registered():
    # Default (no --monitors) -> every registered domain, so the runner
    # auto-starts each newly registered DomainConfig with no flag change.
    assert run_local.parse_monitors(None) == list(REGISTRY.keys())
    assert "k8s" in run_local.parse_monitors(None)


def test_parse_monitors_single_domain():
    assert run_local.parse_monitors("k8s") == ["k8s"]


def test_parse_monitors_strips_whitespace_and_empty_entries():
    # "k8s, " tolerates whitespace and trailing empty comma segments.
    assert run_local.parse_monitors(" k8s , ") == ["k8s"]


def test_parse_monitors_empty_string_returns_all():
    assert run_local.parse_monitors("") == list(REGISTRY.keys())


def test_parse_monitors_unknown_domain_exits(capsys):
    # T-01-17: an unknown --monitors domain must fail loud (sys.exit(1) with the
    # available list), never silently no-op.
    with pytest.raises(SystemExit) as exc:
        run_local.parse_monitors("nope")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "unknown monitor domain" in out
    assert "k8s" in out  # available list shown
