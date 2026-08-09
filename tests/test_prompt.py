from wyzer.brain import SystemPromptBuilder
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
    assert "Use an available tool" in prompt
    assert "Never claim an action succeeded" in prompt
    assert "Do not expose tool names" in prompt
    assert "do not be dramatic" in prompt
    assert "ExecutionPlan" not in prompt
    assert "output_schema" not in prompt


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
    assert "A minimized window is still open" in prompt
    assert "never substitute is_process_running" in prompt
    assert "perform the action directly" in prompt
    assert "do not add a preliminary status check" in prompt


def test_system_prompt_requires_planning_before_multiple_capabilities() -> None:
    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())

    assert "first response must contain only task_plan_create" in prompt
    assert "Never batch capability calls before that plan exists" in prompt


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
    assert "fresh read-only observation" in prompt
