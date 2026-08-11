"""Bounded, read-only end-to-end model acceptance trajectories."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from wyzer.brain import SystemPromptBuilder, create_chat_provider
from wyzer.config import WyzerSettings
from wyzer.models import ChatMessage, ConversationState, WorldStateSnapshot
from wyzer.policy.confirmations import ConfirmationPolicy
from wyzer.tasks.tools import TASK_ARGUMENT_TYPES, CreateTaskPlanArguments, task_native_tools
from wyzer.tools import create_default_registry
from wyzer.tools.capabilities import ACTIVATE_CAPABILITY_TOOL, LIST_CAPABILITIES_TOOL


class ToolOutcome(BaseModel):
    """One successfully simulated call required for a final outcome."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    argument_options: dict[str, list[Any]] = Field(default_factory=dict)


class OutcomeAlternative(BaseModel):
    """One independently sufficient way to satisfy the requested goal."""

    model_config = ConfigDict(extra="forbid")

    name: str
    required_tools: list[ToolOutcome] = Field(default_factory=list)
    clarification: bool = False


class PlanExpectations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_steps: int = Field(default=2, ge=2, le=12)
    require_distinct_steps: bool = True
    require_success_criteria: bool = True


class EfficiencyExpectations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ideal_first_tools: list[str] = Field(default_factory=list)
    ideal_provider_rounds: int = Field(ge=1, le=20)
    ideal_tool_calls: int = Field(ge=0, le=50)
    unnecessary_observation_tools: list[str] = Field(default_factory=list)


class EndToEndCase(BaseModel):
    """A bounded model trajectory evaluated against explicit safe outcomes."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    requested_goal: str = Field(min_length=1, max_length=2_000)
    allowed_tool_paths: list[list[str]] = Field(min_length=1)
    prohibited_tools: list[str] = Field(default_factory=list)
    required_final_outcome: str = Field(min_length=1, max_length=1_000)
    outcomes: list[OutcomeAlternative] = Field(min_length=1)
    maximum_model_rounds: int = Field(default=6, ge=1, le=20)
    clarification: Literal["forbidden", "allowed", "required"] = "forbidden"
    confirmation: Literal["none", "required", "required_if_action"] = "none"
    efficiency: EfficiencyExpectations
    mock_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    plan_expectations: PlanExpectations | None = None


class ExecutedCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict[str, Any]
    confirmation_required: bool = False


class EndToEndCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    passed: bool
    satisfied_outcome: str | None = None
    tool_trace: list[str] = Field(default_factory=list)
    successful_tool_path: list[str] = Field(default_factory=list)
    executed_calls: list[ExecutedCall] = Field(default_factory=list)
    provider_rounds: int
    tool_calls: int
    capability_activation_rounds: int
    unnecessary_observations: int
    ideal_first_tool: bool
    efficiency_score: float
    confirmation_boundaries: list[str] = Field(default_factory=list)
    clarification_provided: bool = False
    latency_ms: int
    response_text: str = ""
    failure_reason: str | None = None
    error: str | None = None


class EndToEndReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    passed: int
    total: int
    success_rate: float
    minimum_success_rate: float
    efficiency_score: float
    average_provider_rounds: float
    average_tool_calls: float
    average_latency_ms: float
    results: list[EndToEndCaseResult]


def load_end_to_end_cases(path: Path) -> list[EndToEndCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("end-to-end acceptance case file must contain a JSON list")
    cases = [EndToEndCase.model_validate(item) for item in raw]
    identifiers = [case.case_id for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("end-to-end acceptance case IDs must be unique")
    return cases


def _matches_value(actual: Any, options: list[Any]) -> bool:
    for expected in options:
        if isinstance(actual, str) and isinstance(expected, str):
            if actual.strip().casefold() == expected.strip().casefold():
                return True
        elif actual == expected:
            return True
    return False


def _matches_outcome(call: ExecutedCall, outcome: ToolOutcome) -> bool:
    return call.tool == outcome.tool and all(
        key in call.arguments and _matches_value(call.arguments[key], options)
        for key, options in outcome.argument_options.items()
    )


def _plan_is_meaningful(arguments: dict[str, Any], expected: PlanExpectations) -> bool:
    try:
        validated = CreateTaskPlanArguments.model_validate(arguments)
    except Exception:
        return False
    steps = validated.steps
    if len(steps) < expected.minimum_steps:
        return False
    descriptions = [" ".join(step.description.casefold().split()) for step in steps]
    if expected.require_distinct_steps and len(set(descriptions)) != len(descriptions):
        return False
    return not (
        expected.require_success_criteria
        and any(not step.success_criteria.strip() for step in steps)
    )


def _satisfied_outcome(
    case: EndToEndCase,
    executed: list[ExecutedCall],
    *,
    clarification: bool,
) -> str | None:
    for alternative in case.outcomes:
        if alternative.clarification:
            if clarification:
                return alternative.name
            continue
        remaining = list(executed)
        matched = True
        for required in alternative.required_tools:
            index = next(
                (i for i, call in enumerate(remaining) if _matches_outcome(call, required)),
                None,
            )
            if index is None:
                matched = False
                break
            call = remaining.pop(index)
            if (
                call.tool == "task_plan_create"
                and case.plan_expectations is not None
                and not _plan_is_meaningful(call.arguments, case.plan_expectations)
            ):
                matched = False
                break
        if matched:
            return alternative.name
    return None


def _path_is_prefix(path: list[str], allowed_paths: list[list[str]]) -> bool:
    return any(path == allowed[: len(path)] for allowed in allowed_paths)


def _path_is_complete(path: list[str], allowed_paths: list[list[str]]) -> bool:
    return any(path == allowed for allowed in allowed_paths)


def _efficiency_score(
    case: EndToEndCase,
    *,
    provider_rounds: int,
    tool_calls: int,
    first_tool: str | None,
    unnecessary_observations: int,
) -> tuple[bool, float]:
    ideal_first = (
        first_tool in case.efficiency.ideal_first_tools
        if case.efficiency.ideal_first_tools
        else first_tool is None
    )
    round_score = min(1.0, case.efficiency.ideal_provider_rounds / provider_rounds)
    if case.efficiency.ideal_tool_calls == 0:
        call_score = 1.0 if tool_calls == 0 else 0.0
    else:
        call_score = min(1.0, case.efficiency.ideal_tool_calls / max(tool_calls, 1))
    observation_score = 1.0 / (1 + unnecessary_observations)
    return ideal_first, round(
        (float(ideal_first) + round_score + call_score + observation_score) / 4, 4
    )


def _tool_payload(tool: str, data: dict[str, Any]) -> str:
    return json.dumps(
        {
            "ok": True,
            "data": data,
            "evidence": {"simulated": True, "tool": tool},
            "instruction": "The controlled acceptance result is complete. Continue the request if any requested outcome remains.",
        },
        separators=(",", ":"),
        default=str,
    )


async def evaluate_end_to_end(
    settings: WyzerSettings,
    cases: list[EndToEndCase],
    *,
    minimum_success_rate: float,
) -> EndToEndReport:
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
    task_tool_names = {tool.function.name for tool in task_native_tools()}
    known_tools = {*registry, *task_tool_names}
    coordination_tools = {
        LIST_CAPABILITIES_TOOL,
        ACTIVATE_CAPABILITY_TOOL,
        *(name for name in registry if registry.activation_capability(name) is not None),
    }
    for case in cases:
        referenced = {
            *case.prohibited_tools,
            *(name for path in case.allowed_tool_paths for name in path),
            *(outcome.tool for alt in case.outcomes for outcome in alt.required_tools),
        }
        unknown = referenced - known_tools
        if unknown:
            raise ValueError(
                f"case {case.case_id} references unknown tools: {', '.join(sorted(unknown))}"
            )
        simulated = {
            name
            for path in case.allowed_tool_paths
            for name in path
            if name not in coordination_tools and name not in task_tool_names
        }
        missing_results = simulated - set(case.mock_results)
        if missing_results:
            raise ValueError(
                f"case {case.case_id} lacks mock results for: {', '.join(sorted(missing_results))}"
            )

    personality = settings.personality.model_dump(mode="json")
    system_prompt = SystemPromptBuilder(personality=personality).build(
        WorldStateSnapshot(), ConversationState()
    )
    confirmation_policy = ConfirmationPolicy(settings.confirmation_ttl_seconds)
    results: list[EndToEndCaseResult] = []

    for case in cases:
        started = time.perf_counter()
        trace: list[str] = []
        successful_path: list[str] = []
        executed: list[ExecutedCall] = []
        activated: set[str] = set()
        activation_rounds: set[int] = set()
        confirmation_boundaries: list[str] = []
        response_text = ""
        failure_reason: str | None = None
        error_text: str | None = None
        rounds = 0
        clarification_provided = False
        satisfied: str | None = None
        try:
            messages = [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=case.requested_goal),
            ]
            for round_number in range(1, case.maximum_model_rounds + 1):
                rounds = round_number
                tools = [*registry.model_view(activated).native_tools(), *initial_task_tools]
                offered = {tool.function.name for tool in tools}
                response = await provider.chat(messages, tools)
                response_text = (response.message.content or "").strip()
                calls = response.message.tool_calls
                names = [call.function.name for call in calls]
                trace.extend(names)

                if not calls:
                    clarification_provided = bool(response_text and "?" in response_text)
                    satisfied = _satisfied_outcome(
                        case, executed, clarification=clarification_provided
                    )
                    if satisfied is None:
                        failure_reason = "model stopped before a required final outcome"
                    break

                messages.append(response.message)
                round_offered = set(offered)
                for call in calls:
                    name = call.function.name
                    raw_arguments = call.function.arguments
                    if name not in known_tools:
                        failure_reason = f"unknown or hallucinated tool: {name}"
                        break
                    if name in case.prohibited_tools:
                        failure_reason = f"prohibited tool selected: {name}"
                        break

                    if name in coordination_tools:
                        if name == LIST_CAPABILITIES_TOOL:
                            payload: dict[str, Any] = {
                                "capabilities": registry.capability_manifest(activated)
                            }
                        else:
                            capability = (
                                str(raw_arguments.get("name") or "")
                                if name == ACTIVATE_CAPABILITY_TOOL
                                else registry.activation_capability(name) or ""
                            )
                            if capability not in registry.available_capabilities():
                                failure_reason = f"unknown capability activation: {capability}"
                                break
                            activated.add(capability)
                            activation_rounds.add(round_number)
                            payload = {
                                "name": capability,
                                "instruction": (
                                    "Activation completed without performing the requested action. "
                                    "Continue with the matching new direct tool; do not create a plan "
                                    "merely because activation was needed."
                                ),
                            }
                        successful_path.append(name)
                        if not _path_is_prefix(successful_path, case.allowed_tool_paths):
                            failure_reason = (
                                f"tool path is not allowed: {' -> '.join(successful_path)}"
                            )
                            break
                        messages.append(
                            ChatMessage(
                                role="tool",
                                name=name,
                                tool_call_id=call.id,
                                content=_tool_payload(name, payload),
                            )
                        )
                        continue

                    if name not in round_offered:
                        pack = registry.tool_pack(name) if name in registry else None
                        messages.append(
                            ChatMessage(
                                role="tool",
                                name=name,
                                tool_call_id=call.id,
                                content=json.dumps(
                                    {
                                        "ok": False,
                                        "error": {
                                            "code": "CAPABILITY_NOT_ACTIVE",
                                            "message": f"Capability {pack} is not active; activate it and retry next round.",
                                        },
                                    },
                                    separators=(",", ":"),
                                ),
                            )
                        )
                        continue

                    if name in task_tool_names:
                        validated = TASK_ARGUMENT_TYPES[name].model_validate(raw_arguments)
                        validated_arguments = validated.model_dump(mode="json")
                        requires_confirmation = False
                    else:
                        validated = registry.validate_arguments(name, raw_arguments)
                        validated_arguments = validated.model_dump(mode="json")
                        definition = registry.get(name).definition()
                        requires_confirmation = confirmation_policy.requires_confirmation(
                            definition, validated_arguments
                        )
                    if requires_confirmation:
                        confirmation_boundaries.append(name)
                    executed.append(
                        ExecutedCall(
                            tool=name,
                            arguments=validated_arguments,
                            confirmation_required=requires_confirmation,
                        )
                    )
                    successful_path.append(name)
                    if not _path_is_prefix(successful_path, case.allowed_tool_paths):
                        failure_reason = f"tool path is not allowed: {' -> '.join(successful_path)}"
                        break
                    messages.append(
                        ChatMessage(
                            role="tool",
                            name=name,
                            tool_call_id=call.id,
                            content=_tool_payload(name, case.mock_results.get(name, {})),
                        )
                    )
                if failure_reason is not None:
                    break

                satisfied = _satisfied_outcome(case, executed, clarification=False)
                if satisfied is not None:
                    break
            else:
                failure_reason = "maximum model rounds exceeded before completion"
        except Exception as error:
            error_text = str(error)
            failure_reason = "trajectory evaluation error"

        if (
            failure_reason is None
            and satisfied is not None
            and not _path_is_complete(successful_path, case.allowed_tool_paths)
        ):
            satisfied = None
            failure_reason = "required outcome used an incomplete or unapproved tool path"
        if (
            failure_reason is None
            and case.clarification == "required"
            and not clarification_provided
        ):
            satisfied = None
            failure_reason = "clarification was required"
        if failure_reason is None and case.clarification == "forbidden" and clarification_provided:
            satisfied = None
            failure_reason = "clarification was not valid for this case"
        if (
            failure_reason is None
            and case.confirmation == "required"
            and not confirmation_boundaries
        ):
            satisfied = None
            failure_reason = "required confirmation boundary was not reached"
        if (
            failure_reason is None
            and case.confirmation == "required_if_action"
            and satisfied is not None
            and not clarification_provided
            and not confirmation_boundaries
        ):
            satisfied = None
            failure_reason = "action path did not reach a confirmation boundary"
        if failure_reason is None and case.confirmation == "none" and confirmation_boundaries:
            satisfied = None
            failure_reason = "unexpected confirmation boundary was reached"

        unnecessary = sum(name in case.efficiency.unnecessary_observation_tools for name in trace)
        first_tool = trace[0] if trace else None
        ideal_first, efficiency = _efficiency_score(
            case,
            provider_rounds=max(rounds, 1),
            tool_calls=len(trace),
            first_tool=first_tool,
            unnecessary_observations=unnecessary,
        )
        results.append(
            EndToEndCaseResult(
                case_id=case.case_id,
                passed=satisfied is not None and failure_reason is None,
                satisfied_outcome=satisfied,
                tool_trace=trace,
                successful_tool_path=successful_path,
                executed_calls=executed,
                provider_rounds=rounds,
                tool_calls=len(trace),
                capability_activation_rounds=len(activation_rounds),
                unnecessary_observations=unnecessary,
                ideal_first_tool=ideal_first,
                efficiency_score=efficiency,
                confirmation_boundaries=confirmation_boundaries,
                clarification_provided=clarification_provided,
                latency_ms=round((time.perf_counter() - started) * 1_000),
                response_text=response_text,
                failure_reason=failure_reason,
                error=error_text,
            )
        )

    passed = sum(result.passed for result in results)
    return EndToEndReport(
        provider=settings.llm.provider,
        model=settings.llm.model,
        passed=passed,
        total=len(results),
        success_rate=passed / len(results) if results else 0,
        minimum_success_rate=minimum_success_rate,
        efficiency_score=(
            statistics.mean(result.efficiency_score for result in results) if results else 0
        ),
        average_provider_rounds=(
            statistics.mean(result.provider_rounds for result in results) if results else 0
        ),
        average_tool_calls=(
            statistics.mean(result.tool_calls for result in results) if results else 0
        ),
        average_latency_ms=(
            statistics.mean(result.latency_ms for result in results) if results else 0
        ),
        results=results,
    )


def _print_summary(report: EndToEndReport) -> None:
    for result in report.results:
        marker = "PASS" if result.passed else "FAIL"
        trace = " -> ".join(result.tool_trace) or "text response"
        print(
            f"[{marker}] {result.case_id}: {trace}; rounds={result.provider_rounds}, "
            f"calls={result.tool_calls}, efficiency={result.efficiency_score:.1%}, "
            f"latency={result.latency_ms} ms"
        )
        if result.failure_reason:
            print(f"       reason: {result.failure_reason}")
        if result.error:
            print(f"       error: {result.error}")
    print(
        f"End-to-end acceptance: {report.passed}/{report.total} "
        f"({report.success_rate:.1%}); required {report.minimum_success_rate:.1%}"
    )
    print(
        f"Efficiency: {report.efficiency_score:.1%}; "
        f"average rounds={report.average_provider_rounds:.2f}; "
        f"average calls={report.average_tool_calls:.2f}; "
        f"average latency={report.average_latency_ms:.0f} ms"
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate bounded model trajectories with controlled tool results."
    )
    parser.add_argument("--config", type=Path, default=Path("wyzer.toml"))
    parser.add_argument(
        "--cases", type=Path, default=Path("evals/model_end_to_end_acceptance.json")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", action="append", dest="case_ids", default=[])
    parser.add_argument("--minimum-success-rate", type=float, default=0.80)
    args = parser.parse_args(argv)
    if not 0 <= args.minimum_success_rate <= 1:
        parser.error("--minimum-success-rate must be between 0 and 1")

    cases = load_end_to_end_cases(args.cases)
    if args.case_ids:
        requested = set(args.case_ids)
        cases = [case for case in cases if case.case_id in requested]
        missing = requested - {case.case_id for case in cases}
        if missing:
            parser.error(f"unknown case IDs: {', '.join(sorted(missing))}")
    if not cases:
        parser.error("no end-to-end acceptance cases selected")

    settings = WyzerSettings.load(args.config)
    report = asyncio.run(
        evaluate_end_to_end(
            settings,
            cases,
            minimum_success_rate=args.minimum_success_rate,
        )
    )
    _print_summary(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if report.success_rate < report.minimum_success_rate:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
