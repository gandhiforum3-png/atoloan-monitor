# Requirements — Atoloan Monitor

## v1 Requirements

### INFRA — Infrastructure Prerequisites

- [ ] **INFRA-01**: Monitoring agent runs on a dedicated 4th EC2 instance with an IAM role scoped to read + safe operations (no delete/terminate/destroy permissions)
- [ ] **INFRA-02**: Prometheus + Alertmanager deployed in the Kubernetes cluster to expose application and K8s metrics
- [ ] **INFRA-03**: CloudWatch Agent installed on all 3 application EC2 instances to expose memory and disk metrics
- [ ] **INFRA-04**: A dedicated PostgreSQL instance (separate from application DB) stores all monitoring data: baselines, incidents, SLO records, agent action logs
- [ ] **INFRA-05**: Redis instance deployed as the event bus (Redis Streams) for cross-orchestrator signal passing and alert aggregation
- [ ] **INFRA-06**: Agent heartbeat metric published to CloudWatch every 60 seconds; a CloudWatch Alarm + watchdog Lambda alerts if heartbeat stops

### OBS — Observation Layer (Event-Driven)

- [ ] **OBS-01**: K8s pod observer subscribes to the Kubernetes Watch API for pod events: OOMKills, CrashLoopBackOff, pending pods, failed scheduling — with 410 Gone reconnection and resourceVersion bookmarking
- [ ] **OBS-02**: K8s node observer watches node conditions: memory pressure, disk pressure, PID pressure, node not ready, eviction events
- [ ] **OBS-03**: EC2 metrics observer receives CloudWatch Alarm state-change events for CPU, memory, disk, and network across all 3 instances
- [ ] **OBS-04**: FastAPI trace observer scrapes Prometheus endpoints every 30 seconds for: request latency (p50/p95/p99), error rate per endpoint, active connections, retry counts
- [ ] **OBS-05**: Postgres observer queries `pg_stat_activity`, `pg_stat_statements`, `pg_stat_replication`, and connection pool depth every 60 seconds on the application database
- [ ] **OBS-06**: Log analyzer streams application and system logs, classifies log lines using Claude Haiku, emits structured anomaly events for ERROR/FATAL patterns and repeated warnings
- [ ] **OBS-07**: Security group auditor runs on a 6-hour schedule to check for overly permissive rules (0.0.0.0/0 on non-80/443 ports, unexpected port openings)
- [ ] **OBS-08**: Secrets health checker runs on a 1-hour schedule to detect expiring secrets (< 7 days) or stale secrets (not rotated in > 90 days) in AWS Secrets Manager
- [ ] **OBS-09**: All observer signals normalized to a shared `InfraEvent` schema (source, layer, severity, metric_name, value, timestamp, raw_payload) before entering the event bus

### DIAG — Diagnosis Layer

- [ ] **DIAG-01**: Alert aggregation window debounces correlated signals within a 30-second window before dispatching to RCA; signals from the same incident are bundled into one diagnosis request
- [ ] **DIAG-02**: Anomaly detector maintains dual baselines per metric: EWMA current baseline (adapts over time) + pinned reference baseline (healthy-state snapshot); detects drift using EWMA for resources, MAD for latency, IQR for error rates
- [ ] **DIAG-03**: Root cause analyzer runs Claude Sonnet for single-domain incidents; escalates to Claude Opus with extended thinking (budget: 8,000–32,000 tokens) for cross-domain incidents spanning 2+ layers
- [ ] **DIAG-04**: Every diagnosis produces a structured `DiagnosisResult`: primary causal anomaly, list of secondary contextual contributors, confidence score (0.0–1.0), recommended action, action confidence
- [ ] **DIAG-05**: Dependency tracer maps upstream/downstream relationships between K8s services, FastAPI endpoints, and Postgres to determine blast radius of a failing component
- [ ] **DIAG-06**: Resource saturation predictor trends CPU/memory metrics to project node exhaustion time; surfaces predictions > 24h horizon to dashboard
- [ ] **DIAG-07**: SLO burn rate calculator implements multi-window burn rates (1h + 6h windows, Google SRE Workbook thresholds) to detect transient but severe error spikes

### REM — Remediation Layer (Safe Actions Only)

- [ ] **REM-01**: Hard-stop FORBIDDEN_OPERATIONS blocklist enforced at the skill boundary for: delete, drop, destroy, terminate, truncate, force-delete — cannot be overridden by any orchestrator or RCA output
- [ ] **REM-02**: Pod restarter: restarts a specific pod or triggers rolling deployment restart; pre-flight: check restart count cooldown (no restart if pod restarted < 5 minutes ago), verify PodDisruptionBudget allows disruption
- [ ] **REM-03**: Deployment scaler: scales K8s replicas up or down; pre-flight: check namespace ResourceQuota headroom, check node capacity across all nodes in the namespace, verify PodDisruptionBudget
- [ ] **REM-04**: Resource limit adjuster: updates pod CPU/memory limits in K8s deployment spec; pre-flight: verify new limits do not exceed namespace quota
- [ ] **REM-05**: Postgres query killer: terminates long-running or blocking queries; pre-flight: DDL classifier must first classify query as application query (not a migration, vacuum, or known-long batch job) — skip kill if classification is uncertain
- [ ] **REM-06**: Secrets refresher: force-pulls latest secret version to pods via rolling restart; pre-flight: verify new version exists and is different from current, allow grace period for connection draining
- [ ] **REM-07**: Circuit breaker toggler: disables/re-enables traffic to a service endpoint; pre-flight: confirm downstream dependency exists and is reachable before re-enabling
- [ ] **REM-08**: Human escalator: when confidence < per-action threshold OR action is in FORBIDDEN_OPERATIONS, surface full `DiagnosisResult` to dashboard with recommended action for human execution — never silently drop

### SLO — SLO Management

- [ ] **SLO-01**: Two-phase operation: Learning mode (days 1–7, observe only, build baselines, no alerts or remediation) → Enforcement mode (full alerting and remediation active)
- [ ] **SLO-02**: Learning mode contamination detection: mark baseline windows as contaminated during K8s rolling updates, node evictions, and manually-triggered restarts; exclude contaminated windows from baseline computation
- [ ] **SLO-03**: SLO baselines discovered automatically from observed traffic; no manual configuration required to start — agent derives availability, latency, and error rate targets from p99 of learning period data
- [ ] **SLO-04**: Error budget tracking per service: remaining budget shown as percentage and as time-to-exhaustion at current burn rate
- [ ] **SLO-05**: SLI definitions auto-generated per layer: availability (K8s pod uptime), latency (FastAPI p95), saturation (EC2 CPU/memory), error rate (FastAPI 5xx / total)

### DASH — Dashboard

- [ ] **DASH-01**: Real-time infrastructure health grid showing current status of all 4 layers (K8s, EC2, FastAPI, Postgres) with per-component health scores
- [ ] **DASH-02**: Live agent action feed via Server-Sent Events (SSE) showing what the agent is observing, diagnosing, and doing in real time
- [ ] **DASH-03**: SLO status panel: current SLI values, error budget remaining, burn rate, projected exhaustion time per service
- [ ] **DASH-04**: Active incident view: live diagnosis timeline, agent confidence score, recommended vs. taken actions, current status
- [ ] **DASH-05**: Human escalation panel: surfaces diagnosis + recommended action when agent confidence is below threshold or action is forbidden — operator can approve, modify, or dismiss
- [ ] **DASH-06**: Incident history: searchable log of all past incidents with severity, duration, root cause summary, and link to full incident document

### INC — Incident Documentation

- [ ] **INC-01**: Auto-generated incident document created after every resolved incident (threshold: severity ≥ medium, duration > 2 minutes — minor blips do not generate docs)
- [ ] **INC-02**: Incident timeline: chronological log of every observation, signal, RCA step, and agent action with timestamps
- [ ] **INC-03**: Root cause summary: which layer the incident originated in, what the causal anomaly was, which secondary factors contributed
- [ ] **INC-04**: SLO impact record: which SLIs were breached, error budget burned (percentage + minutes), affected services and endpoints
- [ ] **INC-05**: Postmortem draft: 3–5 follow-up actions to prevent recurrence, generated by Claude Sonnet with full incident context

## v2 Requirements (Deferred)

- Slack / PagerDuty alert routing — dashboard is primary interface for v1
- Multi-tenant support — Atoloan-first, generalize later
- External incident doc export (Notion, Confluence, Jira)
- CI/CD pipeline monitoring — runtime infrastructure first
- AWS cost optimization recommendations — observability before cost analysis
- Mobile dashboard — web only for v1
- Automated SLO target adjustment after milestone reviews
- Agent configuration UI — config-file driven for v1

## Out of Scope

- **Destructive operations** (delete pod, terminate instance, drop table, destroy any resource) — permanent hard limit, not a v1 constraint
- **Multi-tenant SaaS** — Atoloan-first; single-tenant architecture in v1
- **Infrastructure provisioning** — monitoring only, not Terraform/CDK management
- **Application code analysis** — agent monitors runtime behavior, not source code
- **Security scanning / SAST** — operational monitoring only, not security testing

## Traceability

| Requirement | Phase |
|-------------|-------|
| INFRA-01 through INFRA-06 | Phase 1 |
| OBS-01 through OBS-09 | Phase 1 |
| DIAG-01 through DIAG-07 | Phase 2 |
| REM-01 through REM-08 | Phase 3 |
| SLO-01 through SLO-05 | Phase 2 |
| DASH-01 through DASH-06 | Phase 4 |
| INC-01 through INC-05 | Phase 4 |
