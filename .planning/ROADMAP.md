# Roadmap — Atoloan Monitor

## Overview

Atoloan Monitor is built in four dependency-ordered phases. Phase 1 lays the event-driven infrastructure and deploys all observers in learning mode — the system watches but does not act. Phase 2 activates the diagnosis layer, anomaly detection, and SLO baseline derivation once real baselines exist. Phase 3 gates all safe remediation actions behind the confidence scores and pre-flight checks proven in Phase 2. Phase 4 surfaces everything to operators through a real-time dashboard and auto-generated incident documentation.

## Phases

- [ ] **Phase 1: Infrastructure and Observers** - Deploy event-driven monitoring infrastructure and all 8 observers in 7-day learning mode
- [ ] **Phase 2: Diagnosis and SLO Engine** - Activate anomaly detection, root cause analysis, and SLO burn rate calculation on top of learned baselines
- [ ] **Phase 3: Safe Remediation** - Enable confidence-gated auto-remediation with pre-flight safety checks across all 7 remediator skills
- [ ] **Phase 4: Dashboard and Incident Docs** - Ship real-time operator dashboard with SSE action feed and auto-generated incident documentation

## Phase Details

### Phase 1: Infrastructure and Observers
**Goal**: All 8 observers are running event-driven on live infrastructure, normalizing signals into the shared event bus, and building per-metric baselines during a mandatory 7-day learning period — the system is watching and learning but not yet alerting or acting
**Depends on**: Nothing (first phase)
**Requirements**: INFRA-01, INFRA-02, INFRA-03, INFRA-04, INFRA-05, INFRA-06, OBS-01, OBS-02, OBS-03, OBS-04, OBS-05, OBS-06, OBS-07, OBS-08, OBS-09
**Success Criteria** (what must be TRUE):
  1. Monitoring agent process is running on the dedicated 4th EC2 instance and CloudWatch shows a heartbeat metric arriving every 60 seconds; the watchdog Lambda fires a visible alert within 3 minutes of the heartbeat stopping
  2. K8s pod observer correctly reconnects to the Watch API after receiving a 410 Gone response (resourceVersion reset to "" and re-list triggered), confirmed by injecting a watch disconnect and observing the observer resume without missing events
  3. All 8 observer skills are publishing structured `InfraEvent` objects to the Redis Streams event bus; events from all 4 infrastructure layers (K8s, EC2, FastAPI, Postgres) appear in the stream within their expected polling intervals
  4. Postgres observer queries the application database using a connection pool of at most 3 connections with `application_name='atoloan-monitor'`, and the PostgreSQL `superuser_reserved_connections` slot is reserved — the monitoring agent cannot consume application connection budget
  5. Learning mode is active: no alerts fire, no remediation actions execute, and the system logs a contamination flag when a K8s rolling update or node eviction is in progress — contaminated baseline windows are excluded from the computed baseline
  6. After 7 days of learning mode, per-metric EWMA baselines and pinned reference baseline snapshots exist in the monitoring database for all observed metrics; the system transitions to Enforcement mode automatically and logs the transition event
**Sub-scope (this planning round)**: Generic-monitor framework refactor (CONTEXT.md D-01..D-16) — generalize the existing K8s node monitor into a registry-based observer/orchestrator/skill framework. Migrates OBS-02 behavior-preserving and ships the ADDING-A-MONITOR.md recipe; INFRA-01..06 and OBS-01/03..08 remain Pending for future phases.
**Plans**: 6 plans (6 waves)
Plans:
- [x] 01-01-PLAN.md — Wave 0: pytest harness + baseline tests capturing CURRENT behavior
- [x] 01-02-PLAN.md — D-03/D-04/D-05: extract shared remediation primitives (domain-keyed THRESHOLDS)
- [ ] 01-03-PLAN.md — D-06/D-07/D-08/D-09: extract diagnoser_base + agent_loop shared modules
- [ ] 01-04-PLAN.md — D-10/D-11/D-12/D-13: DomainConfig registry + generic orchestrator + per-domain urgency
- [ ] 01-05-PLAN.md — D-01/D-02: open action_type to str + safety-floor regression (checkpoint)
- [ ] 01-06-PLAN.md — D-14/D-15/D-16: registry-driven runner + ADDING-A-MONITOR.md + e2e checkpoint

### Phase 2: Diagnosis and SLO Engine
**Goal**: The agent diagnoses infrastructure incidents using correlated multi-layer signals, anomaly detection against dual baselines, and multi-window SLO burn rate calculation — confidence-scored DiagnosisResult objects are produced for every aggregated signal bundle but no automated actions are taken yet
**Depends on**: Phase 1
**Requirements**: DIAG-01, DIAG-02, DIAG-03, DIAG-04, DIAG-05, DIAG-06, DIAG-07, SLO-01, SLO-02, SLO-03, SLO-04, SLO-05
**Success Criteria** (what must be TRUE):
  1. Correlated signals arriving within a 30-second window are bundled into a single diagnosis request; a single-domain incident produces a `DiagnosisResult` with primary causal anomaly, secondary contributors, confidence score, and recommended action — visible in agent logs
  2. Anomaly detector maintains two separate baselines per metric: a current EWMA baseline that adapts over time and a pinned reference snapshot of the healthy-state baseline; an alert fires when EWMA drift from the reference exceeds threshold — confirming the dual-baseline data model is in place from day one
  3. SLO burn rate calculator implements both a 1-hour and 6-hour window using Google SRE Workbook thresholds (14.4x at 1h, 6x at 6h); a simulated error spike that would exhaust the budget in under 1 hour triggers an alert on the 1h window but not a false positive on the 6h window
  4. Cross-domain incidents (signals from 2+ layers) escalate to Claude Opus with extended thinking; single-domain incidents are diagnosed by Claude Sonnet — confirmed by log entries showing model selection logic
  5. Per-action confidence thresholds are enforced: a DiagnosisResult with confidence below 0.90 for a `postgres-query-killer` or `circuit-breaker-toggler` action is routed to human escalation, not auto-remediation — observable via escalation log entries
  6. SLO baselines auto-discovered from Phase 1 learning data: SLI definitions for availability, latency p95, saturation, and error rate exist per service with no manual configuration; error budget remaining and projected time-to-exhaustion are queryable via the agent API
**Plans**: TBD

### Phase 3: Safe Remediation
**Goal**: All 7 remediator skills are live and confidence-gated, each with mandatory pre-flight checks; the agent closes the loop from diagnosis to action — executing safe remediations autonomously and surfacing anything below threshold or in FORBIDDEN_OPERATIONS to a human escalation queue
**Depends on**: Phase 2
**Requirements**: REM-01, REM-02, REM-03, REM-04, REM-05, REM-06, REM-07, REM-08
**Success Criteria** (what must be TRUE):
  1. FORBIDDEN_OPERATIONS blocklist (delete_pod, terminate_instance, drop_table, truncate_table, and all other destructive operations) is enforced at the Python function boundary — calling any forbidden operation directly raises an exception regardless of what the orchestrator or RCA output requested, confirmed by unit test with direct function invocation
  2. Pod restarter enforces a per-pod restart cooldown: a pod that restarted less than 5 minutes ago cannot be restarted again by the agent; the pre-flight check queries the PodDisruptionBudget and aborts if disruption is not allowed — confirmed by observing a blocked restart attempt in agent logs
  3. Deployment scaler runs a two-dimensional pre-flight before any scale-up: it checks both namespace ResourceQuota headroom and available node capacity across all nodes in the namespace; a scale-up that would exceed either constraint is blocked and escalated — confirmed via a simulated quota-full scenario
  4. Postgres query killer requires the DDL classifier to complete and return a non-uncertain classification before any kill executes; ALTER, CREATE INDEX CONCURRENTLY, autovacuum, and known long-batch queries are never killed — confirmed by observing a classification result in logs before every kill action
  5. Human escalator surfaces a complete `DiagnosisResult` (including confidence score, recommended action, and primary causal evidence) to the escalation queue for every incident where confidence falls below the per-action threshold or the action is in FORBIDDEN_OPERATIONS — no incident is silently dropped
  6. End-to-end remediation loop completes: agent detects a simulated pod CrashLoopBackOff, diagnoses root cause, satisfies pre-flight checks, restarts the pod, and logs a post-action verification result — the full cycle is observable in structured agent logs
**Plans**: TBD

### Phase 4: Dashboard and Incident Documentation
**Goal**: Operators have a real-time web dashboard showing infrastructure health, a live SSE-powered agent action feed, SLO status, human escalation panel, incident history, and auto-generated incident documents with postmortems for all qualifying incidents
**Depends on**: Phase 3
**Requirements**: DASH-01, DASH-02, DASH-03, DASH-04, DASH-05, DASH-06, INC-01, INC-02, INC-03, INC-04, INC-05
**Success Criteria** (what must be TRUE):
  1. Real-time infrastructure health grid shows current status of all 4 layers (K8s, EC2, FastAPI, Postgres) with per-component health scores; the grid updates without page refresh as agent observations arrive
  2. Live agent action feed is delivered via Server-Sent Events (SSE) using the native `EventSource` API — not WebSocket; the feed shows what the agent is observing, diagnosing, and doing in real time and auto-reconnects on connection loss without losing the feed
  3. Human escalation panel displays a complete `DiagnosisResult` with confidence score and recommended action; an operator can approve, modify, or dismiss the action from the panel — the agent's escalation queue reflects the operator's decision
  4. Incident documents are auto-generated only for incidents meeting the severity gate: P2 or higher, OR remediation was triggered, OR duration exceeded 2 minutes, OR more than 0.1% of error budget was burned — minor blips below all thresholds produce no document, confirmed by a simulated 90-second low-severity event generating no doc
  5. Each qualifying incident document contains: full chronological timeline of observations and agent actions, root cause summary identifying the originating layer and causal anomaly, SLO impact record showing error budget burned, and a postmortem draft with 3-5 specific follow-up actions generated by Claude
  6. Incident history view is searchable by severity, service, date range, and root cause layer; all past incident documents are linked from the history and open an inline full-incident view
**Plans**: TBD
**UI hint**: yes

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Infrastructure and Observers | 2/6 | In progress | - |
| 2. Diagnosis and SLO Engine | 0/TBD | Not started | - |
| 3. Safe Remediation | 0/TBD | Not started | - |
| 4. Dashboard and Incident Docs | 0/TBD | Not started | - |

---
*Generated: 2026-06-03*
