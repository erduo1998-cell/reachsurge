import asyncio
import os
import shutil
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


async def _exercise_server(tmp_path):
    env = os.environ.copy()
    env.update({
        "LEADGEN_DATA_DIR": str(tmp_path / "data"),
        "LEADGEN_ENV_FILE": str(tmp_path / "missing.env"),
        "REACHSURGE_USER_ID": "test",
        "REACHSURGE_ENABLE_SEND_EMAIL": "0",
        "PYTHONUNBUFFERED": "1",
    })
    launcher_name = "reachsurge-mcp.exe" if os.name == "nt" else "reachsurge-mcp"
    candidates = [
        Path(sys.executable).with_name(launcher_name),
        Path(sys.executable).parent / "Scripts" / launcher_name,
    ]
    located = shutil.which(launcher_name)
    if located:
        candidates.append(Path(located))
    launcher = next((path for path in candidates if path.exists()), candidates[0])
    assert launcher.exists()
    params = StdioServerParameters(
        command=str(launcher),
        args=[],
        env=env,
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            assert "setup_status" in (initialized.instructions or "")
            tools = await session.list_tools()
            assert len(tools.tools) == 22
            names = {tool.name for tool in tools.tools}
            assert {"setup_status", "complete_setup", "search_leads", "get_task_status", "send_email"} <= names

            blocked = await session.call_tool("list_leads", {})
            assert blocked.isError is True
            assert blocked.content[0].text.startswith("SETUP_REQUIRED:")

            status = await session.call_tool("setup_status", {})
            assert '"phase": "needs_profile"' in status.content[0].text
            saved = await session.call_tool("save_user_config", {
                "industry": "LED lighting",
                "product_description": "Commercial LED fixtures",
                "target_markets": "Germany,France",
            })
            assert saved.isError is not True
            completed = await session.call_tool("complete_setup", {})
            assert '"onboarding_complete": true' in completed.content[0].text

            result = await session.call_tool("list_leads", {})
            assert result.isError is not True
            assert "暂无保存的线索" in result.content[0].text
            # This handler used to print diagnostics to stdout and corrupt JSON-RPC.
            result = await session.call_tool("enrich_company_profile", {"company_name": "Smoke Test"})
            assert result.content

    pending = list((tmp_path / "data" / "state").glob("*.pending.json"))
    complete = list((tmp_path / "data" / "state").glob("*.complete.json"))
    assert pending == []
    assert len(complete) == 1

    # A fresh MCP process reusing the same data directory must skip onboarding.
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            assert "首次设置已完成" in (initialized.instructions or "")
            result = await session.call_tool("list_leads", {})
            assert result.isError is not True


def test_real_stdio_initialize_list_and_calls(tmp_path):
    asyncio.run(_exercise_server(tmp_path))
