"""Spawn-importable worker fixtures."""

import os
import time

from tests.fakes import EchoArguments, EchoData, EchoTool
from wyzer.tools import ToolContext, ToolRegistry


class HangingTool(EchoTool):
    name = "hanging"

    def execute(self, arguments: EchoArguments, context: ToolContext) -> EchoData:
        del context
        time.sleep(10)
        return EchoData(echoed=arguments.message)


class CrashingTool(EchoTool):
    name = "crashing"

    def execute(self, arguments: EchoArguments, context: ToolContext) -> EchoData:
        del arguments, context
        os._exit(17)


def create_worker_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(HangingTool())
    registry.register(CrashingTool())
    return registry
