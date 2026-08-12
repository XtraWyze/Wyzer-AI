"""Bounded runtime self-capability context for the conversational model."""

from __future__ import annotations

from dataclasses import dataclass

from wyzer.tools.registry import SemanticCapability, ToolRegistry


@dataclass(frozen=True, slots=True)
class OrchestratorFeatures:
    """Architectural facts supplied by the running orchestrator."""

    persistent_complex_task_planning: bool
    direct_multi_tool_execution: bool = True
    authors_intermediate_steps: bool = True
    selects_tools_and_arguments: bool = True
    uses_previous_tool_results: bool = True
    user_goal_required: bool = True
    autonomous_goal_creation: bool = False
    continuous_passive_observation: bool = False
    can_bypass_safety_policies: bool = False


class CapabilityContextBuilder:
    """Render semantic registry metadata without exposing native tool schemas."""

    def __init__(
        self,
        registry: ToolRegistry,
        features: OrchestratorFeatures,
        *,
        maximum_characters: int = 3_200,
        maximum_capabilities: int = 24,
        maximum_description_characters: int = 240,
    ) -> None:
        if maximum_characters < 1_000:
            raise ValueError("capability context limit must be at least 1000 characters")
        if maximum_capabilities < 1:
            raise ValueError("maximum capabilities must be positive")
        self._registry = registry
        self._features = features
        self._maximum_characters = maximum_characters
        self._maximum_capabilities = maximum_capabilities
        self._maximum_description_characters = maximum_description_characters
        self._cache_key: tuple[object, ...] | None = None
        self._cache_value = ""

    def build(self, activated_capabilities: tuple[str, ...] = ()) -> str:
        semantic = self._registry.semantic_capabilities()
        active_names = set(self._registry.default_capabilities) | set(activated_capabilities)
        key: tuple[object, ...] = (
            semantic,
            tuple(sorted(active_names)),
            self._features,
            self._maximum_characters,
            self._maximum_capabilities,
        )
        if key == self._cache_key:
            return self._cache_value

        active = [item for item in semantic if item.name in active_names]
        activatable = [item for item in semantic if item.name not in active_names]
        ordered = [*active, *activatable]
        shown = ordered[: self._maximum_capabilities]
        shown_active = [item for item in shown if item.name in active_names]
        shown_activatable = [item for item in shown if item.name not in active_names]

        omitted = len(ordered) - len(shown)
        rendered = self._render(shown_active, shown_activatable, omitted)
        if len(rendered) > self._maximum_characters:
            rendered = self._fit(rendered, shown_active, shown_activatable)
        self._cache_key = key
        self._cache_value = rendered
        return rendered

    def _capability_lines(self, capabilities: list[SemanticCapability]) -> list[str]:
        return [
            f"{item.name}: {self._compact(item.description)}"
            for item in capabilities
        ] or ["none"]

    def _compact(self, description: str) -> str:
        text = " ".join(description.split())
        if len(text) <= self._maximum_description_characters:
            return text
        shortened = text[: self._maximum_description_characters - 1].rsplit(" ", 1)[0]
        return shortened + "…"

    @staticmethod
    def _yes_no(value: bool) -> str:
        return "yes" if value else "no"

    def _base_lines(self) -> list[str]:
        return [
            "SELF_CAPABILITIES",
            (
                "direct_multi_tool_execution="
                + self._yes_no(self._features.direct_multi_tool_execution)
            ),
            (
                "multi_action_scope=small immediate sequences use multiple direct tools; complex "
                "dependent, resumable, or cross-verified work may use persistent planning"
            ),
            "authors_intermediate_steps=" + self._yes_no(self._features.authors_intermediate_steps),
            (
                "selects_tools_and_arguments="
                + self._yes_no(self._features.selects_tools_and_arguments)
            ),
            (
                "uses_previous_tool_results="
                + self._yes_no(self._features.uses_previous_tool_results)
                + "; successful tool results remain context/evidence for subsequent decisions"
            ),
            (
                "persistent_complex_task_planning="
                + self._yes_no(self._features.persistent_complex_task_planning)
            ),
            "user_goal_required=" + self._yes_no(self._features.user_goal_required),
            "autonomous_goal_creation=" + self._yes_no(self._features.autonomous_goal_creation),
            (
                "continuous_passive_observation="
                + self._yes_no(self._features.continuous_passive_observation)
            ),
            (
                "can_bypass_safety_confirmation_or_validation="
                + self._yes_no(self._features.can_bypass_safety_policies)
            ),
            (
                "planning_scope=authors execution plans for user-provided goals; clarification may "
                "be needed only when the desired outcome is ambiguous"
            ),
            (
                "autonomous_planning_distinction=cannot create own goals; can author intermediate "
                "steps and execution plans for user-provided goals"
            ),
            (
                "prior_result_behavior=can continue from an observed tool result and use it to "
                "choose the next action"
            ),
            (
                "activatable_status=available ability; activation only exposes detailed action "
                "schemas and does not mean the ability is missing"
            ),
        ]

    def _render(
        self,
        active: list[SemanticCapability],
        activatable: list[SemanticCapability],
        omitted: int,
    ) -> str:
        lines = [
            *self._base_lines(),
            "ACTIVE_CAPABILITIES",
            *self._capability_lines(active),
            "ACTIVATABLE_CAPABILITIES",
            *self._capability_lines(activatable),
            "ARCHITECTURAL_LIMITATIONS",
            *self._limitation_lines(),
        ]
        if omitted:
            lines.append(
                f"additional_registered_capabilities={omitted}; consult capability activation "
                "metadata before claiming they are unavailable"
            )
        lines.append(
            "UNAVAILABLE_RULE=An ability is unavailable only when no registered active or "
            "activatable capability provides it; capability metadata never overrides safety, "
            "confirmation, validation, or execution policy."
        )
        return "\n".join(lines)

    def _limitation_lines(self) -> list[str]:
        limitations: list[str] = []
        if not self._features.autonomous_goal_creation:
            limitations.append(
                "cannot independently invent and initiate goals without a user request"
            )
        if not self._features.continuous_passive_observation:
            limitations.append(
                "cannot continuously/passively observe application or system state; observations "
                "are on demand"
            )
        if not self._features.can_bypass_safety_policies:
            limitations.append(
                "cannot bypass safety, confirmation, validation, or execution policy"
            )
        return limitations or ["none declared by the running orchestrator"]

    def _fit(
        self,
        rendered: str,
        active: list[SemanticCapability],
        activatable: list[SemanticCapability],
    ) -> str:
        """Drop tail capability lines while preserving architectural facts and rules."""
        del rendered
        retained_active = list(active)
        retained_activatable = list(activatable)
        total = len(retained_active) + len(retained_activatable)
        while retained_activatable or retained_active:
            if retained_activatable:
                retained_activatable.pop()
            else:
                retained_active.pop()
            omitted = total - len(retained_active) - len(retained_activatable)
            candidate = self._render(retained_active, retained_activatable, omitted)
            if len(candidate) <= self._maximum_characters:
                return candidate
        raise ValueError("capability context limit is too small for the architectural contract")
