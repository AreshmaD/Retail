from contextlib import AsyncExitStack
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from scout.config import settings

SERVER_PATH = Path(__file__).resolve().parent.parent / "mcp_server" / "server.py"

MCP_SERVERS_CONFIG = {
    "scout": {
        "transport": "stdio",
        "command": "python3",
        "args": [str(SERVER_PATH)],
    },
    "stripe": {
        "transport": "streamable_http",
        "url": settings.STRIPE_MCP_URL,
        "headers": {
            "Authorization": f"Bearer {settings.STRIPE_MCP_KEY}",
        },
    },
}


class MCPToolManager:
    def __init__(self):
        self.client = MultiServerMCPClient(MCP_SERVERS_CONFIG)
        self._exit_stack = AsyncExitStack()
        self.tools: list = []

    async def start(self) -> list:
        scout_session = await self._exit_stack.enter_async_context(
            self.client.session("scout")
        )
        scout_tools = await load_mcp_tools(scout_session)
        stripe_tools = await self.client.get_tools(server_name="stripe")
        self.tools = scout_tools + stripe_tools
        return self.tools

    async def stop(self) -> None:
        await self._exit_stack.aclose()


def get_tools_by_name(tools: list, names: list[str]) -> list:
    by_name = {t.name: t for t in tools}
    return [by_name[n] for n in names if n in by_name]
