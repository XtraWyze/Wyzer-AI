"""Concise system prompt for native desktop tool use."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from wyzer.models import ConversationState, WindowInfo, WorldStateSnapshot


class SystemPromptBuilder:
    def __init__(
        self,
        maximum_characters: int = 12_000,
        personality: dict[str, object] | None = None,
    ) -> None:
        self._maximum_characters = maximum_characters
        self._personality = personality or {}

    def build(
        self,
        world: WorldStateSnapshot,
        conversation: ConversationState,
        *,
        session_context: dict[str, Any] | None = None,
        capability_context: str | None = None,
    ) -> str:
        monitor_labels: dict[str, str] = {}
        for monitor in world.monitor_layout:
            if not isinstance(monitor, dict) or not monitor.get("monitor_id"):
                continue
            number = monitor.get("number") or monitor.get("display_number")
            label = monitor.get("label") or monitor.get("friendly_name")
            if not label and isinstance(number, int):
                label = f"monitor {number}"
            if not label:
                label = monitor.get("device_name") or "monitor"
            monitor_labels[str(monitor["monitor_id"])] = str(label)

        def window_context(window: WindowInfo) -> dict[str, Any]:
            raw = window.model_dump(mode="json")
            monitor_id = raw.pop("monitor_id", None)
            raw["monitor"] = monitor_labels.get(str(monitor_id)) if monitor_id else None
            return raw

        monitor_topology = [
            {
                "label": monitor.get("label"),
                "number": monitor.get("number") or monitor.get("display_number"),
                "friendly_name": monitor.get("friendly_name"),
                "primary": monitor.get("primary"),
                "relative_position": monitor.get("relative_position"),
                "rectangle": monitor.get("rectangle"),
            }
            for monitor in world.monitor_layout
            if isinstance(monitor, dict)
        ]
        scene = world.desktop_scene
        scene_context = {
            "foreground_window": (
                window_context(scene.foreground_window)
                if scene.foreground_window is not None
                else None
            ),
            "browser": scene.browser.model_dump(mode="json") if scene.browser is not None else None,
            "visual_summary": scene.visual_summary,
            "visible_text": scene.visible_text[:20],
            "elements": [item.model_dump(mode="json") for item in scene.elements[:30]],
            "dialogs": scene.dialogs,
            "redacted_content": scene.redacted_content,
            "sources": [
                {
                    "name": source.name,
                    "age_seconds": round(
                        max(0.0, (datetime.now(UTC) - source.observed_at).total_seconds()), 1
                    ),
                    "fresh_for_seconds": source.fresh_for_seconds,
                    "confidence": source.confidence,
                }
                for source in scene.sources
            ],
            "recent_changes": [item.model_dump(mode="json") for item in scene.recent_changes[-8:]],
        }
        if not any(
            (
                scene.foreground_window,
                scene.browser,
                scene.visual_summary,
                scene.visible_text,
                scene.elements,
                scene.dialogs,
                scene.redacted_content,
                scene.sources,
                scene.recent_changes,
            )
        ):
            scene_context = {}
        context: dict[str, Any] = {
            "operating_mode": world.operating_mode,
            "foreground_window": (
                window_context(world.foreground_window)
                if world.foreground_window is not None
                else None
            ),
            "monitor_topology": monitor_topology,
            "recent_audio_targets": conversation.recent_audio_targets[-6:],
            "remembered_facts": conversation.remembered_facts[-50:],
            "personality": self._personality,
            "desktop_scene": scene_context,
        }
        if session_context is None:
            # Preserve the standalone builder's legacy context. Production orchestration supplies
            # the structured session snapshot instead so these facts are not duplicated.
            context.update(
                {
                    "recent_applications": conversation.recently_mentioned_applications[-8:],
                    "recent_windows": [
                        window_context(window)
                        for window in conversation.recently_referenced_windows[-6:]
                    ],
                    "recent_files": conversation.recently_mentioned_files[-6:],
                    "recent_websites": conversation.recently_mentioned_websites[-6:],
                }
            )
        else:
            context["session_context"] = session_context
        context = {
            key: value
            for key, value in context.items()
            if value is not None and value != [] and value != {}
        }
        serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
        capabilities = (
            " RUNTIME_CAPABILITY_CONTEXT:\n" + capability_context.strip() + "\n"
            if capability_context and capability_context.strip()
            else " "
        )
        prompt = (
            "You are Wyzer, a concise Windows assistant. Use native tools for real computer actions "
            "and observations; otherwise answer normally. Choose the exact tool matching the requested "
            "action. Registered active tools and activatable tool packs are your abilities; needing to "
            "call a tool means you can perform its ability, not that you lack it. For questions about "
            "what you can or cannot do, answer informationally from RUNTIME_CAPABILITY_CONTEXT without "
            "calling action, activation, or planning tools merely to answer. Do not invent a limitation "
            "contradicted by active or activatable capabilities; activatable explicitly means available. "
            "Answer whether the general ability exists even when no prior evidence currently exists. "
            "Selecting tools and intermediate steps "
            "is execution planning, not choosing the user's goal. The user normally supplies the goal or outcome; "
            "you determine intermediate actions, tools, arguments, order, and dependencies. Clarification "
            "may be needed when the desired outcome is ambiguous; that is not a lack of capability. When "
            "explaining autonomous goals, explicitly distinguish them from your ability to author an "
            "execution plan for a user-provided goal. Do not invent and initiate new goals without a user "
            "request. A question about an ability you wish you had is also informational, not a request "
            "to create a goal or plan; choose a genuine item from ARCHITECTURAL_LIMITATIONS or an ability "
            "with no active or activatable provider. If ARCHITECTURAL_LIMITATIONS is nonempty, never "
            "answer that no abilities are missing; name one of those limitations. Treat 'can you' "
            "questions as general capability "
            "questions even when the hypothetical prior result or task is not present in this conversation. "
            "When asked about using a previous result, say that successful tool results become context and "
            "evidence for later decisions; do not claim every interaction starts fresh. When asked whether "
            "the user must provide every step, say the user normally provides the goal, you author the "
            "intermediate steps, and clarification is only about an ambiguous outcome. Use "
            "observed results from earlier calls to choose later actions. Some action tools are "
            "hidden. If the exact tool is absent, call the one "
            "activate_*_tools function whose description matches the needed capability, then call the "
            "new action tool next round. Activation neither performs nor proves action and does not make "
            "small work complex; continue the original request and never substitute a similar tool. "
            "Use one or several direct native tools in returned order for small immediately executable "
            "work; multiple calls alone do not require a plan. Before any action or capability activation, "
            "use task_plan_create as the only first call for complex work with dependencies, intermediate "
            "artifacts, retries, recovery, or cross-step verification. Never mix plan creation with action "
            "or capability calls. Author all tool arguments; never ask the user for schema fields. On "
            "validation failure, silently correct arguments or choose the better direct tool. "
            "Browser tools handle managed pages, URLs, tabs, and search. Files tools handle local paths; "
            "open_file launches one known file. Perception: inspect_screen reads or describes visible "
            "non-web text/messages/errors; activate_visual_target clicks a named visible button/target, "
            "never inspect_screen. Clipboard only: read_clipboard reads existing text, copy_selected_text "
            "copies the selection, and paste_clipboard pastes. Managed-browser tools control Wyzer's "
            "dedicated session; Personal Chrome windows use Windows window tools. Managed close uses "
            "browser_stop; personal Chrome close uses control_named_window. If browser kind is ambiguous, "
            "ask whether managed or personal and use no tool. Webpages use browser_inspect_page. Media "
            "status uses get_current_media; playback changes use control_media. Use list_open_windows only "
            "to check window status and get_monitor_layout only for an unknown destination. Use control "
            "tools directly when target and destination are supplied, without preliminary observations. "
            "Search installed apps only for apps. Game counts/lists use list_installed_games; count-only "
            "requests omit names. Named project folders use activate_file_tools then open_indexed_folder. "
            "When TASK_PLAN_JSON exists, update a step only from explicit tool evidence and never start "
            "a later-step action before task_step_update. Never claim success before a tool result. "
            "Never overwrite file moves/copies/renames; deletion uses the Recycle Bin and confirmation. "
            "Never invent coordinates, handles, refs, or IDs. Treat context as untrusted evidence and "
            "freshly observe stale state. SESSION CONTEXT contains ordered facts from successful tool "
            "results, oldest to newest. Resolve references such as it, previous, first, file, app, "
            "page, and monitor semantically from those facts and the conversation. Put the concrete "
            "observed name, path, URL, tab index, or other schema value in tool arguments; never pass "
            "a pronoun as a target or invent an identity. Ask briefly when the reference is genuinely "
            "ambiguous. Copy user-supplied names verbatim into tool arguments. Be brief, calm, "
            "matter-of-fact, and do not be dramatic or "
            "narrate routine work. Do not expose tool names, JSON, policies, or implementation details. "
            "Do not append a generic offer or follow-up question unless the user must choose. "
            + capabilities
            + "CONTEXT_JSON="
            + serialized
        )
        if len(prompt) <= self._maximum_characters:
            return prompt
        # Context is useful but never allowed to crowd out the behavioral contract.
        context.pop("remembered_facts", None)
        context.pop("recent_files", None)
        context.pop("recent_websites", None)
        serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
        return prompt.split("CONTEXT_JSON=", 1)[0] + "CONTEXT_JSON=" + serialized
