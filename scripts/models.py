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
    # Bucket A/B/C additions — see REMEDIATION_AGENT_DESIGN.md Action Catalog
    CREATE_CONTAINER_CONFIG_ERROR = "CreateContainerConfigError"
    CREATE_CONTAINER_ERROR        = "CreateContainerError"
    INVALID_IMAGE_NAME            = "InvalidImageName"
    ERR_IMAGE_NEVER_PULL          = "ErrImageNeverPull"
    CONTAINER_CREATING_STUCK      = "ContainerCreatingStuck"
    NODE_PRESSURE                 = "NodePressure"
    ENDPOINT_MISMATCH             = "EndpointMismatch"
    ROLLOUT_STUCK                 = "RolloutStuck"
    QUOTA_EXCEEDED                = "QuotaExceeded"
    PDB_BLOCKED                   = "PDBBlocked"
    TAINT_MISMATCH                = "TaintMismatch"
    HPA_DEGRADED                  = "HPADegraded"


class ActionTier(str, Enum):
    """
    Execution tier for a remediation action, keyed off the Action Catalog.
    AUTO executes immediately after the confirm check passes; NEEDS_APPROVAL
    executes only when the caller passes --auto-approve; DIAGNOSE_ONLY never
    executes and always produces an escalation-style summary.
    """
    AUTO           = "auto"
    NEEDS_APPROVAL = "needs_approval"
    DIAGNOSE_ONLY  = "diagnose_only"


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
    # status.reason — e.g. "Evicted" for a node-pressure-evicted pod
    reason:            Optional[str]  = None
    # metadata.deletionTimestamp — set while a pod is Terminating, cleared once gone
    deletion_timestamp: Optional[str] = None
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


class ConfigRefCheck(BaseModel):
    """Returned by diagnose_container_config_error() — ConfigMap/Secret reference audit."""
    pod:           str
    namespace:     str
    missing_refs:  list[str] = Field(default_factory=list)   # "configmap/name" or "secret/name"
    present_refs:  list[str] = Field(default_factory=list)
    all_present:   bool      = True
    summary:       str       = ""


class ContainerCreatingStatus(BaseModel):
    """Returned by diagnose_stuck_container_creating()"""
    pod:                str
    namespace:          str
    stuck_seconds:      float           = 0.0
    adverse_events:     list[K8sEvent]  = Field(default_factory=list)
    likely_cause:       Optional[str]   = None   # "volume_attach" | "cni" | "slow_pull" | None
    summary:            str             = ""


class NodePressureCheck(BaseModel):
    """Returned by check_pod_node_pressure()"""
    pod:        str
    namespace:  str
    node:       Optional[str]         = None
    conditions: list[NodeCondition]   = Field(default_factory=list)
    pressured:  bool                  = False
    summary:    str                  = ""


class EndpointMismatch(BaseModel):
    """Returned by diagnose_endpoint_mismatch()"""
    pod:            str
    namespace:      str
    service:        Optional[str]      = None
    pod_labels:     dict[str, str]     = Field(default_factory=dict)
    selector:       dict[str, str]     = Field(default_factory=dict)
    labels_match:   bool               = True
    pod_in_endpoints: bool             = False
    summary:        str                = ""


class DeploymentConditionStatus(BaseModel):
    """Returned by get_deployment_conditions()"""
    deployment:          str
    namespace:            str
    progressing:          Optional[str] = None   # True / False / Unknown
    progress_reason:      Optional[str] = None   # e.g. ProgressDeadlineExceeded
    available:            Optional[str] = None
    replicas_desired:     int           = 0
    replicas_available:   int           = 0
    replicas_updated:     int           = 0
    stuck:                bool          = False
    summary:              str           = ""


class QuotaStatus(BaseModel):
    """Returned by get_resourcequota_status()"""
    namespace: str
    quotas:    list[dict] = Field(default_factory=list)   # {name, hard, used}
    exceeded:  list[str]  = Field(default_factory=list)   # dimensions at/over hard limit
    summary:   str        = ""


class PDBStatus(BaseModel):
    """Returned by get_pdb_status()"""
    namespace:            str
    name:                 str
    disruptions_allowed:  int  = 0
    current_healthy:      int  = 0
    desired_healthy:      int  = 0
    blocking:             bool = False
    summary:              str  = ""


class HPAStatus(BaseModel):
    """Returned by get_hpa_status()"""
    namespace:         str
    name:              str
    able_to_scale:     Optional[bool] = None
    scaling_active:    Optional[bool] = None
    current_replicas:  int            = 0
    desired_replicas:  int            = 0
    degraded:          bool           = False
    reason:            Optional[str]  = None
    summary:           str            = ""


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


# ---------------------------------------------------------------------------
# Remediation Agent types (orchestrator.find_issue() -> remediation_agent.dispatch())
#
# Standalone pipeline, independent of the L1 Runbook types above:
# see REMEDIATION_AGENT_DESIGN.md.
# ---------------------------------------------------------------------------

class RemediationResolution(str, Enum):
    RESOLVED      = "resolved"        # action executed and verified healthy
    ESCALATED     = "escalated"       # action executed but did not verify healthy, or Tier 3
    PROPOSED      = "proposed"        # Tier 2 action identified but not executed (no --auto-approve)
    DIAGNOSE_ONLY = "diagnose_only"   # Tier 3 classification, or confirm check failed to corroborate
    HEALTHY       = "healthy"         # pod was already healthy, no action needed


class IssueReport(BaseModel):
    """
    Returned by orchestrator.find_issue() — the Find-stage output and the
    structured handoff into remediation_agent.dispatch().
    """
    pod:              str
    namespace:        str            = "default"
    deployment:       Optional[str]  = None
    classification:   str            = FailureClass.UNKNOWN.value
    confidence:       str            = "low"     # high | medium | low
    finding:          str            = ""        # one-sentence bottom line
    root_cause:       str            = ""
    evidence:         dict           = Field(default_factory=dict)
    # Full narrative (raw final text from Claude)
    summary:          str            = ""
    tool_calls_made:  int            = 0


class RemediationSummary(BaseModel):
    """
    Returned by remediation_agent.dispatch() — the Fix-stage output printed
    by remediate.py.
    """
    pod:               str
    namespace:         str
    deployment:        Optional[str]           = None
    classification:    str
    confidence:        str
    tier:              ActionTier
    confirmed:         bool                    # did the deterministic re-check corroborate the classification
    resolution:        RemediationResolution
    action_taken:      Optional[RemediationResult] = None
    verified_healthy:  Optional[bool]          = None
    root_cause:        str                     = ""
    evidence:          dict                    = Field(default_factory=dict)
    next_steps:        list[str]               = Field(default_factory=list)
    duration_s:        Optional[float]         = None
    summary:           str                     = ""
