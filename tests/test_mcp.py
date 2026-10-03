"""MCP stdio server checks: a real `kuska mcp` subprocess and a real client."""

import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import kuska as ac


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "mcpproject"
    (project / ".agents" / "prompts").mkdir(parents=True)
    ac.config_path(project).write_text('[agents.codex-1]\nbackend = "codex"\nrole = "second opinion"\n')
    return project


def test_mcp_stdio_server(project):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "kuska", "--project", str(project), "mcp", "--agent", "codex-1"],
    )

    async def run() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                assert names == list(ac.tools.BASE_TOOLS), "a configured agent gets its flavor's tools"
                schema = next(t for t in tools.tools if t.name == "send_message").input_schema
                assert schema["required"] == ["recipient", "payload"], "schema has required args"

                res = await session.call_tool("docs_set", {"key": "notes", "content": "from codex"})
                assert not res.is_error, "docs_set via mcp"
                res = await session.call_tool("docs_get", {"key": "notes"})
                assert "from codex" in res.content[0].text, "docs_get via mcp"

                conn = ac.connect(ac.db_path(project))
                assert ac.docs_list(conn)[-1]["updated_by"] == "codex-1", "attributed to caller"
                ac.add_task(conn, "codex task", "do the thing", "codex-1")

                # Verify claim_task tool is no longer available
                claim_task_tool = next((t for t in tools.tools if t.name == "claim_task"), None)
                assert claim_task_tool is None, "claim_task tool removed"
                assert ac.get_task(conn, 1)["status"] == "todo", "task remains todo"

                # cost is the daemon's to record, from the backend's own figures
                reply_schema = next(t for t in tools.tools if t.name == "reply").input_schema
                reply_props = reply_schema.get("properties", {})
                assert not any(k in reply_props for k in ["cost_usd", "input_tokens", "output_tokens"]), "reply takes no cost/token fields"
                assert reply_props["status"]["enum"] == ["done", "blocked", "needs_approval"], "reply statuses limited"

                res = await session.call_tool("reply", {"task_id": 1, "payload": "done"})
                assert res.is_error and ac.get_task(conn, 1)["status"] == "todo", "reply refused on a task not in progress"

                ac.update_task_status(conn, 1, "ready")
                ac.claim_task(conn, "codex-1")
                res = await session.call_tool("reply", {"task_id": 1, "payload": "done", "status": "ready"})
                assert res.is_error and ac.get_task(conn, 1)["status"] == "in_progress", "reply refuses a status outside done/blocked/needs_approval"

                ac.register_agent(conn, "dev-agent", "claude")
                other = ac.add_task(conn, "someone else's", "", "dev-agent")
                ac.update_task_status(conn, other, "in_progress")
                res = await session.call_tool("reply", {"task_id": other, "payload": "done"})
                assert res.is_error and ac.get_task(conn, other)["status"] == "in_progress", "reply refused on another agent's task"

                await session.call_tool("reply", {"task_id": 1, "payload": "done"})
                assert ac.get_task(conn, 1)["status"] == "done", "reply via mcp"

                res = await session.call_tool("reply", {"task_id": 1, "payload": "again"})
                assert res.is_error and "already done" in res.content[0].text, "second reply refused"

                res = await session.call_tool("reply", {"payload": "missing task_id"})
                assert res.is_error, "bad args are an error result"
                conn.close()

    anyio.run(run)


def test_mcp_refuses_unconfigured_agent(project):
    import subprocess
    r = subprocess.run(
        [sys.executable, "-m", "kuska", "--project", str(project), "mcp", "--agent", "nobody"],
        capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    assert r.returncode != 0 and b"not an agent" in r.stderr


def test_mcp_operator(project):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "kuska", "--project", str(project), "mcp", "--operator", "--agent", "boss"],
    )

    async def run() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                assert names == {s["name"] for s in ac.tools.TOOL_SPECS}
                res = await session.call_tool("docs_set", {"key": "notes", "content": "x"})
                assert not res.is_error
                conn = ac.connect(ac.db_path(project))
                assert ac.docs_list(conn)[-1]["updated_by"] == "boss"
                conn.close()

    anyio.run(run)
