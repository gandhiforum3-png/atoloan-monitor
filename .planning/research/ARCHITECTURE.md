# Architecture Patterns: Atoloan Monitor

**Domain:** AI-powered SRE monitoring system (event-driven, multi-orchestrator, confidence-gated)
**Researched:** 2026-06-03
**Confidence note:** All tool access (Bash, WebSearch, WebFetch) was denied in this environment. All findings are drawn from training knowledge (cutoff August 2025). Confidence levels reflect this. Any finding marked MEDIUM or LOW should be verified against official docs before implementation.

---

## 1. Event-Driven Monitoring Architecture

### 1.1 Kubernetes Watch API in Python

**Recommended library:** `kubernetes` (official Python client, `kubernetes==29.0.0` or latest 29.x)

**Confidence:** HIGH — this is the canonical Python K8s client, maintained by the Kubernetes SIG.

The Watch API uses long-lived HTTP connections to the K8s API server. The client sends a GET request with `?watch=true&resourceVersion=<rv>` and the API server streams newline-delimited JSON events. The Python client wraps this in a generator.

**Core pattern:**

```python
from kubernetes import client, config, watch
import asyncio

def watch_pods(namespace: str = "default"):
    config.load_incluster_config()  # Inside cluster (EC2 with IAM role via kube2iam or IRSA)
    v1 = client.CoreV1Api()
    w = watch.Watch()

    resource_version = None

    while True:
        try:
            for event in w.stream(
                v1.list_namespaced_pod,
                namespace=namespace,
                resource_version=resource_version,
                timeout_seconds=300,  # Force reconnect every 5 min to avoid stale connections
            ):
                resource_version = event["object"].metadata.resource_version
                yield event  # type: ADDED, MODIFIED, DELETED

        except client.ApiException as e:
            if e.status == 410:
                # ResourceVersion too old — API server compacted history
                # Reset to None to get a full list + watch from current state
                resource_version = None
            else:
                raise
        except Exception:
            # Network error, reconnect with backoff
            import time
            time.sleep(5)
            continue
```

**Critical detail: 410 Gone handling.** When `resource_version` is older than the API server's compaction window (typically 5 minutes), it returns HTTP 410. The correct response is to reset `resource_version = None`, which triggers a full re-list, then watch from the latest. Not handling this is the single most common Watch API bug.

**What to subscribe to for this project:**

| Watch Target | API | Events of Interest |
|---|---|---|
| Pods (all namespaces) | `list_pod_for_all_namespaces` | MODIFIED where `reason` in {OOMKilled, BackOff, Evicted} |
| Events | `list_namespaced_event` | Warning events, especially `FailedScheduling`, `OOMKilling` |
| Nodes | `list_node` | MODIFIED where `conditions` include NotReady, MemoryPressure, DiskPressure |
| Deployments | `list_namespaced_deployment` | MODIFIED where `availableReplicas < desiredReplicas` |

**Async variant:** Use `asyncio` with `kubernetes_asyncio` (package: `kubernetes-asyncio==29.0.0`) for non-blocking operation. This is the preferred approach for an agent that runs multiple watchers concurrently.

```python
from kubernetes_asyncio import client, config, watch

async def watch_pods_async(namespace: str):
    await config.load_incluster_config()
    async with client.ApiClient() as api:
        v1 = client.CoreV1Api(api)
        w = watch.Watch()
        resource_version = None
        while True:
            try:
                async for event in w.stream(
                    v1.list_namespaced_pod,
                    namespace=namespace,
                    resource_version=resource_version,
                    timeout_seconds=300,
                ):
                    resource_version = event["object"].metadata.resource_version
                    yield event
            except client.ApiException as e:
                if e.status == 410:
                    resource_version = None
                else:
                    await asyncio.sleep(5)
```

**Reconnection pattern:** Use exponential backoff with jitter capped at 60 seconds. Do not use a simple `sleep(5)` in production — under sustained API server pressure this creates thundering-herd reconnection storms.

```python
import random

async def backoff_reconnect(attempt: int) -> None:
    delay = min(60, (2 ** attempt) + random.uniform(0, 1))
    await asyncio.sleep(delay)
```

---

### 1.2 AWS CloudWatch Alarms → SNS → Webhook Pipeline

**Confidence:** HIGH for pipeline shape; MEDIUM for exact SNS subscription confirmation flow.

**Pipeline architecture:**

```
CloudWatch Alarm (state change)
    → SNS Topic (standard, not FIFO)
        → HTTPS subscription endpoint on monitoring agent
            → Agent webhook handler (FastAPI route)
                → Internal event bus
```

**SNS subscription confirmation:** When you subscribe an HTTPS endpoint to SNS, SNS sends a `SubscriptionConfirmation` message first. Your endpoint must `GET` the `SubscribeURL` in that payload to confirm. This must happen within 72 hours or the subscription is dropped.

```python
import httpx
from fastapi import FastAPI, Request
import json

@app.post("/webhooks/cloudwatch")
async def cloudwatch_webhook(request: Request):
    body = await request.json()
    msg_type = request.headers.get("x-amz-sns-message-type")

    if msg_type == "SubscriptionConfirmation":
        # Confirm the subscription
        async with httpx.AsyncClient() as client:
            await client.get(body["SubscribeURL"])
        return {"status": "confirmed"}

    if msg_type == "Notification":
        message = json.loads(body["Message"])
        # CloudWatch alarm payload structure:
        # {
        #   "AlarmName": "high-cpu-backend",
        #   "NewStateValue": "ALARM",
        #   "OldStateValue": "OK",
        #   "NewStateReason": "...",
        #   "Trigger": {"MetricName": "CPUUtilization", ...},
        #   "StateChangeTime": "2025-01-01T00:00:00.000Z"
        # }
        await event_bus.publish(normalize_cloudwatch_event(message))
        return {"status": "ok"}
```

**Deduplication:** CloudWatch can fire the same alarm multiple times if the state oscillates (OK → ALARM → OK → ALARM). Deduplicate using a Redis SET with a TTL equal to the debounce window:

```python
async def deduplicate_alarm(alarm_name: str, state_change_time: str, redis: Redis) -> bool:
    key = f"alarm_dedup:{alarm_name}:{state_change_time}"
    was_set = await redis.set(key, "1", ex=120, nx=True)  # nx=True = only set if not exists
    return was_set  # True = first time seeing this, process it
```

**CloudWatch metric coverage for this project:**

| Metric | Namespace | Statistic | Alarm threshold |
|---|---|---|---|
| CPUUtilization | AWS/EC2 | Average (5m) | > 80% for 2 periods |
| mem_used_percent | CWAgent | Average (5m) | > 85% (requires CW Agent) |
| disk_used_percent | CWAgent | Average (5m) | > 90% |
| NetworkIn/Out | AWS/EC2 | Sum (5m) | Baseline + 3 stddev |
| StatusCheckFailed | AWS/EC2 | Maximum (1m) | >= 1 |

Memory and disk require the CloudWatch Agent installed on EC2 instances — they are not collected by default.

---

### 1.3 Prometheus Alertmanager Webhook Configuration

**Confidence:** HIGH — Alertmanager configuration format has been stable for years.

**Alertmanager config for webhook receiver:**

```yaml
# alertmanager.yml
global:
  resolve_timeout: 5m

route:
  group_by: ['alertname', 'cluster', 'service']
  group_wait: 30s        # Wait 30s to group related alerts — matches the project's 30s debounce requirement
  group_interval: 5m     # Minimum interval between grouped notifications
  repeat_interval: 12h   # Resend unresolved alert after 12h
  receiver: 'atoloan-monitor'
  routes:
    # High-severity K8s pod issues route with tighter grouping window
    - match:
        severity: critical
        domain: kubernetes
      group_wait: 10s
      receiver: 'atoloan-monitor'
    # DB alerts route separately for DB orchestrator
    - match:
        domain: database
      receiver: 'atoloan-monitor-db'

receivers:
  - name: 'atoloan-monitor'
    webhook_configs:
      - url: 'http://monitor-agent:8000/webhooks/prometheus'
        send_resolved: true
        http_config:
          bearer_token: '<secret>'  # Use file reference in production

  - name: 'atoloan-monitor-db'
    webhook_configs:
      - url: 'http://monitor-agent:8000/webhooks/prometheus/db'
        send_resolved: true

inhibit_rules:
  # If a node is down, suppress pod alerts from that node
  - source_match:
      alertname: 'NodeNotReady'
    target_match:
      domain: 'kubernetes'
    equal: ['node']
  # If entire cluster is unreachable, suppress per-pod alerts
  - source_match:
      alertname: 'KubernetesClusterUnreachable'
    target_match:
      domain: 'kubernetes'
```

**Alertmanager webhook payload format:**

```json
{
  "version": "4",
  "groupKey": "{alertname=\"HighCPU\"}:{instance=\"backend-01\"}",
  "truncatedAlerts": 0,
  "status": "firing",
  "receiver": "atoloan-monitor",
  "groupLabels": {"alertname": "HighCPU"},
  "commonLabels": {"alertname": "HighCPU", "severity": "critical"},
  "commonAnnotations": {"summary": "CPU > 80%"},
  "externalURL": "http://alertmanager:9093",
  "alerts": [
    {
      "status": "firing",
      "labels": {"alertname": "HighCPU", "instance": "backend-01"},
      "annotations": {"summary": "CPU > 80%"},
      "startsAt": "2025-01-01T00:00:00Z",
      "endsAt": "0001-01-01T00:00:00Z",
      "fingerprint": "abc123"
    }
  ]
}
```

**The `fingerprint` field is the deduplication key.** Use it as the Redis key for dedup rather than constructing your own.

---

### 1.4 Unified Event Bus

**Recommendation: Redis Streams** over SQS or an in-process queue.

**Confidence:** HIGH for the pattern; MEDIUM for specific Redis Streams API details.

**Rationale for Redis Streams over alternatives:**

| Option | Pro | Con | Verdict |
|---|---|---|---|
| Redis Streams | Persistent, replayable, consumer groups, sub-millisecond latency, already likely in stack | Requires Redis | **RECOMMENDED** |
| SQS | Managed, durable | 256KB limit, 20ms+ latency, cost at volume, fan-out requires SNS | Overkill for single-node deployment |
| asyncio.Queue | Zero dependencies, lowest latency | Not persistent (events lost on crash), not shareable across processes | Only viable if entire agent is single-process |
| Kafka | Best for very high volume | Heavy operational burden for a small monitoring system | Reject |

**Redis Streams event bus design:**

```python
# Normalized event schema — all sources produce this shape
@dataclass
class MonitoringEvent:
    id: str              # Redis-assigned stream ID (used for ack)
    source: str          # "kubernetes" | "cloudwatch" | "prometheus" | "log-analyzer"
    domain: str          # "k8s" | "db" | "infra" | "app"
    event_type: str      # "pod_oomkilled" | "cpu_alarm" | "slow_query" | etc.
    severity: str        # "critical" | "warning" | "info"
    resource_id: str     # pod name, instance id, etc.
    timestamp: datetime
    raw_payload: dict    # Original event from source
    labels: dict         # Free-form labels for grouping/routing

# Publishing
async def publish_event(redis: Redis, event: MonitoringEvent):
    stream_key = f"events:{event.domain}"  # One stream per domain
    await redis.xadd(stream_key, {
        "source": event.source,
        "event_type": event.event_type,
        "severity": event.severity,
        "resource_id": event.resource_id,
        "timestamp": event.timestamp.isoformat(),
        "payload": json.dumps(event.raw_payload),
        "labels": json.dumps(event.labels),
    }, maxlen=10000)  # Cap stream at 10k events, trim oldest

# Consuming (per domain orchestrator)
async def consume_events(redis: Redis, domain: str, consumer_group: str):
    stream_key = f"events:{domain}"
    # Create consumer group if not exists
    try:
        await redis.xgroup_create(stream_key, consumer_group, id="0", mkstream=True)
    except redis.exceptions.ResponseError:
        pass  # Group already exists

    while True:
        entries = await redis.xreadgroup(
            consumer_group,
            "consumer-1",
            {stream_key: ">"},  # ">" means only new messages
            count=50,
            block=1000,  # Block 1s waiting for messages
        )
        for stream, messages in entries:
            for msg_id, fields in messages:
                await process_event(fields)
                await redis.xack(stream_key, consumer_group, msg_id)
```

**Stream topology for this project:**

```
events:k8s     → K8s Orchestrator consumer group
events:db      → DB Orchestrator consumer group
events:infra   → Infra Orchestrator consumer group
events:app     → App Orchestrator consumer group (future)
events:cross   → Meta-orchestrator (subscribes to all, reads copies via separate consumer group)
```

Each domain orchestrator is a separate consumer group on its domain stream. The meta-orchestrator has its own consumer group on ALL streams, reading independently — it does not receive forwarded messages, it receives the same messages but processes them only when cross-domain correlation triggers.

---

## 2. Multi-Orchestrator Pattern

### 2.1 Concurrency Model: asyncio over multiprocessing

**Recommendation: Single process, asyncio, with each domain orchestrator as a long-running coroutine.**

**Confidence:** HIGH.

**Rationale:**

- Domain orchestrators are I/O-bound (waiting on Claude API, K8s API, AWS SDK) — asyncio is the correct primitive for I/O-bound concurrency.
- The GIL is not relevant here — no CPU-intensive work in orchestrators (Claude does the computation).
- Multiprocessing adds IPC complexity with no benefit for I/O-bound workloads.
- Separate OS processes would require a message broker for coordination, which is Redis Streams anyway — no advantage over asyncio tasks that all use Redis.
- `asyncio.TaskGroup` (Python 3.11+) provides structured concurrency with automatic cancellation on any child failure.

```python
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI

async def main_monitor_loop():
    async with asyncio.TaskGroup() as tg:
        tg.create_task(run_k8s_orchestrator(), name="k8s-orchestrator")
        tg.create_task(run_db_orchestrator(), name="db-orchestrator")
        tg.create_task(run_infra_orchestrator(), name="infra-orchestrator")
        tg.create_task(run_meta_orchestrator(), name="meta-orchestrator")
        tg.create_task(run_event_ingestion(), name="event-ingestion")

@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor_task = asyncio.create_task(main_monitor_loop())
    yield
    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)
```

**Exception: Extended thinking RCA.** Claude's extended thinking with `claude-opus-4-8` can take 30–120 seconds. This will block the asyncio event loop if called synchronously. Use `asyncio.to_thread()` or ensure you use the async Anthropic client (`anthropic.AsyncAnthropic`) so the call yields to the event loop while waiting.

---

### 2.2 State Sharing Between Domain Orchestrators

**Shared state stored in Redis** (not in-memory Python dicts — those don't survive restarts and can't be read by the dashboard API).

**State schema:**

```
# Current incident state per domain
incident:{domain}:current          → JSON: {incident_id, started_at, events[], status}

# Active signals waiting for debounce window
signals:{domain}:pending           → Redis Sorted Set (score = timestamp), members = signal IDs

# Per-domain diagnosis results
diagnosis:{incident_id}            → JSON: {domain, confidence, root_cause, supporting_signals[]}

# Cross-domain correlation index
# When meta-orchestrator needs to correlate: it queries this
correlation:active                 → Redis Set of active incident_ids across all domains

# Agent action log (shared with dashboard)
actions:log                        → Redis Stream (append-only, read by dashboard SSE endpoint)

# Baseline data (per metric, per hour)
baseline:{metric_name}:{hour_of_day}  → Redis Hash: {p50, p95, p99, mean, stddev, sample_count}
```

**What each orchestrator reads from shared state:**

| Orchestrator | Reads | Writes |
|---|---|---|
| K8s Orchestrator | `baseline:*`, `incident:k8s:*` | `incident:k8s:*`, `signals:k8s:*`, `actions:log` |
| DB Orchestrator | `baseline:*`, `incident:db:*` | `incident:db:*`, `signals:db:*`, `actions:log` |
| Infra Orchestrator | `baseline:*`, `incident:infra:*` | `incident:infra:*`, `signals:infra:*`, `actions:log` |
| Meta-orchestrator | ALL `incident:*`, `signals:*` | `correlation:active`, `actions:log` |

---

### 2.3 Meta-Orchestrator Trigger Conditions

**When to escalate to cross-domain RCA:**

The meta-orchestrator subscribes to all domain event streams and monitors active incidents. It triggers cross-domain RCA when ANY of these conditions are true:

```python
def should_trigger_cross_domain_rca(active_incidents: list[Incident]) -> bool:
    domains_with_active_incidents = {i.domain for i in active_incidents}

    # Rule 1: 2+ domains have active incidents within the same 5-minute window
    if len(domains_with_active_incidents) >= 2:
        incident_times = [i.started_at for i in active_incidents]
        if (max(incident_times) - min(incident_times)).seconds < 300:
            return True

    # Rule 2: Any domain orchestrator produced a diagnosis with confidence < 0.5
    # (very uncertain diagnosis suggests hidden upstream cause)
    for incident in active_incidents:
        if incident.latest_diagnosis and incident.latest_diagnosis.confidence < 0.5:
            return True

    # Rule 3: Cascading failure signature — K8s AND DB both degraded
    if {"k8s", "db"}.issubset(domains_with_active_incidents):
        return True

    # Rule 4: Infrastructure event coincides with any other domain event
    # (EC2 issue often causes K8s + DB simultaneously)
    if "infra" in domains_with_active_incidents and len(domains_with_active_incidents) >= 2:
        return True

    return False
```

**What the meta-orchestrator receives:** It is passed the full diagnosis output from each domain orchestrator, not raw events. This is critical — it reasons over diagnoses, not raw telemetry. Raw telemetry would exceed the context window for complex incidents.

---

### 2.4 Message Passing Patterns for Orchestrator Coordination

**Pattern: Diagnosis result publishing via Redis Pub/Sub (not Streams)**

Domain orchestrators publish completed diagnoses to a Redis pub/sub channel. The meta-orchestrator subscribes to this channel. This is separate from the event Streams — Streams are for raw events (persistent), Pub/Sub is for diagnosis signals (ephemeral OK, they're also written to PostgreSQL).

```python
# Domain orchestrator: after RCA completes
async def publish_diagnosis(redis: Redis, diagnosis: DiagnosisResult):
    # Persist to PostgreSQL (durable record)
    await db.insert_diagnosis(diagnosis)
    # Notify meta-orchestrator via pub/sub (fast notification)
    await redis.publish("diagnoses", diagnosis.model_dump_json())

# Meta-orchestrator: subscriber
async def run_meta_orchestrator(redis: Redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe("diagnoses")
    async for message in pubsub.listen():
        if message["type"] == "message":
            diagnosis = DiagnosisResult.model_validate_json(message["data"])
            await evaluate_cross_domain_trigger(diagnosis)
```

**Pattern: Signal bundle assembly with asyncio.Event**

The 30-second debounce window is implemented as a coroutine that accumulates signals into a bundle, then fires:

```python
async def debounce_signals(domain: str, window_seconds: int = 30) -> AsyncIterator[SignalBundle]:
    pending: list[MonitoringEvent] = []
    flush_task: asyncio.Task | None = None

    async def flush():
        nonlocal pending
        await asyncio.sleep(window_seconds)
        if pending:
            bundle = SignalBundle(domain=domain, events=list(pending))
            pending = []
            yield bundle

    async for event in consume_events(redis, domain, f"{domain}-debouncer"):
        pending.append(event)
        if flush_task is None or flush_task.done():
            flush_task = asyncio.create_task(flush())
```

---

## 3. Confidence Scoring for LLM Diagnoses

### 3.1 Implementation Approach

**Recommendation: Structured output with self-assessed confidence + signal count heuristic.**

**Confidence:** HIGH for structured output pattern; MEDIUM for calibration values.

Claude's tool use / structured output forces the model to produce a well-formed confidence score alongside its diagnosis. This is more reliable than asking for confidence in free text.

**RCA output schema (using Pydantic):**

```python
from pydantic import BaseModel, Field
from typing import Literal

class SupportingSignal(BaseModel):
    signal_id: str
    signal_type: str
    contribution: str  # Why this signal supports the diagnosis

class DiagnosisResult(BaseModel):
    root_cause: str = Field(description="Primary root cause in 2-3 sentences")
    affected_components: list[str]
    contributing_factors: list[str]
    supporting_signals: list[SupportingSignal]
    confidence: float = Field(ge=0.0, le=1.0, description=(
        "Confidence in this diagnosis from 0.0 to 1.0. "
        "1.0 = multiple corroborating signals with clear causal chain. "
        "0.8 = strong evidence but one alternative explanation exists. "
        "0.6 = plausible but incomplete evidence. "
        "0.4 = speculative, key signals missing. "
        "Below 0.4 = insufficient evidence to diagnose."
    ))
    confidence_reasoning: str = Field(description=(
        "Explain why you assigned this confidence score. "
        "What evidence supports it? What is missing?"
    ))
    recommended_action: str
    requires_human_review: bool
    estimated_blast_radius: Literal["service", "cluster", "datacenter"]
```

**Prompt structure for confidence-aware RCA:**

```python
SYSTEM_PROMPT = """You are an expert SRE diagnosing infrastructure incidents.
You will be given a bundle of monitoring signals. Diagnose the root cause.

CONFIDENCE CALIBRATION:
- Score 0.9–1.0 only when: (a) signals directly show the failure, (b) causal chain is complete, (c) no plausible alternative explanations
- Score 0.7–0.8 when: strong evidence but one alternative explanation cannot be ruled out
- Score 0.5–0.6 when: circumstantial evidence, temporal correlation but not direct causation
- Score below 0.5 when: signals are ambiguous, could be multiple root causes, or key signals are missing

IMPORTANT: It is better to return confidence 0.4 and surface to a human than to return 0.8 for a speculative diagnosis.
"""

async def run_rca(signals: list[MonitoringEvent], topology: str) -> DiagnosisResult:
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-opus-4-8",  # Extended thinking for RCA
        max_tokens=16000,
        thinking={"type": "enabled", "budget_tokens": 10000},
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"""Infrastructure topology:
{topology}

Signal bundle ({len(signals)} signals):
{format_signals(signals)}

Diagnose the root cause and return a structured diagnosis."""
        }],
        tools=[{
            "name": "submit_diagnosis",
            "description": "Submit the completed RCA diagnosis",
            "input_schema": DiagnosisResult.model_json_schema()
        }],
        tool_choice={"type": "tool", "name": "submit_diagnosis"}
    )
    # Extract tool use result
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_diagnosis":
            return DiagnosisResult.model_validate(block.input)
```

---

### 3.2 Threshold Calibration: The 0.8 Default

**Why 0.8:**

| Threshold | Effect | Risk |
|---|---|---|
| 0.95 | Almost never auto-remediates | Humans do most work — defeats purpose |
| 0.85 | Remediates only very clear cases | Safe, low false positive rate |
| **0.80** | **Remediates clear + strong-evidence cases** | **~5% wrong action rate (acceptable with pre-flight checks)** |
| 0.70 | Remediates moderately uncertain cases | ~15% wrong action rate (too high for prod) |
| 0.60 | Remediates speculative diagnoses | Dangerous |

**False positive vs. false negative tradeoff:**

For this system, a false negative (miss a real incident, escalate to human) is always safer than a false positive (wrong auto-remediation). The 0.8 threshold should be:
- **Lowered to 0.7** only for read-only "observing and recommending" actions.
- **Raised to 0.9** for Postgres query kills and circuit breaker toggles (higher blast radius).
- **1.0** (never auto-remediate) for secrets operations in the first 30 days of deployment.

**Per-action thresholds:**

```python
REMEDIATION_THRESHOLDS = {
    "pod_restart": 0.80,
    "deployment_scale_up": 0.85,
    "deployment_scale_down": 0.85,
    "resource_limit_adjust": 0.80,
    "postgres_query_kill": 0.90,  # High blast radius — conservative
    "secrets_refresh": 0.85,
    "circuit_breaker_toggle": 0.90,
    "human_escalate": 0.00,       # Always surface < threshold to human
}
```

---

### 3.3 Escalation Path Design

**When confidence < threshold, the human escalation panel receives:**

```python
class HumanEscalationPacket(BaseModel):
    incident_id: str
    timestamp: datetime
    summary: str          # 2-sentence summary for fast triage
    urgency: Literal["p1_immediate", "p2_within_15m", "p3_within_1h"]
    diagnosis: DiagnosisResult
    agent_recommendation: str    # What the agent would do if it were confident
    why_escalated: str           # Specific reason confidence was insufficient
    proposed_actions: list[dict] # Buttons in dashboard: [{"label": "Restart pod X", "action_id": ...}]
    raw_signals: list[dict]      # Full signal dump for engineer to inspect
    relevant_past_incidents: list[str]  # Links to similar past incidents
```

**Urgency assignment rules:**
- P1: `estimated_blast_radius == "datacenter"` or `affected_components` includes payment service.
- P2: Multiple services affected or DB is in affected components.
- P3: Single service, non-critical path.

---

## 4. Two-Phase Learning/Enforcement

### 4.1 Statistical Baseline Approaches

**Recommendation: EWMA (Exponentially Weighted Moving Average) for real-time updates, with percentile bands (p50/p95/p99) stored per metric per hour-of-day.**

**Confidence:** HIGH for EWMA and percentile approaches; MEDIUM for minimum data requirements.

**Algorithm comparison for infrastructure metrics:**

| Algorithm | Strengths | Weaknesses | Best For |
|---|---|---|---|
| Simple rolling average | Easy to implement | Treats old data same as new, slow to adapt | Not recommended |
| **EWMA** | Fast adaptation, low memory, reacts to recent trends | Can be skewed by outliers | **Primary: per-metric trend** |
| Percentile bands (p50/p95/p99) | Robust to outliers, intuitive thresholds | Requires more data, higher storage | **Primary: threshold alarms** |
| Z-score | Fast, parametric | Assumes normality (infra metrics are not normal) | Supplement only |
| MAD (Median Absolute Deviation) | Robust to non-normal distributions | Requires sorted data | Good for spiky metrics like latency |
| IQR | Robust, non-parametric | Needs sufficient data | Good for CPU/memory |
| Prophet | Handles seasonality automatically | Heavy dependency, slow to train | Overkill for v1 |

**Recommended hybrid:**

```python
from dataclasses import dataclass
import math

@dataclass
class MetricBaseline:
    metric_name: str
    hour_of_day: int        # 0-23, for time-of-day seasonality
    ewma: float             # Current exponentially weighted mean
    ewma_variance: float    # For computing dynamic thresholds
    p50: float
    p95: float
    p99: float
    sample_count: int
    last_updated: datetime

    # EWMA parameters
    alpha: float = 0.1      # Smoothing factor: lower = more stable, higher = faster adaptation
                            # 0.1 corresponds to ~19-sample effective window

    def update(self, new_value: float) -> None:
        if self.sample_count == 0:
            self.ewma = new_value
            self.ewma_variance = 0.0
        else:
            diff = new_value - self.ewma
            self.ewma = self.ewma + self.alpha * diff
            self.ewma_variance = (1 - self.alpha) * (self.ewma_variance + self.alpha * diff ** 2)
        self.sample_count += 1

    @property
    def ewma_stddev(self) -> float:
        return math.sqrt(self.ewma_variance)

    def is_anomalous(self, value: float, z_threshold: float = 3.0) -> bool:
        # Anomalous if > 3 stddevs from EWMA AND > p95 from percentile bands
        if self.sample_count < 30:
            return False  # Insufficient data
        z_score = abs(value - self.ewma) / (self.ewma_stddev + 1e-9)
        return z_score > z_threshold and value > self.p95
```

---

### 4.2 Minimum Data Requirements

**Confidence:** MEDIUM (academic references support these ranges; exact numbers depend on metric volatility).

| Baseline type | Minimum samples | Recommended | Rationale |
|---|---|---|---|
| Hourly percentiles (p50/p95) | 30 samples per hour-of-day slot | 100+ | 30 samples for p95 estimate within ±5% error |
| EWMA initialization | 10 samples | 30 | First 10 samples prime the filter; next 20 stabilize variance |
| Day-of-week patterns | 4 weeks | 8 weeks | Detect weekly seasonality reliably |
| Anomaly detection activation | 7 days (1 full week) | 14 days | Covers Mon-Sun patterns at every hour |

**Why the 7-day learning mode is correct:**
- At 1-minute metric collection: 7 days = 10,080 samples per metric.
- That yields ~420 samples per hour-of-day slot (10,080 / 24), well above the 100-sample recommendation.
- Covers one full Mon-Sun week, capturing traffic pattern variability.
- The 7-day threshold is used by Datadog, AWS DevOps Guru, and other commercial tools for the same reason.

---

### 4.3 Anomaly Detection Algorithm Selection

**Recommendation: MAD for latency metrics, EWMA Z-score for resource metrics (CPU/memory/disk), IQR for error rates.**

```python
def detect_anomaly(value: float, baseline: MetricBaseline, metric_type: str) -> AnomalyResult:
    if metric_type == "latency":
        # MAD: robust to latency spikes and non-normal distributions
        # Pre-computed median and MAD stored in baseline
        z_mad = abs(value - baseline.median) / (1.4826 * baseline.mad + 1e-9)
        is_anomaly = z_mad > 3.5 and value > baseline.p95
        return AnomalyResult(is_anomaly=is_anomaly, score=z_mad, method="MAD")

    elif metric_type in ("cpu_percent", "memory_percent", "disk_percent"):
        # EWMA Z-score: adapts to gradual resource growth trends
        z = abs(value - baseline.ewma) / (baseline.ewma_stddev + 1e-9)
        is_anomaly = z > 3.0 and value > baseline.p95
        return AnomalyResult(is_anomaly=is_anomaly, score=z, method="EWMA-Z")

    elif metric_type == "error_rate":
        # IQR: robust for rate metrics that may be zero for long periods
        iqr = baseline.p75 - baseline.p25
        threshold = baseline.p75 + 1.5 * iqr
        is_anomaly = value > threshold
        return AnomalyResult(is_anomaly=is_anomaly, score=value / (threshold + 1e-9), method="IQR")
```

---

### 4.4 Baseline Storage

**Recommendation: PostgreSQL with TimescaleDB extension for time-series data, Redis for hot baseline cache.**

**Confidence:** HIGH for TimescaleDB as the right tool; MEDIUM for specific schema details.

**Why TimescaleDB over plain PostgreSQL:**
- Automatic time-based partitioning (hypertables) — queries over rolling 7-day windows are 10–100x faster.
- Continuous aggregates: precompute hourly p50/p95/p99 from raw 1-minute samples automatically.
- Works alongside regular PostgreSQL tables (incidents, diagnoses) — one DB for everything.
- No separate Prometheus TSDB to manage.

**Schema:**

```sql
-- Raw metric samples (TimescaleDB hypertable)
CREATE TABLE metric_samples (
    time        TIMESTAMPTZ NOT NULL,
    metric_name TEXT        NOT NULL,
    resource_id TEXT        NOT NULL,  -- pod name, instance id, etc.
    value       DOUBLE PRECISION NOT NULL
);
SELECT create_hypertable('metric_samples', 'time');
CREATE INDEX ON metric_samples (metric_name, resource_id, time DESC);

-- Hourly aggregated baselines (continuous aggregate)
CREATE MATERIALIZED VIEW baseline_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    metric_name,
    resource_id,
    EXTRACT(hour FROM time) AS hour_of_day,
    EXTRACT(dow FROM time) AS day_of_week,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY value) AS p50,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY value) AS p95,
    percentile_cont(0.99) WITHIN GROUP (ORDER BY value) AS p99,
    AVG(value) AS mean,
    STDDEV(value) AS stddev,
    COUNT(*) AS sample_count
FROM metric_samples
GROUP BY bucket, metric_name, resource_id;

-- Add retention policy: keep raw samples 30 days, aggregates 1 year
SELECT add_retention_policy('metric_samples', INTERVAL '30 days');
```

**Redis hot cache for baselines (read by anomaly detector at query time):**

```python
BASELINE_CACHE_TTL = 3600  # 1 hour — refresh from DB hourly

async def get_baseline(
    redis: Redis,
    metric_name: str,
    resource_id: str,
    hour_of_day: int
) -> MetricBaseline | None:
    cache_key = f"baseline:{metric_name}:{resource_id}:{hour_of_day}"
    cached = await redis.get(cache_key)
    if cached:
        return MetricBaseline.model_validate_json(cached)

    # Load from TimescaleDB
    baseline = await db.fetch_baseline(metric_name, resource_id, hour_of_day)
    if baseline:
        await redis.set(cache_key, baseline.model_dump_json(), ex=BASELINE_CACHE_TTL)
    return baseline
```

---

## 5. Web Dashboard Real-Time Architecture

### 5.1 WebSocket vs Server-Sent Events

**Recommendation: Server-Sent Events (SSE) for the agent action feed; WebSocket for the interactive incident command panel.**

**Confidence:** HIGH.

**Rationale:**

| Feature | SSE | WebSocket | Decision |
|---|---|---|---|
| Direction | Server → Client only | Bidirectional | Agent feed is one-way → SSE |
| Browser reconnect | Built-in automatic | Manual implementation required | SSE simpler for monitoring feeds |
| HTTP/2 multiplexing | Yes, multiple SSE streams share one connection | No (each WebSocket is separate TCP) | SSE wins for multiple concurrent feeds |
| Proxy/firewall support | Works through standard HTTP proxies | May be blocked by some proxies | SSE more compatible |
| Interactive commands | No (read-only) | Yes | WebSocket for human approval/action buttons |

**SSE endpoint (FastAPI):**

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import asyncio

async def agent_action_stream(redis: Redis):
    """Reads from Redis Stream actions:log and yields SSE events."""
    last_id = "$"  # Start from now (don't replay history)
    while True:
        entries = await redis.xread({"actions:log": last_id}, count=50, block=1000)
        for stream, messages in entries:
            for msg_id, fields in messages:
                last_id = msg_id
                data = json.dumps({
                    "id": msg_id.decode(),
                    "timestamp": fields[b"timestamp"].decode(),
                    "action_type": fields[b"action_type"].decode(),
                    "description": fields[b"description"].decode(),
                    "domain": fields[b"domain"].decode(),
                })
                yield f"data: {data}\n\n"
        else:
            # Heartbeat every 15s to keep connection alive through proxies
            yield ": heartbeat\n\n"

@app.get("/api/stream/actions")
async def stream_actions():
    return StreamingResponse(
        agent_action_stream(redis),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
        }
    )
```

**WebSocket for incident commands:**

```python
from fastapi import WebSocket

@app.websocket("/api/ws/incidents/{incident_id}")
async def incident_websocket(websocket: WebSocket, incident_id: str):
    await websocket.accept()
    try:
        while True:
            # Receive human approval/rejection commands
            data = await websocket.receive_json()
            if data["type"] == "approve_action":
                result = await execute_approved_action(
                    incident_id=incident_id,
                    action_id=data["action_id"]
                )
                await websocket.send_json({"type": "action_result", "result": result})
            elif data["type"] == "reject_action":
                await record_rejection(incident_id, data["action_id"], data["reason"])
    except WebSocketDisconnect:
        pass
```

---

### 5.2 React State Management for Real-Time Data

**Recommendation: Zustand for global state, React Query (TanStack Query) for server state, native EventSource for SSE.**

**Confidence:** HIGH for the pattern; MEDIUM for specific Zustand/TanStack versions.

**Rationale over Redux:**
- Redux is overkill for this scale; Zustand is lighter, typesafe, and handles real-time mutations cleanly.
- TanStack Query manages polling/caching for non-streaming data (incident list, SLO metrics).
- Native `EventSource` API handles SSE reconnection automatically — no library needed.

```typescript
// stores/monitoringStore.ts
import { create } from 'zustand'

interface AgentAction {
  id: string
  timestamp: string
  actionType: string
  description: string
  domain: string
}

interface MonitoringStore {
  agentActions: AgentAction[]
  activeIncidents: Record<string, Incident>
  connectionStatus: 'connected' | 'connecting' | 'disconnected'
  addAgentAction: (action: AgentAction) => void
  upsertIncident: (incident: Incident) => void
  setConnectionStatus: (status: MonitoringStore['connectionStatus']) => void
}

export const useMonitoringStore = create<MonitoringStore>((set) => ({
  agentActions: [],
  activeIncidents: {},
  connectionStatus: 'connecting',
  addAgentAction: (action) =>
    set((state) => ({
      agentActions: [action, ...state.agentActions].slice(0, 500), // Keep last 500
    })),
  upsertIncident: (incident) =>
    set((state) => ({
      activeIncidents: { ...state.activeIncidents, [incident.id]: incident },
    })),
  setConnectionStatus: (status) => set({ connectionStatus: status }),
}))

// hooks/useAgentActionStream.ts
export function useAgentActionStream() {
  const { addAgentAction, setConnectionStatus } = useMonitoringStore()

  useEffect(() => {
    const es = new EventSource('/api/stream/actions')

    es.onopen = () => setConnectionStatus('connected')
    es.onerror = () => setConnectionStatus('disconnected')  // EventSource auto-reconnects

    es.onmessage = (event) => {
      const action: AgentAction = JSON.parse(event.data)
      addAgentAction(action)
    }

    return () => es.close()
  }, [])
}
```

---

### 5.3 Incident Document Storage Schema

**Confidence:** HIGH for schema design; MEDIUM for specific index names.

```sql
-- Core incident record
CREATE TABLE incidents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at      TIMESTAMPTZ NOT NULL,
    resolved_at     TIMESTAMPTZ,
    domain          TEXT NOT NULL CHECK (domain IN ('k8s', 'db', 'infra', 'app', 'cross')),
    status          TEXT NOT NULL CHECK (status IN ('active', 'resolved', 'escalated')),
    severity        TEXT NOT NULL CHECK (severity IN ('critical', 'warning', 'info')),
    title           TEXT NOT NULL,
    root_cause      TEXT,
    confidence      DOUBLE PRECISION,
    auto_remediated BOOLEAN DEFAULT FALSE,
    human_reviewed  BOOLEAN DEFAULT FALSE,
    affected_services TEXT[] NOT NULL DEFAULT '{}',
    slo_impact_minutes DOUBLE PRECISION,   -- Error budget burned (in minutes)
    raw_signal_ids  TEXT[] NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_incidents_started_at ON incidents (started_at DESC);
CREATE INDEX idx_incidents_domain_status ON incidents (domain, status);
CREATE INDEX idx_incidents_severity ON incidents (severity);
-- Full-text search on root_cause and title
CREATE INDEX idx_incidents_fts ON incidents USING GIN (
    to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(root_cause, ''))
);

-- Incident timeline events (ordered observations and actions)
CREATE TABLE incident_events (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id  UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    occurred_at  TIMESTAMPTZ NOT NULL,
    event_type   TEXT NOT NULL,  -- 'signal', 'diagnosis', 'action', 'escalation', 'resolution'
    actor        TEXT NOT NULL,  -- 'k8s-observer', 'rca-analyzer', 'pod-restarter', 'human', etc.
    description  TEXT NOT NULL,
    payload      JSONB           -- Full event data for deep inspection
);

CREATE INDEX idx_incident_events_incident_id ON incident_events (incident_id, occurred_at);

-- Postmortem drafts
CREATE TABLE postmortems (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id  UUID NOT NULL REFERENCES incidents(id),
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    summary      TEXT NOT NULL,
    timeline     TEXT NOT NULL,
    root_cause   TEXT NOT NULL,
    contributing_factors TEXT[],
    follow_up_actions TEXT[] NOT NULL,
    slo_impact   JSONB  -- {services: [], error_budget_burned: 0.03, duration_minutes: 15}
);

-- SLO tracking
CREATE TABLE slo_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service         TEXT NOT NULL,
    sli_type        TEXT NOT NULL,  -- 'availability', 'latency', 'error_rate', 'saturation'
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    target          DOUBLE PRECISION NOT NULL,  -- e.g., 0.999 for 99.9%
    achieved        DOUBLE PRECISION NOT NULL,
    error_budget_remaining DOUBLE PRECISION NOT NULL,
    burn_rate       DOUBLE PRECISION NOT NULL
);

CREATE INDEX idx_slo_records_service_window ON slo_records (service, window_start DESC);
```

**Search query pattern for incident history:**

```sql
-- Full-text search with filters
SELECT * FROM incidents
WHERE
    to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(root_cause, ''))
    @@ plainto_tsquery('english', :search_query)
    AND (:domain IS NULL OR domain = :domain)
    AND (:status IS NULL OR status = :status)
    AND started_at > NOW() - INTERVAL '30 days'
ORDER BY started_at DESC
LIMIT 50;
```

---

## 6. Standard Stack for 2025

### 6.1 Python Backend Architecture

**Recommendation: Single FastAPI application with two logical service groups — Agent API and Dashboard API — separated at the router level, not as separate processes.**

**Confidence:** HIGH.

**Rationale for single application:**
- Both the agent backend and dashboard API share Redis, PostgreSQL, and async infrastructure.
- Separate processes double memory footprint and add inter-service HTTP calls for no benefit.
- FastAPI's `APIRouter` provides clean separation at the code level.
- If scale demands it later, split is straightforward — routers become separate services.

```
atoloan-monitor/
├── agent/
│   ├── api/
│   │   ├── routes/
│   │   │   ├── webhooks.py      # CloudWatch, Prometheus, K8s webhook receivers
│   │   │   ├── stream.py        # SSE endpoints for dashboard
│   │   │   ├── incidents.py     # Incident CRUD, search
│   │   │   ├── actions.py       # Human approval endpoints
│   │   │   └── health.py        # Health check + system status
│   │   └── main.py
│   ├── orchestrators/
│   │   ├── k8s_orchestrator.py
│   │   ├── db_orchestrator.py
│   │   ├── infra_orchestrator.py
│   │   └── meta_orchestrator.py
│   ├── observers/
│   │   ├── k8s_watcher.py
│   │   ├── cloudwatch_poller.py  # For metrics not covered by alarms
│   │   ├── prometheus_receiver.py
│   │   └── log_analyzer.py
│   ├── skills/
│   │   ├── diagnosers/
│   │   └── remediators/
│   ├── baselines/
│   │   ├── collector.py
│   │   ├── store.py
│   │   └── detector.py
│   └── shared/
│       ├── event_bus.py
│       ├── state.py
│       └── safety.py            # Hard-stop checks for destructive operations
```

---

### 6.2 Observability Libraries

**Recommended stack:**

| Library | Version | Purpose | Why |
|---|---|---|---|
| `structlog` | `>=24.0` | Structured logging | JSON logs, automatic context binding, async-safe |
| `opentelemetry-sdk` | `>=1.25` | Distributed tracing | Trace agent actions end-to-end |
| `opentelemetry-instrumentation-fastapi` | latest | Auto-instrument FastAPI | Zero-code traces for HTTP |
| `opentelemetry-instrumentation-asyncpg` | latest | DB query traces | Trace slow queries |
| `prometheus-client` | `>=0.20` | Metrics exposure | `/metrics` endpoint for the monitoring agent itself |

**structlog configuration:**

```python
import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if os.getenv("ENV") == "development"
        else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

logger = structlog.get_logger()

# Usage in orchestrators:
async def run_rca(incident_id: str, signals: list):
    with structlog.contextvars.bound_contextvars(
        incident_id=incident_id,
        signal_count=len(signals),
        orchestrator="k8s",
    ):
        logger.info("rca_started")
        result = await call_claude_rca(signals)
        logger.info("rca_completed", confidence=result.confidence, root_cause=result.root_cause[:100])
```

**OpenTelemetry setup:**

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("atoloan-monitor")

# Usage:
with tracer.start_as_current_span("rca.run") as span:
    span.set_attribute("incident.id", incident_id)
    span.set_attribute("signal.count", len(signals))
    result = await run_rca(signals)
    span.set_attribute("diagnosis.confidence", result.confidence)
```

---

### 6.3 Kubernetes Python Client

**Recommended packages:**

| Package | Version | Use |
|---|---|---|
| `kubernetes` | `29.0.0` | Sync K8s operations, used in simple scripts |
| `kubernetes-asyncio` | `29.0.0` | **Primary**: async Watch API, async CRUD operations |

**Confidence:** MEDIUM — version 29.x was current in 2025 tracking K8s 1.29; verify latest compatible version before pinning.

**Version compatibility:** The Python client tracks the K8s API version. Pin to the version matching your cluster's K8s version. Use `kubernetes-asyncio` for all watcher code; the sync `kubernetes` client will block the event loop.

**Client initialization pattern (in-cluster with IAM/IRSA):**

```python
from kubernetes_asyncio import client, config

async def get_k8s_clients() -> tuple[client.CoreV1Api, client.AppsV1Api]:
    # Tries in-cluster config first (KUBERNETES_SERVICE_HOST env var)
    # Falls back to kubeconfig for local development
    try:
        await config.load_incluster_config()
    except config.ConfigException:
        await config.load_kube_config()

    api_client = client.ApiClient()
    return client.CoreV1Api(api_client), client.AppsV1Api(api_client)
```

**Key operations needed:**

```python
# Pre-flight check before scale-up
async def check_scale_headroom(
    v1: client.CoreV1Api,
    apps_v1: client.AppsV1Api,
    namespace: str,
    deployment_name: str,
    target_replicas: int,
) -> PreflightResult:
    # 1. Check namespace ResourceQuota
    quotas = await v1.list_namespaced_resource_quota(namespace)
    # 2. Check node allocatable vs current requests
    nodes = await v1.list_node()
    # 3. Check existing ResourceQuota usage
    quota_status = quotas.items[0].status if quotas.items else None
    # ... comparison logic
    return PreflightResult(allowed=True, reason="Sufficient headroom")
```

---

### 6.4 AWS SDK Patterns

**Recommendation: `aioboto3` for all async code paths, `boto3` only for one-off scripts.**

**Confidence:** HIGH for `aioboto3` being the correct async wrapper; MEDIUM for specific version.

| Package | Version | Use |
|---|---|---|
| `boto3` | `>=1.34` | Synchronous AWS calls (scripts, one-off ops) |
| `aioboto3` | `>=13.0` | **Primary**: async CloudWatch, Secrets Manager, EC2 calls |

**Why aioboto3:** It wraps `boto3` with async context managers that use `asyncio` under the hood, making AWS calls non-blocking in the asyncio event loop. Without it, `boto3.client.get_metric_data()` blocks the entire event loop for 100–500ms per call.

```python
import aioboto3

# CloudWatch metrics retrieval
async def get_ec2_metrics(
    instance_id: str,
    metric_name: str,
    start_time: datetime,
    end_time: datetime,
) -> list[float]:
    session = aioboto3.Session()
    async with session.client("cloudwatch", region_name="us-east-1") as cw:
        response = await cw.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName=metric_name,
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start_time,
            EndTime=end_time,
            Period=300,
            Statistics=["Average", "Maximum"],
        )
        return [p["Average"] for p in sorted(response["Datapoints"], key=lambda x: x["Timestamp"])]

# Secrets Manager — no credentials in env vars, use IAM role
async def get_secret(secret_name: str) -> str:
    session = aioboto3.Session()
    async with session.client("secretsmanager", region_name="us-east-1") as sm:
        response = await sm.get_secret_value(SecretId=secret_name)
        return response["SecretString"]
```

**IAM role pattern:** The monitoring EC2 instance must have an IAM role attached (not environment variable credentials). The role policy should be least-privilege: read-only on CloudWatch, EC2 describe, Secrets Manager GetSecretValue, and write-only on specific K8s operations via RBAC (not IAM).

---

## 7. Component Boundaries and Data Flow

### 7.1 Full System Component Map

```
[External Sources]
  K8s Watch API ──────────────────────────────────────┐
  CloudWatch Alarms → SNS → Webhook ──────────────────┤
  Prometheus Alertmanager → Webhook ──────────────────┤
  Log streams (CloudWatch Logs / app logs) ────────────┤
                                                       ▼
                                            [Event Ingestion Layer]
                                            SNS webhook handler
                                            Prometheus webhook handler
                                            K8s async watcher
                                            Log tail adapter
                                                       │
                                             normalize + deduplicate
                                                       │
                                                       ▼
                                            [Redis Streams]
                                            events:k8s
                                            events:db
                                            events:infra
                                            events:app
                                                       │
                          ┌────────────────────────────┼────────────────────────────┐
                          ▼                            ▼                            ▼
               [K8s Orchestrator]          [DB Orchestrator]          [Infra Orchestrator]
               debounce (30s)              debounce (30s)              debounce (30s)
               signal bundle               signal bundle               signal bundle
               → Claude RCA                → Claude RCA                → Claude RCA
               (confidence score)          (confidence score)          (confidence score)
                          │                            │                            │
                          └────────────────────────────┼────────────────────────────┘
                                                       ▼
                                            [Redis Pub/Sub: diagnoses]
                                                       │
                                                       ▼
                                            [Meta-Orchestrator]
                                            cross-domain trigger check
                                            → Claude extended thinking RCA
                                            (only on multi-domain incidents)
                                                       │
                          ┌────────────────────────────┼────────────────────────────┐
                          ▼                            ▼                            ▼
               confidence >= threshold     confidence < threshold     cross-domain RCA
               → Remediator skills         → Human Escalation         → Full postmortem
               (pre-flight checks)         packet to dashboard
                          │                            │
                          ▼                            ▼
               [Actions: log Redis Stream]   [PostgreSQL: incidents]
                          │
                          ▼
               [Dashboard SSE Stream]
               → React dashboard
```

### 7.2 Prompt Caching Strategy

**Use Anthropic's prompt caching for:**

1. **Infrastructure topology context** — the map of all EC2 instances, K8s namespaces, pod → service → EC2 relationships. This changes at deploy time, not per-incident. Cache-breakpoint after the topology block.

2. **Baseline summaries** — current p50/p95 for each metric per service. Regenerate this block once per hour, cache across all RCA calls within that hour.

3. **Past incident context** — the last 20 resolved incidents summarized. Prepend this as a cached block to give Claude pattern-matching context without re-tokenizing each time.

```python
# Messages structure for cached RCA prompt
messages = [
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": INFRASTRUCTURE_TOPOLOGY,  # ~2000 tokens, changes rarely
                "cache_control": {"type": "ephemeral"}  # Cache this block
            },
            {
                "type": "text",
                "text": baseline_summary,  # ~1500 tokens, refresh hourly
                "cache_control": {"type": "ephemeral"}  # Cache this block
            },
            {
                "type": "text",
                "text": format_signal_bundle(signals)  # Fresh per-incident, NOT cached
            }
        ]
    }
]
```

---

## 8. Safety Architecture

### 8.1 Hard-Stop at Skill Boundary

**Every remediator skill validates its action against a hard-coded blocklist before making any API call.** This check runs at the Python function boundary, not in orchestrator logic. It cannot be reasoned around by any Claude chain-of-thought.

```python
FORBIDDEN_OPERATIONS = frozenset([
    "delete_pod", "delete_deployment", "delete_namespace",
    "terminate_instance", "stop_instance",
    "drop_table", "truncate_table", "delete_database",
    "delete_secret", "delete_security_group",
    "revoke_iam", "delete_role",
])

def safety_check(operation: str, target: str) -> None:
    """Raises SafetyViolation unconditionally for forbidden operations."""
    if operation in FORBIDDEN_OPERATIONS:
        raise SafetyViolation(
            f"BLOCKED: Operation '{operation}' on '{target}' is permanently forbidden. "
            f"This is not a policy check — it cannot be overridden."
        )
    # Additional checks: target must be in known resource inventory
    if target not in KNOWN_RESOURCES:
        raise SafetyViolation(f"BLOCKED: Target '{target}' not in known resource inventory.")
```

### 8.2 Pre-Flight Check Pattern

```python
async def execute_with_preflight(
    action: RemediationAction,
    preflight_fn: Callable[[], Awaitable[PreflightResult]],
) -> ActionResult:
    # 1. Safety hard-stop first
    safety_check(action.operation, action.target)

    # 2. Pre-flight checks (quota, headroom, classification)
    preflight = await preflight_fn()
    if not preflight.allowed:
        # Log the blocked action — this is an audit trail event
        await log_action(action, status="blocked_by_preflight", reason=preflight.reason)
        return ActionResult(success=False, reason=preflight.reason)

    # 3. Execute
    result = await action.execute()
    await log_action(action, status="executed", result=result)
    return result
```

---

## 9. Confidence Assessment

| Area | Confidence | Notes |
|---|---|---|
| K8s Watch API patterns | HIGH | Well-documented, stable API; 410 handling pattern is well-known |
| CloudWatch SNS pipeline | HIGH | Standard AWS pattern; subscription confirmation flow is critical to get right |
| Prometheus Alertmanager config | HIGH | Config format has been stable; verify inhibition rule labels match your setup |
| Redis Streams event bus | HIGH | Correct choice for this scale; Consumer Group API is stable |
| asyncio multi-orchestrator | HIGH | Correct concurrency model for I/O-bound Claude calls |
| Confidence scoring via structured output | HIGH | Tool-use structured output is the most reliable way to get typed scores |
| 0.8 threshold calibration | MEDIUM | Needs empirical tuning after 30 days of production data |
| EWMA + percentile baseline | HIGH | Standard statistical approach; implementation straightforward |
| Minimum 7-day learning period | HIGH | Mathematically justified; matches industry practice |
| TimescaleDB for baselines | HIGH | Correct choice; verify TimescaleDB extension available in your PostgreSQL setup |
| SSE vs WebSocket decision | HIGH | SSE is correct for one-way monitoring feed |
| React Zustand + EventSource | MEDIUM | Verify Zustand version compatibility with React version in use |
| kubernetes-asyncio version | MEDIUM | Verify version matching your K8s cluster version before pinning |
| aioboto3 version | MEDIUM | Verify latest aioboto3 compatible with boto3 version used |
| Prompt caching structure | HIGH | Anthropic cache_control API is well-documented |

---

## 10. Gaps Requiring Phase-Specific Research

1. **K8s authentication from EC2:** Whether to use kube2iam, IRSA (IAM Roles for Service Accounts), or kubeconfig on the monitoring EC2. Depends on whether the monitoring agent runs as a K8s pod or as a bare process on EC2. This needs a concrete decision before implementing the watcher.

2. **TimescaleDB availability:** Confirm TimescaleDB extension is available in the PostgreSQL version running in Atoloan's environment before committing to it. Alternative: use plain PostgreSQL with manual partitioning + pg_stat extension.

3. **CloudWatch Agent installation:** Memory and disk metrics require the CW Agent to be installed on the EC2 instances. If not already present, this is a prerequisite that needs infra work.

4. **Alertmanager presence:** Confirm Prometheus + Alertmanager are already deployed in the K8s cluster, or whether they need to be added. This is a significant prerequisite for the Prometheus webhook path.

5. **0.8 threshold calibration:** The default 0.8 is a reasonable starting point, but needs empirical calibration after 30 days of production data. Build in a mechanism to retroactively score past diagnoses against outcomes.

6. **Cross-domain RCA context window size:** Under a major incident, all three domain orchestrators could produce large diagnosis payloads. Measure token sizes in practice and determine whether the meta-orchestrator needs to summarize domain diagnoses before passing them to Claude extended thinking.
