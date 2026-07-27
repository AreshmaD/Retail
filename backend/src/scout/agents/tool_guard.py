import contextvars
from typing import Any

from langchain_core.tools import StructuredTool

_active_guard: contextvars.ContextVar = contextvars.ContextVar("active_guard", default=None)


class ToolCallGuard:
    def __init__(self, max_total_calls: int = 10, max_identical_calls: int = 1):
        self.max_total_calls = max_total_calls
        self.max_identical_calls = max_identical_calls
        self.total_calls = 0
        self.call_signatures: dict[tuple, int] = {}

    def _signature(self, tool_name: str, args: dict) -> tuple:
        try:
            normalized = tuple(sorted((k, str(v)) for k, v in args.items()))
        except Exception:
            normalized = (str(args),)
        return (tool_name, normalized)

    def check(self, tool_name: str, args: dict) -> tuple[bool, str]:
        if self.total_calls >= self.max_total_calls:
            return False, (
                f"Tool call limit reached ({self.max_total_calls} calls this turn). "
                "Stop making tool calls and respond to the customer with what you "
                "already know, or tell them you're having trouble completing this request."
            )

        sig = self._signature(tool_name, args)
        if self.call_signatures.get(sig, 0) >= self.max_identical_calls:
            return False, (
                f"This exact tool call ({tool_name} with these same arguments) was "
                "already made this turn. Do not repeat it — use the result you already "
                "have, try different arguments, or respond to the customer directly."
            )

        return True, ""

    def record(self, tool_name: str, args: dict) -> None:
        self.total_calls += 1
        sig = self._signature(tool_name, args)
        self.call_signatures[sig] = self.call_signatures.get(sig, 0) + 1


def reset_guard(max_total_calls: int = 10, max_identical_calls: int = 1) -> ToolCallGuard:
    guard = ToolCallGuard(max_total_calls, max_identical_calls)
    _active_guard.set(guard)
    return guard


def wrap_tool_with_guard(tool: StructuredTool) -> StructuredTool:
    original_coroutine = tool.coroutine

    async def guarded_coroutine(*args, **kwargs) -> Any:
        guard = _active_guard.get()
        call_args = kwargs if kwargs else (args[0] if args and isinstance(args[0], dict) else {})

        if guard is not None:
            allowed, reason = guard.check(tool.name, call_args)
            if not allowed:
                return reason
            guard.record(tool.name, call_args)

        if original_coroutine is not None:
            return await original_coroutine(*args, **kwargs)
        return tool.func(*args, **kwargs)

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=guarded_coroutine,
        func=None,
    )


def wrap_tools_with_guard(tools: list[StructuredTool]) -> list[StructuredTool]:
    return [wrap_tool_with_guard(t) for t in tools]
