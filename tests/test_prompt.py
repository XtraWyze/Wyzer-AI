from wyzer.brain import SystemPromptBuilder
from wyzer.brain import prompt as prompt_module
from wyzer.models import (
    BrowserScene,
    ConversationState,
    DesktopScene,
    SceneSource,
    WindowInfo,
    WorldStateSnapshot,
)


def test_system_prompt_is_concise_and_describes_native_tool_behavior() -> None:
    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())
    assert "Use native tools for real computer actions" in prompt
    assert "Never claim success before a tool result" in prompt
    assert "Do not expose tool names" in prompt
    assert "do not be dramatic" in prompt
    assert "ExecutionPlan" not in prompt
    assert "output_schema" not in prompt


def test_system_prompt_grounds_model_authored_common_folder_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        prompt_module,
        "common_user_folders",
        lambda: {"desktop": r"C:\Users\me\Desktop"},
    )

    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())

    assert '"user_folders":{"desktop":"C:\\\\Users\\\\me\\\\Desktop"}' in prompt
    assert "Author exact absolute file paths" in prompt
    assert "never invent a relative folder" in prompt


def test_system_prompt_grounds_self_awareness_in_runtime_capability_context() -> None:
    capability_context = """SELF_CAPABILITIES
direct_multi_tool_execution=yes
authors_intermediate_steps=yes
ACTIVE_CAPABILITIES
files: search/read and write/edit text files"""

    prompt = SystemPromptBuilder().build(
        WorldStateSnapshot(),
        ConversationState(),
        capability_context=capability_context,
    )

    assert "Registered active tools and activatable tool packs are your abilities" in prompt
    assert "needing to call a tool means you can perform its ability" in prompt
    assert "answer informationally from RUNTIME_CAPABILITY_CONTEXT" in prompt
    assert "without calling action, activation, or planning tools merely to answer" in prompt
    assert "user normally supplies the goal or outcome" in prompt
    assert "determine intermediate actions, tools, arguments, order, and dependencies" in prompt
    assert "Clarification may be needed when the desired outcome is ambiguous" in prompt
    assert "Do not invent and initiate new goals" in prompt
    assert "Use observed results from earlier calls" in prompt
    assert capability_context in prompt


def test_system_prompt_closes_unqualified_chrome_without_kind_confirmation() -> None:
    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())

    assert "unqualified request to close Chrome" in prompt
    assert "personal Chrome windows and Wyzer's managed browser as candidates" in prompt
    assert "closes the sole candidate" in prompt
    assert "ask which returned title or managed browser to close" in prompt


def test_system_prompt_avoids_generic_follow_up_offers() -> None:
    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())

    assert "Do not append a generic offer" in prompt


def test_system_prompt_contains_bounded_recent_application_and_window_context() -> None:
    conversation = ConversationState(
        recently_mentioned_applications=[str(index) for index in range(12)],
        recently_referenced_windows=[
            WindowInfo(handle=index + 1, title=f"Window {index}", process_id=index)
            for index in range(9)
        ],
        remembered_facts=["my name is Koly"],
    )
    prompt = SystemPromptBuilder(personality={"tone": "warm"}).build(
        WorldStateSnapshot(), conversation
    )
    assert '"recent_applications":["4","5"' in prompt
    assert "Window 8" in prompt
    assert "Window 0" not in prompt
    assert "my name is Koly" in prompt
    assert '"tone":"warm"' in prompt


def test_system_prompt_uses_session_context_without_duplicating_legacy_recent_lists() -> None:
    conversation = ConversationState(
        recently_mentioned_applications=["legacy duplicate"],
        recently_mentioned_files=[r"C:\legacy.txt"],
    )
    session_context = {
        "active_window": {"kind": "window", "name": "Notepad", "handle": 44},
        "recent_actions": [{"tool": "open_application", "ok": True, "target": "Notepad"}],
    }

    prompt = SystemPromptBuilder().build(
        WorldStateSnapshot(), conversation, session_context=session_context
    )

    assert '"session_context"' in prompt
    assert "Notepad" in prompt
    assert "legacy duplicate" not in prompt
    assert r"C:\legacy.txt" not in prompt
    assert "Resolve references" in prompt
    assert "never pass a pronoun as a target" in prompt


def test_system_prompt_requires_live_rechecks_without_dumping_broad_window_state() -> None:
    world = WorldStateSnapshot(
        known_open_windows=[
            WindowInfo(
                handle=77,
                title="Calculator",
                process_id=42,
                application="CalculatorApp.exe",
                minimized=True,
            )
        ]
    )

    prompt = SystemPromptBuilder().build(world, ConversationState())

    assert '"observed_open_windows"' not in prompt
    assert "Use list_open_windows only to check window status" in prompt
    assert "Use control tools directly when target and destination are supplied" in prompt
    assert "without preliminary observations" in prompt


def test_system_prompt_distinguishes_direct_sequences_from_persistent_plans() -> None:
    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())

    assert "one or several direct native tools" in prompt
    assert "multiple calls alone do not require a plan" in prompt
    assert "complex work with dependencies" in prompt
    assert "task_plan_create as the only first call" in prompt
    assert "Never mix plan creation with action or capability calls" in prompt
    assert "never ask the user for schema fields" in prompt
    assert "silently correct arguments" in prompt


def test_system_prompt_routes_game_inventory_and_named_projects_semantically() -> None:
    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())

    assert "Game counts/lists use list_installed_games" in prompt
    assert "count-only requests omit names" in prompt
    assert "activate_file_tools then open_indexed_folder" in prompt
    assert "Copy user-supplied names verbatim" in prompt


def test_system_prompt_explains_capability_activation_is_not_the_action() -> None:
    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())

    assert "activate_*_tools function" in prompt
    assert "neither performs nor proves" in prompt
    assert "new action tool next round" in prompt
    assert "continue the original request" in prompt


def test_system_prompt_distinguishes_observation_from_direct_actions() -> None:
    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())

    assert "inspect_screen reads or describes" in prompt
    assert "activate_visual_target clicks" in prompt
    assert "status uses get_current_media" in prompt
    assert "playback changes use control_media" in prompt
    assert "control tools directly when target and destination are supplied" in prompt


def test_system_prompt_distinguishes_clipboard_and_browser_semantics() -> None:
    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())

    assert "read_clipboard reads existing text" in prompt
    assert "copy_selected_text copies the selection" in prompt
    assert "Managed-browser tools control Wyzer's dedicated session" in prompt
    assert "Personal Chrome windows use Windows window tools" in prompt


def test_system_prompt_replaces_raw_monitor_id_with_friendly_label() -> None:
    window = WindowInfo(
        handle=77,
        title="Notepad",
        process_id=42,
        application="notepad.exe",
        monitor_id="monitor:131073",
    )
    world = WorldStateSnapshot(
        foreground_window=window,
        monitor_layout=[
            {
                "monitor_id": "monitor:131073",
                "device_name": "DISPLAY1",
                "display_number": 1,
                "friendly_name": "monitor 1",
                "primary": True,
            }
        ],
    )
    conversation = ConversationState(recently_referenced_windows=[window])

    prompt = SystemPromptBuilder().build(world, conversation)

    assert "monitor:131073" not in prompt
    assert '"monitor":"monitor 1"' in prompt


def test_system_prompt_includes_scene_freshness_without_control_references() -> None:
    world = WorldStateSnapshot(
        desktop_scene=DesktopScene(
            browser=BrowserScene(running=True, active_url="https://example.test"),
            visible_text=["Visible result"],
            sources=[SceneSource(name="browser_page", fresh_for_seconds=15)],
        )
    )

    prompt = SystemPromptBuilder().build(world, ConversationState())

    assert '"desktop_scene"' in prompt
    assert '"name":"browser_page"' in prompt
    assert "freshly observe stale state" in prompt
