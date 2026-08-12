"""Read-only acceptance checks for a configured tool-calling model."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from wyzer.brain import (
    CapabilityContextBuilder,
    OrchestratorFeatures,
    SystemPromptBuilder,
    create_chat_provider,
)
from wyzer.config import WyzerSettings
from wyzer.models import ChatMessage, ConversationState, WorldStateSnapshot
from wyzer.tasks.tools import task_native_tools
from wyzer.tools import create_default_registry
from wyzer.tools.capabilities import (
    ACTIVATE_CAPABILITY_TOOL,
    LIST_CAPABILITIES_TOOL,
)


class AcceptanceCase(BaseModel):
    """One expected decision or short direct sequence from the configured model."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    prompt: str = Field(min_length=1, max_length=2_000)
    expected_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    required_response_concepts: list[list[str]] = Field(default_factory=list)
    forbidden_response_terms: list[str] = Field(default_factory=list)


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    passed: bool
    expected_tools: list[str]
    actual_tools: list[str]
    tool_trace: list[str] = Field(default_factory=list)
    forbidden_tools_seen: list[str]
    latency_ms: int
    response_text: str
    missing_response_concepts: list[list[str]] = Field(default_factory=list)
    forbidden_response_terms_seen: list[str] = Field(default_factory=list)
    error: str | None = None


class AcceptanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    passed: int
    total: int
    pass_rate: float
    minimum_pass_rate: float
    results: list[CaseResult]


def load_cases(path: Path) -> list[AcceptanceCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("acceptance case file must contain a JSON list")
    cases = [AcceptanceCase.model_validate(item) for item in raw]
    identifiers = [case.case_id for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("acceptance case IDs must be unique")
    return cases


async def evaluate(
    settings: WyzerSettings,
    cases: list[AcceptanceCase],
    *,
    minimum_pass_rate: float,
) -> AcceptanceReport:
    provider = create_chat_provider(settings.llm, settings.personality)
    if not provider.available:
        raise RuntimeError("the configured model provider is unavailable")

    registry = create_default_registry(
        audio_options=settings.audio.model_dump(),
        perception_options={
            **settings.perception.model_dump(),
            "endpoint": str(settings.llm.endpoint) if settings.llm.endpoint else None,
            "model": settings.llm.model,
        },
        enabled_entrypoint_packs=settings.tool_packs.enabled,
    )
    initial_task_tools = task_native_tools(active_plan=False)
    default_tools = [*registry.native_tools(), *initial_task_tools]
    available_tools = {tool.function.name for tool in default_tools}
    all_available_tools = {
        *(tool.function.name for tool in registry.all_native_tools()),
        *(tool.function.name for tool in task_native_tools()),
    }
    known_tools = {*registry, *(tool.function.name for tool in task_native_tools())}
    for case in cases:
        unknown = (set(case.expected_tools) | set(case.forbidden_tools)) - known_tools
        if unknown:
            raise ValueError(
                f"case {case.case_id} references unknown tools: {', '.join(sorted(unknown))}"
            )
        unavailable = set(case.expected_tools) - all_available_tools
        if unavailable:
            raise ValueError(
                f"case {case.case_id} expects unavailable tools: {', '.join(sorted(unavailable))}"
            )

    personality = settings.personality.model_dump(mode="json")
    system_prompt = SystemPromptBuilder(personality=personality).build(
        WorldStateSnapshot(),
        ConversationState(),
        capability_context=CapabilityContextBuilder(
            registry,
            OrchestratorFeatures(
                persistent_complex_task_planning=settings.task_engine.enabled
            ),
        ).build(),
    )
    results: list[CaseResult] = []
    for case in cases:
        started = time.perf_counter()
        try:
            messages = [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=case.prompt),
            ]
            activated: set[str] = set()
            trace: list[str] = []
            direct_sequence: list[str] = []
            actual: list[str] = []
            response_text = ""
            for _ in range(6):
                capability_tools = registry.model_view(activated).native_tools()
                tools = [
                    *capability_tools,
                    *initial_task_tools,
                ]
                offered = {tool.function.name for tool in tools}
                response = await provider.chat(messages, tools)
                calls = response.message.tool_calls
                names = [call.function.name for call in calls]
                trace.extend(names)
                response_text = (response.message.content or "").strip()
                if not calls:
                    actual = direct_sequence
                    break

                # Core expectations must remain direct; only specialized expectations may
                # use capability coordination in this read-only simulation.
                expected_is_specialized = bool(
                    case.expected_tools and not set(case.expected_tools).issubset(available_tools)
                )
                if not expected_is_specialized and any(
                    registry.is_capability_coordination_tool(name) for name in names
                ):
                    actual = names
                    break

                messages.append(response.message)
                if all(name == LIST_CAPABILITIES_TOOL for name in names):
                    serialized_payload = json.dumps(
                        {
                            "ok": True,
                            "data": {"capabilities": registry.capability_manifest(activated)},
                        },
                        separators=(",", ":"),
                    )
                    for call in calls:
                        messages.append(
                            ChatMessage(
                                role="tool",
                                name=call.function.name,
                                tool_call_id=call.id,
                                content=serialized_payload,
                            )
                        )
                    continue

                if all(
                    name == ACTIVATE_CAPABILITY_TOOL
                    or registry.activation_capability(name) is not None
                    for name in names
                ):
                    activation_failed = False
                    for call in calls:
                        capability = (
                            str(call.function.arguments.get("name") or "")
                            if call.function.name == ACTIVATE_CAPABILITY_TOOL
                            else registry.activation_capability(call.function.name) or ""
                        )
                        if capability not in registry.available_capabilities():
                            activation_failed = True
                            payload_data = {
                                "ok": False,
                                "error": {
                                    "code": "UNKNOWN_CAPABILITY",
                                    "message": f"No activatable capability named {capability}.",
                                },
                            }
                        else:
                            activated.add(capability)
                            payload_data = {
                                "ok": True,
                                "data": {
                                    "name": capability,
                                    "instruction": (
                                        "Activation is complete but performed no action. Continue the "
                                        "original request now with the matching newly available action "
                                        "or observation tool. Activation does not make a small request "
                                        "complex; do not create a plan merely because activation was "
                                        "needed."
                                    ),
                                },
                            }
                        messages.append(
                            ChatMessage(
                                role="tool",
                                name=call.function.name,
                                tool_call_id=call.id,
                                content=json.dumps(payload_data, separators=(",", ":")),
                            )
                        )
                    if activation_failed:
                        continue
                    continue

                inactive = [
                    call
                    for call in calls
                    if call.function.name in registry and call.function.name not in offered
                ]
                if len(inactive) == len(calls):
                    for call in inactive:
                        pack = registry.tool_pack(call.function.name)
                        messages.append(
                            ChatMessage(
                                role="tool",
                                name=call.function.name,
                                tool_call_id=call.id,
                                content=json.dumps(
                                    {
                                        "ok": False,
                                        "error": {
                                            "code": "CAPABILITY_NOT_ACTIVE",
                                            "message": (
                                                f"Capability {pack} is not active. List and "
                                                "activate it, then retry on the next round."
                                            ),
                                        },
                                    },
                                    separators=(",", ":"),
                                ),
                            )
                        )
                    continue

                if len(case.expected_tools) > 1:
                    candidate = [*direct_sequence, *names]
                    expected_prefix = case.expected_tools[: len(candidate)]
                    if candidate == case.expected_tools:
                        actual = candidate
                        break
                    if candidate == expected_prefix and not any(
                        name in case.forbidden_tools for name in names
                    ):
                        direct_sequence = candidate
                        for call in calls:
                            messages.append(
                                ChatMessage(
                                    role="tool",
                                    name=call.function.name,
                                    tool_call_id=call.id,
                                    content=json.dumps(
                                        {
                                            "ok": True,
                                            "data": {
                                                "simulated": True,
                                                "instruction": (
                                                    "This action succeeded in the read-only acceptance "
                                                    "simulation. Continue any remaining requested action."
                                                ),
                                            },
                                        },
                                        separators=(",", ":"),
                                    ),
                                )
                            )
                        continue
                    actual = candidate
                    break

                actual = names
                break
            else:
                actual = direct_sequence or trace[-1:]

            forbidden_seen = [name for name in actual if name in case.forbidden_tools]
            normalized_response = " ".join(response_text.casefold().split())
            missing_concepts = [
                alternatives
                for alternatives in case.required_response_concepts
                if not any(term.casefold() in normalized_response for term in alternatives)
            ]
            forbidden_terms_seen = [
                term
                for term in case.forbidden_response_terms
                if term.casefold() in normalized_response
            ]
            passed = (
                actual == case.expected_tools
                and not forbidden_seen
                and not missing_concepts
                and not forbidden_terms_seen
            )
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    passed=passed,
                    expected_tools=case.expected_tools,
                    actual_tools=actual,
                    tool_trace=trace,
                    forbidden_tools_seen=forbidden_seen,
                    latency_ms=round((time.perf_counter() - started) * 1_000),
                    response_text=response_text,
                    missing_response_concepts=missing_concepts,
                    forbidden_response_terms_seen=forbidden_terms_seen,
                )
            )
        except Exception as error:
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    passed=False,
                    expected_tools=case.expected_tools,
                    actual_tools=[],
                    tool_trace=[],
                    forbidden_tools_seen=[],
                    latency_ms=round((time.perf_counter() - started) * 1_000),
                    response_text="",
                    error=str(error),
                )
            )

    passed_count = sum(result.passed for result in results)
    return AcceptanceReport(
        provider=settings.llm.provider,
        model=settings.llm.model,
        passed=passed_count,
        total=len(results),
        pass_rate=(passed_count / len(results)) if results else 0,
        minimum_pass_rate=minimum_pass_rate,
        results=results,
    )


def _print_summary(report: AcceptanceReport) -> None:
    for result in report.results:
        marker = "PASS" if result.passed else "FAIL"
        actual = ", ".join(result.actual_tools) if result.actual_tools else "text response"
        expected = ", ".join(result.expected_tools) if result.expected_tools else "text response"
        print(
            f"[{marker}] {result.case_id}: expected {expected}; got {actual} "
            f"({result.latency_ms} ms)"
        )
        if result.tool_trace != result.actual_tools:
            print(f"       trace: {', '.join(result.tool_trace) or 'text response'}")
        if result.error:
            print(f"       error: {result.error}")
        if result.missing_response_concepts:
            print(f"       missing concepts: {result.missing_response_concepts}")
        if result.forbidden_response_terms_seen:
            print(
                "       forbidden response terms: "
                + ", ".join(result.forbidden_response_terms_seen)
            )
    print(
        f"Model acceptance: {report.passed}/{report.total} "
        f"({report.pass_rate:.1%}); required {report.minimum_pass_rate:.1%}"
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate configured model tool decisions without executing any tools."
        )
    )
    parser.add_argument("--config", type=Path, default=Path("wyzer.toml"))
    parser.add_argument("--cases", type=Path, default=Path("evals/model_acceptance.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", action="append", dest="case_ids", default=[])
    parser.add_argument("--minimum-pass-rate", type=float, default=0.80)
    args = parser.parse_args(argv)
    if not 0 <= args.minimum_pass_rate <= 1:
        parser.error("--minimum-pass-rate must be between 0 and 1")

    cases = load_cases(args.cases)
    if args.case_ids:
        requested = set(args.case_ids)
        cases = [case for case in cases if case.case_id in requested]
        missing = requested - {case.case_id for case in cases}
        if missing:
            parser.error(f"unknown case IDs: {', '.join(sorted(missing))}")
    if not cases:
        parser.error("no acceptance cases selected")

    settings = WyzerSettings.load(args.config)
    report = asyncio.run(evaluate(settings, cases, minimum_pass_rate=args.minimum_pass_rate))
    _print_summary(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if report.pass_rate < report.minimum_pass_rate:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
