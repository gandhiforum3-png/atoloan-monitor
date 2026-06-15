"""
Domain registry — the single wiring point for monitor domains.

A new monitor domain is added by constructing one ``DomainConfig`` and calling
``register(cfg)``. The generic orchestrator (``agent/orchestrators/generic_orchestrator.py``)
consumes a ``DomainConfig`` and drives the consume/debounce/bundle/dispatch loop
for any domain — no per-domain orchestrator copy-paste.

This module deliberately imports NOTHING from ``agent.skills`` or
``agent.observers``: the registry is consumed BY the domain orchestrators
(e.g. k8s_orchestrator), not the reverse. Keeping the dependency one-directional
avoids a circular import. It also carries NO ``kubernetes_asyncio`` import — the
K8s-specific context fetch is injected via ``context_fetcher``.
"""

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


@dataclass
class DomainConfig:
    """Everything the generic orchestrator needs to run one domain.

    Fields:
        domain:          Domain key ("k8s", "db", ...). Used for InfraEvent.domain,
                         SignalBundle.domain, escalation routing.
        stream:          Redis stream the observer publishes to (runtime key).
        consumer_group:  Redis consumer group for this orchestrator (runtime key).
        observer:        Observer entrypoint coroutine (publishes events to ``stream``).
        diagnose:        One-shot diagnoser coroutine (client, bundle, context) -> result.
        remediate:       Remediator coroutine (redis, *, incident_id, diagnosis, signals,
                         learning_mode).
        run_incident:    Agentic tool-use loop coroutine (anthropic_client, redis, bundle,
                         incident_id, *, learning_mode).
        escalate_below:  Per-action confidence floor map (the routing _ESCALATE_BELOW map;
                         distinct from THRESHOLDS — do NOT merge).
        urgency_map:     Per-domain event_type -> urgency string map for escalation.
        context_fetcher: Optional domain-specific context fetcher (e.g. fetch pods on
                         affected nodes); None for domains that need no extra context.
    """

    domain: str
    stream: str
    consumer_group: str
    observer: Callable[..., Awaitable[None]]
    diagnose: Callable[..., Awaitable]
    remediate: Callable[..., Awaitable]
    run_incident: Callable[..., Awaitable]
    escalate_below: dict[str, float]
    urgency_map: dict[str, str]
    context_fetcher: Optional[Callable[..., Awaitable[list[dict]]]] = None


REGISTRY: dict[str, DomainConfig] = {}


def register(cfg: DomainConfig) -> None:
    """Register a domain config under its domain key."""
    REGISTRY[cfg.domain] = cfg
