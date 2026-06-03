# Skills Specification and Claude Features

**Project:** Atoloan Monitor
**Researched:** 2026-06-03
**Model running this research:** claude-sonnet-4-6
**Confidence:** MEDIUM-HIGH (based on training knowledge through Aug 2025; web fetch unavailable in this session)

---

## Skill Specifications

### Layer 1: Observers

---

#### k8s-pod-observer

**Inputs:**
- Kubernetes namespace list (from config or auto-discovered)
- K8s API server URL + service account token (from in-cluster config or kubeconfig)
- Watch stream from `CoreV1Api.list_namespaced_pod(watch=True)` — event type + pod object
- Optional: field selectors (e.g., `status.phase=Failed`) to scope watch

**Outputs:**
```json
{
  "event_type": "MODIFIED|ADDED|DELETED",
  "pod_name": "backend-abc123",
  "namespace": "production",
  "node": "ip-10-0-1-5",
  "phase": "Failed",
  "conditions": [...],
  "container_statuses": [{
    "name": "api",
    "ready": false,
    "restart_count": 4,
    "last_state": {"terminated": {"reason": "OOMKilled", "exit_code": 137}}
  }],
  "signal_type": "OOMKill|CrashLoopBackOff|Pending|Evicted|NodePressure",
  "severity": "critical|warning|info",
  "observed_at": "2026-06-03T14:22:00Z",
  "raw_event": {...}
}
```

**Tools required:**
- `kubernetes.client.CoreV1Api.list_namespaced_pod(watch=True)` — continuous event stream
- `kubernetes.client.CoreV1Api.read_namespaced_pod(name, namespace)` — fetch full pod spec on event
- `kubernetes.client.CoreV1Api.read_namespaced_pod_log(name, namespace, previous=True)` — last N lines of crashed container logs
- `kubernetes.client.AppsV1Api.list_namespaced_deployment(namespace)` — check owning deployment state
- `kubernetes.client.CoreV1Api.list_node()` — node pressure conditions

**Recommended model:** No Claude call needed for raw observation. This skill is pure Python + K8s API. Claude is invoked at the aggregation layer (diagnoser), not at observation time. If signal classification (OOMKill vs. ConfigError) requires NLP, use **claude-haiku-3-5** — it's sufficient for string pattern matching and is ~10x cheaper than Sonnet.

**Extended thinking:** No

**Approx tokens/call:** ~500 input / ~200 output if Claude classification is used. Most invocations are zero-Claude.

**Error modes:**
- `kubernetes.client.exceptions.ApiException` (403): IAM/RBAC permission missing → log, alert engineer, skip namespace
- Watch stream disconnects (normal after ~5 min per K8s spec): re-establish with `resourceVersion` from last event to avoid re-processing
- `ApiException` (410 Gone): `resourceVersion` expired → re-list with `resourceVersion=""` then re-watch
- Pod log unavailable (container not started): catch `ApiException(404)`, emit signal without log context
- Node unreachable: events stop arriving — implement heartbeat check; if no events for >60s on active namespace, emit `observer_stall` signal

---

#### ec2-metrics-observer

**Inputs:**
- Instance IDs for all 4 EC2s (frontend, backend, postgres, monitor)
- AWS region
- CloudWatch namespace + metric names: `AWS/EC2` (CPUUtilization, NetworkIn/Out, StatusCheckFailed), `CWAgent` (mem_used_percent, disk_used_percent) — requires CloudWatch Agent on instances
- Alarm ARN list from CloudWatch Alarms (event-driven trigger)
- CloudWatch Events / EventBridge rule targeting this observer's SQS queue

**Outputs:**
```json
{
  "instance_id": "i-0abc123",
  "instance_role": "backend",
  "metrics": {
    "cpu_percent": 94.2,
    "mem_percent": 87.1,
    "disk_percent": 76.3,
    "network_in_bytes": 1500000,
    "network_out_bytes": 800000
  },
  "alarm_state": "ALARM",
  "alarm_name": "backend-cpu-high",
  "threshold_breached": "CPUUtilization > 90",
  "signal_type": "cpu_saturation|memory_pressure|disk_pressure|network_anomaly|status_check_fail",
  "severity": "critical|warning|info",
  "observed_at": "2026-06-03T14:22:00Z"
}
```

**Tools required:**
- `boto3.client('cloudwatch').describe_alarm_history()` — poll on alarm state change via EventBridge SQS queue
- `boto3.client('cloudwatch').get_metric_statistics(Namespace, MetricName, Dimensions, StartTime, EndTime, Period, Statistics)` — fetch last 5-minute window on alarm fire
- `boto3.client('cloudwatch').get_metric_data()` — multi-metric batch fetch (more efficient for correlation)
- `boto3.client('ec2').describe_instances(InstanceIds=[...])` — enrich with instance metadata
- EventBridge rule → SQS queue → observer polls SQS for alarm triggers (event-driven, not polling CloudWatch)

**Recommended model:** No Claude call at observation. Python-only metric collection. Use **claude-haiku-3-5** if anomaly scoring requires LLM context (~$0.001/call). Most classification is threshold-based Python.

**Extended thinking:** No

**Approx tokens/call:** ~800 input / ~300 output (if Claude is called for anomaly contextualization)

**Error modes:**
- `botocore.exceptions.ClientError` (AccessDenied): IAM role missing `cloudwatch:GetMetricData` → fail loudly, surface to dashboard
- CloudWatch metric gap: instance stopped or CloudWatch Agent not running → emit `metric_gap` signal with duration
- SQS queue backlog: process in FIFO order, drop events older than 5 minutes (stale by the time processed)
- Rate limiting (`ThrottlingException`): implement exponential backoff with jitter, max 3 retries

---

#### fastapi-trace-observer

**Inputs:**
- Prometheus metrics endpoint: `http://<backend-ec2>:8000/metrics` (requires `prometheus-fastapi-instrumentator` on the FastAPI app)
- OR OpenTelemetry collector endpoint if OTEL is deployed
- Prometheus query API or direct scrape of `/metrics` text format
- Alert threshold config: p95 latency > 500ms, error rate > 1%, etc.

**Outputs:**
```json
{
  "service": "fastapi-backend",
  "endpoint": "/api/loans",
  "window_seconds": 60,
  "latency_p50_ms": 45,
  "latency_p95_ms": 680,
  "latency_p99_ms": 1200,
  "request_rate_rps": 12.4,
  "error_rate_percent": 3.2,
  "error_breakdown": {"5xx": 2.8, "4xx": 0.4},
  "slow_dependency": "postgres",
  "signal_type": "latency_spike|error_rate_spike|retry_storm|dependency_slow",
  "severity": "critical|warning|info",
  "observed_at": "2026-06-03T14:22:00Z"
}
```

**Tools required:**
- HTTP GET to `http://<backend>:8000/metrics` — parse Prometheus text format with `prometheus_client.parser` or regex
- If Prometheus server deployed: `requests.get(f"{PROM_URL}/api/v1/query", params={"query": "histogram_quantile(0.95, ...)"})` 
- Alertmanager webhook receiver (FastAPI endpoint on the monitor agent) for push-based alerting
- `kubernetes.client.CoreV1Api.list_namespaced_service()` — resolve backend service IP dynamically

**Recommended model:** No Claude call at observation. Pure metrics math. **claude-haiku-3-5** acceptable for retry storm pattern classification if needed.

**Extended thinking:** No

**Approx tokens/call:** ~600 input / ~200 output (rare LLM call)

**Error modes:**
- `/metrics` endpoint unreachable: the service itself may be down → emit `service_unreachable` signal immediately (high severity)
- Metrics endpoint returns malformed Prometheus text: log raw, skip cycle, emit parse error
- Counter resets (pod restart resets Prometheus counters): detect via `rate()` going negative, treat as restart event not anomaly
- Missing histogram buckets: can't compute percentiles — fall back to request count and error count only

---

#### postgres-observer

**Inputs:**
- PostgreSQL DSN: `postgresql://monitor_user:password@<postgres-ec2>:5432/atoloan` (read-only monitoring user)
- Password pulled from AWS Secrets Manager at startup (never hardcoded)
- Observation queries run every 30s via asyncpg connection pool

**Outputs:**
```json
{
  "observed_at": "2026-06-03T14:22:00Z",
  "slow_queries": [{
    "pid": 1234,
    "query": "SELECT * FROM loans WHERE...",
    "duration_seconds": 45.2,
    "state": "active",
    "wait_event": "Lock",
    "application_name": "backend-api",
    "is_blocking": true,
    "blocking_pids": [1235, 1236]
  }],
  "connection_pool": {
    "total": 95,
    "max": 100,
    "idle": 5,
    "active": 90,
    "waiting": 12
  },
  "replication_lag_bytes": 0,
  "lock_contentions": 3,
  "index_bloat_pct": 12.4,
  "signal_type": "slow_query|connection_exhaustion|lock_contention|replication_lag|index_bloat",
  "severity": "critical|warning|info"
}
```

**Tools required:**
- `asyncpg.connect()` or `asyncpg.create_pool()` — async connection pool
- Query: `SELECT pid, now() - pg_stat_activity.query_start AS duration, query, state, wait_event_type, wait_event, application_name FROM pg_stat_activity WHERE state != 'idle' AND query_start IS NOT NULL ORDER BY duration DESC LIMIT 20;`
- Query: `SELECT count(*), state FROM pg_stat_activity GROUP BY state;` — connection pool depth
- Query: `SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) FROM pg_stat_replication;` — replication lag
- Query: `SELECT schemaname, tablename, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;` — table sizes
- `boto3.client('secretsmanager').get_secret_value(SecretId='atoloan/postgres/monitor')` — credential retrieval

**Recommended model:** No Claude call at observation. Pure SQL. **claude-haiku-3-5** for query classification (application vs. migration query) in the pre-flight check before kill action.

**Extended thinking:** No

**Approx tokens/call:** ~400 input / ~150 output (query classification only)

**Error modes:**
- Connection refused: Postgres down → emit `database_unreachable` critical signal, skip all further queries
- `asyncpg.TooManyConnectionsError`: monitor itself is adding to the pool pressure — use connection pool of max 2-3 connections
- `asyncpg.InsufficientPrivilegeError`: monitor user lacks `pg_monitor` role — fail loudly at startup
- Long-running query disappears between observe and kill decision: handle gracefully, log `query_already_gone`
- pg_stat_replication empty: no replicas configured → skip replication lag check

---

#### log-analyzer

**Inputs:**
- CloudWatch Logs log group ARNs: `/atoloan/backend`, `/atoloan/frontend`, `/var/log/syslog` (via CloudWatch Agent)
- CloudWatch Logs Insights query results (triggered by filter pattern match)
- OR: tail K8s pod logs via `CoreV1Api.read_namespaced_pod_log(follow=True)`
- Anomaly patterns config: list of regex patterns that trigger signals (e.g., `ERROR`, `FATAL`, `OOMKill`, `connection refused`)

**Outputs:**
```json
{
  "log_group": "/atoloan/backend",
  "log_stream": "backend-pod-abc123",
  "matched_at": "2026-06-03T14:22:00Z",
  "pattern": "database connection refused",
  "log_line": "2026-06-03 14:22:00 ERROR: database connection refused after 3 retries",
  "context_lines": ["line before", "matched line", "line after"],
  "signal_type": "error_pattern|exception|connection_refused|oom_signal|security_event",
  "severity": "critical|warning|info",
  "frequency": 12,
  "window_seconds": 60
}
```

**Tools required:**
- `boto3.client('logs').filter_log_events(logGroupName, filterPattern, startTime, endTime)` — pull matching log events
- `boto3.client('logs').start_query()` + `get_query_results()` — CloudWatch Logs Insights for pattern analysis
- CloudWatch Logs subscription filter → Kinesis Data Streams → observer Lambda/EC2 (for true event-driven log streaming)
- `kubernetes.client.CoreV1Api.read_namespaced_pod_log(name, namespace, follow=True, since_seconds=60)` — tail pod logs
- **Claude haiku-3-5**: for log line semantic classification when regex is insufficient (e.g., "is this a transient network blip or a persistent failure?")

**Recommended model:** **claude-haiku-3-5** for log semantic classification. Log lines are short; Haiku is fast and cheap. Reserve Sonnet only if log context exceeds 10,000 lines requiring summarization.

**Extended thinking:** No

**Approx tokens/call:** ~1,000 input (log batch) / ~200 output. Haiku cost: ~$0.003/call.

**Error modes:**
- CloudWatch Logs rate limit: max 5 concurrent queries per account → queue and serialize
- Log stream too large (>10MB response): use `nextToken` pagination
- Kubernetes pod log unavailable: container not started or already terminated → catch `ApiException(404)`
- False positive pattern match (e.g., "connection refused" in a test log): short-circuit with service + environment tag filtering

---

#### security-group-auditor

**Inputs:**
- AWS Account ID + Region
- List of security group IDs attached to Atoloan EC2 instances (from config or auto-discovered via EC2 describe)
- EventBridge rule for `AWS API Call via CloudTrail`: event source `ec2.amazonaws.com`, event name `AuthorizeSecurityGroupIngress|RevokeSecurityGroupEgress|ModifySecurityGroupRules`

**Outputs:**
```json
{
  "event_type": "AuthorizeSecurityGroupIngress",
  "security_group_id": "sg-0abc123",
  "security_group_name": "backend-sg",
  "changed_by": "arn:aws:iam::123456789:user/deploy-bot",
  "changed_at": "2026-06-03T14:22:00Z",
  "new_rule": {"protocol": "tcp", "port": 5432, "cidr": "0.0.0.0/0"},
  "risk_assessment": "CRITICAL: PostgreSQL port exposed to internet",
  "signal_type": "sg_open_to_internet|sg_rule_added|sg_rule_removed",
  "severity": "critical|warning|info"
}
```

**Tools required:**
- EventBridge rule → SQS queue → observer polls SQS for CloudTrail events
- `boto3.client('ec2').describe_security_groups(GroupIds=[...])` — fetch current rule set
- `boto3.client('ec2').describe_security_group_rules(Filters=[{'Name': 'group-id', 'Values': [...]}])` — enumerate all rules
- **Claude haiku-3-5**: classify risk of new rule (e.g., "port 5432 open to 0.0.0.0/0" → CRITICAL)

**Recommended model:** **claude-haiku-3-5** for risk classification of security group changes. Simple binary: dangerous vs. acceptable.

**Extended thinking:** No

**Approx tokens/call:** ~600 input / ~150 output. Cost: ~$0.001/call.

**Error modes:**
- CloudTrail event delay: up to 15 minutes for CloudTrail delivery (not suitable for real-time); use EventBridge direct integration or VPC Flow Logs for faster signals
- SecurityGroup not attached to known instance: compare against instance metadata cache, log unknown SG changes for review
- `AccessDenied` on describe_security_groups: IAM role missing `ec2:DescribeSecurityGroups` → emit permission error

---

#### secrets-health-checker

**Inputs:**
- AWS Secrets Manager secret ARNs for all Atoloan secrets (DB passwords, API keys, TLS certs)
- Rotation schedule config (e.g., rotate every 90 days)
- EventBridge rule for Secrets Manager events: `RotationStarted|RotationFailed|RotationSucceeded|SecretVersionStaged`

**Outputs:**
```json
{
  "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789:secret:atoloan/postgres/app",
  "secret_name": "atoloan/postgres/app",
  "last_rotated": "2026-03-01T00:00:00Z",
  "days_since_rotation": 93,
  "rotation_enabled": true,
  "next_rotation_due": "2026-06-03T00:00:00Z",
  "overdue_days": 3,
  "signal_type": "rotation_overdue|rotation_failed|rotation_missing|secret_stale",
  "severity": "critical|warning|info",
  "affected_services": ["backend-api"]
}
```

**Tools required:**
- `boto3.client('secretsmanager').list_secrets()` — enumerate all secrets
- `boto3.client('secretsmanager').describe_secret(SecretId)` — get rotation config + last rotated date
- `boto3.client('secretsmanager').get_secret_value(SecretId, VersionStage='AWSCURRENT')` — check secret accessibility (does NOT log secret value, only checks it's retrievable)
- EventBridge rule watching Secrets Manager API calls via CloudTrail

**Recommended model:** No Claude call needed. Pure Python date math for rotation schedule checking. This skill is entirely rule-based.

**Extended thinking:** No

**Approx tokens/call:** 0 (no LLM call)

**Error modes:**
- `AccessDenied` on describe_secret: insufficient IAM permissions → emit permission error, skip secret
- `ResourceNotFoundException`: secret deleted or ARN changed → emit `secret_missing` signal
- Rotation Lambda not configured: Secrets Manager won't auto-rotate without rotation Lambda → detect and surface as warning

---

### Layer 2: Diagnosers

---

#### root-cause-analyzer

**Inputs:**
- Aggregated signal bundle from the 30-second debounce window: list of signals from all observers
- Infrastructure topology context (cached): pod→node→EC2→VPC relationships, service dependency graph
- Recent incident history (last 10 incidents): from persistent memory store
- Per-metric baseline context (cached): normal CPU/memory/latency ranges by hour-of-day, day-of-week
- Time window of signals: ISO timestamps, all signals within the bundle

**Outputs:**
```json
{
  "root_cause": "PostgreSQL connection pool exhaustion caused by N+1 query storm from backend deployment abc123",
  "confidence": 0.91,
  "causal_chain": [
    {"layer": "fastapi", "signal": "p95_latency_spike_680ms", "time": "T+0"},
    {"layer": "postgres", "signal": "connection_pool_90pct", "time": "T+2s"},
    {"layer": "k8s", "signal": "backend_pod_crashloop", "time": "T+15s"}
  ],
  "affected_services": ["backend-api", "postgres"],
  "origin_layer": "postgres",
  "recommended_actions": ["postgres-query-killer", "deployment-scaler"],
  "remediation_confidence": 0.91,
  "alternative_hypotheses": [...],
  "thinking_summary": "...",
  "incident_id": "inc-20260603-001"
}
```

**Tools required:**
- `get_infrastructure_topology` (custom tool): returns cached pod→node→EC2 map
- `get_recent_incidents` (custom tool): queries incident history DB, returns last N incidents
- `get_baseline_context` (custom tool): returns normal metric ranges for current hour/day
- `query_metric_history` (custom tool): fetches CloudWatch metrics for specified time window
- `get_pod_logs` (custom tool): fetches recent logs from relevant pods

**Recommended model:** **claude-opus-4-8** for complex multi-layer correlation. This is the most cognitively demanding task in the system — root cause across 4 infrastructure layers simultaneously. Sonnet is acceptable for single-layer incidents.

**Decision rule:** If signals span >1 layer → opus-4-8 with extended thinking. If single-layer → sonnet-4-6 without extended thinking.

**Extended thinking:** YES — required for multi-layer incidents.
- `budget_tokens`: 8,000–16,000 for typical incidents; 32,000 for catastrophic cascades involving all 4 layers
- Latency impact: +15–45 seconds vs. standard. Acceptable for RCA (not a sub-second requirement).
- `thinking` blocks are logged but not shown in dashboard; only `text` blocks surface to users.

**Approx tokens/call:**
- Input: ~8,000–15,000 (topology + signals + baseline + history)
- Thinking: ~8,000–16,000
- Output: ~800–2,000
- Total: ~20,000–30,000 tokens
- Cost (opus-4-8): ~$0.45–$0.90/call. Acceptable: this only runs on real incidents.

**Error modes:**
- Confidence below 0.8: do NOT trigger remediation; route to human-escalator with full context
- All hypotheses have low confidence (< 0.6): emit `rca_inconclusive` signal, escalate to human with raw signals
- Token context window overflow: if signal bundle > 50 signals, summarize per-layer before sending to RCA (use Haiku for compression)
- Extended thinking timeout: set max_tokens high enough (>= budget_tokens + 2,000 output tokens)
- Model overload / 529: exponential backoff, max 3 retries; if all fail, escalate to human

---

#### anomaly-detector

**Inputs:**
- Current metric snapshot: {metric_name, value, timestamp, service, instance}
- Baseline statistics (from learning mode): {metric_name, hour_of_day, day_of_week, mean, stddev, p95, p99}
- Current hour-of-day and day-of-week
- Learning mode flag: if True, skip anomaly scoring and update baseline instead

**Outputs:**
```json
{
  "metric": "cpu_percent",
  "service": "backend-ec2",
  "current_value": 94.2,
  "expected_range": {"p5": 20.0, "p95": 65.0},
  "z_score": 3.8,
  "is_anomaly": true,
  "anomaly_type": "sudden_spike|gradual_drift|periodic_miss|saturation_approach",
  "severity": "critical|warning|info",
  "context": "CPU 94% at 14:00 on Tuesday — baseline for this hour is 30-65%"
}
```

**Tools required:**
- `get_baseline_stats` (custom tool): queries baseline DB for this metric/hour/day
- `update_baseline` (custom tool): during learning mode, updates rolling statistics
- **Claude sonnet-4-6**: for anomaly type classification and natural language context generation. Statistical z-score computation is Python; LLM adds context about whether the anomaly matches a known pattern.

**Recommended model:** **claude-sonnet-4-6** for anomaly contextualization. Haiku is sufficient if only generating a brief summary; use Sonnet if cross-correlating anomaly against recent incident history.

**Extended thinking:** No — anomaly detection is pattern matching, not complex reasoning.

**Approx tokens/call:** ~1,500 input / ~400 output. Cost: ~$0.005/call.

**Error modes:**
- Baseline not yet built (first 7 days): return `learning_mode=True`, skip anomaly scoring
- Baseline has insufficient data (< 100 samples for this hour): flag as `low_confidence_baseline`
- z-score undefined (stddev = 0 — perfectly flat metric): treat as informational, not anomaly
- Metric missing from baseline (new metric added after learning period): trigger mini-learning-mode for that metric

---

#### slo-burn-calculator

**Inputs:**
- SLI measurements: error count, total request count, latency measurements for the window
- SLO target: {sli_type: "availability", target: 0.999, window_days: 30}
- Error budget remaining (from state store)
- Historical burn rate (last 1h, 6h, 24h)

**Outputs:**
```json
{
  "service": "fastapi-backend",
  "slo_target": 0.999,
  "current_error_rate": 0.003,
  "error_budget_total_minutes": 43.2,
  "error_budget_consumed_minutes": 18.4,
  "error_budget_remaining_percent": 57.4,
  "burn_rate_1h": 4.2,
  "burn_rate_6h": 1.8,
  "burn_rate_24h": 0.9,
  "projected_exhaustion_hours": 14.2,
  "alert_level": "page|ticket|none",
  "window_days_remaining": 18
}
```

**Tools required:**
- `query_sli_metrics` (custom tool): fetches error count + request count from Prometheus or CloudWatch
- `get_error_budget_state` (custom tool): reads current budget from state store (Redis or Postgres)
- `update_error_budget_state` (custom tool): writes updated budget after calculation
- No Claude call needed for calculation — pure math. **Claude sonnet-4-6** only for generating the SLO summary narrative for the dashboard report.

**Recommended model:** **claude-sonnet-4-6** for narrative generation only. All calculations are Python. LLM call is optional and deferred.

**Extended thinking:** No

**Approx tokens/call:** ~600 input / ~300 output (narrative only). Cost: ~$0.003/call.

**Error modes:**
- No SLO defined yet (learning mode): skip burn calculation, return `learning_mode=True`
- Missing SLI data (metric endpoint down): cannot compute burn rate → emit `sli_data_missing` warning
- Burn rate = 0 for entire window (no errors at all): healthy, log and skip alert
- Error budget fully consumed: emit `budget_exhausted` critical alert regardless of current burn rate

---

#### dependency-tracer

**Inputs:**
- Failing service name
- Service dependency map (from topology cache): who calls whom
- Active signals from all observers: which services are currently exhibiting problems
- Optional: distributed trace IDs from OpenTelemetry (if deployed)

**Outputs:**
```json
{
  "failing_service": "backend-api",
  "root_service": "postgres",
  "dependency_path": ["backend-api", "→", "postgres"],
  "upstream_affected": [],
  "downstream_affected": ["frontend"],
  "cascade_risk": "high",
  "trace_evidence": ["query latency 45s", "connection pool 90%"],
  "hypothesis": "postgres is the origin; backend degradation is downstream effect"
}
```

**Tools required:**
- `get_service_topology` (custom tool): returns dependency graph from topology cache
- `get_active_signals` (custom tool): queries current signal store
- **Claude sonnet-4-6** for dependency chain reasoning — graph traversal plus language about direction of causality.

**Recommended model:** **claude-sonnet-4-6** — this is a focused reasoning task, not multi-layer RCA. Sonnet handles graph + causal reasoning well.

**Extended thinking:** No for simple 2-3 hop chains; YES (budget: 4,000) for complex dependency webs with circular dependencies or ambiguous signals.

**Approx tokens/call:** ~2,000 input / ~500 output. Cost: ~$0.008/call.

**Error modes:**
- Topology cache stale (service added/removed): fall back to runtime discovery via K8s service labels
- Circular dependency in graph: cap traversal depth at 5 hops, flag circular dependency as architectural issue
- No topology data at all (day 1): emit signal with `topology_unknown`, proceed with best-effort layer-based analysis

---

#### resource-saturation-predictor

**Inputs:**
- Time-series metric data: CPU, memory, disk for last 24h–7d (from CloudWatch)
- Current resource utilization
- Kubernetes node capacity and current allocations
- Historical growth rate (from baseline store)

**Outputs:**
```json
{
  "resource": "memory",
  "node": "ip-10-0-1-5",
  "current_percent": 72.4,
  "trend": "linear_growth",
  "growth_rate_percent_per_hour": 1.2,
  "predicted_exhaustion": {
    "at_90pct": "2026-06-03T20:00:00Z",
    "at_100pct": "2026-06-04T02:00:00Z"
  },
  "confidence": 0.78,
  "recommendation": "Scale up node or reduce replica count before 20:00 UTC",
  "signal_type": "saturation_warning|saturation_imminent"
}
```

**Tools required:**
- `boto3.client('cloudwatch').get_metric_data()` — multi-metric time series for trend analysis
- `kubernetes.client.CoreV1Api.list_node()` — current node allocatable resources
- Python `numpy` or `scipy.stats.linregress` for trend calculation — no LLM needed for math
- **Claude sonnet-4-6** for recommendation generation and confidence contextualization

**Recommended model:** **claude-sonnet-4-6** for recommendation narrative. Trend calculation is Python/numpy.

**Extended thinking:** No

**Approx tokens/call:** ~1,500 input / ~400 output. Cost: ~$0.005/call.

**Error modes:**
- Non-linear growth (exponential): linear regression will underestimate; detect R² < 0.7 and flag `nonlinear_growth`
- Insufficient data points (< 12 hours of data): prediction unreliable — flag `insufficient_history`
- Metric drops after prediction (e.g., GC cleared memory): prediction was wrong — log, don't alert retroactively

---

### Layer 3: Remediators

---

#### pod-restarter

**Inputs:**
- Pod name, namespace
- Reason for restart (from RCA output)
- Owning deployment name (for rolling restart)
- Pre-flight data: deployment health status, replica count, PodDisruptionBudget

**Outputs:**
```json
{
  "action": "pod_restart",
  "target": {"namespace": "production", "deployment": "backend", "pod": "backend-abc123"},
  "method": "rolling_restart",
  "initiated_at": "2026-06-03T14:22:00Z",
  "pre_flight_checks": {
    "pdb_allows_disruption": true,
    "replica_count": 3,
    "min_available": 2
  },
  "status": "initiated|completed|failed",
  "completion_time": "2026-06-03T14:22:45Z",
  "verification": "new_pod_healthy=true, restart_count_reset=true"
}
```

**Tools required:**
- `kubernetes.client.AppsV1Api.read_namespaced_deployment(name, namespace)` — pre-flight: check current state
- `kubernetes.client.PolicyV1Api.read_namespaced_pod_disruption_budget()` — pre-flight: PDB check
- Rolling restart: `AppsV1Api.patch_namespaced_deployment(name, namespace, {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": timestamp}}}}})` — triggers rolling restart without downtime
- Single pod delete (if no deployment): `CoreV1Api.delete_namespaced_pod(name, namespace)` — K8s recreates automatically
- Post-action: `CoreV1Api.list_namespaced_pod(watch=True)` — watch for new pod to reach Running state

**SAFETY HARD STOP:** `delete_namespaced_pod` is the only destructive call permitted. It is safe because K8s immediately recreates it. No `delete_namespaced_deployment` or `delete_namespaced_namespace` is ever called.

**Recommended model:** **claude-sonnet-4-6** for pre-flight reasoning: "Is it safe to restart this pod right now given current cluster state?"

**Extended thinking:** No

**Approx tokens/call:** ~1,200 input / ~300 output. Cost: ~$0.004/call.

**Error modes:**
- PDB blocks restart (min available violated): abort, emit `restart_blocked_by_pdb`, escalate to human
- Deployment not found: pod is orphaned — restart via pod delete, note in incident log
- New pod enters CrashLoopBackOff after restart: remediation failed — escalate immediately
- Rolling restart stalls (pod stuck Pending): detect via watch stream, emit `remediation_stalled`, escalate

---

#### deployment-scaler

**Inputs:**
- Deployment name, namespace
- Target replica count (from RCA recommendation)
- Current replica count
- Reason for scale action

**Outputs:**
```json
{
  "action": "scale_deployment",
  "target": {"namespace": "production", "deployment": "backend"},
  "from_replicas": 2,
  "to_replicas": 4,
  "pre_flight_checks": {
    "namespace_quota_allows": true,
    "node_headroom_cpu_cores": 3.2,
    "node_headroom_memory_gb": 8.1,
    "resource_quota_remaining": {"cpu": "4", "memory": "8Gi"}
  },
  "status": "initiated|completed|failed",
  "initiated_at": "2026-06-03T14:22:00Z"
}
```

**Tools required:**
- `kubernetes.client.CoreV1Api.list_namespaced_resource_quota(namespace)` — pre-flight: quota check
- `kubernetes.client.CoreV1Api.list_node()` — pre-flight: node headroom (allocatable minus requested)
- `kubernetes.client.AppsV1Api.patch_namespaced_deployment_scale(name, namespace, {"spec": {"replicas": N}})` — scale action
- `kubernetes.client.AppsV1Api.read_namespaced_deployment(name, namespace)` — post-action verification

**PRE-FLIGHT MANDATORY:** Must verify (a) namespace resource quota has capacity, (b) at least 1 node has enough headroom for new pod resource requests. If either check fails → abort, escalate to human.

**Recommended model:** **claude-sonnet-4-6** for pre-flight reasoning and scale factor determination.

**Extended thinking:** No

**Approx tokens/call:** ~1,500 input / ~400 output. Cost: ~$0.005/call.

**Error modes:**
- `Forbidden` (403): RBAC doesn't allow scale → emit permission error, escalate
- Quota exceeded: abort scale, emit `quota_exceeded`, recommend quota increase to human
- Scale up increases load on Postgres (new pods open connections): cross-check with postgres-observer before scaling
- Scale to 0: never allowed — enforce minimum replicas = 1 in pre-flight check

---

#### resource-limit-adjuster

**Inputs:**
- Pod/deployment name, namespace
- Container name within pod
- Recommended CPU/memory limits (from RCA or saturation predictor)
- Current limits from deployment spec

**Outputs:**
```json
{
  "action": "adjust_resource_limits",
  "target": {"deployment": "backend", "container": "api"},
  "from": {"cpu_limit": "500m", "memory_limit": "512Mi"},
  "to": {"cpu_limit": "1000m", "memory_limit": "1Gi"},
  "pre_flight_checks": {
    "node_has_headroom": true,
    "quota_allows": true
  },
  "status": "initiated|completed|failed",
  "note": "Rolling restart required to apply changes"
}
```

**Tools required:**
- `kubernetes.client.AppsV1Api.read_namespaced_deployment()` — get current resource spec
- `kubernetes.client.AppsV1Api.patch_namespaced_deployment()` — patch containers[].resources.limits
- Rolling restart automatically triggered by K8s on deployment spec change
- `CoreV1Api.list_namespaced_resource_quota()` — quota pre-flight

**Recommended model:** **claude-sonnet-4-6** for limit recommendation reasoning (e.g., "OOMKilled at 512Mi — suggest 1Gi based on peak usage 480Mi").

**Extended thinking:** No

**Approx tokens/call:** ~800 input / ~200 output. Cost: ~$0.003/call.

**Error modes:**
- Vertical Pod Autoscaler (VPA) conflict: if VPA is managing this deployment, manual patch may be overridden → check for VPA first
- Memory limit set below current usage: would cause immediate OOMKill — validate new limit > current memory usage before applying
- CPU throttling worse than OOMKill: increasing memory but not CPU may shift the bottleneck — recommend both together

---

#### postgres-query-killer

**Inputs:**
- PID(s) of blocking or long-running queries (from postgres-observer)
- Query text (for classification)
- Duration threshold exceeded
- Classification from claude (application query vs. migration vs. maintenance)

**Outputs:**
```json
{
  "action": "terminate_query",
  "pid": 1234,
  "query_preview": "SELECT * FROM loans WHERE customer_id = ...",
  "query_classification": "application_query",
  "duration_before_kill": 45.2,
  "method": "pg_terminate_backend",
  "pre_flight_result": "safe_to_kill",
  "success": true,
  "blocked_queries_released": 3
}
```

**Tools required:**
- `asyncpg` query: `SELECT pg_cancel_backend($1)` — soft cancel (sends SIGINT, query can retry)
- `asyncpg` query: `SELECT pg_terminate_backend($1)` — hard terminate (sends SIGTERM, connection dropped)
- Pre-flight classification via **claude-haiku-3-5**: "Is this query a schema migration, a long-running ETL job, or a stuck application query?"
- `asyncpg` query: `SELECT * FROM pg_stat_activity WHERE pid = $1` — verify query still running before kill

**PRE-FLIGHT MANDATORY:** Classify query type before kill. Never kill: DDL (CREATE TABLE, ALTER TABLE, etc.), migrations (identified by `application_name = 'alembic'` or query containing `CREATE INDEX CONCURRENTLY`), or queries with `pg_try_advisory_lock` held.

**Recommended model:** **claude-haiku-3-5** for query classification (safe/unsafe to kill). Fast, cheap, sufficient.

**Extended thinking:** No

**Approx tokens/call:** ~500 input / ~100 output. Cost: ~$0.001/call.

**Error modes:**
- Query completes naturally before kill: `pg_terminate_backend` returns false → log as benign
- Migration killed accidentally: triggers schema corruption — prevent entirely with pre-flight classification
- pg_terminate_backend fails (superuser required for some processes): detect and escalate to human
- Killing one query triggers cascade (deferred constraints): rare, but monitor for new errors after kill action

---

#### secrets-refresher

**Inputs:**
- Secret ARN to refresh
- List of deployments/pods consuming this secret (from annotation scan)
- Current secret version being used by pods

**Outputs:**
```json
{
  "action": "refresh_secret",
  "secret_arn": "arn:aws:secretsmanager:...",
  "previous_version": "v3",
  "current_version": "v4",
  "affected_deployments": ["backend"],
  "method": "rolling_restart_to_pick_up_new_version",
  "status": "initiated|completed"
}
```

**Tools required:**
- `boto3.client('secretsmanager').get_secret_value(SecretId, VersionStage='AWSCURRENT')` — verify new version available
- `kubernetes.client.CoreV1Api.list_namespaced_secret()` — find K8s Secrets mounting the AWS secret (via external-secrets-operator or manual sync)
- If using External Secrets Operator: `kubernetes.client.CustomObjectsApi.patch_namespaced_custom_object()` — annotate ExternalSecret to force sync
- If using direct mounting: `AppsV1Api.patch_namespaced_deployment()` — annotate to trigger rolling restart

**Recommended model:** No Claude call needed. Deterministic workflow: check new version exists → trigger pod refresh → verify pods running new version.

**Extended thinking:** No

**Approx tokens/call:** 0 (no LLM call in normal path)

**Error modes:**
- Secret rotation in progress (AWSPENDING stage active): wait for rotation completion before refreshing
- External Secrets Operator not installed: fall back to manual K8s secret patch + rolling restart
- New secret version causes auth failure: detect via pod CrashLoopBackOff after restart → roll back annotation, escalate to human

---

#### circuit-breaker-toggler

**Inputs:**
- Service name to disable/re-enable
- Direction: `disable` or `enable`
- Reason (from RCA or manual trigger)
- Current traffic routing state

**Outputs:**
```json
{
  "action": "circuit_break",
  "service": "backend-api",
  "direction": "disable",
  "method": "k8s_service_selector_update",
  "from_selector": {"app": "backend", "version": "v1"},
  "to_selector": {"app": "backend", "version": "maintenance"},
  "traffic_impact": "100% of backend traffic redirected",
  "status": "applied",
  "re_enable_after_minutes": 5
}
```

**Tools required:**
- `kubernetes.client.CoreV1Api.patch_namespaced_service()` — update service selector to route away from failing pods
- OR: Istio VirtualService patch (if service mesh deployed): `CustomObjectsApi.patch_namespaced_custom_object()`
- `kubernetes.client.CoreV1Api.read_namespaced_service()` — verify selector change applied

**Recommended model:** **claude-sonnet-4-6** for circuit break reasoning: "Is a full circuit break warranted, or is a traffic weight shift (50/50) better given current state?"

**Extended thinking:** No

**Approx tokens/call:** ~800 input / ~200 output. Cost: ~$0.003/call.

**Error modes:**
- No maintenance selector pods running: circuit break would drop all traffic → abort, escalate to human
- Istio not deployed: fall back to K8s service selector method
- Circuit breaker left open too long: implement automatic re-enable timer (5 minutes default), alert human if service still unhealthy after re-enable

---

#### human-escalator

**Inputs:**
- Full incident context: signals, RCA output, confidence score, attempted remediations
- Reason for escalation: confidence < 0.8, destructive action required, remediation failed
- Affected services and severity

**Outputs:**
```json
{
  "escalation_id": "esc-20260603-001",
  "reason": "confidence_below_threshold",
  "confidence": 0.71,
  "rca_summary": "Likely database issue but cannot confirm without DBA access to pg_stat_bgwriter",
  "signals": [...],
  "recommended_human_actions": [
    "Check pg_stat_bgwriter for checkpoint frequency",
    "Review recent schema migrations in the last 2 hours"
  ],
  "dashboard_url": "https://monitor.atoloan.com/incidents/inc-20260603-001",
  "severity": "critical",
  "created_at": "2026-06-03T14:22:00Z"
}
```

**Tools required:**
- `write_escalation_to_db` (custom tool): persists escalation to incident database
- `publish_to_dashboard` (custom tool): pushes to dashboard WebSocket feed for real-time display
- **Claude sonnet-4-6** for generating human-readable summary and recommended actions

**Recommended model:** **claude-sonnet-4-6** — escalation summaries need to be clear and actionable for on-call engineers. Quality matters here.

**Extended thinking:** No

**Approx tokens/call:** ~2,000 input / ~600 output. Cost: ~$0.007/call.

**Error modes:**
- Dashboard WebSocket connection lost: fall back to writing escalation to DB + log; engineer sees on next page load
- Escalation DB write fails: retry 3x with backoff; if all fail, write to local file as last resort

---

### Layer 4: Documenters

---

#### incident-reporter

**Inputs:**
- Full incident record: all signals, RCA output, remediation actions taken, timeline
- Incident resolution confirmation (from observer — "pod now healthy", "error rate back to baseline")
- Affected SLO budgets

**Outputs:**
```json
{
  "incident_id": "inc-20260603-001",
  "title": "PostgreSQL Connection Pool Exhaustion — Backend API Degraded",
  "severity": "critical",
  "duration_minutes": 12,
  "started_at": "2026-06-03T14:22:00Z",
  "resolved_at": "2026-06-03T14:34:00Z",
  "root_cause": "N+1 query storm from backend deployment...",
  "services_affected": ["backend-api", "postgres"],
  "error_budget_burned_minutes": 12,
  "remediation_taken": ["postgres-query-killer", "deployment-scaler"],
  "human_intervention_required": false,
  "document_markdown": "# Incident Report — inc-20260603-001\n..."
}
```

**Tools required:**
- `write_incident_to_db` (custom tool): persists full incident document
- `publish_to_dashboard` (custom tool): makes incident available in incident history view
- **Claude sonnet-4-6** for generating the human-readable markdown incident document

**Recommended model:** **claude-sonnet-4-6** — incident reports must be clear, accurate, and professional. Haiku produces lower quality summaries.

**Extended thinking:** No

**Approx tokens/call:** ~3,000 input / ~1,500 output. Cost: ~$0.01/call.

**Error modes:**
- Incident data incomplete (some signals not captured): generate report with `[DATA MISSING]` markers, don't block on completeness
- Long incident (multi-hour): truncate timeline to key events before sending to Claude; full timeline stored in DB separately

---

#### timeline-builder

**Inputs:**
- Ordered list of all events: observations, diagnoses, actions, verification results with millisecond-precision timestamps
- Incident start and end time

**Outputs:**
```markdown
# Incident Timeline: inc-20260603-001

14:22:00 UTC — [OBSERVED] FastAPI p95 latency spiked to 680ms (baseline: 120ms)
14:22:02 UTC — [OBSERVED] PostgreSQL connection pool at 90% (85/95 connections active)
14:22:15 UTC — [DIAGNOSED] Root cause: connection pool exhaustion (confidence: 0.91)
14:22:16 UTC — [ACTED] Killed 3 blocking queries (PIDs 1234, 1235, 1236)
14:22:18 UTC — [ACTED] Scaled backend from 2 → 4 replicas
14:25:00 UTC — [VERIFIED] p95 latency returned to 130ms
14:34:00 UTC — [RESOLVED] All metrics within baseline
```

**Tools required:**
- `get_incident_events` (custom tool): fetches ordered event log from incident DB
- **Claude haiku-3-5** for formatting events into clean timeline markdown (simple formatting task)

**Recommended model:** **claude-haiku-3-5** — timeline formatting is mechanical. No complex reasoning required.

**Extended thinking:** No

**Approx tokens/call:** ~2,000 input / ~800 output. Cost: ~$0.004/call.

**Error modes:**
- Out-of-order events (clock skew between services): sort by timestamp before sending; note clock skew in timeline
- Duplicate events (same signal from multiple observers): deduplicate by event_id before formatting

---

#### slo-impact-recorder

**Inputs:**
- Incident duration
- Affected services with their SLO targets
- Error budget state before and after incident
- SLI measurements during incident

**Outputs:**
```json
{
  "incident_id": "inc-20260603-001",
  "slo_impacts": [{
    "service": "backend-api",
    "slo_target": 0.999,
    "error_budget_window_days": 30,
    "error_budget_total_minutes": 43.2,
    "error_budget_consumed_this_incident_minutes": 12,
    "error_budget_remaining_after_minutes": 31.2,
    "error_budget_remaining_percent": 72.2,
    "slo_violated": false
  }],
  "aggregate_slo_health": "healthy|warning|violated"
}
```

**Tools required:**
- `get_slo_state` (custom tool): reads current error budget state
- `update_slo_state` (custom tool): writes updated budget after incident
- Python calculations only — no LLM needed for math
- **Claude sonnet-4-6** only for generating the narrative SLO impact summary in incident report

**Recommended model:** **claude-sonnet-4-6** for narrative generation only. Math is Python.

**Extended thinking:** No

**Approx tokens/call:** ~500 input / ~200 output. Cost: ~$0.002/call.

**Error modes:**
- Incident spans multiple SLO windows (e.g., crosses midnight on 30-day window boundary): handle window boundary correctly
- SLO not yet defined for service (learning mode): skip SLO impact, note `slo_not_configured`

---

#### postmortem-drafter

**Inputs:**
- Incident report (from incident-reporter)
- Root cause analysis output
- Remediation actions taken and their results
- Similar past incidents (from incident history search)
- Service architecture context

**Outputs:**
```markdown
# Postmortem: PostgreSQL Connection Pool Exhaustion
**Incident:** inc-20260603-001 | **Severity:** Critical | **Duration:** 12 minutes

## What Happened
...

## Root Cause
...

## Timeline
...

## Impact
...

## Follow-Up Actions (3–5 items)
1. **[Backend]** Add connection pool monitoring alert at 80% threshold (not just 90%)
2. **[Backend]** Implement query timeout at application layer (30s max)
3. **[Postgres]** Enable pg_stat_statements to identify N+1 query patterns
4. **[Architecture]** Evaluate PgBouncer for connection pooling at proxy layer
5. **[Process]** Add load test with N+1 queries to CI pipeline for backend deployments
```

**Tools required:**
- `search_incident_history` (custom tool): finds similar past incidents for pattern matching
- `get_service_architecture` (custom tool): retrieves current service topology for context
- **Claude sonnet-4-6** for full postmortem generation — this is the highest quality output required from Layer 4.

**Recommended model:** **claude-sonnet-4-6**. The postmortem is the primary artifact engineers read. Quality is critical. If the incident was exceptionally complex (cascading failure across all 4 layers), use **claude-opus-4-8**.

**Extended thinking:** No for standard incidents; YES (budget: 6,000) for complex multi-layer postmortems.

**Approx tokens/call:** ~4,000 input / ~2,000 output. Cost: ~$0.015/call (Sonnet).

**Error modes:**
- Similar incident search returns no results (first time seeing this failure mode): generate postmortem without pattern context, note `first_occurrence`
- Postmortem action items are too generic: include service-specific context in prompt to force specific recommendations

---

## Claude API Features

### Tool Use / Function Calling

Tool use is defined via the `tools` parameter in the messages API. Each tool has a `name`, `description`, and `input_schema` (JSON Schema). Claude decides when to call tools and returns `tool_use` content blocks.

**Pattern for AWS SDK tools:**

```python
import anthropic

client = anthropic.Anthropic()

# Define tools for a diagnoser skill
tools = [
    {
        "name": "get_cloudwatch_metrics",
        "description": "Fetch CloudWatch metric statistics for an EC2 instance over a time window.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "EC2 instance ID, e.g. i-0abc123"},
                "metric_name": {"type": "string", "enum": ["CPUUtilization", "mem_used_percent", "disk_used_percent"]},
                "period_seconds": {"type": "integer", "default": 300},
                "lookback_minutes": {"type": "integer", "default": 15}
            },
            "required": ["instance_id", "metric_name"]
        }
    },
    {
        "name": "list_failing_pods",
        "description": "List all pods not in Running phase across specified namespaces.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespaces": {"type": "array", "items": {"type": "string"}},
                "phase_filter": {"type": "string", "enum": ["Failed", "Pending", "Unknown"]}
            },
            "required": ["namespaces"]
        }
    },
    {
        "name": "query_postgres_stat_activity",
        "description": "Query pg_stat_activity to retrieve currently running queries and their durations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_duration_seconds": {"type": "number", "default": 10},
                "include_idle": {"type": "boolean", "default": False}
            }
        }
    }
]

# Tool execution dispatcher — maps tool names to Python implementations
async def execute_tool(tool_name: str, tool_input: dict) -> dict:
    if tool_name == "get_cloudwatch_metrics":
        return await fetch_cloudwatch_metrics(**tool_input)
    elif tool_name == "list_failing_pods":
        return await list_k8s_failing_pods(**tool_input)
    elif tool_name == "query_postgres_stat_activity":
        return await query_pg_stat_activity(**tool_input)
    raise ValueError(f"Unknown tool: {tool_name}")

# Agentic tool use loop
async def run_diagnoser(system_prompt: str, user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=messages
        )
        
        # If Claude is done (no more tool calls), return text
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")
        
        # Process all tool_use blocks in this response
        if response.stop_reason == "tool_use":
            # Add Claude's response to conversation
            messages.append({"role": "assistant", "content": response.content})
            
            # Execute all tool calls (can be parallel)
            tool_results = []
            tool_calls = [b for b in response.content if b.type == "tool_use"]
            
            # Execute in parallel using asyncio.gather
            import asyncio
            results = await asyncio.gather(*[
                execute_tool(tc.name, tc.input) for tc in tool_calls
            ])
            
            for tc, result in zip(tool_calls, results):
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": json.dumps(result)
                })
            
            # Return tool results to Claude
            messages.append({"role": "user", "content": tool_results})
```

**Key implementation notes:**
- `tool_choice`: use `{"type": "auto"}` (default) for all diagnosers — let Claude decide when to call tools
- `tool_choice: {"type": "any"}` forces at least one tool call — use for remediators where action is always required
- Parallel tool calls: Claude often requests multiple tools at once; always execute them concurrently with `asyncio.gather`
- Tool result errors: return `{"type": "tool_result", "tool_use_id": tc.id, "is_error": True, "content": "error message"}` — Claude will reason about the failure
- Tool input validation: validate against JSON Schema before executing; return error result if invalid

---

### Multi-Agent Orchestration

The Atoloan Monitor uses a domain-partitioned orchestrator pattern. Three domain orchestrators (K8s, DB, Infra) run in parallel, each as a long-running asyncio task. A meta-orchestrator handles cross-domain correlation.

**Architecture:**

```
Meta-Orchestrator (claude-opus-4-8, only on cross-domain signals)
├── K8s Orchestrator (claude-sonnet-4-6, continuous)
│   ├── k8s-pod-observer
│   ├── k8s-node-observer
│   └── ...diagnosers and remediators for K8s domain
├── DB Orchestrator (claude-sonnet-4-6, continuous)
│   ├── postgres-observer
│   └── ...
└── Infra Orchestrator (claude-sonnet-4-6, continuous)
    ├── ec2-metrics-observer
    ├── security-group-auditor
    ├── secrets-health-checker
    └── ...
```

**Subagent spawning pattern:**

The Anthropic SDK does not have a native "spawn subagent" primitive as of 2025. The pattern is Python-level orchestration: the orchestrator Claude agent generates tool calls, and the tool implementations create new Claude API calls (which are effectively subagents).

```python
import asyncio
from anthropic import AsyncAnthropic
import json

client = AsyncAnthropic()

class DomainOrchestrator:
    """
    Each domain orchestrator maintains its own conversation context
    and runs as an independent asyncio task.
    """
    def __init__(self, domain: str, model: str = "claude-sonnet-4-6"):
        self.domain = domain
        self.model = model
        self.signal_queue: asyncio.Queue = asyncio.Queue()
        self.conversation_history: list = []
        self.shared_context: dict = {}  # Cross-domain signal bus
    
    async def process_signals(self):
        """Main loop: drain signal queue and invoke Claude for diagnosis."""
        while True:
            # Wait for signals (30s debounce window)
            signals = await self._drain_queue_with_debounce(timeout=30)
            if not signals:
                continue
            
            # Invoke Claude for this domain's analysis
            result = await self._invoke_claude(signals)
            
            # If cross-domain correlation needed, publish to meta-orchestrator
            if result.get("needs_cross_domain_correlation"):
                await self.shared_context["meta_queue"].put({
                    "domain": self.domain,
                    "signals": signals,
                    "diagnosis": result
                })
    
    async def _invoke_claude(self, signals: list) -> dict:
        # Each domain orchestrator call is an independent stateless Claude invocation
        # Conversation history is NOT maintained between incidents (too costly)
        # Context is injected via system prompt + cached topology
        response = await client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=self._build_system_prompt(),
            tools=self._get_domain_tools(),
            messages=[{
                "role": "user",
                "content": f"Analyze these signals from the {self.domain} domain: {json.dumps(signals)}"
            }]
        )
        # ... tool loop ...

class MetaOrchestrator:
    """
    Only invoked when multiple domain orchestrators have active incidents.
    Uses opus-4-8 for cross-domain root cause analysis.
    """
    async def correlate(self, domain_signals: list[dict]) -> dict:
        response = await client.messages.create(
            model="claude-opus-4-8",
            max_tokens=8192,
            thinking={"type": "enabled", "budget_tokens": 16000},
            system=META_ORCHESTRATOR_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Multiple domains are reporting incidents simultaneously. Determine if these are correlated: {json.dumps(domain_signals)}"
            }]
        )
        return self._parse_rca_response(response)

# Launch all orchestrators
async def main():
    meta_queue = asyncio.Queue()
    shared_context = {"meta_queue": meta_queue}
    
    k8s_orch = DomainOrchestrator("kubernetes", model="claude-sonnet-4-6")
    db_orch = DomainOrchestrator("database", model="claude-sonnet-4-6")
    infra_orch = DomainOrchestrator("infrastructure", model="claude-sonnet-4-6")
    
    for orch in [k8s_orch, db_orch, infra_orch]:
        orch.shared_context = shared_context
    
    meta_orch = MetaOrchestrator()
    
    async def meta_loop():
        while True:
            # Collect cross-domain signals with 5s window
            cross_domain = []
            try:
                while True:
                    item = await asyncio.wait_for(meta_queue.get(), timeout=5.0)
                    cross_domain.append(item)
            except asyncio.TimeoutError:
                pass
            
            if len(cross_domain) >= 2:  # Only correlate if multiple domains affected
                await meta_orch.correlate(cross_domain)
    
    await asyncio.gather(
        k8s_orch.process_signals(),
        db_orch.process_signals(),
        infra_orch.process_signals(),
        meta_loop()
    )
```

**Key multi-agent design decisions:**
- Each domain orchestrator is a separate long-running asyncio Task — not a separate process or thread
- Domain orchestrators do NOT share conversation history — each invocation is stateless; context comes from cached topology + DB state
- The meta-orchestrator is the only agent that needs cross-domain context in a single prompt
- Signal queue per orchestrator acts as the backpressure mechanism — signals pile up during heavy incidents without blocking observers
- Max 3 concurrent Claude API calls at any time (rate limit buffer); use `asyncio.Semaphore(3)`

---

### Extended Thinking

Extended thinking makes Claude's internal reasoning visible and improves quality on complex multi-step problems. For Atoloan Monitor, it is used exclusively by the root-cause-analyzer on multi-layer incidents.

**How to enable:**

```python
# Extended thinking requires: max_tokens >= budget_tokens + expected_output_tokens
response = client.messages.create(
    model="claude-opus-4-8",  # Extended thinking is most effective on Opus
    max_tokens=20000,  # Must be > budget_tokens
    thinking={
        "type": "enabled",
        "budget_tokens": 16000  # Thinking budget (not charged as output, charged as output in practice — check billing)
    },
    messages=[{"role": "user", "content": rca_prompt}],
    system=rca_system_prompt
)

# Parse response: thinking blocks come BEFORE text blocks
thinking_content = None
text_content = None

for block in response.content:
    if block.type == "thinking":
        thinking_content = block.thinking  # Internal reasoning — log but don't show to users
    elif block.type == "text":
        text_content = block.text  # Final output — surface to dashboard and incident report
```

**budget_tokens guidance:**

| Scenario | budget_tokens | Expected latency |
|----------|--------------|-----------------|
| Single-layer incident (one domain) | 4,000 | +10-15s |
| Two-layer incident (e.g., K8s + Postgres) | 8,000 | +20-30s |
| Full cascade (all 4 layers) | 16,000-32,000 | +40-90s |
| Postmortem for complex incident | 6,000 | +15-25s |

**Decision rule for enabling extended thinking:**
- Signals from 2+ domains → enable thinking, budget 8,000+
- Signals from 1 domain only → disable thinking (Sonnet without thinking is faster and sufficient)
- Postmortem drafting → optional, enable if incident was complex (>3 layers, >30 minutes)

**Important constraints:**
- Extended thinking does NOT support `temperature` parameter — must be omitted or set to 1
- Streaming with thinking: thinking blocks stream as `content_block_start` with `type: thinking` — filter these from dashboard stream
- `budget_tokens` minimum is 1,024; maximum varies by model (check Anthropic docs for current limits)
- Thinking blocks are billed as output tokens — factor into cost estimates
- Tool use + extended thinking: Claude thinks before deciding which tools to call. The thinking block appears before tool_use blocks in the response.

**Latency management:**
- Run extended thinking asynchronously — do not block the dashboard update while RCA is in progress
- Emit a `rca_in_progress` event to the dashboard immediately when signals are bundled
- Stream the RCA result to the dashboard via WebSocket as soon as it completes

---

### Prompt Caching

Prompt caching reduces token costs by caching large, stable context between API calls. For Atoloan Monitor, infrastructure topology and baselines change slowly — ideal for caching.

**Cache TTL:** 5 minutes (Anthropic's current default cache TTL as of 2025). Content must be re-sent after 5 minutes if the cache has not been hit. The cache resets on each cache hit within the TTL window.

**What to cache (in order of cache_control placement):**

```python
# Pattern: cache_control goes on the LAST message block in a cacheable sequence
# Anthropic allows up to 4 cache breakpoints per request

messages_create_kwargs = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 4096,
    "system": [
        # Block 1: Static system role — CACHE THIS (changes never)
        {
            "type": "text",
            "text": AGENT_IDENTITY_AND_ROLE_PROMPT,  # ~500 tokens
            "cache_control": {"type": "ephemeral"}
        },
        # Block 2: Infrastructure topology — CACHE THIS (changes rarely, ~30 min)
        {
            "type": "text",
            "text": infrastructure_topology_context,  # ~2,000-4,000 tokens: pod→node→EC2 map, service dependency graph
            "cache_control": {"type": "ephemeral"}
        },
        # Block 3: Baseline statistics — CACHE THIS (updated every hour)
        {
            "type": "text",
            "text": baseline_context,  # ~1,000-2,000 tokens: normal ranges per metric
            "cache_control": {"type": "ephemeral"}
        }
        # Block 4: Recent incident history — CACHE THIS if checking frequently
        # No cache_control on the last block (the dynamic signal bundle)
    ],
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"Analyze these current signals: {json.dumps(current_signals)}"
                    # No cache_control — this changes every call
                }
            ]
        }
    ]
}
```

**Cache economics for Atoloan Monitor:**
- Infrastructure topology: ~3,000 tokens, cached across all diagnoser invocations
- Baseline context: ~1,500 tokens, cached per hour
- System prompt: ~500 tokens, always cached
- Per-cycle savings: ~5,000 tokens cached = ~$0.005 saved per Sonnet call (at $3/1M input cached tokens)
- With 60 diagnoser calls/hour, cache saves ~$0.30/hour vs. uncached

**Implementation detail — cache invalidation:**
- Rebuild topology cache on: pod added/removed, deployment scaled, new EC2 instance
- Rebuild baseline cache on: every hour boundary, after 7-day learning period completes
- Do NOT invalidate cache on every incident — cache content is the stable background context

**Minimum cacheable size:** Anthropic requires at least 1,024 tokens in a cached block for caching to activate. Verify topology context exceeds this minimum.

---

### Streaming

Streaming delivers Claude's response token-by-token as it's generated, enabling real-time dashboard updates.

**Use cases in Atoloan Monitor:**
1. Live agent action feed: stream RCA reasoning and remediation decisions to the dashboard
2. Live incident timeline: as incident-reporter generates the document, stream it to the active incident view
3. Postmortem generation: stream as Claude writes the postmortem

**Implementation with async streaming:**

```python
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

async def stream_rca_to_dashboard(signals: list, websocket) -> str:
    """
    Stream root cause analysis to the dashboard WebSocket in real time.
    """
    full_text = ""
    
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=rca_system_prompt,
        messages=[{
            "role": "user",
            "content": f"Diagnose: {json.dumps(signals)}"
        }]
    ) as stream:
        async for event in stream:
            # Filter event types for dashboard consumption
            if event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    chunk = event.delta.text
                    full_text += chunk
                    # Push to dashboard via WebSocket
                    await websocket.send_json({
                        "type": "rca_chunk",
                        "content": chunk,
                        "incident_id": current_incident_id
                    })
                elif event.delta.type == "thinking_delta":
                    # Do NOT send thinking to dashboard — internal reasoning only
                    pass  # Log internally if desired
            
            elif event.type == "content_block_start":
                if event.content_block.type == "tool_use":
                    # Signal dashboard: agent is executing a tool
                    await websocket.send_json({
                        "type": "tool_executing",
                        "tool_name": event.content_block.name,
                        "incident_id": current_incident_id
                    })
            
            elif event.type == "message_stop":
                await websocket.send_json({
                    "type": "rca_complete",
                    "incident_id": current_incident_id
                })
    
    return full_text
```

**Streaming with tool use — important note:**

When streaming is combined with tool use, tool inputs stream as `input_json_delta` events. The full tool input is only available after the `content_block_stop` event for that tool_use block. Execute tool calls only after their input is complete:

```python
async def stream_with_tools(messages: list, tools: list, websocket):
    current_tool_inputs = {}  # accumulate streaming tool input JSON
    
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        tools=tools,
        messages=messages
    ) as stream:
        async for event in stream:
            if event.type == "content_block_start":
                if event.content_block.type == "tool_use":
                    current_tool_inputs[event.content_block.id] = {
                        "name": event.content_block.name,
                        "input_str": ""
                    }
            
            elif event.type == "content_block_delta":
                if event.delta.type == "input_json_delta":
                    tool_id = event.index  # needs cross-referencing with block index
                    # Accumulate JSON string — parse only when complete
                
            elif event.type == "content_block_stop":
                # Tool input is now complete — safe to parse and execute
                pass
        
        # Get final message after stream completes
        final_message = await stream.get_final_message()
        return final_message
```

**FastAPI WebSocket integration:**

```python
from fastapi import FastAPI, WebSocket
import asyncio

app = FastAPI()

@app.websocket("/ws/agent-feed")
async def agent_feed(websocket: WebSocket):
    await websocket.accept()
    # Subscribe to agent event bus
    async for event in agent_event_bus.subscribe():
        await websocket.send_json(event)
```

---

### Batch API

The Batch API is used for non-time-sensitive, periodic analysis jobs. It processes requests asynchronously and returns results within 24 hours. Cost is 50% of standard API pricing.

**Use cases in Atoloan Monitor:**
- Nightly log review: summarize last 24h of logs across all services at 2:00 AM
- Weekly SLO report: comprehensive SLO performance analysis for the last 7 days
- Baseline recalculation: weekly re-computation of per-metric, per-hour-of-day baselines from historical data

**Implementation:**

```python
import anthropic
import json

client = anthropic.Anthropic()

def submit_nightly_log_review(log_batches: list[dict]) -> str:
    """
    Submit a batch of log analysis jobs. Returns batch_id.
    Each request in the batch analyzes one service's logs for the day.
    """
    requests = []
    for service_logs in log_batches:
        requests.append({
            "custom_id": f"log-review-{service_logs['service']}-{service_logs['date']}",
            "params": {
                "model": "claude-haiku-3-5",  # Use cheaper model for batch jobs
                "max_tokens": 2048,
                "messages": [{
                    "role": "user",
                    "content": f"Summarize anomalies and notable events in these logs for {service_logs['service']} on {service_logs['date']}:\n\n{service_logs['log_text']}"
                }]
            }
        })
    
    batch = client.messages.batches.create(requests=requests)
    return batch.id

def submit_weekly_slo_report(slo_data: dict) -> str:
    """Submit weekly SLO report generation as a batch job."""
    batch = client.messages.batches.create(
        requests=[{
            "custom_id": f"slo-report-{slo_data['week']}",
            "params": {
                "model": "claude-sonnet-4-6",
                "max_tokens": 4096,
                "messages": [{
                    "role": "user",
                    "content": f"Generate a weekly SLO report for this data: {json.dumps(slo_data)}"
                }]
            }
        }]
    )
    return batch.id

def poll_batch_results(batch_id: str) -> list[dict]:
    """
    Poll batch status until complete, then retrieve results.
    In production, use a scheduled job rather than blocking poll.
    """
    import time
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            break
        time.sleep(60)  # Check every minute
    
    results = []
    for result in client.messages.batches.results(batch_id):
        if result.result.type == "succeeded":
            results.append({
                "custom_id": result.custom_id,
                "content": result.result.message.content[0].text
            })
        else:
            results.append({
                "custom_id": result.custom_id,
                "error": result.result.error.error.message
            })
    return results

# Schedule with APScheduler or cron-equivalent
# from apscheduler.schedulers.asyncio import AsyncIOScheduler
# scheduler.add_job(run_nightly_log_review, 'cron', hour=2, minute=0)
```

**Batch API operational notes:**
- Minimum 24-hour SLA for results — do not use for anything time-sensitive
- Batch requests cannot be cancelled once submitted
- Each request in the batch is independent — errors in one don't affect others
- Use `custom_id` to match results back to requests
- Results available for 29 days after batch completes

---

## Integration Patterns

### Kubernetes Python Client Integration

```python
from kubernetes import client as k8s_client, config, watch
from kubernetes.client.exceptions import ApiException
import asyncio
import json

def get_k8s_client():
    """Load K8s config from in-cluster service account or local kubeconfig."""
    try:
        config.load_incluster_config()  # Running inside K8s pod
    except config.ConfigException:
        config.load_kube_config()  # Local development
    
    return k8s_client

def build_k8s_tools(core_v1: k8s_client.CoreV1Api, apps_v1: k8s_client.AppsV1Api) -> list:
    """Build Claude tool definitions for K8s operations."""
    return [
        {
            "name": "list_pod_events",
            "description": "List recent events for a specific pod (warnings, restarts, OOMKills)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 20}
                },
                "required": ["namespace", "pod_name"]
            }
        },
        {
            "name": "get_pod_resource_usage",
            "description": "Get current CPU and memory usage for a pod (requires metrics-server)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod_name": {"type": "string"}
                },
                "required": ["namespace", "pod_name"]
            }
        }
    ]

# Tool implementation — maps Claude tool call to K8s SDK
async def execute_k8s_tool(tool_name: str, tool_input: dict,
                            core_v1: k8s_client.CoreV1Api,
                            apps_v1: k8s_client.AppsV1Api) -> dict:
    loop = asyncio.get_event_loop()
    
    if tool_name == "list_pod_events":
        # K8s SDK is synchronous — run in executor to avoid blocking asyncio
        events = await loop.run_in_executor(
            None,
            lambda: core_v1.list_namespaced_event(
                namespace=tool_input["namespace"],
                field_selector=f"involvedObject.name={tool_input['pod_name']}",
                limit=tool_input.get("limit", 20)
            )
        )
        return {
            "events": [
                {
                    "type": e.type,
                    "reason": e.reason,
                    "message": e.message,
                    "count": e.count,
                    "last_timestamp": str(e.last_timestamp)
                }
                for e in events.items
            ]
        }

# Event-driven watch — must run in a separate thread (watch.Watch() is blocking)
def start_pod_watch(namespace: str, signal_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    """Run K8s watch in a separate thread, push events to asyncio queue."""
    core_v1 = k8s_client.CoreV1Api()
    w = watch.Watch()
    
    resource_version = ""
    while True:
        try:
            for event in w.stream(
                core_v1.list_namespaced_pod,
                namespace=namespace,
                resource_version=resource_version,
                timeout_seconds=300  # Re-establish every 5 minutes
            ):
                resource_version = event["object"].metadata.resource_version
                # Safely put event into asyncio queue from sync thread
                asyncio.run_coroutine_threadsafe(
                    signal_queue.put(event),
                    loop
                )
        except ApiException as e:
            if e.status == 410:  # Gone — resourceVersion too old
                resource_version = ""  # Re-list from beginning
            else:
                raise
```

**Critical K8s integration note:** The `kubernetes-client/python` library is synchronous. Always run K8s API calls in `asyncio.get_event_loop().run_in_executor(None, ...)` to avoid blocking the asyncio event loop. The `watch.Watch()` stream must run in a separate OS thread.

---

### boto3 Integration

```python
import boto3
import asyncio
from functools import partial
import json

# Use separate boto3 sessions per async context to avoid thread safety issues
def get_aws_clients():
    session = boto3.Session(region_name="us-east-1")
    return {
        "cloudwatch": session.client("cloudwatch"),
        "ec2": session.client("ec2"),
        "secretsmanager": session.client("secretsmanager"),
        "logs": session.client("logs"),
        "sqs": session.client("sqs")
    }

async def fetch_cloudwatch_metrics(
    instance_id: str,
    metric_name: str,
    period_seconds: int = 300,
    lookback_minutes: int = 15
) -> dict:
    """Async wrapper for boto3 CloudWatch — runs in executor."""
    loop = asyncio.get_event_loop()
    cw = boto3.client("cloudwatch", region_name="us-east-1")
    
    from datetime import datetime, timezone, timedelta
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=lookback_minutes)
    
    result = await loop.run_in_executor(
        None,
        partial(
            cw.get_metric_statistics,
            Namespace="AWS/EC2",
            MetricName=metric_name,
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start_time,
            EndTime=end_time,
            Period=period_seconds,
            Statistics=["Average", "Maximum"]
        )
    )
    
    if not result["Datapoints"]:
        return {"error": f"No datapoints for {metric_name} on {instance_id}"}
    
    latest = max(result["Datapoints"], key=lambda x: x["Timestamp"])
    return {
        "instance_id": instance_id,
        "metric": metric_name,
        "average": latest["Average"],
        "maximum": latest["Maximum"],
        "timestamp": latest["Timestamp"].isoformat()
    }

# SQS polling for EventBridge-triggered events (event-driven, not polling CloudWatch)
async def poll_alarm_queue(queue_url: str, signal_queue: asyncio.Queue):
    """Poll SQS queue for CloudWatch Alarm state changes."""
    loop = asyncio.get_event_loop()
    sqs = boto3.client("sqs", region_name="us-east-1")
    
    while True:
        messages = await loop.run_in_executor(
            None,
            partial(
                sqs.receive_message,
                QueueUrl=queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=20  # Long polling — blocks up to 20s
            )
        )
        
        for msg in messages.get("Messages", []):
            event = json.loads(msg["Body"])
            await signal_queue.put(event)
            
            # Delete processed message
            await loop.run_in_executor(
                None,
                partial(
                    sqs.delete_message,
                    QueueUrl=queue_url,
                    ReceiptHandle=msg["ReceiptHandle"]
                )
            )
```

**IAM policy required for the monitoring EC2 (minimum permissions):**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics", "cloudwatch:DescribeAlarms"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["ec2:DescribeInstances", "ec2:DescribeSecurityGroups", "ec2:DescribeSecurityGroupRules"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret", "secretsmanager:ListSecrets"], "Resource": "arn:aws:secretsmanager:us-east-1:ACCOUNT_ID:secret:atoloan/*"},
    {"Effect": "Allow", "Action": ["logs:FilterLogEvents", "logs:StartQuery", "logs:GetQueryResults", "logs:DescribeLogGroups"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], "Resource": "arn:aws:sqs:us-east-1:ACCOUNT_ID:atoloan-monitor-*"}
  ]
}
```

---

### asyncpg / psycopg2 Integration

Use `asyncpg` (not psycopg2) for the monitoring agent. asyncpg is natively async, faster, and cleaner in an asyncio context. psycopg2 requires the same run_in_executor pattern as K8s/boto3.

```python
import asyncpg
import boto3
import json
from typing import Optional

# Connection pool — limit to 3 connections (monitoring must not stress the DB)
MONITOR_POOL: Optional[asyncpg.Pool] = None

async def get_pg_pool() -> asyncpg.Pool:
    global MONITOR_POOL
    if MONITOR_POOL is None:
        # Fetch password from Secrets Manager at startup
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        secret = json.loads(
            sm.get_secret_value(SecretId="atoloan/postgres/monitor")["SecretString"]
        )
        
        MONITOR_POOL = await asyncpg.create_pool(
            host=secret["host"],
            port=secret.get("port", 5432),
            database=secret["database"],
            user=secret["username"],
            password=secret["password"],
            min_size=1,
            max_size=3,  # Hard limit: monitoring must not consume app connections
            command_timeout=30,  # Cancel queries that run > 30s
            statement_cache_size=50
        )
    return MONITOR_POOL

async def query_stat_activity(min_duration_seconds: float = 10.0) -> list[dict]:
    """Fetch slow/blocking queries from pg_stat_activity."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                pid,
                now() - query_start AS duration,
                query,
                state,
                wait_event_type,
                wait_event,
                application_name,
                client_addr::text,
                backend_type,
                -- Find blocking PIDs
                (SELECT array_agg(blocking.pid)
                 FROM pg_stat_activity blocking
                 WHERE blocking.pid != sa.pid
                   AND pg_blocking_pids(sa.pid) @> ARRAY[blocking.pid]) AS blocking_pids
            FROM pg_stat_activity sa
            WHERE state != 'idle'
              AND query_start IS NOT NULL
              AND now() - query_start > make_interval(secs => $1)
            ORDER BY duration DESC
            LIMIT 20
        """, min_duration_seconds)
        
        return [dict(row) for row in rows]

async def kill_query(pid: int, hard_terminate: bool = False) -> bool:
    """
    Cancel or terminate a query by PID.
    Returns True if the query was still running and was cancelled.
    """
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        if hard_terminate:
            result = await conn.fetchval("SELECT pg_terminate_backend($1)", pid)
        else:
            result = await conn.fetchval("SELECT pg_cancel_backend($1)", pid)
        return result  # True = successfully cancelled, False = pid not found

# Connection pool monitoring — report pool state to observer
async def get_pool_stats() -> dict:
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                count(*) FILTER (WHERE state = 'active') AS active,
                count(*) FILTER (WHERE state = 'idle') AS idle,
                count(*) FILTER (WHERE wait_event_type = 'Client') AS waiting,
                count(*) AS total,
                (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS max_connections
            FROM pg_stat_activity
        """)
        return dict(row)
```

**asyncpg vs psycopg2 decision:**
- asyncpg: native async, binary protocol, faster, simpler in asyncio context — **use this**
- psycopg2: sync only, requires run_in_executor, well-tested but adds complexity
- psycopg3: has async support but asyncpg is more mature for pure async use cases
- Never use `asyncio.to_thread` for database access — use a proper async driver

---

### FastAPI Backend

The monitoring agent exposes a FastAPI backend that serves:
1. REST endpoints for the React dashboard (incident history, SLO status, topology)
2. WebSocket endpoint for real-time agent action feed
3. Webhook endpoints for Alertmanager and CloudWatch Alarm callbacks
4. Internal agent control endpoints (start/stop learning mode, etc.)

```python
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import json

# Connection manager for dashboard WebSocket clients
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    
    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
    
    async def broadcast(self, message: dict):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except WebSocketDisconnect:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)

manager = ConnectionManager()

# Startup: launch monitoring agent tasks
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start domain orchestrators as background tasks
    k8s_task = asyncio.create_task(k8s_orchestrator.run())
    db_task = asyncio.create_task(db_orchestrator.run())
    infra_task = asyncio.create_task(infra_orchestrator.run())
    
    yield  # Application is running
    
    # Shutdown: cancel orchestrator tasks
    for task in [k8s_task, db_task, infra_task]:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(title="Atoloan Monitor API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://monitor.atoloan.com"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# Real-time dashboard feed
@app.websocket("/ws/agent-feed")
async def agent_feed(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; agent events pushed via manager.broadcast()
            await asyncio.sleep(30)
            await websocket.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# REST endpoints
@app.get("/api/incidents")
async def list_incidents(limit: int = 50, offset: int = 0):
    return await incident_db.list_incidents(limit=limit, offset=offset)

@app.get("/api/incidents/{incident_id}")
async def get_incident(incident_id: str):
    return await incident_db.get_incident(incident_id)

@app.get("/api/slo/status")
async def slo_status():
    return await slo_store.get_all_current_budgets()

@app.get("/api/health")
async def infrastructure_health():
    return await topology_cache.get_current_health_snapshot()

# Webhook receiver for Alertmanager
@app.post("/webhooks/alertmanager")
async def alertmanager_webhook(payload: dict, background_tasks: BackgroundTasks):
    # Quick acknowledgment, process in background
    background_tasks.add_task(process_alertmanager_payload, payload)
    return {"status": "accepted"}

# Publish agent events to dashboard
async def publish_agent_event(event: dict):
    """Called by orchestrators to broadcast events to all connected dashboard clients."""
    await manager.broadcast(event)
```

---

### Tool Use with Async Python

The Anthropic Python SDK `AsyncAnthropic` client is the correct choice for asyncio integration. The synchronous client blocks the event loop.

```python
from anthropic import AsyncAnthropic
import asyncio
import json

client = AsyncAnthropic()  # Use AsyncAnthropic, not Anthropic, in async context

async def run_skill_with_tools(
    skill_name: str,
    system_prompt: str,
    user_message: str,
    tools: list,
    tool_executor,  # Callable[[str, dict], Awaitable[dict]]
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    use_thinking: bool = False,
    thinking_budget: int = 8000
) -> dict:
    """
    Generic async tool-use loop for any skill.
    Handles multi-turn tool use, parallel tool execution, and error recovery.
    """
    messages = [{"role": "user", "content": user_message}]
    
    # Build create kwargs
    create_kwargs = {
        "model": model,
        "max_tokens": max_tokens + (thinking_budget if use_thinking else 0),
        "system": system_prompt,
        "tools": tools,
        "messages": messages
    }
    
    if use_thinking:
        create_kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
    
    max_iterations = 10  # Safety: prevent infinite tool loops
    
    for iteration in range(max_iterations):
        response = await client.messages.create(**create_kwargs)
        
        if response.stop_reason == "end_turn":
            # Extract text from response
            text_blocks = [b.text for b in response.content if b.type == "text"]
            return {
                "success": True,
                "output": "\n".join(text_blocks),
                "iterations": iteration + 1
            }
        
        if response.stop_reason == "tool_use":
            # Add Claude's response to conversation
            messages.append({"role": "assistant", "content": response.content})
            
            # Execute all tool calls concurrently
            tool_calls = [b for b in response.content if b.type == "tool_use"]
            
            results = await asyncio.gather(*[
                _safe_tool_call(tc.id, tc.name, tc.input, tool_executor)
                for tc in tool_calls
            ], return_exceptions=False)
            
            messages.append({"role": "user", "content": results})
            create_kwargs["messages"] = messages
            continue
        
        # Unexpected stop reason
        break
    
    return {"success": False, "error": f"Max iterations ({max_iterations}) reached"}

async def _safe_tool_call(tool_use_id: str, tool_name: str, tool_input: dict, executor) -> dict:
    """Execute a tool call with error handling."""
    try:
        result = await executor(tool_name, tool_input)
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": json.dumps(result)
        }
    except Exception as e:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": True,
            "content": f"Tool execution failed: {type(e).__name__}: {str(e)}"
        }

# Semaphore to limit concurrent Claude API calls (rate limit protection)
_claude_semaphore = asyncio.Semaphore(5)

async def rate_limited_skill_call(*args, **kwargs):
    async with _claude_semaphore:
        return await run_skill_with_tools(*args, **kwargs)
```

**AsyncAnthropic notes:**
- Always use `AsyncAnthropic()` (not `Anthropic()`) when inside asyncio — the sync client blocks the entire event loop on API calls
- The `AsyncAnthropic` client uses `httpx.AsyncClient` internally
- API key is read from `ANTHROPIC_API_KEY` environment variable automatically — do not pass it explicitly (it'll appear in logs)
- Connection pooling is handled internally; share a single `AsyncAnthropic()` instance across the application

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Claude API (tool use, caching, streaming, extended thinking) | HIGH | Stable API, well-documented, matches training knowledge through Aug 2025 |
| Claude model recommendations (Haiku/Sonnet/Opus) | HIGH | Based on documented model characteristics; model IDs confirmed from env |
| Kubernetes Python client patterns | HIGH | kubernetes-client/python API is stable; sync-in-executor pattern well-established |
| boto3 integration patterns | HIGH | AWS SDK is mature; SQS long-polling, CloudWatch patterns are standard |
| asyncpg patterns | HIGH | API is stable, pool configuration is standard |
| Batch API | MEDIUM | Implementation pattern is correct; exact result polling API may have minor changes |
| Exact prompt caching TTL | MEDIUM | 5-minute TTL confirmed as of 2025; verify against current Anthropic docs |
| Extended thinking budget_tokens limits | MEDIUM | Min 1,024 confirmed; max depends on model/version — verify current limits |
| Token cost estimates per skill | MEDIUM | Rough order-of-magnitude; actual costs depend on prompt content length |

## Gaps Requiring Phase-Specific Research

1. **External Secrets Operator vs. manual secret sync**: If ESO is already deployed in the K8s cluster, the secrets-refresher pattern changes significantly. Investigate cluster configuration in Phase 1.

2. **Prometheus vs. CloudWatch for FastAPI metrics**: If `prometheus-fastapi-instrumentator` is not already installed, the fastapi-trace-observer needs a different data source (either add Prometheus or use CloudWatch custom metrics). Investigate in Phase 1.

3. **K8s cluster version and API availability**: `PolicyV1Api` for PodDisruptionBudget is available from K8s 1.21+. Verify cluster version before using PDB checks.

4. **Istio service mesh presence**: The circuit-breaker-toggler uses K8s service selectors as fallback. If Istio is deployed, use VirtualService weight routing instead for more granular control.

5. **CloudWatch Agent deployment**: `mem_used_percent` and `disk_used_percent` require CloudWatch Agent on EC2 instances. If not deployed, memory and disk metrics will be unavailable — work around with K8s metrics-server for container metrics.

6. **Extended thinking billing**: As of 2025, extended thinking tokens (budget_tokens) are billed at the output token rate. Verify current billing model before setting large thinking budgets.
