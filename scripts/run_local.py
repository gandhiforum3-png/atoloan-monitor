#!/usr/bin/env python3
"""
Local development runner — full end-to-end pipeline for k8s-node-observer.

Starts three concurrent tasks:
  1. k8s-node-observer   — watches minikube node conditions → events:k8s
  2. k8s-orchestrator    — debounces signals → Claude diagnosis → escalate/remediate
  3. stream-consumer     — tails events:k8s + escalations:k8s → prints to terminal

Usage:
    python scripts/run_local.py                       # normal mode (5s debounce)
    python scripts/run_local.py --verbose             # log every watch event
    python scripts/run_local.py --debounce 10         # change debounce window
    python scripts/run_local.py --fix                 # enable real auto-fix (learning_mode=False)
    python scripts/run_local.py --agent               # use the agentic tool-use loop instead of diagnoser+remediator
    python scripts/run_local.py --agent --fix         # agent loop with real execution enabled

Prerequisites:
    1. minikube:          minikube status
    2. Redis:             docker run -d --name atoloan-redis -p 6379:6379 redis:7-alpine
    3. API key:           export ANTHROPIC_API_KEY=sk-ant-...
    4. Deps:              pip install -r requirements-monitor.txt
"""

import argparse
import asyncio
import json
import logging
import os
import sys

import anthropic
import structlog
from redis.asyncio import Redis

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.observers.k8s_node_observer import watch_nodes
from agent.orchestrators.k8s_orchestrator import run as run_orchestrator
from agent.shared.event_bus import tail

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

_C = {
    "critical": "\033[91m",
    "warning":  "\033[93m",
    "info":     "\033[94m",
    "reset":    "\033[0m",
    "bold":     "\033[1m",
    "dim":      "\033[2m",
    "green":    "\033[92m",
    "magenta":  "\033[95m",
}


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
    )


async def _tail_events(redis: Redis) -> None:
    """Print incoming node events from the observer."""
    print(f"\n{_C['dim']}[events:k8s] Tailing — waiting for node events...{_C['reset']}\n")
    async for event in tail(redis, "k8s"):
        sev = event["severity"]
        color = _C.get(sev, "")
        p = event["payload"]
        print(
            f"{color}{_C['bold']}  ▶ OBSERVER [{sev.upper()}] {event['event_type']}{_C['reset']}\n"
            f"    node      : {event['resource_id']}\n"
            f"    condition : {p.get('condition_type')} = {p.get('status')}\n"
            f"    reason    : {p.get('reason')}\n"
            f"    stream_id : {event['stream_id']}\n"
        )


async def _tail_escalations(redis: Redis) -> None:
    """Print escalation packets from the orchestrator."""
    print(f"{_C['dim']}[escalations:k8s] Tailing — waiting for escalations...{_C['reset']}\n")
    last_id = b"$"
    while True:
        entries = await redis.xread({"escalations:k8s": last_id}, count=10, block=1000)
        for _, messages in entries:
            for msg_id, fields in messages:
                last_id = msg_id
                urgency = fields[b"urgency"].decode()
                urgency_color = _C["critical"] if urgency == "p1_immediate" else _C["warning"]
                print(
                    f"\n{urgency_color}{_C['bold']}  ⚠ ESCALATION [{urgency.upper()}]{_C['reset']}\n"
                    f"    incident_id       : {fields[b'incident_id'].decode()}\n"
                    f"    root_cause        : {fields[b'root_cause'].decode()[:140]}\n"
                    f"    confidence        : {fields[b'confidence'].decode()}\n"
                    f"    recommended_action: {fields[b'recommended_action'].decode()}\n"
                    f"    why_escalated     : {fields[b'why_escalated'].decode()}\n"
                )


async def _tail_actions(redis: Redis) -> None:
    """Print actions:log entries from the remediator."""
    last_id = b"$"
    while True:
        entries = await redis.xread({"actions:log": last_id}, count=10, block=1000)
        for _, messages in entries:
            for msg_id, fields in messages:
                last_id = msg_id
                action = fields[b"action"].decode()
                status = fields[b"status"].decode()
                # Skip escalation entries (already shown by _tail_escalations)
                if action == "human_escalate":
                    continue
                color = _C["green"] if "blocked" not in status else _C["magenta"]
                print(
                    f"\n{color}{_C['bold']}  ⚙ ACTION [{status.upper()}]{_C['reset']}\n"
                    f"    incident_id : {fields[b'incident_id'].decode()}\n"
                    f"    action      : {action}\n"
                    f"    confidence  : {fields[b'confidence'].decode()}\n"
                    f"    detail      : {fields.get(b'detail', b'').decode()[:140]}\n"
                )


async def main(verbose: bool, debounce: int, learning_mode: bool, mode: str) -> None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("\n[error] ANTHROPIC_API_KEY is not set.")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    redis = Redis.from_url(REDIS_URL, decode_responses=False)
    try:
        await redis.ping()
        print(f"[redis] Connected to {REDIS_URL}")
    except Exception as exc:
        print(f"\n[redis] Cannot connect to {REDIS_URL}: {exc}")
        print("  Start Redis first:\n    docker run -d --name atoloan-redis -p 6379:6379 redis:7-alpine\n")
        sys.exit(1)

    anthropic_client = anthropic.AsyncAnthropic(api_key=api_key)

    print(f"[runner] Starting full end-to-end pipeline (debounce={debounce}s, learning_mode={learning_mode}, mode={mode})")
    print( "[runner] Press Ctrl+C to stop\n")

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(
                watch_nodes(redis, verbose=verbose),
                name="k8s-node-observer",
            )
            tg.create_task(
                run_orchestrator(redis, anthropic_client, debounce_seconds=debounce, learning_mode=learning_mode, mode=mode),
                name="k8s-orchestrator",
            )
            tg.create_task(_tail_events(redis),       name="tail-events")
            tg.create_task(_tail_escalations(redis),  name="tail-escalations")
            tg.create_task(_tail_actions(redis),      name="tail-actions")
    except* KeyboardInterrupt:
        print("\n[runner] Stopped.")
    finally:
        await redis.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run k8s-node-observer end-to-end locally")
    parser.add_argument("--verbose",  "-v", action="store_true", help="Log every watch event")
    parser.add_argument("--debounce", "-d", type=int, default=5,
                        help="Signal debounce window in seconds (default: 5 for local testing)")
    parser.add_argument("--fix", action="store_true",
                        help="Enable real auto-fix execution (learning_mode=False). Default: learning mode only.")
    parser.add_argument("--agent", action="store_true",
                        help="Use the agentic tool-use loop (node_agent) instead of the diagnoser+remediator pipeline.")
    args = parser.parse_args()
    _configure_logging(args.verbose)
    mode = "agent" if args.agent else "diagnoser"
    asyncio.run(main(args.verbose, args.debounce, not args.fix, mode))
