"""Read-only acceptance checks for a configured tool-calling model."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from wyzer.brain import SystemPromptBuilder, create_chat_provider
from wyzer.config import WyzerSettings
from wyzer.models import ChatMessage, ConversationState, WorldStateSnapshot
from wyzer.tasks.tools import task_native_tools
from wyzer.tools import create_default_registry


class AcceptanceCase(BaseModel):
    """One expected first decision from the configured chat model."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    prompt: str = Field(min_length=1, max_length=2_000)
    expected_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    passed: bool
    expected_tools: list[str]
    actual_tools: list[str]
    forbidden_tools_seen: list[str]
    latency_ms: int
    response_text: str
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
    tools = [*registry.native_tools(), *task_native_tools()]
    available_tools = {tool.function.name for tool in tools}
    known_tools = {*registry, *(tool.function.name for tool in task_native_tools())}
    for case in cases:
        unknown = (set(case.expected_tools) | set(case.forbidden_tools)) - known_tools
        if unknown:
            raise ValueError(
                f"case {case.case_id} references unknown tools: {', '.join(sorted(unknown))}"
            )
        unavailable = set(case.expected_tools) - available_tools
        if unavailable:
            raise ValueError(
                f"case {case.case_id} expects unavailable tools: {', '.join(sorted(unavailable))}"
            )

    personality = settings.personality.model_dump(mode="json")
    system_prompt = SystemPromptBuilder(personality=personality).build(
        WorldStateSnapshot(), ConversationState()
    )
    results: list[CaseResult] = []
    for case in cases:
        started = time.perf_counter()
        try:
            response = await provider.chat(
                [
                    ChatMessage(role="system", content=system_prompt),
                    ChatMessage(role="user", content=case.prompt),
                ],
                tools,
            )
            actual = [call.function.name for call in response.message.tool_calls]
            forbidden_seen = [name for name in actual if name in case.forbidden_tools]
            passed = actual == case.expected_tools and not forbidden_seen
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    passed=passed,
                    expected_tools=case.expected_tools,
                    actual_tools=actual,
                    forbidden_tools_seen=forbidden_seen,
                    latency_ms=round((time.perf_counter() - started) * 1_000),
                    response_text=(response.message.content or "").strip(),
                )
            )
        except Exception as error:
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    passed=False,
                    expected_tools=case.expected_tools,
                    actual_tools=[],
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
        if result.error:
            print(f"       error: {result.error}")
    print(
        f"Model acceptance: {report.passed}/{report.total} "
        f"({report.pass_rate:.1%}); required {report.minimum_pass_rate:.1%}"
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate the configured model's first tool decision without executing any tools."
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
