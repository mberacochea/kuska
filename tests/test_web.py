#!/usr/bin/env python3
"""Web UI + MCP server checks: `uv run tests/test_web.py`."""

import shutil
import sys
import tempfile
from pathlib import Path

import achka as ac

PASSED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        sys.exit(1)


def make_project(tmp: Path) -> Path:
    project = tmp / "webproject"
    (project / ".agents" / "prompts").mkdir(parents=True)
    ac.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nmodel = "claude-opus-5"\nrole = "builder"\n'
    )
    return project


def test_web(project: Path) -> None:
    app = ac.create_app(project)
    app.config.update(TESTING=True)
    c = app.test_client()

    print("project page")
    html = c.get("/").get_data(as_text=True)
    check("renders", "<title>webproject - achka</title>" in html)
    check("htmx loaded", "htmx.min.js" in html)
    check("empty state", "No tasks yet." in html)

    check("description saved", c.post("/description", data={"content": "# Web project"}).status_code == 200)
    check("description round-trips", "# Web project" in c.get("/").get_data(as_text=True))

    print("tasks")
    frag = c.post("/tasks", data={"title": "Ship it", "description": "carefully", "assigned_to": "dev-agent"}).get_data(as_text=True)
    check("row added", 'id="task-1"' in frag and "Ship it" in frag)
    check("blank title ignored", "No tasks yet." not in c.post("/tasks", data={"title": "  "}).get_data(as_text=True))
    conn = ac.connect(ac.db_path(project))
    check("only one task", len(ac.list_tasks(conn)) == 1)

    row = c.post("/tasks/1", data={"status": "blocked"}).get_data(as_text=True)
    check("status patched", ac.get_task(conn, 1)["status"] == "blocked")
    check("patch returns row", 'id="task-1"' in row and "selected" in row)
    c.post("/tasks/1", data={"assigned_to": ""})
    check("unassign", ac.get_task(conn, 1)["assigned_to"] is None)
    c.post("/tasks/1", data={"assigned_to": "dev-agent"})

    detail = c.get("/tasks/1/detail").get_data(as_text=True)
    check("detail expands", "carefully" in detail and "No messages yet." in detail)
    check("requeue offered when not todo", "Re-queue" in detail)
    c.post("/tasks/1/requeue")
    check("requeued", ac.get_task(conn, 1)["status"] == "todo")

    thread = c.post("/tasks/1/message", data={"payload": "check the edge case"}).get_data(as_text=True)
    check("human message posted", "check the edge case" in thread)
    check("message is routed to assignee", ac.get_inbox(conn, "dev-agent")[0]["payload"] == "check the edge case")

    ac.reply(conn, "dev-agent", 1, "Shipped.", input_tokens=10, output_tokens=5, cost_usd=0.01)
    detail = c.get("/tasks/1/detail").get_data(as_text=True)
    check("result shows in thread", "Shipped." in detail and "$0.0100" in detail)
    check("collapse route", 'id="task-1"' in c.get("/tasks/1/row").get_data(as_text=True))

    print("approval and dependencies")
    c.post("/tasks", data={"title": "design the schema", "assigned_to": "dev-agent"})
    c.post("/tasks", data={"title": "build on it", "assigned_to": "dev-agent"})
    ids = [t["id"] for t in ac.list_tasks(conn)]
    first, second = ids[-2], ids[-1]
    panel = c.post(f"/tasks/{second}/deps", data={"depends_on": first}).get_data(as_text=True)
    check("dependency added", [d["id"] for d in ac.task_dependencies(conn, second)] == [first])
    check("panel lists it", f"#{first} design the schema" in panel, panel[:0])
    check("cycle refused with a toast", "already depends" in
          c.post(f"/tasks/{first}/deps", data={"depends_on": second}).get_data(as_text=True))

    c.post(f"/tasks/{first}", data={"status": "needs_approval"})
    check("held task is not claimable", ac.claim_task(conn, "dev-agent") is None)
    row = c.get(f"/tasks/{second}/row").get_data(as_text=True)
    check("row shows what it waits on", f"#{first} needs_approval" in row, row)
    detail = c.get(f"/tasks/{first}/detail").get_data(as_text=True)
    check("approval prompt shown", "Waiting for your approval." in detail)
    check("both resolutions offered", "Approve (mark done)" in detail and "Send back (re-queue)" in detail)

    sent_back = c.post(f"/tasks/{first}/send-back").get_data(as_text=True)
    check("send back re-queues", ac.get_task(conn, first)["status"] == "todo")
    check("said so", "sent back" in sent_back)
    check("dependent still waits", ac.claim_task(conn, "dev-agent")["id"] == first)
    ac.update_task_status(conn, first, "needs_approval")
    c.post(f"/tasks/{first}/approve")
    check("approve marks it done", ac.get_task(conn, first)["status"] == "done")
    check("dependent runs now", ac.claim_task(conn, "dev-agent")["id"] == second)
    c.post(f"/tasks/{second}/deps/{first}/delete")
    check("dependency removed", ac.task_dependencies(conn, second) == [])
    for tid in (first, second):
        c.post(f"/tasks/{tid}/delete")

    print("markdown rendering")
    c.post("/tasks/1", data={"title": "Ship it", "description": "## Plan\n\n- one\n- two\n\n`code`"})
    detail = c.get("/tasks/1/detail").get_data(as_text=True)
    check("headings rendered", "<h2>Plan</h2>" in detail, detail[:0])
    check("lists rendered", "<li>one</li>" in detail)
    check("inline code rendered", "<code>code</code>" in detail)
    check("source not shown raw", "## Plan" not in detail)
    check("edit view gives the source back", "## Plan" in c.get("/tasks/1/detail?edit=1").get_data(as_text=True))
    ac.send_message(conn, "dev-agent", "human", 1, "result", "**done** &lt;ok&gt;")
    thread = c.get("/tasks/1/detail").get_data(as_text=True)
    check("message markdown rendered", "<strong>done</strong>" in thread)
    ac.send_message(conn, "dev-agent", "human", 1, "note", "<script>alert(1)</script>")
    check("html from agents is escaped", "<script>alert(1)</script>" not in c.get("/tasks/1/detail").get_data(as_text=True))
    check("docs render too", "<strong>bold</strong>" in (
        c.post("/docs", data={"key": "notes"}),
        c.post("/docs/notes", data={"content": "**bold**"}),
        c.get("/docs/notes").get_data(as_text=True))[-1])
    c.post("/docs/notes/delete")

    print("agents page")
    html = c.get("/agents").get_data(as_text=True)
    check("agent listed", "dev-agent" in html and "claude" in html)
    check("model column", "claude-opus-5" in html)
    check("spend shown", "$0.0100" in html)
    check("polls itself", 'hx-get="/agents/rows"' in html)
    rows = c.get("/agents/rows").get_data(as_text=True)
    check("rows fragment", "<table>" in rows)
    # the poll replaces #agent-rows wholesale, so the editor must live outside
    # it - otherwise opening one and typing loses the text 5s later
    check("editor host is outside the poll", 'id="agent-editor"' in html and 'id="agent-editor"' not in rows)

    print("claims panel")
    ac.heartbeat(conn, "dev-agent", "working")
    ac.claim_files(conn, "dev-agent", ["src/parser.py"], task_id=1, run_id="r1", note="rewriting")
    panel = c.get("/agents/claims").get_data(as_text=True)
    check("panel polls itself", 'hx-get="/agents/claims"' in panel)
    check("shows the file and holder", "src/parser.py" in panel and "dev-agent" in panel)
    check("shows why", "rewriting" in panel)
    check("on the agents page", 'id="claims"' in c.get("/agents").get_data(as_text=True))
    ac.release_run(conn, "r1")
    check("empty state", "Nothing claimed" in c.get("/agents/claims").get_data(as_text=True))
    check("claims are editable as rows", 'id="rows"' in c.get("/data/file_claims").get_data(as_text=True))

    print("agent settings")
    editor = c.get("/agents/dev-agent/context").get_data(as_text=True)
    check("settings form", 'name="model"' in editor and 'name="backend"' in editor)
    check("current model prefilled", 'value="claude-opus-5"' in editor, editor)
    check("every field offered", all(f'name="{f["key"]}"' in editor for f in ac.AGENT_FIELDS))
    check("prompt editor too", "textarea" in editor and "prompts/dev-agent.md" in editor)

    saved = c.post("/agents/dev-agent", data={
        "backend": "claude", "model": "claude-sonnet-5", "role": "builder",
        "permission_mode": "plan", "sandbox": "", "codex_bin": "",
        "price_in_per_mtok": "", "price_out_per_mtok": "",
    }).get_data(as_text=True)
    cfg = ac.load_config(project)["agents"]["dev-agent"]
    check("model written to config.toml", cfg["model"] == "claude-sonnet-5", cfg)
    check("option written", cfg["permission_mode"] == "plan", cfg)
    check("blank fields dropped", "sandbox" not in cfg and "codex_bin" not in cfg, cfg)
    check("role synced to the db", ac.get_agent(conn, "dev-agent")["role"] == "builder")
    check("table swapped back", 'id="agent-rows"' in saved and "claude-sonnet-5" in saved)
    check("toast is out-of-band", 'id="toast" hx-swap-oob="true"' in saved, saved[-200:])
    check("editor reflects the change", 'value="claude-sonnet-5"' in c.get("/agents/dev-agent/context").get_data(as_text=True))

    print("adding and removing agents")
    added = c.post("/agents", data={"name": "bench-1", "backend": "codex", "model": "gpt-5-codex", "role": "benchmarks"}).get_data(as_text=True)
    check("agent added to config", ac.load_config(project)["agents"]["bench-1"]["backend"] == "codex")
    check("agent registered in db", ac.get_agent(conn, "bench-1")["backend"] == "codex")
    check("prompt file seeded", ac.prompt_path(project, "bench-1").exists())
    check("shows in the table", "bench-1" in added and "gpt-5-codex" in added)
    check("bad name refused", "not a usable agent name" in c.post("/agents", data={"name": "../etc/passwd"}).get_data(as_text=True))
    check("nothing written for a bad name", "../etc/passwd" not in str(ac.load_config(project)["agents"]))
    check("duplicate refused", "already exists" in c.post("/agents", data={"name": "bench-1"}).get_data(as_text=True))

    c.post("/agents/bench-1", data={"backend": "codex", "price_in_per_mtok": "1.25", "price_out_per_mtok": "10"})
    check("prices stored as numbers", ac.load_config(project)["agents"]["bench-1"]["price_in_per_mtok"] == 1.25)
    check("bad price refused", "must be numbers" in c.post("/agents/bench-1", data={"price_in_per_mtok": "cheap"}).get_data(as_text=True))

    t_id = ac.add_task(conn, "for bench", assigned_to="bench-1")
    removed = c.post("/agents/bench-1/delete").get_data(as_text=True)
    check("gone from config", "bench-1" not in ac.load_config(project)["agents"])
    check("gone from db", ac.get_agent(conn, "bench-1") is None)
    check("its task survives, unassigned", ac.get_task(conn, t_id)["assigned_to"] is None)
    check("said so", "1 task unassigned" in removed, removed[-200:])
    check("editor cleared out-of-band", 'id="agent-editor" hx-swap-oob="true"' in removed)
    check("prompt file kept", ac.prompt_path(project, "bench-1").exists())
    check("close empties the editor", c.get("/agents/close").get_data(as_text=True).strip() == '<div id="agent-editor"></div>')

    c.post("/agents/dev-agent/context", data={"content": "be terse"})
    check("prompt written to disk", ac.prompt_path(project, "dev-agent").read_text() == "be terse")
    check("editor reloads it", "be terse" in c.get("/agents/dev-agent/context").get_data(as_text=True))

    print("activity")
    mono = ac.Monologue(conn, "dev-agent", 1, quiet=True)
    mono.record("prompt", "Task 1: Ship it")
    mono.tool_call("Edit", {"file_path": "src/app.py", "old_string": "a" * 500})
    tail = c.get("/agents/activity").get_data(as_text=True)
    check("tail polls itself", 'hx-get="/agents/activity"' in tail)
    check("tail shows the tool", "Edit" in tail and "dev-agent" in tail)
    check("tail truncates", "a" * 400 not in tail)
    check("tail on the agents page", 'id="activity"' in c.get("/agents").get_data(as_text=True))
    detail = c.get("/tasks/1/detail").get_data(as_text=True)
    check("task log rendered", "Task 1: Ship it" in detail and "<details" in detail)
    check("full body available to expand", "a" * 400 in detail)
    check("log does not poll over the form", 'hx-trigger="every 3s"' not in detail, detail[:0])

    print("docs page")
    html = c.get("/docs").get_data(as_text=True)
    check("lists existing docs", "description" in html)
    created = c.post("/docs", data={"key": "architecture"}).get_data(as_text=True)
    check("create opens the editor", 'id="doc-editor"' in created and "architecture" in created)
    check("created in the db", ac.docs_get(conn, "architecture") == "")
    saved = c.post("/docs/architecture", data={"content": "One SQLite DB per project."}).get_data(as_text=True)
    check("content saved", ac.docs_get(conn, "architecture") == "One SQLite DB per project.")
    check("save returns just the table", 'id="docs"' in saved and 'id="doc-editor"' not in saved)
    check("editor reloads content", "One SQLite DB per project." in c.get("/docs/architecture").get_data(as_text=True))
    check("attributed to the human", [d for d in ac.docs_list(conn) if d["key"] == "architecture"][0]["updated_by"] == "human")
    c.post("/docs", data={"key": "architecture"})
    check("recreating does not wipe", ac.docs_get(conn, "architecture") == "One SQLite DB per project.")
    removed = c.post("/docs/architecture/delete").get_data(as_text=True)
    check("deleted", 'hx-get="/docs/architecture"' not in removed, removed)
    check("gone from the db", ac.docs_get(conn, "architecture") is None)
    check("swaps just the table", removed.count("hx-post=\"/docs\"") == 0, removed)

    print("data browser")
    for table in ("tasks", "agents", "messages", "docs", "events"):
        page = c.get(f"/data/{table}").get_data(as_text=True)
        check(f"{table} page renders", 'id="rows"' in page and "Insert row" in page)
    check("unknown table refused", "no such table" in c.get("/data/nope").get_data(as_text=True))

    inserted = c.post("/data/messages", data={
        "sender": "human", "recipient": "dev-agent", "msg_type": "note", "payload": "typed by hand", "task_id": "1",
    }).get_data(as_text=True)
    msg = [m for m in ac.task_messages(conn, 1) if m["payload"] == "typed by hand"]
    check("row inserted", len(msg) == 1, msg)
    check("insert stamps ts", msg[0]["ts"] > 0)
    check("numbers coerced, not strings", msg[0]["task_id"] == 1)
    check("toast reports the new pk", "inserted messages" in inserted)

    editor = c.get(f"/data/messages/row?pk={msg[0]['id']}").get_data(as_text=True)
    check("row editor prefilled", "typed by hand" in editor)
    check("pk is read-only", "read-only" in editor)
    c.post(f"/data/messages/row?pk={msg[0]['id']}", data={"payload": "edited by hand", "cost_usd": "0.25"})
    row = ac.get_task and [m for m in ac.task_messages(conn, 1) if m["id"] == msg[0]["id"]][0]
    check("row updated", row["payload"] == "edited by hand" and row["cost_usd"] == 0.25)
    check("row deleted", "row deleted" in c.post(f"/data/messages/delete?pk={msg[0]['id']}").get_data(as_text=True))
    check("really gone", not [m for m in ac.task_messages(conn, 1) if m["id"] == msg[0]["id"]])

    check("bad value reported, not raised", "not inserted" in c.post("/data/events", data={"task_id": "abc", "kind": "text"}).get_data(as_text=True))
    check("fk violation reported", "not inserted" in c.post("/data/tasks", data={"title": "x", "assigned_to": "ghost"}).get_data(as_text=True))
    check("missing row is harmless", c.get("/data/tasks/row?pk=9999").get_data(as_text=True).strip() == '<div id="row-editor"></div>')
    check("paging shown", "of " in c.get("/data/events/rows?offset=0").get_data(as_text=True))

    print("export + delete")
    msg = c.post("/export").get_data(as_text=True)
    check("export ran", "exported" in msg and (project / ".agents-export" / "tasks.md").exists())
    for task in ac.list_tasks(conn):
        last = c.post(f"/tasks/{task['id']}/delete").get_data(as_text=True)
    check("delete empties table", "No tasks yet." in last)
    check("gone from db", ac.list_tasks(conn) == [])
    check("missing task is harmless", c.get("/tasks/99/detail").get_data(as_text=True) == "")
    conn.close()


def test_mcp(project: Path) -> None:
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    print("mcp stdio server")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "achka", "--project", str(project), "mcp", "--agent", "codex-1"],
    )

    async def run() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                check("tools advertised", names == [s["name"] for s in ac.TOOL_SPECS], names)
                schema = next(t for t in tools.tools if t.name == "send_message").input_schema
                check("schema has required args", schema["required"] == ["recipient", "payload"])

                res = await session.call_tool("docs_set", {"key": "notes", "content": "from codex"})
                check("docs_set via mcp", not res.is_error, res.content)
                res = await session.call_tool("docs_get", {"key": "notes"})
                check("docs_get via mcp", "from codex" in res.content[0].text, res.content)

                conn = ac.connect(ac.db_path(project))
                check("attributed to caller", ac.docs_list(conn)[-1]["updated_by"] == "codex-1")
                ac.add_task(conn, "codex task", "do the thing", "codex-1")

                res = await session.call_tool("claim_task", {})
                check("claim via mcp", '"codex task"' in res.content[0].text, res.content)
                check("task in progress", ac.get_task(conn, 1)["status"] == "in_progress")
                await session.call_tool("reply", {"task_id": 1, "payload": "done", "cost_usd": 0.5})
                check("reply via mcp", ac.get_task(conn, 1)["status"] == "done")
                check("cost logged", ac.token_usage_by_agent(conn)[0]["cost_usd"] == 0.5)

                res = await session.call_tool("reply", {"payload": "missing task_id"})
                check("bad args are an error result", res.is_error)
                conn.close()

    anyio.run(run)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="achka-web-"))
    try:
        test_web(make_project(tmp))
        mcp_project = tmp / "mcpproject"
        (mcp_project / ".agents" / "prompts").mkdir(parents=True)
        ac.config_path(mcp_project).write_text('[agents.codex-1]\nbackend = "codex"\nrole = "second opinion"\n')
        test_mcp(mcp_project)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    main()
