from tests.fakes import ConsequentialEchoTool, EchoTool
from wyzer.brain import CapabilityContextBuilder, OrchestratorFeatures
from wyzer.models import ConfirmationMode
from wyzer.tools import SimpleToolPack, ToolRegistry, create_default_registry


def _builder(
    registry: ToolRegistry,
    *,
    planning: bool = True,
    maximum_characters: int = 3_200,
) -> CapabilityContextBuilder:
    return CapabilityContextBuilder(
        registry,
        OrchestratorFeatures(persistent_complex_task_planning=planning),
        maximum_characters=maximum_characters,
    )


def test_runtime_context_distinguishes_active_and_activatable_semantic_abilities() -> None:
    registry = create_default_registry()

    context = _builder(registry).build()

    assert "direct_multi_tool_execution=yes" in context
    assert "authors_intermediate_steps=yes" in context
    assert "selects_tools_and_arguments=yes" in context
    assert "uses_previous_tool_results=yes" in context
    assert "persistent_complex_task_planning=yes" in context
    assert "autonomous_goal_creation=no" in context
    assert "activatable_status=available ability" in context
    active, activatable = context.split("ACTIVATABLE_CAPABILITIES", 1)
    assert "windows: Observe and control desktop windows" in active
    assert "files: open named local folders/projects; search/read files; " in activatable
    assert "write/edit/append text" in activatable
    assert "create/copy/move/rename/delete" in activatable
    assert "browser:" in activatable
    assert "ARCHITECTURAL_LIMITATIONS" in context
    assert "cannot continuously/passively observe" in context
    assert "cannot bypass safety" in context


def test_activation_moves_a_pack_to_the_active_capability_section() -> None:
    registry = create_default_registry()
    builder = _builder(registry)

    context = builder.build(("files",))
    active, activatable = context.split("ACTIVATABLE_CAPABILITIES", 1)

    assert "files:" in active
    assert "files:" not in activatable


def test_registry_changes_and_runtime_availability_refresh_cached_semantics() -> None:
    registry = ToolRegistry()
    registry.register_pack(
        SimpleToolPack("alpha", (EchoTool,), "Perform alpha echo work."),
        default_visible=True,
    )
    builder = _builder(registry)
    assert "alpha: Perform alpha echo work." in builder.build()

    class BetaTool(EchoTool):
        name = "beta_echo"

    registry.register_pack(
        SimpleToolPack("beta", (BetaTool,), "Perform beta work."),
        default_visible=False,
    )
    assert "beta: Perform beta work." in builder.build()

    registry.get("beta_echo").available = False
    assert "beta: Perform beta work." not in builder.build()

    fresh_registry = ToolRegistry()
    fresh_registry.register_pack(
        SimpleToolPack("beta", (BetaTool,), "Perform beta work."),
        default_visible=True,
    )
    fresh_context = _builder(fresh_registry).build()
    assert "alpha:" not in fresh_context
    assert "beta:" in fresh_context


def test_capability_context_is_bounded_and_does_not_include_hidden_schemas() -> None:
    registry = ToolRegistry()
    for index in range(40):
        tool_name = f"echo_{index}"

        def factory(name: str = tool_name) -> EchoTool:
            tool = EchoTool()
            tool.name = name
            return tool

        registry.register_pack(
            SimpleToolPack(
                f"pack_{index}",
                (factory,),
                "Perform a specialized semantic operation " + ("safely " * 80),
            ),
            default_visible=False,
        )

    context = _builder(registry, maximum_characters=2_000).build()

    assert len(context) <= 2_000
    assert "additional_registered_capabilities=" in context
    assert '"properties"' not in context
    assert "arguments_schema" not in context
    assert "message" not in context


def test_capability_metadata_does_not_change_visibility_or_confirmation_policy() -> None:
    registry = ToolRegistry()
    registry.register_pack(
        SimpleToolPack(
            "messaging",
            (ConsequentialEchoTool,),
            "Send consequential messages.",
        ),
        default_visible=False,
    )

    context = _builder(registry).build()

    assert "messaging: Send consequential messages." in context
    assert registry.native_tools() == []
    assert registry.get("send_message").confirmation == ConfirmationMode.ALWAYS


def test_task_planning_fact_reflects_the_running_orchestrator_feature() -> None:
    context = _builder(ToolRegistry(), planning=False).build()

    assert "persistent_complex_task_planning=no" in context
