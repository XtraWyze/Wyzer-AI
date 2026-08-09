"""High-level read-only Windows diagnostics for the model.

The model sees one bounded diagnostic tool instead of a large menu of low-level
telemetry probes. The Windows backend owns collection and deterministic finding
classification; the LLM remains responsible for interpreting the returned facts
and deciding what, if anything, should happen next.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from wyzer.desktop.system import WindowsSystemBackend
from wyzer.models import ConfirmationMode, RiskLevel, ToolArguments
from wyzer.tools.base import Tool, ToolContext

DiagnosticScope = Literal[
    "auto",
    "performance",
    "hardware",
    "storage",
    "network",
    "windows",
    "security",
]


class DiagnoseSystemArguments(ToolArguments):
    scope: DiagnosticScope = Field(
        default="auto",
        description=(
            "Diagnostic area. Use auto for a broad health snapshot; performance for CPU/RAM/GPU/"
            "process load; hardware for CPU/GPU/battery/firmware; storage for disks and space; "
            "network for adapters/connectivity; windows for services/devices/recent serious "
            "events; "
            "security for Defender and firewall state."
        ),
    )


class DiagnosticFinding(BaseModel):
    severity: Literal["info", "attention", "warning"]
    component: str
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class DiagnoseSystemResult(BaseModel):
    scope: DiagnosticScope
    health: Literal["ok", "attention", "warning", "unknown"]
    collected_at: str
    summary: list[str] = Field(default_factory=list)
    findings: list[DiagnosticFinding] = Field(default_factory=list)
    telemetry: dict[str, Any] = Field(default_factory=dict)
    unavailable: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class DiagnoseSystemTool(Tool[DiagnoseSystemArguments, DiagnoseSystemResult]):
    name = "diagnose_system"
    description = (
        "Inspect current Windows health and telemetry: CPU, RAM, GPU/VRAM, disks, network, "
        "processes, devices, services, serious events, Defender, and firewall. Read-only."
    )
    arguments_type = DiagnoseSystemArguments
    result_type = DiagnoseSystemResult
    risk_level = RiskLevel.LOW
    read_only = True
    confirmation = ConfirmationMode.NEVER
    default_timeout_seconds = 30.0

    def __init__(self, backend: WindowsSystemBackend) -> None:
        self.backend = backend

    def execute(
        self,
        arguments: DiagnoseSystemArguments,
        context: ToolContext,
    ) -> DiagnoseSystemResult:
        del context
        return DiagnoseSystemResult.model_validate(
            self.backend.diagnose_system(scope=arguments.scope)
        )
