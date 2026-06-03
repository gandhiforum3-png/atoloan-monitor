# Research Summary — Atoloan Monitor

**Synthesized:** 2026-06-03
**Sources:** REFERENCE-AGENTS.md, SKILLS-AND-FEATURES.md, ARCHITECTURE.md, PITFALLS.md, PROJECT.md
**Overall confidence:** HIGH — all five research files converge on consistent recommendations; medium-confidence items are explicitly flagged.

---

## TL;DR

- **Build an event-driven, domain-partitioned agent, not a polling loop.** K8s Watch API + CloudWatch EventBridge + Prometheus Alertmanager webhooks replace polling entirely. This is non-negotiable for catching fast failures like OOMKills (15-second lifecycle).
- **The 20-skill, 4-layer architecture (Observers → Diagnosers → Remediators → Documenters) is the right structure.** Each layer has clear input/output contracts; model assignments are settled (Haiku for classification, Sonnet for domain RCA, Opus+extended thinking for cross-domain RCA).
- **Confidence-gated remediation at 0.8 is the correct default, but thresholds must be per-action** — 0.90 for PostgreSQL query kills and circuit breaker toggles, never auto-remediate on <0.8.
- **The 7-day learning mode is mathematically justified** (420+ samples per hour-of-day slot) but requires contamination protection from day one — eviction events and rolling deployments during learning corrupt baselines silently.
- **Five critical safety patterns must be in Phase 1 infrastructure or Phase 3 remediation from day one** — they cannot be retrofitted: DDL query classifier, PDB pre-flight, two-dimensional quota check, per-pod restart cooldown, and the hard-stop FORBIDDEN_OPERATIONS blocklist at skill boundary.
- **All 5 reference agents are detect-or-diagnose only.** None of them close the loop: observe → diagnose → act → document. This is Atoloan Monitor's market differentiator and the reason building it is justified over adopting a commercial tool.
- **The biggest unresolved operational risk is baseline contamination and gradual drift.** A dual-baseline model (current adaptive + reference pinned snapshot) is required in the data model from Phase 1; adding it later requires a full schema migration.

---

## Stack Recommendation

Pick exactly these libraries. No alternatives — research converged on specific choices.

### Python Agent Backend

| Library | Version | Rationale |
|---------|---------|-----------|
| `fastapi` | `>=0.110` | Single process serves agent API, webhook receivers, SSE streams, and WebSocket |
| `anthropic` (AsyncAnthropic) | `>=0.30` | Native async Claude client — never use the sync Anthropic() in asyncio context |
| `kubernetes-asyncio` | `29.x` (match cluster K8s version) | Non-blocking K8s Watch API — the sync `kubernetes` client blocks the event loop |
| `aioboto3` | `>=13.0` | Non-blocking AWS SDK — `boto3` blocks the event loop for 100–500ms per call |
| `asyncpg` | `>=0.29` | Native async Postgres driver — faster binary protocol, pool of max 3 connections |
| `redis[hiredis]` | `>=5.0` | Redis Streams event bus + hot baseline cache + shared state; hiredis for speed |
| `pydantic` | `v2` | Structured output schemas for RCA, confidence scoring, pre-flight results |
| `structlog` | `>=24.0` | Structured JSON logging with automatic context binding per incident |
| `numpy` / `scipy` | latest stable | EWMA + percentile baseline calculations; trend regression for saturation predictor |
| `opentelemetry-sdk` | `>=1.25` | Trace agent actions end-to-end through the K8s/AWS/DB call chain |
| `apscheduler` | `>=3.10` | Cron-style scheduling for nightly batch jobs (log review, weekly SLO reports) |

### Data Layer

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Primary DB | PostgreSQL 15+ with TimescaleDB extension | Incident records, baselines, postmortems — one DB; TimescaleDB hypertables for time-series |
| Hot cache | Redis 7+ (Streams + Hash + Sorted Set) | Event bus, baseline cache, dedup, shared orchestrator state |
| Baseline storage | TimescaleDB continuous aggregates | Hourly p50/p95/p99 auto-materialized; 10–100x faster window queries than plain Postgres |

Confirm TimescaleDB extension availability before Phase 1 commits to it. If unavailable, fall back to plain Postgres with manual monthly partitioning.

### Frontend Dashboard

| Library | Choice | Rationale |
|---------|--------|-----------|
| Framework | React 18+ | Specified in PROJECT.md |
| Global state | Zustand | Lighter than Redux; handles real-time mutations cleanly |
| Server state | TanStack Query (React Query) | Polling/caching for non-streaming data (incident list, SLO status) |
| Real-time feed | Native `EventSource` API | SSE for one-way agent action feed — auto-reconnect built in; no library needed |
| Interactive panel | FastAPI WebSocket | Bidirectional for human approval/rejection actions on incidents |

### Claude Model Assignments (Final)

| Skill | Model | Extended Thinking | Rationale |
|-------|-------|-------------------|-----------|
| k8s-pod-observer, ec2-metrics-observer, postgres-observer | No LLM call | No | Pure Python + SDK |
| log-analyzer, security-group-auditor, postgres-query-killer (pre-flight) | claude-haiku-3-5 | No | Short-text classification; ~$0.001/call |
| anomaly-detector, dependency-tracer, slo-burn-calculator (narrative) | claude-sonnet-4-6 | No | Pattern-matching + focused reasoning |
| root-cause-analyzer (single-domain) | claude-sonnet-4-6 | No | Single-layer incident |
| root-cause-analyzer (multi-domain), meta-orchestrator | claude-opus-4-8 | YES (8K–32K budget) | Cross-layer causal reasoning |
| postmortem-drafter (complex incident) | claude-opus-4-8 | Optional (6K budget) | Multi-layer narrative quality |
| incident-reporter, timeline-builder, human-escalator, postmortem-drafter (standard) | claude-sonnet-4-6 | No | Clear prose generation |

**Model pinning rule:** Pin to specific model IDs in all config — never use floating aliases like `claude-sonnet-latest` in production.

---

## Skill Architecture (Final)

20 skills across 4 layers.

### Layer 1 — Observers (8 skills, continuous, event-driven, no LLM at observation time)

| Skill | Signal Source | Key Implementation Note |
|-------|--------------|------------------------|
| `k8s-pod-observer` | K8s Watch API (kubernetes-asyncio) | Track `resourceVersion` on every event; 410 Gone → reset to "" and re-list |
| `k8s-node-observer` | K8s Watch API on nodes | Watch for NotReady, MemoryPressure, DiskPressure conditions |
| `ec2-metrics-observer` | CloudWatch EventBridge → SQS → poll | Event-driven via SQS; use `get_metric_data` for batch fetch on alarm fire |
| `fastapi-trace-observer` | Prometheus `/metrics` scrape or Alertmanager webhook | Requires `prometheus-fastapi-instrumentator` on FastAPI app — verify in Phase 1 |
| `postgres-observer` | asyncpg direct queries every 30s | Pool of max 3 connections with `application_name='atoloan-monitor'`; reserved superuser slot |
| `log-analyzer` | CloudWatch Logs subscription or K8s pod log tail | Wrap all log payloads in XML data envelope to prevent prompt injection |
| `security-group-auditor` | CloudTrail → EventBridge → SQS | 15-min CloudTrail delivery delay — use EventBridge direct integration for speed |
| `secrets-health-checker` | Secrets Manager describe + EventBridge | Pure Python date math; no LLM call; check rotation schedule and accessibility |

All observer outputs normalize to a single `MonitoringEvent` schema before entering Redis Streams.

### Layer 2 — Diagnosers (5 skills, triggered by 30s signal bundles)

| Skill | Model | Key Implementation Note |
|-------|-------|------------------------|
| `root-cause-analyzer` | Sonnet (single domain) / Opus+thinking (cross-domain) | Force structured output via `tool_choice: {"type": "tool", "name": "submit_diagnosis"}` with Pydantic-derived schema |
| `anomaly-detector` | Sonnet (contextualization) | Dual-baseline: current EWMA + pinned reference snapshot; MAD for latency, EWMA-Z for resources, IQR for error rates |
| `slo-burn-calculator` | Sonnet (narrative only) | Multi-window burn rate (1h + 6h); Google SRE thresholds: 14.4x at 1h, 6x at 6h |
| `dependency-tracer` | Sonnet | Graph traversal + causal direction; max 5-hop depth |
| `resource-saturation-predictor` | Sonnet | numpy linregress; flag non-linear growth when R² < 0.7 |

### Layer 3 — Remediators (7 skills, confidence-gated, pre-flight mandatory)

Per-action confidence thresholds:
- `pod-restarter`: 0.80 — per-pod cooldown: no re-restart same pod within 2 minutes; escalate after 2 failed restarts
- `deployment-scaler`: 0.85 — pre-flight checks node headroom AND namespace ResourceQuota (both required)
- `resource-limit-adjuster`: 0.80 — validate new limit > current usage before patching
- `postgres-query-killer`: 0.90 — DDL classifier mandatory; never kill ALTER, CREATE INDEX CONCURRENTLY, autovacuum workers
- `secrets-refresher`: 0.85 — configure Secrets Manager grace period (old + new version valid for 15 min)
- `circuit-breaker-toggler`: 0.90 — auto re-enable after 5 minutes; abort if no maintenance-selector pods running
- `human-escalator`: 0.00 — always surface anything below threshold

Hard-stop FORBIDDEN_OPERATIONS blocklist enforced at Python function boundary (not orchestrator logic): `delete_pod`, `delete_deployment`, `delete_namespace`, `terminate_instance`, `stop_instance`, `drop_table`, `truncate_table`, `delete_database`, `delete_secret`, `delete_security_group`, `revoke_iam`, `delete_role`.

### Layer 4 — Documenters (4 skills, post-resolution)

| Skill | Model | Threshold for Full Doc |
|-------|-------|----------------------|
| `incident-reporter` | Sonnet | P2+, OR remediation triggered, OR >5 min, OR >0.1% error budget burned |
| `timeline-builder` | Haiku | All P2+ incidents |
| `slo-impact-recorder` | Sonnet (narrative) | All incidents affecting a service with a defined SLO |
| `postmortem-drafter` | Sonnet (standard) / Opus (complex cascade) | All P1–P2 incidents |

---

## Claude API Usage Plan

**Tool Use:** Every diagnoser and remediator uses the agentic loop (execute tools concurrently via `asyncio.gather`). `root-cause-analyzer` forces structured output via named tool choice — most reliable method for typed confidence scores.

**Extended Thinking:** Used by `root-cause-analyzer` (multi-domain) and optionally `postmortem-drafter` (complex). Decision rule: 2+ domains → enable thinking, budget 8,000+. Two-tier strategy: fast small-budget Sonnet (2–5s) fires first; full Opus extended thinking (30–60s) follows. Thinking blocks logged internally, never shown to dashboard users.

**Prompt Caching:** Three cached blocks per diagnoser call: (1) agent identity system prompt (~500 tokens), (2) infrastructure topology (~3,000 tokens, invalidated at deploy time), (3) baseline statistics (~1,500 tokens, refreshed hourly). Signal bundle is never cached. Cache TTL is 5 minutes. Minimum cacheable block size is 1,024 tokens.

**Streaming:** SSE stream for one-way agent action feed to dashboard. Stream RCA reasoning and postmortem generation token-by-token. Filter thinking deltas — do not send to dashboard. Emit `tool_executing` events so engineers see agent actions in real time.

**Multi-Agent Orchestration:** Three domain orchestrators as independent asyncio tasks sharing a single process. Each has its own Redis Stream consumer group. Meta-orchestrator subscribes to `diagnoses` Redis Pub/Sub channel and triggers only when 2+ domains are active or single-domain confidence < 0.5. Max 5 concurrent Claude API calls via `asyncio.Semaphore(5)`.

**Batch API:** Three scheduled jobs at 50% cost savings: nightly log review (Haiku, 02:00 UTC), weekly SLO report (Sonnet), weekly baseline recalculation. Schedule with APScheduler.

**Confidence Scoring:** Custom tool forces self-assessed confidence with explicit calibration anchors in system prompt. Per-action threshold gates before any remediation API call. Below threshold → `HumanEscalationPacket` with proposed actions as dashboard approval buttons.

---

## Architecture Decisions

Decisions that must be made before Phase 1 begins.

| Decision | Recommended Answer |
|----------|--------------------|
| Monitoring agent deployment target | Bare EC2 process (not K8s pod) — avoids circular dependency where the monitor is affected by the incidents it monitors |
| TimescaleDB vs. plain Postgres | TimescaleDB if available (confirm before Phase 1); fallback is manual monthly RANGE partitioning |
| FastAPI metrics source | Prometheus + Alertmanager webhook if already deployed; otherwise add `prometheus-fastapi-instrumentator` to FastAPI app |
| Event bus | Redis Streams (not asyncio.Queue — survives restarts, readable by dashboard API) |
| Dashboard feed protocol | SSE for one-way agent action feed; WebSocket only for interactive incident command panel |
| Orchestrator split-brain prevention | Redis distributed lock per affected service; meta-orchestrator veto power; 60s post-remediation cooling-off |
| Baseline learning period | 7-day active baseline; day-of-week dimension in data model from day one; 28-day shadow baseline built in background during weeks 2–4 |

---

## Phase-by-Phase Risk Register

### Phase 1: Infrastructure, Observers, Learning Mode

| Risk | Severity | Required Mitigation |
|------|----------|---------------------|
| K8s Watch API missed events on reconnect | HIGH | Track `resourceVersion`; handle 410 Gone; implement `allowWatchBookmarks: true`; 30s heartbeat check |
| Baseline contamination during learning mode | CRITICAL | Exclude metrics during K8s eviction events and rolling deployments; auto-extend if >20% of window contaminated |
| Monitoring agent goes dark with no detection | CRITICAL | Heartbeat endpoint + CloudWatch Synthetics canary; ASG min=1 for auto-replacement; all state in Redis/Postgres |
| Postgres observer consumes app connections | HIGH | Max 3 asyncpg pool; `application_name='atoloan-monitor'`; PostgreSQL `superuser_reserved_connections` |
| IAM permission creep from day one | HIGH | Canonical permission set in versioned IAM policy document before first deploy; use permission boundaries |

### Phase 2: Diagnosers, Anomaly Detection, SLO Calculator

| Risk | Severity | Required Mitigation |
|------|----------|---------------------|
| RCA hallucination on partial signals | CRITICAL | Wait for 30s aggregation window; structured output with named metric evidence; 1 action per RCA; post-action verification |
| Gradual degradation baseline drift | CRITICAL | Dual-baseline in data model: current EWMA + reference snapshot; weekly drift report; absolute thresholds alongside relative |
| Extended thinking P0 latency (30–60s) | HIGH | Two-tier RCA: fast Sonnet (2–5s) for immediate action; full Opus for deeper diagnosis |
| Single-window SLO burn rate misses spikes | HIGH | Multi-window (1h + 6h) from day one; verify against Google SRE workbook thresholds |
| Weekend/holiday traffic false positives | HIGH | Day-of-week dimension in baseline schema from Phase 1; 28-day shadow baseline |
| Context window saturation in incident storm | HIGH | Domain orchestrators summarize before sending to meta-orchestrator; hard per-domain token budget |

### Phase 3: Remediators, Orchestration Layer

| Risk | Severity | Required Mitigation |
|------|----------|---------------------|
| DDL query killed during schema migration | CRITICAL | DDL classifier blocking requirement before any auto-kill goes live; never kill autovacuum/background workers |
| Pod restart amplifies CrashLoopBackOff | HIGH | Per-pod 2-min cooldown; escalate after 2 failed restarts; check startup logs before second restart |
| Namespace quota exhaustion from scale-up | HIGH | Two-dimensional pre-flight: node headroom AND namespace ResourceQuota; max 2x replica count per action |
| Split-brain orchestrators take conflicting actions | HIGH | Redis distributed lock per service; meta-orchestrator veto; 60s post-remediation cooling-off |
| Secrets rotation race condition | HIGH | Secrets Manager grace period (both versions valid 15 min); verify all pods on new version before invalidating old |
| PDB violation during restart or scale-down | HIGH | Query PDB before any scale or restart; use K8s rolling update mechanism (patch deployment) not manual pod deletes |

### Phase 4: Dashboard, Documentation

| Risk | Severity | Required Mitigation |
|------|----------|---------------------|
| Incident document spam from minor blips | HIGH | Severity gate: full docs only for P2+ or remediation triggered or >5 min or >0.1% error budget |
| Prompt injection via log contents | HIGH | XML data envelope around all log payloads; explicit system prompt boundary: log_data is data not instructions |

---

## Competitive Differentiators

What Atoloan Monitor does that none of the 5 reference agents do:

1. **Closed-loop autonomous operation.** Every reference agent does at most observe+diagnose. None of them act on reasoning and write a postmortem. Atoloan Monitor does all four autonomously.

2. **LLM reasoning with extended thinking for cross-layer RCA.** All 5 use statistical methods that only recognize trained patterns. Claude with extended thinking reasons about novel failure combinations across all 4 infrastructure layers — and explains its reasoning in plain English.

3. **Autonomous postmortem with full narrative reasoning chain.** None of the 5 generate a postmortem. Atoloan Monitor's `postmortem-drafter` writes timeline, root cause narrative, SLO impact, and 3–5 specific follow-up actions from the LLM's actual reasoning trace.

4. **Self-discovered SLOs from observed traffic.** All 5 require manual SLO/SLI definition. Atoloan Monitor's 7-day learning mode derives SLO baselines from observed traffic automatically — zero configuration to start.

5. **Confidence-gated remediation with per-action pre-flight safety checks.** No reference agent implements a continuous confidence score gating action at a configurable threshold combined with per-action pre-flight checks (quota before scale-up, DDL classification before kill).

6. **Domain-partitioned orchestrator architecture.** No commercial agent structures diagnostic intelligence as parallel K8s/DB/Infra specialists converging on a meta-orchestrator for cross-domain incidents only.

7. **Hard safety boundary enforced in code, not configuration.** Commercial agents enforce safety by choosing not to add dangerous runbooks to a catalog. Atoloan Monitor enforces safety at the Python function boundary with a code-level FORBIDDEN_OPERATIONS blocklist — cannot be bypassed by any reasoning chain.

---

## Open Questions (Require User Input Before Implementation)

1. Is the monitoring agent deployed as a K8s pod or bare EC2 process? (Gates K8s auth design)
2. Is Prometheus + Alertmanager already deployed in the K8s cluster? (Gates FastAPI trace observer design)
3. Is TimescaleDB available on the PostgreSQL instance? (Gates baseline storage schema)
4. Is the External Secrets Operator already deployed in the cluster? (Gates secrets-refresher pattern)
5. Is Istio service mesh deployed? (Gates circuit-breaker-toggler implementation)
6. What is the current Kubernetes cluster version? (PolicyV1Api for PDB requires K8s 1.21+)
7. Is the CloudWatch Agent already installed on the 3 production EC2 instances? (Memory and disk metrics require it)
8. What is the monthly Claude API budget? (Sets per-incident cost cap and hard stop values)
9. Does a read-only Postgres monitoring user with `pg_monitor` role exist, or does one need to be created?
10. Does Atoloan's loan application traffic drop significantly on weekends or end-of-month? (Determines urgency of 28-day baseline)

---

## Sources

- **REFERENCE-AGENTS.md** — Davis AI, Watchdog, PagerDuty AIOps, DevOps Guru, Honeycomb patterns. Confidence: HIGH.
- **SKILLS-AND-FEATURES.md** — 20 skill specs, Claude API feature implementations. Confidence: HIGH for API patterns; MEDIUM for exact library versions.
- **ARCHITECTURE.md** — Event-driven patterns, multi-orchestrator design, baseline algorithms, dashboard architecture, DB schemas. Confidence: HIGH overall; MEDIUM for TimescaleDB syntax specifics.
- **PITFALLS.md** — 18 pitfalls across 6 categories with prevention strategies and phase assignments. Confidence: HIGH.
- **PROJECT.md** — Canonical requirements, constraints, and infrastructure context for Atoloan's production stack.
