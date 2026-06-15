from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


@dataclass
class InfraEvent:
    source: str
    domain: Literal["k8s", "db", "infra", "app"]
    event_type: str
    severity: Literal["critical", "warning", "info"]
    resource_id: str
    timestamp: datetime
    raw_payload: dict[str, Any]
    labels: dict[str, str] = field(default_factory=dict)
    stream_id: str = ""  # assigned by Redis after publish


@dataclass
class SignalBundle:
    """A time-windowed group of correlated InfraEvents sent to the diagnoser."""
    domain: str
    signals: list[InfraEvent]
    started_at: datetime
    window_seconds: int = 30


class DiagnosisResult(BaseModel):
    """Structured RCA output from Claude. Pydantic so we can generate JSON schema for tool_use."""
    root_cause: str = Field(description="Primary root cause in 2-3 sentences")
    affected_components: list[str] = Field(description="K8s/infra components affected")
    contributing_factors: list[str] = Field(description="Secondary factors that contributed")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Confidence 0.0–1.0. "
            "0.9–1.0: multiple corroborating signals, complete causal chain. "
            "0.7–0.8: strong evidence but one alternative explanation. "
            "0.5–0.6: circumstantial evidence. "
            "< 0.5: insufficient data."
        ),
    )
    confidence_reasoning: str = Field(description="Why you chose this confidence score")
    recommended_action: str = Field(description="Specific remediation step in plain English")
    action_type: Literal[
        "pod_restart", "deployment_scale_down", "human_escalate", "observe_only"
    ] = Field(description="Machine-readable action category")
    requires_human_review: bool
    estimated_blast_radius: Literal["service", "cluster", "datacenter"]


@dataclass
class HumanEscalationPacket:
    incident_id: str
    timestamp: datetime
    summary: str
    urgency: Literal["p1_immediate", "p2_within_15m", "p3_within_1h"]
    diagnosis: DiagnosisResult
    why_escalated: str
    raw_signals: list[InfraEvent]
