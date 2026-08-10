import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


async def _exercise_server(tmp_path):
    env = os.environ.copy()
    env.update({
        "LEADGEN_DATA_DIR": str(tmp_path / "data"),
        "REACHSURGE_USER_ID": "test",
        "REACHSURGE_ENABLE_SEND_EMAIL": "0",
        "PYTHONUNBUFFERED": "1",
    })
    launcher_name = "reachsurge-mcp.exe" if os.name == "nt" else "reachsurge-mcp"
    launcher = Path(sys.executable).with_name(launcher_name)
    assert launcher.exists()
    params = StdioServerParameters(
        command=str(launcher),
        args=[],
        env=env,
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 20
            names = {tool.name for tool in tools.tools}
            assert {"search_leads", "get_task_status", "send_email"} <= names
            result = await session.call_tool("list_leads", {})
            assert result.isError is not True
            assert "暂无保存的线索" in result.content[0].text
            # This handler used to print diagnostics to stdout and corrupt JSON-RPC.
            result = await session.call_tool("enrich_company_profile", {"company_name": "Smoke Test"})
            assert result.content


def test_real_stdio_initialize_list_and_calls(tmp_path):
    asyncio.run(_exercise_server(tmp_path))
