#!/usr/bin/env python3
"""
Simulate K8s node conditions on minikube for local testing.

Patches node status conditions directly via the K8s API using your
local kubeconfig (minikube admin). The observer will detect the change
within 1–2 seconds. Kubelet will restore the real value in ~10–20s.

Usage:
    python scripts/simulate_node_pressure.py show
    python scripts/simulate_node_pressure.py pressure-on [--condition MemoryPressure]
    python scripts/simulate_node_pressure.py pressure-off [--condition MemoryPressure]
    python scripts/simulate_node_pressure.py cycle [--condition MemoryPressure] [--hold 8]

Commands:
    show         Print all current node conditions
    pressure-on  Set the condition to True (simulate the problem)
    pressure-off Restore the condition to False (normal state)
    cycle        Turn on, hold N seconds, turn off (good for demos)
"""

import argparse
import asyncio
import json
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kubernetes_asyncio import client, config

_RESTORE_REASONS = {
    "MemoryPressure": ("KubeletHasSufficientMemory", "kubelet has sufficient memory available"),
    "DiskPressure": ("KubeletHasNoDiskPressure", "kubelet has no disk pressure"),
    "PIDPressure": ("KubeletHasSufficientPID", "kubelet has sufficient PID available"),
}


async def _get_condition_index(v1: client.CoreV1Api, node_name: str, cond_type: str) -> int | None:
    node = await v1.read_node_status(node_name)
    for i, cond in enumerate(node.status.conditions or []):
        if cond.type == cond_type:
            return i
    return None


async def show_conditions(node_name: str) -> None:
    await config.load_kube_config()
    async with client.ApiClient() as api:
        v1 = client.CoreV1Api(api)
        node = await v1.read_node_status(node_name)
        print(f"\nNode: {node_name}")
        print(f"  {'Condition':<20} {'Status':<10} {'Reason':<35} Message")
        print("  " + "─" * 100)
        for cond in node.status.conditions or []:
            msg = (cond.message or "")[:60]
            print(f"  {cond.type:<20} {cond.status:<10} {(cond.reason or ''):<35} {msg}")
        print()


async def patch_condition(
    node_name: str,
    cond_type: str,
    status: str,
    reason: str,
    message: str,
) -> None:
    await config.load_kube_config()
    async with client.ApiClient() as api:
        v1 = client.CoreV1Api(api)
        idx = await _get_condition_index(v1, node_name, cond_type)
        if idx is None:
            print(f"  Condition '{cond_type}' not found on node '{node_name}'.")
            print("  Run 'show' to see available conditions.")
            sys.exit(1)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        patch = [
            {"op": "replace", "path": f"/status/conditions/{idx}/status", "value": status},
            {"op": "replace", "path": f"/status/conditions/{idx}/reason", "value": reason},
            {"op": "replace", "path": f"/status/conditions/{idx}/message", "value": message},
            {"op": "replace", "path": f"/status/conditions/{idx}/lastHeartbeatTime", "value": now},
            {"op": "replace", "path": f"/status/conditions/{idx}/lastTransitionTime", "value": now},
        ]
        await v1.patch_node_status(
            node_name,
            patch,
            _content_type="application/json-patch+json",
        )
        marker = "ON  ▲" if status == "True" else "OFF ▼"
        print(f"  [{marker}] {node_name}: {cond_type} = {status}  ({reason})")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate K8s node conditions for local testing")
    parser.add_argument(
        "action",
        choices=["show", "pressure-on", "pressure-off", "cycle"],
        help="Action to perform",
    )
    parser.add_argument("--node", default="minikube", help="Node name (default: minikube)")
    parser.add_argument(
        "--condition",
        default="MemoryPressure",
        choices=["MemoryPressure", "DiskPressure", "PIDPressure"],
        help="Node condition to simulate (default: MemoryPressure)",
    )
    parser.add_argument(
        "--hold",
        type=int,
        default=8,
        help="Seconds to hold the condition in 'cycle' mode (default: 8)",
    )
    args = parser.parse_args()

    if args.action == "show":
        await show_conditions(args.node)
        return

    if args.action == "pressure-on":
        await patch_condition(
            args.node,
            args.condition,
            status="True",
            reason="TestSimulation",
            message=f"[atoloan-monitor test] Simulated {args.condition} — run pressure-off to restore",
        )
        return

    if args.action == "pressure-off":
        reason, message = _RESTORE_REASONS[args.condition]
        await patch_condition(args.node, args.condition, status="False", reason=reason, message=message)
        return

    if args.action == "cycle":
        print(f"\n[simulate] Cycling {args.condition} on node '{args.node}'")
        print(f"[simulate] ON for {args.hold}s → OFF\n")

        await patch_condition(
            args.node,
            args.condition,
            status="True",
            reason="TestSimulation",
            message=f"[atoloan-monitor test] Simulated {args.condition}",
        )
        print(f"  Holding for {args.hold}s — watch the observer log...")
        await asyncio.sleep(args.hold)

        reason, message = _RESTORE_REASONS[args.condition]
        await patch_condition(args.node, args.condition, status="False", reason=reason, message=message)
        print("\n[simulate] Done. Kubelet will confirm the restore shortly.")


if __name__ == "__main__":
    asyncio.run(main())
