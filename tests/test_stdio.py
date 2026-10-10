import asyncio
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from onboarding import AUTHOR_SUPPORT_INSTRUCTIONS


ROOT = Path(__file__).resolve().parents[1]


def _assert_support_instructions(initialized):
    # Support is delivered once at protocol initialization, after the existing
    # setup guidance, rather than being appended to business tool payloads.
    instructions = initialized.instructions or ""
    assert instructions.endswith(AUTHOR_SUPPORT_INSTRUCTIONS)
    assert instructions.count(AUTHOR_SUPPORT_INSTRUCTIONS) == 1
    support_urls = [word for word in AUTHOR_SUPPORT_INSTRUCTIONS.split() if word.startswith("https://")]
    assert {urlsplit(url).netloc for url in support_urls} == {"erduo.art", "login.erduo.art"}


def _assert_business_content_has_no_support(result):
    content = " ".join(getattr(item, "text", "") for item in result.content)
    assert "erduo.art" not in content
    assert AUTHOR_SUPPORT_INSTRUCTIONS not in content


async def _exercise_server(tmp_path):
    env = os.environ.copy()
    # This protocol smoke test never opts into paid providers or mail access.
    for name in (
        "DEEPSEEK_API_KEY", "SERPAPI_API_KEYS", "HUNTER_API_KEY",
        "TAVILY_API_KEYS", "CAPSOLVER_API_KEY", "REACHSURGE_SMTP_PASSWORD",
        "REACHSURGE_IMAP_PASSWORD",
    ):
        env.pop(name, None)
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
            _assert_support_instructions(initialized)
            tools = await session.list_tools()
            assert len(tools.tools) == 22
            names = {tool.name for tool in tools.tools}
            assert {"setup_status", "complete_setup", "search_leads", "get_task_status", "send_email"} <= names

            blocked = await session.call_tool("list_leads", {})
            assert blocked.isError is True
            assert blocked.content[0].text.startswith("SETUP_REQUIRED:")
            _assert_business_content_has_no_support(blocked)

            status = await session.call_tool("setup_status", {})
            assert '"phase": "needs_profile"' in status.content[0].text
            _assert_business_content_has_no_support(status)
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
            _assert_business_content_has_no_support(result)
            lead = await session.call_tool("save_lead", {"company_name": "Support isolation test", "country": "Germany"})
            assert lead.isError is not True
            _assert_business_content_has_no_support(lead)
            draft_context = await session.call_tool("compose_outreach", {"company_name": "Support isolation test"})
            assert draft_context.isError is not True
            _assert_business_content_has_no_support(draft_context)
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
            _assert_support_instructions(initialized)
            result = await session.call_tool("list_leads", {})
            assert result.isError is not True


def test_real_stdio_initialize_list_and_calls(tmp_path):
    asyncio.run(_exercise_server(tmp_path))
