# Atoloan Monitor

## What This Is

Atoloan Monitor is an AI-powered SRE (Site Reliability Engineering) agent that continuously watches Atoloan's AWS infrastructure — Kubernetes pods, EC2 instances, FastAPI backend, and PostgreSQL database — and automatically remediates incidents without human intervention, stopping hard at any destructive action. It is built for Atoloan's production stack first, with an architecture designed to generalize into a configurable product for other teams.

## Core Value

The agent detects, diagnoses, and resolves infrastructure incidents automatically — 24/7 — so engineers are only involved when human judgment is genuinely required.

## Requirements

### Validated

(None yet — ship to validate)

### Active

**Observation**
- [ ] Event-driven monitoring across all 4 layers: Kubernetes, EC2, FastAPI, Postgres — no polling
- [ ] K8s observer: pod restarts, OOMKills, pending pods, node pressure, eviction events, namespace health
- [ ] EC2 observer: CPU, memory, disk, network via CloudWatch — all 3 instances (frontend, backend, postgres)
- [ ] FastAPI observer: endpoint latency p50/p95/p99, error rates, retry storms, slow dependency calls
- [ ] Postgres observer: slow queries, connection pool depth, lock contention, replication lag, index bloat
- [ ] AWS observer: security group rule changes, expiring/stale secrets in Secrets Manager
- [ ] Log analyzer: stream and parse app + system logs for anomaly patterns

**Diagnosis**
- [ ] Root cause analysis correlating signals across all 4 layers using extended thinking
- [ ] Anomaly detection comparing current state to learned per-metric, per-hour-of-day baselines
- [ ] Alert aggregation window (30s debounce) to bundle correlated signals before dispatching RCA
- [ ] Confidence scoring on every diagnosis (threshold configurable, default 0.8)
- [ ] Dependency tracer: map which service is upstream/downstream of a failing component
- [ ] Resource saturation predictor: trend CPU/memory to predict node exhaustion before it happens
- [ ] SLO burn rate calculator: compute error budget consumption rate, project exhaustion time

**Remediation (safe actions only — no delete/drop/destroy)**
- [ ] Pod restarter: restart specific pod or trigger rolling deployment restart
- [ ] Deployment scaler: scale K8s replicas up/down with pre-flight quota and headroom checks
- [ ] Resource limit adjuster: update pod CPU/memory limits in K8s
- [ ] Postgres query killer: terminate blocking/long-running queries after classifying query type (not migrations)
- [ ] Secrets refresher: force-pull latest secret version to pods via rolling restart
- [ ] Circuit breaker: disable/re-enable traffic to a failing service endpoint
- [ ] Human escalator: hard-block any action requiring delete/destroy, surface to dashboard with full diagnosis

**SLO Management**
- [ ] Two-phase operation: Learning mode (days 1–7, observe only, build baselines) → Enforcement mode (alerts + remediation)
- [ ] SLO baseline discovery from observed traffic patterns — no manual configuration required to start
- [ ] Error budget tracking per service with burn rate alerts
- [ ] SLI definition per layer: availability, latency, saturation, error rate

**Dashboard**
- [ ] Real-time infrastructure health view across all 4 layers
- [ ] Live agent action feed: what the agent is observing, diagnosing, and doing right now
- [ ] SLO status panel: current SLIs, error budgets, burn rates per service
- [ ] Incident history: searchable log of all past incidents with full documentation
- [ ] Active incident view: live timeline of ongoing incident + agent reasoning

**Incident Documentation**
- [ ] Auto-generated incident document after every resolved incident
- [ ] Incident timeline: chronological log of all observations and agent actions
- [ ] Root cause summary: what happened, why, which layer it originated in
- [ ] SLO impact record: error budget burned, affected services, duration
- [ ] Postmortem draft: 3–5 follow-up actions to prevent recurrence

**Infrastructure**
- [ ] Monitoring agent runs on dedicated EC2 with IAM role scoped to read + safe operations
- [ ] Domain-partitioned orchestrators: K8s orchestrator, DB orchestrator, Infra orchestrator (run in parallel)
- [ ] Meta-orchestrator handles cross-domain RCA only when signals span multiple domains
- [ ] Prompt caching for infrastructure topology and baseline context (changes slowly, high cache hit rate)

### Out of Scope

- **Destructive operations** (delete pod, terminate instance, drop table, destroy any resource) — permanent hard safety limit, not a v1 constraint
- **Multi-tenant SaaS** — Atoloan-first; generalize after the core agent works reliably
- **External incident doc export** (Notion, Confluence, Jira) — in-dashboard only for v1
- **Slack / email alerting** — web dashboard is the primary interface for v1
- **Mobile app** — web dashboard only
- **CI/CD pipeline monitoring** — out of scope, focus is runtime infrastructure
- **Cost optimization recommendations** — observability first, cost analysis later

## Context

**Atoloan's production infrastructure:**
- 3 EC2 instances: one running ReactJS frontend, one running FastAPI backend, one running PostgreSQL
- Kubernetes cluster managing frontend and backend deployments; pods organized by namespace
- Docker images built for frontend and backend services
- AWS Security Groups controlling network access between services
- AWS Secrets Manager for credentials and configuration secrets
- Monitoring agent will run on a 4th dedicated EC2 instance with an IAM role

**Why this project exists:**
The team needs 24/7 SRE coverage without a dedicated on-call human rotation. The cost and latency of detecting issues manually — and the cognitive load of root cause analysis across 4 infrastructure layers simultaneously — make AI automation the right solution.

**Key architectural insight from design review:**
Polling loops miss fast failures (pod OOMKills and restarts can complete in 15 seconds). Event-driven architecture using K8s Watch API, CloudWatch Alarms, and Prometheus webhooks is required for reliable detection. A single orchestrator becomes a bottleneck under real incident pressure — domain-partitioned orchestrators with a meta-orchestrator for cross-domain correlation is the right design.

**Day-one baseline problem:**
The agent cannot enforce SLOs or detect anomalies until it has observed enough traffic to build a baseline. A mandatory 7-day learning mode before enforcement mode activates is required. This is not a limitation — it is a feature that prevents false-positive alert storms on initial deployment.

## AI Skill Architecture

The agent is built as a hierarchy of Claude-powered skills:

**Layer 1 — Observers** (read-only, event-driven, run continuously)
`k8s-pod-observer`, `k8s-node-observer`, `ec2-metrics-observer`, `fastapi-trace-observer`, `postgres-observer`, `log-analyzer`, `security-group-auditor`, `secrets-health-checker`

**Layer 2 — Diagnosers** (triggered by aggregated signal bundles)
`root-cause-analyzer` (extended thinking), `anomaly-detector`, `slo-burn-calculator`, `dependency-tracer`, `resource-saturation-predictor`

**Layer 3 — Remediators** (gated by confidence score ≥ 0.8, pre-flight checks before every action)
`pod-restarter`, `deployment-scaler`, `resource-limit-adjuster`, `postgres-query-killer`, `secrets-refresher`, `circuit-breaker-toggler`, `human-escalator`

**Layer 4 — Documenters** (run after every incident resolution)
`incident-reporter`, `timeline-builder`, `slo-impact-recorder`, `postmortem-drafter`

## Claude Features Used

| Feature | Role in this system |
|---------|---------------------|
| **Tool use (function calling)** | Every observer and remediator skill — AWS SDK, K8s API, pg_stat queries |
| **Multi-agent / subagents** | Domain orchestrators (K8s, DB, Infra) run as parallel specialized Claude agents |
| **Extended thinking** | Root cause analysis across multi-layer signal correlation |
| **Prompt caching** | Infrastructure topology + baseline context cached across monitoring cycles |
| **Streaming** | Real-time feed of agent observations to the web dashboard |
| **Memory (persistent tool)** | Agent remembers past incidents, recurring failure patterns, per-service baselines |
| **Batch API** | Periodic deep analysis: nightly log review, weekly SLO report generation |
| **Confidence scoring** | Custom tool that scores RCA output before gating remediation |

## Constraints

- **Safety**: No delete, drop, destroy, or terminate operations — ever. Enforced at skill boundary, not orchestrator logic.
- **Auth**: IAM role on dedicated EC2 — no credentials in environment variables or code.
- **Remediation gate**: Auto-remediate only when RCA confidence ≥ 0.8. Below threshold → human escalation with full diagnosis context.
- **Learning period**: 7-day observation-only mode before enforcement activates. Cannot be skipped on first deployment.
- **Scope**: Atoloan's stack (AWS, K8s, FastAPI, Postgres, Docker) — no multi-tenant or generic-infra support in v1.
- **Stack**: Python for agent backend, React for dashboard, Claude API (claude-sonnet-4-6 or claude-opus-4-8 for RCA).
- **K8s scaler pre-flight**: Must check namespace resource quotas and node headroom before any scale-up action.
- **Postgres query killer pre-flight**: Must classify query type (application vs. maintenance) before killing.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Event-driven over polling | Polling misses fast failures (OOMKills resolve in ~15s); K8s Watch API + CloudWatch Alarms provide immediate signal | — Pending |
| Domain-partitioned orchestrators | Single orchestrator bottlenecks under incident pressure; parallel K8s/DB/Infra orchestrators prevent cascade | — Pending |
| Confidence-gated remediation (threshold 0.8) | Wrong diagnosis → wrong fix → worse incident; human escalation below threshold prevents compounding failures | — Pending |
| Two-phase learning/enforcement | Day-one baseline problem: no patterns = alert fatigue or blind monitoring; 7-day learn period required | — Pending |
| Hard-stop at skill boundary | Destructive action check at the skill level, not orchestrator logic — cannot be bypassed by any reasoning chain | — Pending |
| Pre-flight checks on K8s scaler | Scaling up without quota checks can starve other namespaces (including payment-critical services) | — Pending |
| Pre-flight query classification on Postgres killer | Killing a migration query corrupts schema state — context-aware classification required before any kill | — Pending |
| Prompt caching for baselines | Infrastructure topology changes slowly; caching dramatically reduces per-cycle token cost | — Pending |
| Atoloan-first architecture | No multi-tenancy in v1 — generalize after the core agent proves reliable in production | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd:transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-06-03 after initialization*
