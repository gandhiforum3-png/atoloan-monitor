"""
models.py — Pydantic schemas for all k8s-pod-observer data structures.

Every observer function returns one of these — no raw strings, no untyped dicts.
All models are JSON-serialisable so they drop straight into Claude tool_result payloads.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PodPhase(str, Enum):
    PENDING   = "Pending"
    RUNNING   = "Running"
    SUCCEEDED = "Succeeded"
    FAILED    = "Failed"
    UNKNOWN   = "Unknown"


class EventType(str, Enum):
    NORMAL  = "Normal"
    WARNING = "Warning"


class FailureClass(str, Enum):
    CRASH_LOOP        = "CrashLoopBackOff"
    OOM_KILLED        = "OOMKilled"
    PENDING_SCHEDULE  = "FailedScheduling"
    IMAGE_PULL        = "ImagePullError"
    PROBE_FAILURE     = "ProbeFailure"
    VOLUME_MOUNT      = "VolumeMountError"
    EVICTED           = "Evicted"
    INIT_FAILURE      = "InitContainerFailure"
    HEALTHY           = "Healthy"
    UNKNOWN           = "Unknown"


# ---------------------------------------------------------------------------
# Core building blocks
# ---------------------------------------------------------------------------

class ContainerTerminatedState(BaseModel):
    exit_code:   int
    reason:      Optional[str] = None   # OOMKilled, Error, Completed …
    message:     Optional[str] = None
    started_at:  Optional[str] = None
    finished_at: Optional[str] = None
    signal:      Optional[int] = None


class ContainerWaitingState(BaseModel):
    reason:  Optional[str] = None       # CrashLoopBackOff, ImagePullBackOff …
    message: Optional[str] = None


class ContainerStatus(BaseModel):
    name:          str
    ready:         bool
    restart_count: int
    image:         str
    state:         str                  # "running" | "waiting" | "terminated"
    waiting:       Optional[ContainerWaitingState]   = None
    terminated:    Optional[ContainerTerminatedState] = None
    last_state:    Optional[ContainerTerminatedState] = None


class K8sEvent(BaseModel):
    type:       EventType
    reason:     str
    message:    str
    count:      int   = 1
    first_time: Optional[str] = None
    last_time:  Optional[str] = None
    component:  Optional[str] = None


class ResourceRequest(BaseModel):
    cpu:    Optional[str] = None
    memory: Optional[str] = None


class ContainerResources(BaseModel):
    container: str
    requests:  ResourceRequest = Field(default_factory=ResourceRequest)
    limits:    ResourceRequest = Field(default_factory=ResourceRequest)
    # Live usage from metrics-server (None when not available)
    cpu_usage:    Optional[str] = None
    memory_usage: Optional[str] = None


# ---------------------------------------------------------------------------
# Observer return types
# ---------------------------------------------------------------------------

class PodStatus(BaseModel):
    """Returned by get_pod_status()"""
    pod:               str
    namespace:         str
    phase:             PodPhase
    node:              Optional[str]  = None
    pod_ip:            Optional[str]  = None
    ready:             bool           = False
    total_restarts:    int            = 0
    container_statuses: list[ContainerStatus] = Field(default_factory=list)
    init_container_statuses: list[ContainerStatus] = Field(default_factory=list)
    conditions:        dict[str, str] = Field(default_factory=dict)  # type → status
    start_time:        Optional[str]  = None
    # Quick-read failure hint (None = healthy)
    failure_hint:      Optional[str]  = None


class PodLogs(BaseModel):
    """Returned by get_pod_logs()"""
    pod:       str
    namespace: str
    container: str
    previous:  bool
    lines:     list[str]
    truncated: bool = False           # True when tail limit was hit


class NamespacePod(BaseModel):
    """One row in the namespace pod list."""
    name:      str
    namespace: str
    phase:     str
    ready:     str    # "2/2" format
    restarts:  int
    node:      Optional[str] = None
    age:       Optional[str] = None


class NodeCondition(BaseModel):
    node:         str
    condition:    str   # MemoryPressure, DiskPressure, PIDPressure, Ready
    status:       str   # True / False / Unknown
    reason:       Optional[str] = None
    message:      Optional[str] = None


class EndpointStatus(BaseModel):
    """Returned by get_endpoints()"""
    service:   str
    namespace: str
    ready_addresses:    list[str] = Field(default_factory=list)
    not_ready_addresses: list[str] = Field(default_factory=list)
    selector:  dict[str, str] = Field(default_factory=dict)


class ResourceUsage(BaseModel):
    """Returned by get_pod_resource_usage()"""
    pod:        str
    namespace:  str
    containers: list[ContainerResources] = Field(default_factory=list)
    # True when metrics-server is unavailable
    metrics_unavailable: bool = False


# ---------------------------------------------------------------------------
# Diagnostic composite types
# ---------------------------------------------------------------------------

class CrashDiagnosis(BaseModel):
    """Returned by diagnose_crash() — everything needed to explain a crash."""
    pod:            str
    namespace:      str
    failure_class:  FailureClass
    exit_code:      Optional[int]  = None
    kill_reason:    Optional[str]  = None   # OOMKilled, Error, …
    restart_count:  int            = 0
    previous_logs:  list[str]      = Field(default_factory=list)
    recent_events:  list[K8sEvent] = Field(default_factory=list)
    memory_limit:   Optional[str]  = None
    memory_usage:   Optional[str]  = None
    # Human-readable one-line summary
    summary: str = ""


class PendingDiagnosis(BaseModel):
    """Returned by diagnose_pending() — scheduling failure analysis."""
    pod:                 str
    namespace:           str
    scheduling_events:   list[K8sEvent] = Field(default_factory=list)
    node_pressure_nodes: list[str]      = Field(default_factory=list)
    unbound_pvcs:        list[str]      = Field(default_factory=list)
    resource_requests:   list[ContainerResources] = Field(default_factory=list)
    summary: str = ""


class NamespaceHealthReport(BaseModel):
    """Returned by namespace_health_sweep()"""
    namespace:          str
    total_pods:         int
    running_pods:       int
    non_running_pods:   list[NamespacePod] = Field(default_factory=list)
    high_restart_pods:  list[NamespacePod] = Field(default_factory=list)   # restarts > 3
    warning_events:     list[K8sEvent]     = Field(default_factory=list)
    pressure_nodes:     list[NodeCondition] = Field(default_factory=list)
    unbound_pvcs:       list[str]          = Field(default_factory=list)
    # Overall health signal
    healthy:            bool  = True
    summary:            str   = ""


# ---------------------------------------------------------------------------
# L1 Runbook types
# ---------------------------------------------------------------------------

class L1Resolution(str, Enum):
    RESOLVED     = "resolved"       # issue fixed, pod healthy
    ESCALATED    = "escalated"      # L1 exhausted, packet ready for L2
    NEEDS_HUMAN  = "needs_human"    # safe to fix but requires manual approval


class Severity(str, Enum):
    P1 = "P1"   # complete outage / data-loss risk
    P2 = "P2"   # degraded / single pod down
    P3 = "P3"   # warning / non-critical


class RemediationResult(BaseModel):
    """Returned by every remediator function."""
    action:     str           # human-readable name of what was attempted
    target:     str           # pod or deployment name
    namespace:  str
    success:    bool
    message:    str           # what happened
    dry_run:    bool = False
    duration_s: Optional[float] = None


class RunbookStep(BaseModel):
    """One step in the L1 decision tree execution."""
    step_num:   int
    action:     str           # what the runbook did
    finding:    str           # what it found
    outcome:    str           # "pass" | "fail" | "remediated" | "escalate"
    timestamp:  str


class EscalationPacket(BaseModel):
    """
    Structured handoff from L1 → L2.
    Contains everything L2 needs to pick up where L1 stopped.
    """
    pod:                    str
    namespace:              str
    severity:               Severity
    created_at:             str
    failure_class:          str
    summary:                str               # one-sentence bottom line
    steps_taken:            list[RunbookStep] = Field(default_factory=list)
    remediations_attempted: list[RemediationResult] = Field(default_factory=list)
    current_pod_state:      Optional[dict]   = None   # serialised PodStatus
    evidence:               dict             = Field(default_factory=dict)
    # L2 guidance (generated by Claude)
    recommended_l2_actions: list[str]        = Field(default_factory=list)


class RunbookResult(BaseModel):
    """Top-level return from run_l1_runbook()."""
    pod:               str
    namespace:         str
    resolution:        L1Resolution
    severity:          Severity
    failure_class:     str
    steps:             list[RunbookStep]         = Field(default_factory=list)
    remediations:      list[RemediationResult]   = Field(default_factory=list)
    escalation_packet: Optional[EscalationPacket] = None
    resolved_in_s:     Optional[float]           = None
    summary:           str                       = ""
