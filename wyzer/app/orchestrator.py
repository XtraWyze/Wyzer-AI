"""LLM-first orchestration using native provider tool calls."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from wyzer.app.tool_context import ToolResultContextBuilder
from wyzer.brain import (
    CapabilityContextBuilder,
    ChatProvider,
    OrchestratorFeatures,
    SystemPromptBuilder,
)
from wyzer.coding.manager import CodingAgentManager
from wyzer.conversation import ConversationManager, SessionContextManager
from wyzer.events import EventLedger
from wyzer.memory import MemoryStore, SensitiveMemoryError
from wyzer.models import (
    AssistantResponse,
    ChatMessage,
    ChatRequestSettings,
    EventKind,
    EventRecord,
    NativeFunctionCall,
    NativeToolCall,
    PendingConfirmation,
    StructuredError,
    ToolResult,
    UserRequest,
    VerificationResult,
    VerificationRule,
    VerificationStatus,
)
from wyzer.policy import ConfirmationPolicy
from wyzer.state import WorldStateManager
from wyzer.tasks import TaskPlanStatus, TaskStateError, TaskStateStore, TaskStepStatus
from wyzer.tasks.tools import TASK_ARGUMENT_TYPES, task_native_tools
from wyzer.tools import ModelToolView, ToolRegistry
from wyzer.tools.capabilities import (
    ACTIVATE_CAPABILITY_TOOL,
    LIST_CAPABILITIES_TOOL,
)
from wyzer.tools.registry import (
    UnavailableToolError,
    UnknownCapabilityError,
    UnknownToolError,
)
from wyzer.workers import ToolExecutor

_YES = re.compile(r"^\s*(?:yes|yep|yeah|do it|go ahead|continue|proceed|sure)\s*[.!]?\s*$", re.I)
_NO = re.compile(r"^\s*(?:no|nope|cancel|never mind|don'?t|stop)\s*[.!]?\s*$", re.I)
_STOP = re.compile(r"^\s*(?:stop|cancel|interrupt|never mind)\s*[.!]?\s*$", re.I)
_HELP = re.compile(r"^\s*(?:help|commands)\s*[?.!]?\s*$", re.I)
_TASK_STATUS = re.compile(r"^\s*(?:task\s+)?status\s*[?.!]?\s*$", re.I)
_PAUSE = re.compile(r"^\s*pause(?:\s+task)?\s*[.!]?\s*$", re.I)
_RESUME = re.compile(r"^\s*resume(?:\s+task)?\s*[.!]?\s*$", re.I)
_HELP_TEXT = (
    "You can ask me to open and control applications, manage windows and files, use the "
    "managed browser, inspect the screen, control media and audio, diagnose Windows, or chat. "
    "Use 'remember that ...' for memory, 'what do you remember about me?', 'stop' or 'cancel' "
    "to interrupt, 'task status', 'pause', or 'resume' for longer work, and 'quit' or "
    "'exit' to close the terminal."
)
_DETAILED_RESPONSE_REQUEST = re.compile(
    r"\b(?:in\s+detail|detailed|thorough(?:ly)?|comprehensive|deep\s+dive|"
    r"walk\s+me\s+through|step[- ]by[- ]step|explain\s+(?:more|fully))\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class _Continuation:
    remaining_calls: tuple[NativeToolCall, ...]
    tool_rounds: int
    coordination_rounds: int


class Orchestrator:
    def __init__(
        self,
        registry: ToolRegistry,
        executor: ToolExecutor,
        provider: ChatProvider,
        *,
        maximum_tool_rounds: int = 6,
        tool_result_context_characters: int = 4_000,
        ledger: EventLedger | None = None,
        world: WorldStateManager | None = None,
        conversation: ConversationManager | None = None,
        session_context: SessionContextManager | None = None,
        confirmation_policy: ConfirmationPolicy | None = None,
        memory: MemoryStore | None = None,
        tasks: TaskStateStore | None = None,
        personality: dict[str, object] | None = None,
        detailed_output_tokens: int = 1_024,
        coding_manager: CodingAgentManager | None = None,
    ) -> None:
        if maximum_tool_rounds < 1:
            raise ValueError("maximum tool rounds must be positive")
        self.registry = registry
        self.ledger = ledger or EventLedger()
        self.world = world or WorldStateManager()
        self.conversation = conversation or ConversationManager()
        self.session_context = session_context or SessionContextManager()
        self._executor = executor
        self._provider = provider
        self._maximum_tool_rounds = maximum_tool_rounds
        # Planning and evidence-state updates do not touch the computer, so they should not
        # consume the action-attempt budget. Keep their own bound to contain a model that loops
        # on invalid task transitions.
        self._maximum_coordination_rounds = max(4, maximum_tool_rounds)
        self._tool_context = ToolResultContextBuilder(tool_result_context_characters)
        self._prompts = SystemPromptBuilder(personality=personality)
        self._capability_context = CapabilityContextBuilder(
            registry,
            OrchestratorFeatures(persistent_complex_task_planning=tasks is not None),
        )
        self._detailed_output_tokens = detailed_output_tokens
        self.coding_manager = coding_manager
        self._confirmation_policy = confirmation_policy or ConfirmationPolicy()
        self._memory = memory
        self._tasks = tasks
        self._progress_callback: Callable[[str], None] | None = None
        self._refresh_memories()
        self._control_lock = RLock()
        self._execution_lock = asyncio.Lock()
        self._active_action: UUID | None = None
        self._interrupted: set[UUID] = set()
        self._provider_task: asyncio.Task[Any] | None = None
        self._pending_continuation: _Continuation | None = None
        self._active_capabilities: set[str] = set()

    async def handle(self, text: str) -> AssistantResponse:
        normalized = " ".join(text.strip().split())
        if not normalized:
            return AssistantResponse(text="I didn't catch anything.", action_id=uuid4())
        request = UserRequest(text=normalized)
        pending = self.world.snapshot().pending_confirmation
        if pending is not None:
            if _YES.fullmatch(normalized):
                self.conversation.record_local_user(request)
                return await self._resume_confirmation(pending)
            if _NO.fullmatch(normalized):
                self.conversation.record_local_user(request)
                self._clear_confirmation()
                self._finish_action(pending.action_id, cancelled=True)
                return self._finish_response(
                    AssistantResponse(text="Okay, I cancelled it.", action_id=pending.action_id),
                    local=True,
                )
            self._clear_confirmation()
            self._finish_action(pending.action_id, cancelled=True)
        if _STOP.fullmatch(normalized):
            self.conversation.record_local_user(request)
            stopped = self.interrupt()
            if self._tasks is not None:
                plan = self._tasks.snapshot()
                if plan is not None and plan.status in {
                    TaskPlanStatus.ACTIVE,
                    TaskPlanStatus.PAUSED,
                    TaskPlanStatus.BLOCKED,
                }:
                    self._tasks.cancel()
                    stopped = True
            return self._finish_response(
                AssistantResponse(
                    text="Okay, I stopped it." if stopped else "There is no active task.",
                    action_id=request.request_id,
                    interrupted=stopped,
                ),
                local=True,
            )

        if _TASK_STATUS.fullmatch(normalized):
            self.conversation.record_local_user(request)
            summary = (
                self._tasks.summary() if self._tasks is not None else "The task engine is disabled."
            )
            return self._finish_response(
                AssistantResponse(text=summary, action_id=request.request_id), local=True
            )

        if _PAUSE.fullmatch(normalized):
            self.conversation.record_local_user(request)
            plan = self._tasks.snapshot() if self._tasks is not None else None
            paused = False
            if plan is None or plan.status != TaskPlanStatus.ACTIVE:
                text = "There is no active planned task to pause."
            else:
                assert self._tasks is not None
                self._tasks.pause()
                self.interrupt()
                paused = True
                text = "I paused the task. Say resume when you want me to continue."
            return self._finish_response(
                AssistantResponse(
                    text=text,
                    action_id=request.request_id,
                    interrupted=paused,
                ),
                local=True,
            )

        if _RESUME.fullmatch(normalized):
            plan = self._tasks.snapshot() if self._tasks is not None else None
            if plan is None or plan.status not in {
                TaskPlanStatus.PAUSED,
                TaskPlanStatus.BLOCKED,
            }:
                self.conversation.record_local_user(request)
                return self._finish_response(
                    AssistantResponse(
                        text="There is no paused task to resume.", action_id=request.request_id
                    ),
                    local=True,
                )
            assert self._tasks is not None
            self._tasks.resume(request.request_id)

        if _HELP.fullmatch(normalized):
            self.conversation.record_local_user(request)
            return self._finish_response(
                AssistantResponse(text=_HELP_TEXT, action_id=request.request_id),
                local=True,
            )

        memory_response = self._handle_memory_request(request)
        if memory_response is not None:
            self.conversation.record_local_user(request)
            return self._finish_response(memory_response, local=True)

        self.conversation.record_user(request)
        self._event(
            EventKind.REQUEST_RECEIVED,
            request.request_id,
            details={"text": self._redact_text_entry(normalized)},
        )
        if not self._provider.available:
            return self._finish_response(
                AssistantResponse(
                    text=(
                        "A tool-capable local model is not configured. Set [llm].provider and "
                        "[llm].model, then try again."
                    ),
                    action_id=request.request_id,
                )
            )

        async with self._execution_lock:
            self._start_action(request.request_id, request.text)
            try:
                response = await self._tool_loop(request.request_id)
                return response
            finally:
                if self.world.snapshot().pending_confirmation is None:
                    self._finish_action(request.request_id)

    async def _tool_loop(
        self,
        action_id: UUID,
        tool_rounds: int = 0,
        coordination_rounds: int = 0,
    ) -> AssistantResponse:
        empty_retry = False
        premature_completion_retries = 0
        while True:
            if self._is_interrupted(action_id):
                return self._interrupted_response(action_id)
            messages = self._provider_messages()
            if premature_completion_retries:
                messages.append(
                    ChatMessage(
                        role="system",
                        content=(
                            "Your previous response tried to finish while TASK_PLAN_JSON still "
                            "contained active steps. Continue with tools, revise the plan, or "
                            "mark the current step blocked. Do not claim completion."
                        ),
                    )
                )
            if empty_retry:
                messages.append(
                    ChatMessage(
                        role="system",
                        content="Return a brief answer or a valid native tool call now.",
                    )
                )
            plan_completed = self._completed_plan_for(action_id)
            if plan_completed:
                messages.append(
                    ChatMessage(
                        role="system",
                        content=(
                            "All planned steps are verified. Return one concise final answer now. "
                            "No more tools are available or needed."
                        ),
                    )
                )
            try:
                provider_response = await self._request_provider(
                    messages, include_tools=not plan_completed
                )
            except asyncio.CancelledError:
                if self._is_interrupted(action_id):
                    return self._interrupted_response(action_id)
                raise
            except Exception as error:
                return self._finish_response(
                    AssistantResponse(
                        text=f"The local model could not respond: {error}", action_id=action_id
                    )
                )
            message = provider_response.message
            if plan_completed and message.tool_calls:
                return self._finish_response(
                    AssistantResponse(
                        text=self._completed_plan_response(action_id), action_id=action_id
                    )
                )
            if not message.tool_calls:
                content = (message.content or "").strip()
                if not content and not empty_retry:
                    empty_retry = True
                    continue
                if not content:
                    content = "The local model returned an empty response twice."
                plan = self._tasks.snapshot() if self._tasks is not None else None
                if (
                    plan is not None
                    and plan.action_id == action_id
                    and plan.status == TaskPlanStatus.ACTIVE
                ):
                    if premature_completion_retries < 2:
                        premature_completion_retries += 1
                        continue
                    return self._finish_response(
                        AssistantResponse(
                            text=(
                                "I couldn't verify every planned step, so I stopped without "
                                "claiming the task was complete. Ask for task status for details."
                            ),
                            action_id=action_id,
                        )
                    )
                return self._finish_response(AssistantResponse(text=content, action_id=action_id))

            empty_retry = False
            action_calls = [
                call
                for call in message.tool_calls
                if call.function.name not in TASK_ARGUMENT_TYPES
                and not self.registry.is_capability_coordination_tool(call.function.name)
            ]
            if action_calls and tool_rounds >= self._maximum_tool_rounds:
                return self._finish_response(
                    AssistantResponse(
                        text=(
                            f"I stopped after {self._maximum_tool_rounds} tool rounds to avoid "
                            "looping. Please narrow the request or try again."
                        ),
                        action_id=action_id,
                    )
                )
            if not action_calls and coordination_rounds >= self._maximum_coordination_rounds:
                return self._finish_response(
                    AssistantResponse(
                        text=(
                            "I stopped after repeated task-coordination calls to avoid looping. "
                            "Ask for task status for details."
                        ),
                        action_id=action_id,
                    )
                )
            if action_calls:
                tool_rounds += 1
            else:
                coordination_rounds += 1
            self.conversation.record_assistant_tool_calls(message)
            confirmation = await self._execute_calls(
                action_id,
                message.tool_calls,
                tool_rounds=tool_rounds,
                coordination_rounds=coordination_rounds,
            )
            if confirmation is not None:
                return confirmation

    async def _execute_calls(
        self,
        action_id: UUID,
        calls: list[NativeToolCall] | tuple[NativeToolCall, ...],
        *,
        tool_rounds: int,
        coordination_rounds: int = 0,
        confirmed_step: UUID | None = None,
    ) -> AssistantResponse | None:
        completed = self._completed_plan_for(action_id)
        if completed:
            self._reject_post_completion_calls(calls, action_id)
            return None
        # Some local models emit the capability call before task_plan_create in
        # one native-call batch. Establish the plan first so no action evidence
        # is lost, while preserving the model's order for every other call.
        ordered_calls = list(calls)
        creates = [call for call in ordered_calls if call.function.name == "task_plan_create"]
        if creates:
            ordered_calls = [
                *creates,
                *[call for call in ordered_calls if call.function.name != "task_plan_create"],
            ]
        visible_call_names = set(self._model_tool_view().tool_names)
        for index, call in enumerate(ordered_calls):
            if self._is_interrupted(action_id):
                return self._interrupted_response(action_id)
            if self._completed_plan_for(action_id):
                self._reject_post_completion_calls(ordered_calls[index:], action_id)
                return None
            step_id = confirmed_step if index == 0 and confirmed_step is not None else uuid4()
            if call.function.name in TASK_ARGUMENT_TYPES:
                result = self._execute_task_call(call, action_id, step_id)
                self._record_result(result, call.id, arguments=call.function.arguments)
                if not result.ok:
                    self._skip_sequence_tail(
                        ordered_calls[index + 1 :], action_id, failed=result
                    )
                    break
                continue
            if self.registry.is_capability_coordination_tool(call.function.name):
                result = self._execute_capability_call(call, action_id, step_id)
                self._record_result(
                    result,
                    call.id,
                    arguments=call.function.arguments,
                    task_evidence=False,
                )
                if not result.ok:
                    self._skip_sequence_tail(
                        ordered_calls[index + 1 :], action_id, failed=result
                    )
                    break
                continue
            if self._current_step_awaits_update(action_id):
                result = self._task_failure(
                    call.function.name,
                    action_id,
                    step_id,
                    "TASK_STEP_UPDATE_REQUIRED",
                    (
                        "The current step already has verified action evidence. Call "
                        "task_step_update before starting another capability action so later "
                        "evidence is attached to the correct step."
                    ),
                )
                self._record_result(
                    result,
                    call.id,
                    arguments=call.function.arguments,
                    task_evidence=False,
                )
                self._skip_sequence_tail(ordered_calls[index + 1 :], action_id, failed=result)
                break
            if self._current_step_needs_verification(action_id):
                try:
                    candidate = self.registry.get(call.function.name, require_available=False)
                except UnknownToolError:
                    candidate = None
                if candidate is not None and not candidate.read_only:
                    result = self._task_failure(
                        call.function.name,
                        action_id,
                        step_id,
                        "TASK_STEP_VERIFICATION_REQUIRED",
                        (
                            "The current step has an unverified action. Use a relevant read-only "
                            "observation, then call task_step_update before starting another "
                            "mutating capability action."
                        ),
                    )
                    self._record_result(
                        result,
                        call.id,
                        arguments=call.function.arguments,
                        task_evidence=False,
                    )
                    self._skip_sequence_tail(
                        ordered_calls[index + 1 :], action_id, failed=result
                    )
                    break
            validated, definition, invalid_result = self._validate_call(
                call,
                action_id,
                step_id,
                visible_call_names=visible_call_names,
            )
            if invalid_result is not None:
                self._record_result(
                    invalid_result, call.id, arguments=call.function.arguments
                )
                self._skip_sequence_tail(
                    ordered_calls[index + 1 :], action_id, failed=invalid_result
                )
                break
            assert validated is not None and definition is not None
            is_confirmed = index == 0 and confirmed_step is not None
            inspection = self._confirmation_inspection(call.function.name, validated)
            if not is_confirmed and self._confirmation_policy.requires_confirmation(
                definition, inspection
            ):
                pending = self._confirmation_policy.issue(
                    action_id,
                    step_id,
                    call.function.name,
                    validated,
                    call.id,
                    inspection,
                )
                self._pending_continuation = _Continuation(
                    tuple(ordered_calls[index + 1 :]), tool_rounds, coordination_rounds
                )
                self.world.set_confirmation(pending)
                self.conversation.set_pending_confirmation(pending)
                self._event(EventKind.CONFIRMATION_REQUESTED, action_id, step_id=step_id)
                return self._finish_response(
                    AssistantResponse(text=pending.prompt, action_id=action_id), local=True
                )
            result = await self._execute_tool(call, validated, action_id, step_id)
            self._record_result(result, call.id, arguments=validated)
            if not result.ok:
                self._skip_sequence_tail(ordered_calls[index + 1 :], action_id, failed=result)
                break
        return None

    def _skip_sequence_tail(
        self,
        calls: list[NativeToolCall],
        action_id: UUID,
        *,
        failed: ToolResult,
    ) -> None:
        """Return one structured non-execution result for each call after a failure."""
        failed_code = failed.error.code if failed.error is not None else "TOOL_FAILED"
        for call in calls:
            result = self._task_failure(
                call.function.name,
                action_id,
                uuid4(),
                "SEQUENCE_STOPPED_AFTER_FAILURE",
                (
                    f"Not executed because the earlier {failed.tool} call failed. "
                    "Reassess the request before retrying later actions."
                ),
                details={"failed_tool": failed.tool, "failed_code": failed_code},
            )
            self._record_result(
                result,
                call.id,
                arguments=call.function.arguments,
                task_evidence=False,
            )

    def _completed_plan_for(self, action_id: UUID) -> bool:
        plan = self._tasks.snapshot() if self._tasks is not None else None
        return bool(
            plan is not None
            and plan.action_id == action_id
            and plan.status == TaskPlanStatus.COMPLETED
        )

    def _current_step_awaits_update(self, action_id: UUID) -> bool:
        plan = self._tasks.snapshot() if self._tasks is not None else None
        if (
            plan is None
            or plan.action_id != action_id
            or plan.status != TaskPlanStatus.ACTIVE
            or plan.current_step is None
        ):
            return False
        for evidence in plan.current_step.evidence:
            if not evidence.ok or evidence.verification_status != "verified":
                continue
            try:
                if not self.registry.get(evidence.tool, require_available=False).read_only:
                    return True
            except UnknownToolError:
                continue
        return False

    def _current_step_needs_verification(self, action_id: UUID) -> bool:
        plan = self._tasks.snapshot() if self._tasks is not None else None
        return bool(
            plan is not None
            and plan.action_id == action_id
            and plan.status == TaskPlanStatus.ACTIVE
            and plan.current_step is not None
            and plan.current_step.status == TaskStepStatus.NEEDS_VERIFICATION
        )

    def _completed_plan_response(self, action_id: UUID) -> str:
        plan = self._tasks.snapshot() if self._tasks is not None else None
        if plan is not None and plan.action_id == action_id:
            goal = plan.goal.strip().rstrip(".!")
            if goal:
                return f"Done — {goal}."
        return "Done."

    def _reject_post_completion_calls(
        self,
        calls: list[NativeToolCall] | tuple[NativeToolCall, ...],
        action_id: UUID,
    ) -> None:
        for call in calls:
            self._record_result(
                self._task_failure(
                    call.function.name,
                    action_id,
                    uuid4(),
                    "TASK_ALREADY_COMPLETED",
                    (
                        "All planned steps are verified. Do not call more tools; return the "
                        "user's concise final result now."
                    ),
                ),
                call.id,
                arguments=call.function.arguments,
            )

    def _confirmation_inspection(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in {"browser_click", "browser_type_text"}:
            return arguments
        ref = arguments.get("ref")
        if not isinstance(ref, str):
            return arguments
        for result in reversed(self.conversation.snapshot().recent_tool_results):
            if result.tool != "browser_inspect_page":
                continue
            elements = (result.data or {}).get("elements")
            if not isinstance(elements, list):
                continue
            for element in elements:
                if not isinstance(element, dict) or element.get("ref") != ref:
                    continue
                label = element.get("name") or element.get("role") or ref
                return {
                    **arguments,
                    "control_label": str(label),
                    "action": "set_value" if tool_name == "browser_type_text" else "invoke",
                }
        return arguments

    def _validate_call(
        self,
        call: NativeToolCall,
        action_id: UUID,
        step_id: UUID,
        *,
        visible_call_names: set[str] | None = None,
    ) -> tuple[dict[str, Any] | None, Any | None, ToolResult | None]:
        try:
            tool = self.registry.get(call.function.name)
            if visible_call_names is not None and call.function.name not in visible_call_names:
                pack = self.registry.tool_pack(call.function.name)
                raise UnknownCapabilityError(pack or "unpacked")
            arguments = tool.arguments_type.model_validate(call.function.arguments)
            return arguments.model_dump(mode="json"), tool.definition(), None
        except UnknownToolError:
            error = StructuredError(
                code="UNKNOWN_TOOL",
                message=f"No registered tool named {call.function.name}.",
            )
        except UnavailableToolError as exception:
            error = StructuredError(code="TOOL_UNAVAILABLE", message=str(exception))
        except UnknownCapabilityError as exception:
            error = StructuredError(
                code="CAPABILITY_NOT_ACTIVE",
                message=(
                    f"Capability {exception.args[0]} is not active. List and activate the exact "
                    "capability, then retry this tool on the next native tool-call round."
                ),
                retryable=True,
            )
        except ValidationError as exception:
            error = StructuredError(
                code="INVALID_TOOL_ARGUMENTS",
                message="Tool arguments did not match the registered schema.",
                retryable=True,
                details={"errors": exception.errors(include_url=False)},
            )
        return None, None, self._local_failure(call.function.name, action_id, step_id, error)

    def _execute_capability_call(
        self, call: NativeToolCall, action_id: UUID, step_id: UUID
    ) -> ToolResult:
        now = datetime.now(UTC)
        try:
            arguments = self.registry.validate_arguments(
                call.function.name, call.function.arguments
            )
            if call.function.name == LIST_CAPABILITIES_TOOL:
                data: dict[str, Any] = {
                    "capabilities": self.registry.capability_manifest(self._active_capabilities)
                }
            else:
                name = (
                    str(arguments.model_dump()["name"])
                    if call.function.name == ACTIVATE_CAPABILITY_TOOL
                    else self.registry.activation_capability(call.function.name)
                )
                if name is None:
                    raise UnknownToolError(call.function.name)
                if name not in self.registry.available_capabilities():
                    raise UnknownCapabilityError(name)
                if name in self.registry.default_capabilities:
                    activated = False
                else:
                    activated = name not in self._active_capabilities
                    self._active_capabilities.add(name)
                    plan = self._tasks.snapshot() if self._tasks is not None else None
                    if (
                        self._tasks is not None
                        and plan is not None
                        and plan.action_id == action_id
                        and plan.status == TaskPlanStatus.ACTIVE
                    ):
                        self._tasks.activate_capability(name)
                data = {
                    "name": name,
                    "activated": activated,
                    "visible_tool_count": len(
                        self.registry.model_view(self._active_capabilities).native_tools()
                    ),
                    "instruction": (
                        "Activation is complete but performed no action. Continue the original request "
                        "now with the matching newly available action or observation tool. Activation "
                        "does not make a small request complex; do not create a plan merely because "
                        "activation was needed."
                    ),
                }
        except UnknownCapabilityError as error:
            return self._local_failure(
                call.function.name,
                action_id,
                step_id,
                StructuredError(
                    code="UNKNOWN_CAPABILITY",
                    message=f"No activatable capability named {error.args[0]}.",
                    retryable=True,
                ),
            )
        except (UnknownToolError, UnavailableToolError, ValidationError) as error:
            code = (
                "INVALID_TOOL_ARGUMENTS" if isinstance(error, ValidationError) else "UNKNOWN_TOOL"
            )
            return self._local_failure(
                call.function.name,
                action_id,
                step_id,
                StructuredError(
                    code=code,
                    message="Capability coordination arguments were invalid.",
                    retryable=True,
                ),
            )
        return ToolResult(
            ok=True,
            tool=call.function.name,
            action_id=action_id,
            step_id=step_id,
            started_at=now,
            finished_at=datetime.now(UTC),
            duration_ms=0,
            data=data,
            evidence={},
        )

    def _execute_task_call(
        self, call: NativeToolCall, action_id: UUID, step_id: UUID
    ) -> ToolResult:
        now = datetime.now(UTC)
        if self._tasks is None:
            return self._task_failure(
                call.function.name,
                action_id,
                step_id,
                "TASK_ENGINE_DISABLED",
                "The task engine is disabled.",
            )
        try:
            arguments_type = TASK_ARGUMENT_TYPES[call.function.name]
            arguments = arguments_type.model_validate(call.function.arguments)
            raw = arguments.model_dump(mode="json")
            if call.function.name == "task_plan_create":
                plan = self._tasks.create(
                    action_id,
                    raw["goal"],
                    raw["steps"],
                    active_capabilities=self._active_capabilities,
                )
                self._progress(self._task_progress_label(plan))
                self._event(
                    EventKind.PLAN_CREATED,
                    action_id,
                    step_id=step_id,
                    success=True,
                    details={"step_count": len(plan.steps)},
                )
            elif call.function.name == "task_step_update":
                plan = self._tasks.update_step(
                    int(raw["step_number"]),
                    TaskStepStatus(str(raw["status"])),
                    str(raw.get("note") or ""),
                )
                self._progress(self._task_progress_label(plan))
                current = next(
                    (step for step in plan.steps if step.number == int(raw["step_number"])),
                    None,
                )
                if current is not None and current.status == TaskStepStatus.VERIFIED:
                    self._event(
                        EventKind.VERIFICATION_PASSED,
                        action_id,
                        step_id=step_id,
                        success=True,
                        details={"task_step": current.number},
                    )
            else:
                plan = self._tasks.revise(
                    str(raw["reason"]),
                    raw["remaining_steps"],
                )
                self._progress(self._task_progress_label(plan))
                self._event(
                    EventKind.PLAN_CREATED,
                    action_id,
                    step_id=step_id,
                    success=True,
                    details={"revision": plan.revision, "step_count": len(plan.steps)},
                )
        except ValidationError as error:
            return self._task_failure(
                call.function.name,
                action_id,
                step_id,
                "INVALID_TASK_ARGUMENTS",
                "Task arguments did not match the planning schema.",
                details={"errors": error.errors(include_url=False)},
            )
        except (KeyError, TaskStateError, ValueError) as error:
            return self._task_failure(
                call.function.name,
                action_id,
                step_id,
                "INVALID_TASK_TRANSITION",
                str(error),
            )
        return ToolResult(
            ok=True,
            tool=call.function.name,
            action_id=action_id,
            step_id=step_id,
            started_at=now,
            finished_at=datetime.now(UTC),
            duration_ms=0,
            data={
                "task_status": plan.status.value,
                "revision": plan.revision,
                "verified_steps": sum(
                    step.status == TaskStepStatus.VERIFIED for step in plan.steps
                ),
                "total_steps": len(plan.steps),
                "current_step": (
                    plan.current_step.model_dump(mode="json")
                    if plan.current_step is not None
                    else None
                ),
                "instruction": (
                    "All steps are verified. Return the user's concise final result now; do not "
                    "call another task-state function."
                    if plan.status == TaskPlanStatus.COMPLETED
                    else "Continue the current step using capability evidence."
                ),
            },
            evidence={"verification_status": "verified", "predicate": "task_state_updated"},
        )

    async def _execute_tool(
        self,
        call: NativeToolCall,
        arguments: dict[str, Any],
        action_id: UUID,
        step_id: UUID,
    ) -> ToolResult:
        plan = self._tasks.snapshot() if self._tasks is not None else None
        if plan is not None and plan.status == TaskPlanStatus.ACTIVE:
            self._progress(self._task_progress_label(plan))
        self._event(
            EventKind.TOOL_STARTED,
            action_id,
            step_id=step_id,
            tool_name=call.function.name,
        )
        if (
            self.coding_manager is not None
            and call.function.name in self.coding_manager.PROXY_TOOLS
        ):
            result = await self.coding_manager.execute_proxy(
                call.function.name, arguments, action_id, step_id
            )
        else:
            result = await self._executor.execute(
                call.function.name, arguments, action_id, step_id
            )
        self._event(
            EventKind.TOOL_COMPLETED if result.ok else EventKind.TOOL_FAILED,
            action_id,
            step_id=step_id,
            tool_name=result.tool,
            success=result.ok,
            error=result.error,
        )
        return result

    def set_progress_callback(self, callback: Callable[[str], None] | None) -> None:
        """Attach a presentation callback without coupling orchestration to a UI."""
        self._progress_callback = callback

    def _progress(self, text: str) -> None:
        callback = self._progress_callback
        if callback is not None:
            callback(text)

    @staticmethod
    def _task_progress_label(plan: Any) -> str:
        current = plan.current_step
        if current is None:
            return f"Task {plan.status.value}"
        return f"Step {current.number}/{len(plan.steps)}: {current.description}"

    def _record_result(
        self,
        result: ToolResult,
        tool_call_id: str | None,
        *,
        arguments: dict[str, Any] | None = None,
        task_evidence: bool = True,
    ) -> None:
        context = self._tool_context.build(result)
        before = self.world.snapshot()
        self.world.record_tool_result(result)
        self.world.apply_tool_observation(result)
        self.session_context.record_tool_result(
            result,
            arguments,
            before=before,
            after=self.world.snapshot(),
        )
        self.conversation.record_tool_result(result, context, tool_call_id)
        verification = self._verification_from_evidence(result)
        if verification is not None:
            self.world.record_verification(verification)
        if (
            task_evidence
            and self._tasks is not None
            and result.tool not in TASK_ARGUMENT_TYPES
            and not self.registry.is_capability_coordination_tool(result.tool)
        ):
            try:
                read_only = self.registry.get(result.tool, require_available=False).read_only
            except UnknownToolError:
                read_only = False
            self._tasks.record_tool_result(result, read_only=read_only)

    async def _resume_confirmation(self, pending: PendingConfirmation) -> AssistantResponse:
        valid, reason = self._confirmation_policy.validate(
            pending, pending.tool_name, pending.arguments
        )
        if not valid:
            self._clear_confirmation()
            self._finish_action(pending.action_id, cancelled=True)
            return self._finish_response(
                AssistantResponse(
                    text=(
                        "That confirmation expired. Please ask me to do it again."
                        if reason == "expired"
                        else f"I couldn't confirm it because the {reason}."
                    ),
                    action_id=pending.action_id,
                ),
                local=True,
            )
        continuation = self._pending_continuation or _Continuation((), 1, 0)
        self._clear_confirmation()
        confirmed_call = NativeToolCall(
            id=pending.provider_call_id,
            function=NativeFunctionCall(name=pending.tool_name, arguments=pending.arguments),
        )
        async with self._execution_lock:
            self._start_action(pending.action_id, pending.prompt)
            try:
                response = await self._execute_calls(
                    pending.action_id,
                    (confirmed_call, *continuation.remaining_calls),
                    tool_rounds=continuation.tool_rounds,
                    coordination_rounds=continuation.coordination_rounds,
                    confirmed_step=pending.step_id,
                )
                if response is not None:
                    return response
                return await self._tool_loop(
                    pending.action_id,
                    tool_rounds=continuation.tool_rounds,
                    coordination_rounds=continuation.coordination_rounds,
                )
            finally:
                if self.world.snapshot().pending_confirmation is None:
                    self._finish_action(pending.action_id)

    def _provider_messages(self) -> list[ChatMessage]:
        conversation = self.conversation.snapshot()
        system = self._prompts.build(
            self.world.snapshot(),
            conversation,
            session_context=self.session_context.model_context(),
            capability_context=self._capability_context.build(
                tuple(sorted(self._active_capabilities))
            ),
        )
        messages = [ChatMessage(role="system", content=system)]
        task_context = self._tasks.context() if self._tasks is not None else None
        if task_context is not None:
            messages.append(
                ChatMessage(
                    role="system",
                    content="TASK_PLAN_JSON=" + json.dumps(task_context, separators=(",", ":")),
                )
            )
        if self.coding_manager is not None:
            coding_context = self.coding_manager.model_context()
            if coding_context:
                messages.append(
                    ChatMessage(
                        role="system",
                        content=(
                            "CODING_AGENT_CONTEXT_JSON="
                            + json.dumps(coding_context, separators=(",", ":"), default=str)
                        ),
                    )
                )
        return [*messages, *conversation.model_messages]

    async def _request_provider(
        self, messages: list[ChatMessage], *, include_tools: bool = True
    ) -> Any:
        native_tools = self._model_tool_view().native_tools() if include_tools else []
        if include_tools and self._tasks is not None:
            task_context = self._tasks.context()
            task_tools = task_native_tools(active_plan=task_context is not None)
            native_tools.extend(task_tools)
        settings = None
        if self._requests_detailed_response():
            settings = ChatRequestSettings(max_output_tokens=self._detailed_output_tokens)
        task = asyncio.create_task(self._provider.chat(messages, native_tools, settings))
        with self._control_lock:
            self._provider_task = task
        try:
            return await task
        finally:
            with self._control_lock:
                if self._provider_task is task:
                    self._provider_task = None

    def _requests_detailed_response(self) -> bool:
        conversation = self.conversation.snapshot()
        return bool(
            conversation.recent_user_messages
            and _DETAILED_RESPONSE_REQUEST.search(conversation.recent_user_messages[-1])
        )

    def interrupt(self) -> bool:
        with self._control_lock:
            action_id = self._active_action
            pending = self.world.snapshot().pending_confirmation
            if action_id is None and pending is None:
                return False
            if action_id is not None:
                target = action_id
            else:
                assert pending is not None
                target = pending.action_id
            self._interrupted.add(target)
            task = self._provider_task
        if task is not None:
            task.cancel()
        if self.coding_manager is not None:
            self.coding_manager.cancel_action(target)
        self._executor.cancel(target)
        self._clear_confirmation()
        self._event(EventKind.TASK_INTERRUPTED, target, success=True)
        return True

    def _start_action(self, action_id: UUID, goal: str) -> None:
        plan = self._tasks.snapshot() if self._tasks is not None else None
        with self._control_lock:
            if self._active_action != action_id:
                available = set(self.registry.available_capabilities())
                self._active_capabilities = set(
                    capability
                    for capability in (
                        plan.active_capabilities
                        if plan is not None and plan.action_id == action_id
                        else ()
                    )
                    if capability in available
                )
            self._active_action = action_id
            self._interrupted.discard(action_id)
        self.world.set_task(goal)
        self.conversation.set_active_task(goal)

    def _model_tool_view(self) -> ModelToolView:
        return self.registry.model_view(self._active_capabilities)

    def _finish_action(self, action_id: UUID, *, cancelled: bool = False) -> None:
        with self._control_lock:
            if self._active_action == action_id:
                self._active_action = None
                self._active_capabilities.clear()
            interrupted = action_id in self._interrupted
            self._interrupted.discard(action_id)
        task = self.world.snapshot().active_task
        plan = self._tasks.snapshot() if self._tasks is not None else None
        plan_is_current = plan is not None and plan.action_id == action_id
        keep_task = bool(
            plan_is_current
            and plan is not None
            and plan.status
            in {
                TaskPlanStatus.ACTIVE,
                TaskPlanStatus.PAUSED,
                TaskPlanStatus.BLOCKED,
            }
        )
        self.world.set_task(plan.goal if keep_task and plan is not None else None)
        if task:
            if keep_task:
                self.conversation.set_active_task(plan.goal if plan is not None else task)
            elif (
                cancelled
                or interrupted
                or (
                    plan_is_current and plan is not None and plan.status == TaskPlanStatus.CANCELLED
                )
            ):
                self.conversation.cancel_task(task)
            else:
                self.conversation.complete_task(task)

    def _clear_confirmation(self) -> None:
        self.world.set_confirmation(None)
        self.conversation.set_pending_confirmation(None)
        self._pending_continuation = None

    def _interrupted_response(self, action_id: UUID) -> AssistantResponse:
        self._event(EventKind.TASK_CANCELLED, action_id, success=True)
        return self._finish_response(
            AssistantResponse(text="Okay, I stopped it.", action_id=action_id, interrupted=True),
            local=True,
        )

    def _is_interrupted(self, action_id: UUID) -> bool:
        with self._control_lock:
            return action_id in self._interrupted

    def _finish_response(
        self, response: AssistantResponse, *, local: bool = False
    ) -> AssistantResponse:
        if local:
            self.conversation.record_local_response(response)
        else:
            self.conversation.record_response(response)
        self._event(EventKind.RESPONSE_GENERATED, response.action_id, success=True)
        return response

    def _handle_memory_request(self, request: UserRequest) -> AssistantResponse | None:
        text = request.text.strip()
        remember = re.fullmatch(r"(?:please\s+)?remember(?:\s+that)?\s+(.+?)\s*[.!]?", text, re.I)
        listing = re.fullmatch(
            r"(?:what|which)\s+(?:things\s+)?do you remember(?:\s+about me)?\s*[?.!]*",
            text,
            re.I,
        )
        clear = re.fullmatch(
            r"forget\s+(?:everything|all)(?:\s+you remember)?(?:\s+about me)?\s*[.!]?",
            text,
            re.I,
        )
        forget = re.fullmatch(r"forget(?:\s+that)?\s+(.+?)\s*[.!]?", text, re.I)
        if not any((remember, listing, clear, forget)):
            return None
        if self._memory is None:
            return AssistantResponse(
                text="Long-term memory is disabled.", action_id=request.request_id
            )
        if remember:
            try:
                record = self._memory.remember(remember.group(1).strip())
            except SensitiveMemoryError:
                return AssistantResponse(
                    text="I won't store that because it may contain sensitive information.",
                    action_id=request.request_id,
                )
            self._refresh_memories()
            return AssistantResponse(
                text=f"I'll remember that {record.content['fact']}.",
                action_id=request.request_id,
            )
        if listing:
            facts = self._memory_facts()
            text_out = (
                "I remember:\n" + "\n".join(f"- {fact}" for fact in facts)
                if facts
                else "I don't have any saved memories about you yet."
            )
            return AssistantResponse(text=text_out, action_id=request.request_id)
        if clear:
            count = self._memory.clear()
            self._refresh_memories()
            return AssistantResponse(
                text=f"I removed {count} saved memories." if count else "I had no saved memories.",
                action_id=request.request_id,
            )
        assert forget is not None
        count = self._memory.forget(forget.group(1).strip())
        self._refresh_memories()
        return AssistantResponse(
            text=f"I forgot {count} matching memories." if count else "I found no matching memory.",
            action_id=request.request_id,
        )

    def _memory_facts(self) -> list[str]:
        if self._memory is None:
            return []
        return [
            fact
            for record in self._memory.list()
            if isinstance((fact := record.content.get("fact")), str)
        ]

    def _refresh_memories(self) -> None:
        self.conversation.set_remembered_facts(self._memory_facts())

    @staticmethod
    def _local_failure(
        tool_name: str, action_id: UUID, step_id: UUID, error: StructuredError
    ) -> ToolResult:
        now = datetime.now(UTC)
        return ToolResult(
            ok=False,
            tool=tool_name,
            action_id=action_id,
            step_id=step_id,
            started_at=now,
            finished_at=now,
            duration_ms=0,
            error=error,
        )

    @staticmethod
    def _task_failure(
        tool_name: str,
        action_id: UUID,
        step_id: UUID,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> ToolResult:
        return Orchestrator._local_failure(
            tool_name,
            action_id,
            step_id,
            StructuredError(
                code=code,
                message=message,
                retryable=True,
                details=details or {},
            ),
        )

    @staticmethod
    def _verification_from_evidence(result: ToolResult) -> VerificationResult | None:
        raw = result.evidence.get("verification_status")
        if raw not in {status.value for status in VerificationStatus}:
            return None
        predicate = str(result.evidence.get("predicate") or "tool_result")
        observed = result.evidence.get("observed", {})
        return VerificationResult(
            rule=VerificationRule(predicate=predicate),
            status=VerificationStatus(raw),
            observed=dict(observed) if isinstance(observed, dict) else {},
            evidence=result.evidence,
        )

    @staticmethod
    def _redact_text_entry(text: str) -> str:
        return re.sub(
            r"(?i)(password|passcode|pin|token|secret)\s*(?:is|=|:)\s*\S+",
            r"\1=[REDACTED]",
            text,
        )

    def _event(
        self,
        kind: EventKind,
        action_id: UUID,
        *,
        step_id: UUID | None = None,
        tool_name: str | None = None,
        success: bool | None = None,
        details: dict[str, Any] | None = None,
        error: StructuredError | None = None,
    ) -> None:
        self.ledger.append(
            EventRecord(
                kind=kind,
                action_id=action_id,
                step_id=step_id,
                tool_name=tool_name,
                success=success,
                details=details or {},
                error=error,
            )
        )
