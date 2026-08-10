"""Measure Wyzer's model-facing system prompt and native tool schemas."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from typing import Any

from wyzer.brain import SystemPromptBuilder
from wyzer.models import ConversationState, WorldStateSnapshot
from wyzer.tasks.tools import task_native_tools
from wyzer.tools import create_default_registry


def _serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def measure(activated_capabilities: tuple[str, ...] = ()) -> dict[str, Any]:
    registry = create_default_registry()
    prompt = SystemPromptBuilder().build(WorldStateSnapshot(), ConversationState())
    view = registry.model_view(activated_capabilities)
    capability_tools = view.native_tools()
    all_capability_tools = registry.all_native_tools()
    task_tools = task_native_tools()
    tools = [*capability_tools, *task_tools]
    all_tools = [*all_capability_tools, *task_tools]

    per_tool: dict[str, int] = {}
    per_pack: dict[str, int] = defaultdict(int)
    for tool in tools:
        payload = tool.model_dump(mode="json")
        size = len(_serialized(payload))
        name = tool.function.name
        per_tool[name] = size
        pack = registry.tool_pack(name) if name in registry else "task_engine"
        per_pack[pack or "unpacked"] += size

    schemas = _serialized([tool.model_dump(mode="json") for tool in tools])
    all_schemas = _serialized([tool.model_dump(mode="json") for tool in all_tools])
    total = len(prompt) + len(schemas)
    return {
        "registered_tool_count": len(registry),
        "all_model_visible_tool_count": len(all_capability_tools),
        "default_model_visible_tool_count": len(registry.native_tools()),
        "activated_capabilities": list(view.activated_capabilities),
        "visible_capability_packs": list(view.capability_packs),
        "system_prompt_characters": len(prompt),
        "tool_schema_characters": len(schemas),
        "all_tool_schema_characters": len(all_schemas),
        "tool_schema_approximate_tokens": round(len(schemas) / 4),
        "all_tool_schema_approximate_tokens": round(len(all_schemas) / 4),
        "total_characters": total,
        "approximate_tokens": round(total / 4),
        "capability_tool_count": len(capability_tools),
        "task_tool_count": len(task_tools),
        "per_tool_schema_characters": dict(sorted(per_tool.items())),
        "per_pack_schema_characters": dict(sorted(per_pack.items())),
        "largest_10_schemas": [
            {"name": name, "characters": size}
            for name, size in sorted(per_tool.items(), key=lambda item: (-item[1], item[0]))[:10]
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--activate",
        action="append",
        default=[],
        metavar="CAPABILITY",
        help="Include an activated capability in the measured model view.",
    )
    args = parser.parse_args()
    result = measure(tuple(args.activate))
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Registered tools: {result['registered_tool_count']}")
    print(
        "Model-visible tools: "
        f"{result['capability_tool_count']} current / "
        f"{result['all_model_visible_tool_count']} all"
    )
    print(f"Activated capabilities: {result['activated_capabilities'] or 'none'}")
    print(f"System prompt: {result['system_prompt_characters']:,} chars")
    print(f"Tool schemas: {result['tool_schema_characters']:,} chars")
    print(f"Total: {result['total_characters']:,} chars")
    print(f"Approximate tokens (chars / 4): {result['approximate_tokens']:,}")
    print("\nPer pack:")
    for name, size in result["per_pack_schema_characters"].items():
        print(f"  {name}: {size:,}")
    print("\nLargest 10 schemas:")
    for item in result["largest_10_schemas"]:
        print(f"  {item['name']}: {item['characters']:,}")


if __name__ == "__main__":
    main()
