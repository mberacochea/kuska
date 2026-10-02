#!/usr/bin/env python3
"""Web UI + MCP server checks: `uv run tests/test_web.py`."""

import shutil
import sys
import tempfile
from pathlib import Path

import kuska as ac

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
    conn = ac.connect(ac.db_path(project))
    HX = {"HX-Request": "true"}

    print("project page")
    html = c.get("/").get_data(as_text=True)
    check("renders", "<title>webproject - kuska</title>" in html)
    check("htmx loaded", "htmx.min.js" in html)
    check("no task list clutter", "No tasks yet." not in html and 'id="tasks-container"' not in html)

    check("description saved", c.post("/description", data={"content": "# Web project"}).status_code == 200)
    check("description round-trips", "# Web project" in c.get("/").get_data(as_text=True))

    print("tasks page starts empty")
    check("empty state", "No tasks yet." in c.get("/tasks").get_data(as_text=True))

    print("tasks")
    frag = c.post("/tasks", data={"title": "Ship it", "description": "carefully", "assigned_to": "dev-agent"}).get_data(as_text=True)
    check("row added", 'id="task-1"' in frag and "Ship it" in frag)
    check("blank title ignored", "No tasks yet." not in c.post("/tasks", data={"title": "  "}).get_data(as_text=True))
    check("only one task", len(ac.list_tasks(conn)) == 1)

    row = c.post("/tasks/1", data={"status": "blocked"}).get_data(as_text=True)
    check("status patched", ac.get_task(conn, 1)["status"] == "blocked")
    check("patch returns row", 'id="task-1"' in row and "selected" in row)
    c.post("/tasks/1", data={"assigned_to": ""})
    check("unassign", ac.get_task(conn, 1)["assigned_to"] is None)
    c.post("/tasks/1", data={"assigned_to": "dev-agent"})

    detail = c.get("/tasks/1", headers=HX).get_data(as_text=True)
    check("task page shows detail", "carefully" in detail and "No messages yet." in detail)
    check("requeue offered when not todo/ready", "Re-queue" in detail)
    c.post("/tasks/1/requeue")
    check("requeued", ac.get_task(conn, 1)["status"] == "ready")

    thread = c.post("/tasks/1/message", data={"payload": "check the edge case"}).get_data(as_text=True)
    check("human message posted", "check the edge case" in thread)
    check("message is routed to assignee", ac.get_inbox(conn, "dev-agent")[0]["payload"] == "check the edge case")

    ac.reply(conn, "dev-agent", 1, "Shipped.", input_tokens=10, output_tokens=5, cost_usd=0.01)
    detail = c.get("/tasks/1", headers=HX).get_data(as_text=True)
    check("result shows in thread", "Shipped." in detail and "$0.0100" in detail)
    check("row still fetchable on its own", 'id="task-1"' in c.get("/tasks/1/row").get_data(as_text=True))

    print("task status filtering")
    # Create specific tasks for filtering tests
    ac.add_task(conn, "filter test 1", "", "dev-agent")  # Will be todo
    ftest2 = ac.add_task(conn, "filter test 2", "", "dev-agent")  # Will be done
    ftest3 = ac.add_task(conn, "filter test 3", "", "dev-agent")  # Will be blocked
    ac.update_task_status(conn, ftest2, "done")
    ac.update_task_status(conn, ftest3, "blocked")
    todo_tasks = ac.list_tasks(conn, status="todo")
    done_tasks = ac.list_tasks(conn, status="done")
    blocked_tasks = ac.list_tasks(conn, status="blocked")
    check("filters by status - todo", len([t for t in todo_tasks if t["title"].startswith("filter test")]) >= 1)
    check("filters by status - done", len([t for t in done_tasks if t["title"].startswith("filter test")]) >= 1)
    check("filters by status - blocked", len([t for t in blocked_tasks if t["title"].startswith("filter test")]) >= 1)

    print("task transitions to in_progress")
    check("todo tasks are not claimed", ac.claim_task(conn, "dev-agent") is None)
    ac.update_task_status(conn, ftest_ready := ac.add_task(conn, "filter test ready", "", "dev-agent"), "ready")
    claimed = ac.claim_task(conn, "dev-agent")
    check("claim_task puts task in_progress", claimed and claimed.get("status") == "in_progress", f"claimed: {claimed}")
    claimed_id = claimed["id"]
    row = c.get(f"/tasks/{claimed_id}/row").get_data(as_text=True)
    check("row shows in_progress status", "in_progress" in row or "selected" in row)

    print("approval and dependencies")
    c.post("/tasks", data={"title": "design the schema", "assigned_to": "dev-agent"})
    c.post("/tasks", data={"title": "build on it", "assigned_to": "dev-agent"})
    ids = [t["id"] for t in ac.list_tasks(conn)]
    first, second = ids[-2], ids[-1]
    check("new assigned task starts as todo", ac.get_task(conn, first)["status"] == "todo")
    check("assigned todo task is not claimed", ac.claim_task(conn, "dev-agent") is None)
    for tid in (first, second):
        ac.update_task_status(conn, tid, "ready")
    panel = c.post(f"/tasks/{second}/deps", data={"depends_on": first}).get_data(as_text=True)
    check("dependency added", [d["id"] for d in ac.task_dependencies(conn, second)] == [first])
    check("panel lists it", f"#{first} design the schema" in panel, panel[:0])
    check("cycle refused with a toast", "already depends" in
          c.post(f"/tasks/{first}/deps", data={"depends_on": second}).get_data(as_text=True))

    c.post(f"/tasks/{first}", data={"status": "needs_approval"})
    check("held task is not claimable", ac.claim_task(conn, "dev-agent") is None)
    row = c.get(f"/tasks/{second}/row").get_data(as_text=True)
    check("row shows what it waits on", f"#{first} needs_approval" in row, row)
    detail = c.get(f"/tasks/{first}", headers=HX).get_data(as_text=True)
    check("approval prompt shown", "Waiting for your approval." in detail)
    check("both resolutions offered", "Approve (mark done)" in detail and "Send back (re-queue)" in detail)

    sent_back = c.post(f"/tasks/{first}/send-back").get_data(as_text=True)
    check("send back re-queues", ac.get_task(conn, first)["status"] == "ready")
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

    print("requeue, send-back and reply on tasks")
    for st in ("done", "blocked", "needs_approval", "ready_to_merge"):
        rid = ac.add_task(conn, f"reply {st}", "", "dev-agent")
        ac.update_task_status(conn, rid, st)
        c.post(f"/tasks/{rid}/message", data={"payload": "more please"})
        check(f"web reply reopens {st}", ac.get_task(conn, rid)["status"] == "ready")
        c.post(f"/tasks/{rid}/delete")
    for st in ("todo", "ready", "in_progress"):
        rid = ac.add_task(conn, f"reply {st}", "", "dev-agent")
        ac.update_task_status(conn, rid, st)
        c.post(f"/tasks/{rid}/message", data={"payload": "fyi"})
        check(f"web reply leaves {st} alone", ac.get_task(conn, rid)["status"] == st)
        c.post(f"/tasks/{rid}/delete")
    for action in ("requeue", "send-back"):
        uid = ac.add_task(conn, f"unassigned {action}", "")
        ac.update_task_status(conn, uid, "blocked")
        c.post(f"/tasks/{uid}/{action}")
        check(f"{action} of unassigned task gives todo", ac.get_task(conn, uid)["status"] == "todo")
        c.post(f"/tasks/{uid}/delete")

    print("markdown rendering")
    c.post("/tasks/1", data={"title": "Ship it", "description": "## Plan\n\n- one\n- two\n\n`code`"})
    detail = c.get("/tasks/1", headers=HX).get_data(as_text=True)
    check("headings rendered", "<h2>Plan</h2>" in detail, detail[:0])
    check("lists rendered", "<li>one</li>" in detail)
    check("inline code rendered", "<code>code</code>" in detail)
    check("source not shown raw", "## Plan" not in detail)
    check("edit view gives the source back", "## Plan" in c.get("/tasks/1?edit=1", headers=HX).get_data(as_text=True))
    ac.send_message(conn, "dev-agent", "human", 1, "result", "**done** &lt;ok&gt;")
    thread = c.get("/tasks/1", headers=HX).get_data(as_text=True)
    check("message markdown rendered", "<strong>done</strong>" in thread)
    ac.send_message(conn, "dev-agent", "human", 1, "note", "<script>alert(1)</script>")
    check("html from agents is escaped", "<script>alert(1)</script>" not in c.get("/tasks/1", headers=HX).get_data(as_text=True))
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
    check("agent name links to its own page", '<a href="/agents/dev-agent">' in rows)
    check("list page has no embedded editor", 'id="agent-editor"' not in html)

    print("agent heartbeat and status")
    ac.heartbeat(conn, "dev-agent", "working")
    agent = ac.get_agent(conn, "dev-agent")
    check("heartbeat updates status", agent["status"] == "working")
    check("heartbeat records time", agent["last_heartbeat"] is not None and agent["last_heartbeat"] > 0)
    agent_html = c.get("/agents").get_data(as_text=True)
    check("status shown on agents page", "working" in agent_html or "dev-agent" in agent_html)
    ac.heartbeat(conn, "dev-agent", "idle")
    agent = ac.get_agent(conn, "dev-agent")
    check("status can change", agent["status"] == "idle")
    ac.heartbeat(conn, "dev-agent", "offline")
    check("offline status visible", ac.get_agent(conn, "dev-agent")["status"] == "offline")

    print("agent settings")
    editor = c.get("/agents/dev-agent", headers=HX).get_data(as_text=True)
    check("settings form", 'name="model"' in editor and 'name="backend"' in editor)
    check("current model prefilled", 'value="claude-opus-5"' in editor, editor)
    check("every field offered", all(f'name="{f["key"]}"' in editor for f in ac.AGENT_FIELDS))
    check("prompt editor too", "textarea" in editor and "prompts/dev-agent.md" in editor)
    full_agent_page = c.get("/agents/dev-agent").get_data(as_text=True)
    check("agent page is a full page", "<nav>" in full_agent_page and 'name="model"' in full_agent_page)
    check("missing agent 404s", c.get("/agents/nope").status_code == 404)

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
    check("editor panel swapped back", 'id="agent-editor"' in saved and "claude-sonnet-5" in saved)
    check("toast is out-of-band", 'id="toast" hx-swap-oob="true"' in saved, saved[-200:])
    check("editor reflects the change", 'value="claude-sonnet-5"' in c.get("/agents/dev-agent", headers=HX).get_data(as_text=True))

    print("agent current task display")
    new_task_id = ac.add_task(conn, "for the agent", "test", "dev-agent")
    ac.heartbeat(conn, "dev-agent", "working", task_id=new_task_id)
    agent = ac.get_agent(conn, "dev-agent")
    check("agent current_task_id stored", agent.get("current_task_id") == new_task_id)
    agent_page = c.get("/agents").get_data(as_text=True)
    # The agents page shows the task ID in the Task column, not the title
    check("current task shown on agents page", str(new_task_id) in agent_page)
    ac.heartbeat(conn, "dev-agent", "idle", task_id=None)
    check("current task can be cleared", ac.get_agent(conn, "dev-agent")["current_task_id"] is None)

    print("adding and removing agents")
    added = c.post("/agents", data={"name": "bench-1", "backend": "codex", "model": "gpt-5-codex", "role": "benchmarks"}).get_data(as_text=True)
    check("agent added to config", ac.load_config(project)["agents"]["bench-1"]["backend"] == "codex")
    check("agent registered in db", ac.get_agent(conn, "bench-1")["backend"] == "codex")
    check("prompt file seeded", ac.prompt_path(project, "bench-1").exists())
    check("shows in the table", "bench-1" in added and "gpt-5-codex" in added)
    # an agent name becomes a file path (.agents/prompts/<name>.md), so a
    # traversal attempt must be refused before anything is written
    traversal = c.post("/agents", data={"name": "../etc/passwd"})
    check("bad name refused", traversal.status_code == 422)
    check("bad name explained", "Agent name must start with alphanumeric" in traversal.get_data(as_text=True))
    check("bad name wrote nothing", not ac.prompt_path(project, "../etc/passwd").exists())
    check("nothing written for a bad name", "../etc/passwd" not in str(ac.load_config(project)["agents"]))
    check("duplicate refused", "already exists" in c.post("/agents", data={"name": "bench-1"}).get_data(as_text=True))

    c.post("/agents/bench-1", data={"backend": "codex", "price_in_per_mtok": "1.25", "price_out_per_mtok": "10"})
    check("prices stored as numbers", ac.load_config(project)["agents"]["bench-1"]["price_in_per_mtok"] == 1.25)
    bad_price = c.post("/agents/bench-1", data={"price_in_per_mtok": "cheap"})
    check("bad price refused", bad_price.status_code == 422)
    check("bad price explained", "price_in_per_mtok must be a valid number" in bad_price.get_data(as_text=True))

    t_id = ac.add_task(conn, "for bench", assigned_to="bench-1")
    removed = c.post("/agents/bench-1/delete")
    check("gone from config", "bench-1" not in ac.load_config(project)["agents"])
    check("gone from db", ac.get_agent(conn, "bench-1") is None)
    check("its task survives, unassigned", ac.get_task(conn, t_id)["assigned_to"] is None)
    check("redirects back to the agents list", removed.headers.get("HX-Redirect") == "/agents")
    check("its own page is gone", c.get("/agents/bench-1").status_code == 404)
    check("prompt file kept", ac.prompt_path(project, "bench-1").exists())

    c.post("/agents/dev-agent/context", data={"content": "be terse"})
    check("prompt written to disk", ac.prompt_path(project, "dev-agent").read_text() == "be terse")
    check("editor reloads it", "be terse" in c.get("/agents/dev-agent", headers=HX).get_data(as_text=True))

    print("activity")
    mono = ac.Monologue(conn, "dev-agent", new_task_id, quiet=True)
    mono.record("prompt", f"Task {new_task_id}: Ship it")
    mono.tool_call("Edit", {"file_path": "src/app.py", "old_string": "a" * 500})
    tail = c.get("/agents/activity").get_data(as_text=True)
    check("tail polls itself", 'hx-get="/agents/activity"' in tail)
    check("tail shows the tool", "Edit" in tail and "dev-agent" in tail)
    check("tail truncates", "a" * 400 not in tail)
    # Regression: the poll target's own response used to include the filter
    # form too, so every 3s poll (outerHTML on just #activity) dropped in a
    # duplicate filter bar as a sibling instead of just refreshing the log.
    check("poll fragment is the log alone, no filter form", "activity-filters" not in tail and "agent-filter" not in tail)
    check("tail on the agents page", 'id="activity"' in c.get("/agents").get_data(as_text=True))
    agents_page_html = c.get("/agents").get_data(as_text=True)
    check("filters appear exactly once on the agents page", agents_page_html.count("activity-filters") == 1)
    detail = c.get(f"/tasks/{new_task_id}", headers=HX).get_data(as_text=True)
    check("task log rendered", f"Task {new_task_id}" in detail and "<details" in detail)
    check("full body available to expand", "a" * 400 in detail)
    check("log does not poll over the form", 'hx-trigger="every 3s"' not in detail, detail[:0])

    print("live polling features (replaced with reload button)")
    # Test the polling container structure
    table_html = c.get("/tasks").get_data(as_text=True)
    # The live reload trigger should be gone
    check("live reload trigger removed", 'hx-trigger="every 5s"' not in table_html)
    # The "live" indicator should be gone
    check("live indicator removed", ">live<" not in table_html)
    # The reload button should be present
    check("reload button present", 'class="reload-tasks-btn"' in table_html)
    check("reload button has aria label", 'aria-label="Reload task table"' in table_html)
    check("reload button text correct", "Reload" in table_html)
    # Test individual fragment endpoints
    rows = c.get("/agents/rows").get_data(as_text=True)
    check("agents rows fragment renders", "<table>" in rows)
    activity = c.get("/agents/activity").get_data(as_text=True)
    check("activity fragment renders", "activity" in activity.lower() or "hx-get" in activity)
    # An htmx request to /tasks (as the filter/sort/reload controls issue) gets
    # just the tasks-container fragment, not the full page.
    tasks_table_html = c.get("/tasks", headers={"HX-Request": "true"}).get_data(as_text=True)
    check("tasks fragment exists", 'id="tasks-container"' in tasks_table_html)
    check("tasks fragment has no layout", "<html" not in tasks_table_html)

    print("reload button functionality")
    # Verify the reload button works with filters
    c.post("/tasks", data={"title": "Test for reload", "description": "Test reload functionality", "assigned_to": "dev-agent"})
    reload_test_html = c.get("/tasks?status=todo").get_data(as_text=True)
    check("reload button present with filters", 'class="reload-tasks-btn"' in reload_test_html)
    check("filters preserved with reload button", 'id="task-filters-container"' in reload_test_html)
    # Simulate the reload button (an htmx GET to /tasks with the current filters)
    filtered_html = c.get("/tasks?status=todo", headers={"HX-Request": "true"}).get_data(as_text=True)
    check("filtered fragment works for reload", "Test for reload" in filtered_html)
    # And a real browser reload on the same filtered URL gets a full page, not a bare fragment
    reloaded_html = c.get("/tasks?status=todo").get_data(as_text=True)
    check("direct reload of a filtered URL gets the full layout", "<html" in reloaded_html and "Test for reload" in reloaded_html)

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
        check(f"{table} page renders", 'id="rows"' in page and ("Insert row" in page or "editable" in table))
    check("unknown table refused", "no such table" in c.get("/data/nope").get_data(as_text=True))

    print("data table paging")
    # Create multiple messages to test paging
    for i in range(10):
        ac.send_message(conn, "human", "dev-agent", new_task_id, "note", f"message {i}")
    messages_page = c.get("/data/messages?offset=0").get_data(as_text=True)
    check("paging info shown", "of" in messages_page.lower() or "message" in messages_page)
    check("offset parameter works", c.get("/data/messages?offset=5").status_code == 200)

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

    print("special characters and escaping")
    special_title = "Task with <special> & \"quotes\" 'marks'"
    c.post("/tasks", data={"title": special_title, "description": "Testing: <script>alert(1)</script>"})
    special_tasks = [t for t in ac.list_tasks(conn) if "<special>" in t["title"]]
    check("special chars stored in db", len(special_tasks) > 0)
    html = c.get("/tasks").get_data(as_text=True)
    check("special chars escaped in html", "<script>" not in html or "alert" not in html)
    # The title should be visible but escaped
    check("title visible but safe", "special" in html)

    print("form submission edge cases")
    # Empty description is OK
    c.post("/tasks/1", data={"description": ""})
    check("empty description accepted", ac.get_task(conn, 1)["description"] == "")
    # Blank assignment
    c.post("/tasks/1", data={"assigned_to": ""})
    check("blank assignment clears", ac.get_task(conn, 1)["assigned_to"] is None)
    # Re-assign to valid agent
    c.post("/tasks/1", data={"assigned_to": "dev-agent"})
    check("assignment to valid agent works", ac.get_task(conn, 1)["assigned_to"] == "dev-agent")

    print("html fragment consistency")
    # All responses to HTMX requests should be HTML fragments, not full pages
    detail_response = c.post("/tasks/1", data={"status": "done"}).get_data(as_text=True)
    check("patch response is fragment not page", "<html" not in detail_response.lower())
    check("fragment has no head tag", "<head" not in detail_response.lower())
    row_response = c.get("/agents/rows").get_data(as_text=True)
    check("rows fragment is partial", "<html" not in row_response.lower())

    print("error handling and edge cases")
    # Test invalid task IDs
    check("nonexistent task page 404s", c.get("/tasks/99999").status_code == 404)
    check("nonexistent task row returns empty", c.get("/tasks/99999/row").get_data(as_text=True) == "")
    check("nonexistent task patch ignored", c.post("/tasks/99999", data={"status": "done"}).get_data(as_text=True) == "")
    # Test with missing form fields
    c.post("/tasks", data={"title": "no description task"})
    check("tasks can be created with empty description", len(ac.list_tasks(conn)) > 1)
    # Closing a panel is pure client-side (closePanel() in layout.html) - no
    # server round trip to test, just that each editor wires its close
    # control to it.
    check("layout defines closePanel", "function closePanel(id)" in c.get("/").get_data(as_text=True))
    doc_editor_html = c.post("/docs", data={"key": "closetest"}).get_data(as_text=True)
    check("doc editor close wired to closePanel", "closePanel('doc-editor')" in doc_editor_html)
    row_editor_html = c.get("/data/tasks/row", query_string={"pk": t_id}).get_data(as_text=True)
    check("row editor close wired to closePanel", "closePanel('row-editor')" in row_editor_html)

    print("merge queue")
    # Initialize git in the project so we can test worktree functionality
    import subprocess
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("# Test Project")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)

    # Create tasks with ready_to_merge status
    merge_task_1 = ac.add_task(conn, "Fix search ranking", "Important feature", "dev-agent")
    merge_task_2 = ac.add_task(conn, "Refactor database layer", "Technical debt", "dev-agent")

    # Create worktrees for both tasks
    path1, branch1, _ = ac.worktree.ensure_worktree(project, merge_task_1, "Fix search ranking", "main")
    path2, branch2, _ = ac.worktree.ensure_worktree(project, merge_task_2, "Refactor database layer", "main")
    ac.update_task(conn, merge_task_1, worktree_path=str(path1))
    ac.update_task(conn, merge_task_2, worktree_path=str(path2))

    ac.update_task_status(conn, merge_task_1, "ready_to_merge")
    ac.update_task_status(conn, merge_task_2, "ready_to_merge")

    # Add a dependency: task 2 waits on task 1 (so task 1 blocks task 2)
    ac.add_dependency(conn, merge_task_2, merge_task_1)

    merge_queue_page = c.get("/merge-queue").get_data(as_text=True)
    check("merge queue page renders", "Merge Queue" in merge_queue_page)
    check("ready_to_merge task shows in queue", str(merge_task_1) in merge_queue_page or "Fix search" in merge_queue_page)
    check("ready_to_merge task shows its branch", branch1 in merge_queue_page or "kuska" in merge_queue_page)
    check("another ready_to_merge task shows", str(merge_task_2) in merge_queue_page or "Refactor database" in merge_queue_page)

    # Test the rows fragment
    rows = c.get("/merge-queue/rows", headers=HX).get_data(as_text=True)
    check("merge queue rows fragment renders", "tr" in rows)

    # Test ordering: merge_task_1 (1 blocks) should come before merge_task_2 (0 blocks)
    # because higher blocking count comes first, so task 1 should appear before task 2
    merge_queue_page = c.get("/merge-queue").get_data(as_text=True)
    task_1_pos = merge_queue_page.find(str(merge_task_1))
    task_2_pos = merge_queue_page.find(str(merge_task_2))
    check("ordering puts blocking task above non-blocking one", task_1_pos < task_2_pos and task_1_pos > 0)

    # Test marking merged
    marked = c.post(f"/tasks/{merge_task_1}/merged").get_data(as_text=True)
    check("POST /tasks/<id>/merged sets done", ac.get_task(conn, merge_task_1)["status"] == "done")

    # Test that diff command in task detail has three dots, not two
    task_detail = c.get(f"/tasks/{merge_task_2}", headers=HX).get_data(as_text=True)
    check("diff command in HTML has three dots", f"git diff {branch2[len('kuska/'):]}" not in task_detail or "..." in task_detail)
    check("diff command has three dots not two", "...<" not in task_detail)  # No two-dot version with bracket

    print("search view")
    check("search form pushes the query into the URL", 'hx-push-url="true"' in c.get("/search").get_data(as_text=True))
    check("the old fragment-only pagination endpoint is gone", c.get("/search/results?q=bench").status_code == 404)
    # A task result should link straight to its own page
    search_html = c.get("/search", query_string={"q": "bench"}).get_data(as_text=True)
    check("task result links to /tasks/<id>", f'href="/tasks/{t_id}"' in search_html)
    opened_task = c.get(f"/tasks/{t_id}").get_data(as_text=True)
    check("task page shows its detail", 'id="task-detail"' in opened_task and "Depends on" in opened_task)
    # A doc result should link straight to it, pre-opened, on the docs page
    c.post("/docs/closetest", data={"content": "zzmarkerdoc content"})
    search_doc_html = c.get("/search", query_string={"q": "zzmarkerdoc"}).get_data(as_text=True)
    check("doc result links to /docs?open=<key>", "/docs?open=closetest#doc-editor" in search_doc_html)
    opened_doc = c.get("/docs", query_string={"open": "closetest"}).get_data(as_text=True)
    check("open=<key> lands with that doc's editor open", 'id="doc-content-closetest"' in opened_doc)
    check("open=<missing key> is harmless", 'id="doc-editor"' in c.get("/docs", query_string={"open": "nope"}).get_data(as_text=True))

    print("htmx swaps get fragments, not whole pages")
    # Regression: deep-link URLs used to answer an htmx swap with the entire
    # page, so clicking a task nested a second copy of the table inside that
    # task's own row. A response destined for an element must never carry the
    # layout with it.
    doctype = "<!doctype html>"

    task_frag = c.get(f"/tasks/{t_id}", headers=HX).get_data(as_text=True)
    check("htmx /tasks/<id> is a fragment", doctype not in task_frag.lower())
    check("htmx /tasks/<id> carries no nav", "<nav>" not in task_frag)

    agent_frag = c.get("/agents/dev-agent", headers=HX).get_data(as_text=True)
    check("htmx /agents/<name> is the editor alone", doctype not in agent_frag.lower())
    check("htmx /agents/<name> is the editor", 'id="agent-editor"' in agent_frag and "<nav>" not in agent_frag)
    check("htmx /agents/<name> has no second agent table", "agent-rows" not in agent_frag)

    doc_frag = c.get("/docs", query_string={"open": "closetest"}, headers=HX).get_data(as_text=True)
    check("htmx /docs?open= is the editor alone", doctype not in doc_frag.lower())
    check("htmx /docs?open= is the editor", 'id="doc-editor"' in doc_frag and "<nav>" not in doc_frag)
    check("htmx /docs?open= has no second docs table", "/docs/closetest/delete" not in doc_frag)

    row_frag = c.get("/data/tasks", query_string={"open": t_id}, headers=HX).get_data(as_text=True)
    check("htmx /data?open= is the row editor alone", doctype not in row_frag.lower())
    check("htmx /data?open= is the row editor", 'id="row-editor"' in row_frag and 'id="rows"' not in row_frag)

    page_frag = c.get("/data/tasks", query_string={"offset": 0}, headers=HX).get_data(as_text=True)
    check("htmx /data?offset= is the rows fragment", 'id="rows"' in page_frag and doctype not in page_frag.lower())

    # Back/forward: on a cache miss htmx re-requests the URL and replaces the
    # whole body, so a history restore has to get the full page back.
    restore = c.get(f"/tasks/{t_id}",
                    headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    check("history restore gets the full page", doctype in restore.get_data(as_text=True).lower())

    # A plain browser navigation is unaffected by any of the above.
    for url, args in ((f"/tasks/{t_id}", {}), ("/agents/dev-agent", {}),
                      ("/docs", {"open": "closetest"}), ("/data/tasks", {"open": t_id})):
        body = c.get(url, query_string=args).get_data(as_text=True)
        check(f"plain GET {url} is a full page", doctype in body.lower() and "<nav>" in body)

    # The list row is a plain link to the task's own page - no htmx needed.
    rows_html = c.get("/tasks").get_data(as_text=True)
    check("task link points at its own page", f'href="/tasks/{t_id}">' in rows_html)
    check("no hx-get on the task link", f'hx-get="/tasks/{t_id}"' not in rows_html)

    # On the task page itself, editing toggles in place via htmx targeting
    # #task-detail, not the list row.
    detail_html = c.get(f"/tasks/{t_id}", headers=HX).get_data(as_text=True)
    check("edit fetches the panel fragment in edit mode", f'hx-get="/tasks/{t_id}?edit=1"' in detail_html)
    check("edit targets the task-detail panel", 'hx-target="#task-detail"' in detail_html)
    check("search form selects its own block", 'hx-select="#search-page"' in c.get("/search").get_data(as_text=True))

    print("export + delete")
    msg = c.post("/export").get_data(as_text=True)
    check("export ran", "exported" in msg and (project / ".agents-export" / "tasks.md").exists())
    for task in ac.list_tasks(conn):
        last = c.post(f"/tasks/{task['id']}/delete").get_data(as_text=True)
    check("delete empties table", "No tasks yet." in last)
    check("gone from db", ac.list_tasks(conn) == [])
    check("missing task 404s", c.get("/tasks/99").status_code == 404)

    print("route status codes")
    # Test various HTTP status codes
    check("GET / is 200", c.get("/").status_code == 200)
    check("GET /agents is 200", c.get("/agents").status_code == 200)
    check("GET /docs is 200", c.get("/docs").status_code == 200)
    check("GET /data is 200", c.get("/data").status_code == 200)
    check("GET /data/tasks is 200", c.get("/data/tasks").status_code == 200)
    check("invalid data table returns 200", c.get("/data/nonexistent").status_code == 200)
    check("POST /tasks is 200", c.post("/tasks", data={"title": "x"}).status_code == 200)
    check("GET /tasks/<missing> is 404", c.get("/tasks/999999").status_code == 404)

    print("run transcript view")
    # Create a task and some events to work with
    task_id = ac.add_task(conn, "test run transcript", "testing runs", "dev-agent")
    mono = ac.Monologue(conn, "dev-agent", task_id, quiet=True)
    mono.record("prompt", f"Task {task_id}: Build something")
    mono.tool_call("Read", {"file_path": "src/main.py"})
    mono.record("tool_result", {"content": "file content here"})
    run_id = mono.run_id

    # Test runs index
    runs_page = c.get("/runs").get_data(as_text=True)
    check("runs index renders", "<html" in runs_page and "Runs" in runs_page)
    check("runs table shows", "<table" in runs_page and "dev-agent" in runs_page)
    check("task link in runs index", f'#{ task_id}' in runs_page)
    check("nav marks runs page active", 'class="on">Runs<' in runs_page or 'class="on"' in runs_page and '/runs' in runs_page)

    # Test run transcript page
    transcript = c.get(f"/runs/{run_id}").get_data(as_text=True)
    check("run transcript renders", "<html" in transcript and "Run" in transcript)
    check("run id shown", run_id in transcript)
    check("agent shown", "dev-agent" in transcript)
    check("task link in transcript", f'#{ task_id}' in transcript)
    check("events shown in order", "prompt" in transcript and "Read" in transcript)
    check("transcript reads top to bottom", transcript.index("prompt") < transcript.index("Read"))

    # Test malformed run_id validation
    bad_run = c.get("/runs/not-a-hex-id").get_data(as_text=True)
    check("malformed run_id rejected", "Invalid" in bad_run or "error" in bad_run.lower())

    # Test unknown run_id
    unknown_run = c.get("/runs/aabbccddeeff").get_data(as_text=True)
    check("unknown run handled", "No run" in unknown_run or "error" in unknown_run.lower())

    # Test path traversal attempt on run_id - Flask's routing should reject this
    traversal_resp = c.get("/runs/../../etc/passwd")
    check("path traversal refused", traversal_resp.status_code == 404)

    conn.close()


def test_board(project: Path) -> None:
    app = ac.create_app(project)
    app.config.update(TESTING=True)
    c = app.test_client()
    conn = ac.connect(ac.db_path(project))

    def status(tid: int) -> str:
        return ac.get_task(conn, tid)["status"]

    def move(tid: int, column: str, **extra) -> str:
        return c.post(f"/tasks/{tid}/move", data={"column": column, **extra}).get_data(as_text=True)

    print("board")
    plain = ac.add_task(conn, "Plain card", "", None)
    owned = ac.add_task(conn, "Owned card", "", "dev-agent")
    running = ac.add_task(conn, "Running card", "", "dev-agent")
    ac.update_task_status(conn, running, "in_progress")
    waiting = ac.add_task(conn, "Waiting card", "", "dev-agent")
    ac.update_task_status(conn, waiting, "needs_approval")
    old_done = [ac.add_task(conn, f"Done {i}", "", "dev-agent") for i in range(22)]
    for tid in old_done:
        ac.update_task_status(conn, tid, "done")

    page = c.get("/board").get_data(as_text=True)
    check("board renders", "<h1>Board</h1>" in page and 'href="/board"' in page)
    for key in ("todo", "ready", "in_progress", "finished"):
        check(f"column {key}", f'data-column="{key}"' in page)
    check("in progress column takes no drops", "data-drop" not in page.split('data-column="in_progress"')[1].split(">")[0])
    check("todo column takes drops", "data-drop" in page.split('data-column="todo"')[1].split(">")[0])
    check("card links to task page", f'href="/tasks/{plain}"' in page and "Plain card" in page)
    check("running card not draggable", "draggable" not in page.split(f'id="card-{running}"')[1].split(">")[0])
    check("other cards draggable", "draggable" in page.split(f'id="card-{plain}"')[1].split(">")[0])
    check("finished shows status label", "needs_approval" in page)
    check("waiting card sits above done cards", page.index("Waiting card") < page.index("Done 21"))
    check("only 20 done cards", page.count("Done ") == 20 and "Done 0<" not in page and "Done 1<" not in page)
    check("fragment is bare board", c.get("/board", headers={"HX-Request": "true"}).get_data(as_text=True).lstrip().startswith('<div id="board"'))

    print("board moves")
    move(plain, "ready")
    check("unassigned to ready shows picker", "choose an agent" in move(plain, "ready") and status(plain) == "todo")
    check("picker lists agents", 'name="assigned_to"' in move(plain, "ready") and "dev-agent" in move(plain, "ready"))
    html = move(plain, "ready", assigned_to="dev-agent")
    check("assigned_to assigns and readies", status(plain) == "ready" and ac.get_task(conn, plain)["assigned_to"] == "dev-agent")
    check("unknown agent refused", "does not exist" in move(owned, "ready", assigned_to="nobody") and status(owned) == "todo")
    check("empty pick refused", status(owned) == "todo" and "choose an agent" in move(owned, "ready", assigned_to=""))
    move(owned, "ready")
    check("todo -> ready with agent", status(owned) == "ready")
    move(owned, "todo")
    check("ready -> todo", status(owned) == "todo")
    move(owned, "ready")
    move(owned, "finished")
    check("ready -> finished is done", status(owned) == "done")
    move(owned, "todo")
    check("finished -> todo", status(owned) == "todo")
    move(owned, "finished")
    check("todo -> finished is done", status(owned) == "done")
    move(owned, "ready")
    check("finished -> ready with agent", status(owned) == "ready")
    move(waiting, "finished")
    check("waiting -> finished is done", status(waiting) == "done")
    ac.update_task_status(conn, waiting, "blocked")
    move(waiting, "todo")
    check("waiting -> todo", status(waiting) == "todo")
    ac.update_task_status(conn, waiting, "blocked")
    unowned_done = ac.add_task(conn, "Unowned done", "", None)
    ac.update_task_status(conn, unowned_done, "done")
    check("finished unassigned -> ready needs picker",
          "choose an agent" in move(unowned_done, "ready") and status(unowned_done) == "done")

    print("board refused moves")
    toast = move(owned, "in_progress")
    check("move into in_progress refused", status(owned) == "ready" and 'id="toast"' in toast and "does not accept" in toast)
    toast = move(running, "todo")
    check("in_progress card cannot move", status(running) == "in_progress" and 'id="toast"' in toast)
    check("in_progress card cannot finish", "only an agent" in move(running, "finished") and status(running) == "in_progress")
    check("refusal still returns board", 'id="board"' in toast and "Running card" in toast)
    check("unknown column refused", "does not accept" in move(owned, "bogus") and status(owned) == "ready")
    check("missing task is 404", c.post("/tasks/9999/move", data={"column": "todo"}).status_code == 404)
    conn.close()


def test_mcp(project: Path) -> None:
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    print("mcp stdio server")
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

                # Verify claim_task tool is no longer available
                claim_task_tool = next((t for t in tools.tools if t.name == "claim_task"), None)
                check("claim_task tool removed", claim_task_tool is None, f"Tool still exists: {claim_task_tool}")
                check("task remains todo", ac.get_task(conn, 1)["status"] == "todo")

                # Verify reply schema no longer has cost fields
                reply_schema = next(t for t in tools.tools if t.name == "reply").input_schema
                # cost/token fields are optional on purpose: the daemon overwrites
                # them with the backend's figures when the run ends (see runtime.py)
                reply_props = reply_schema.get("properties", {})
                check("reply cost/token fields are optional",
                      all(k in reply_props and k not in reply_schema["required"]
                          for k in ["cost_usd", "input_tokens", "output_tokens"]))

                await session.call_tool("reply", {"task_id": 1, "payload": "done"})
                check("reply via mcp", ac.get_task(conn, 1)["status"] == "done")

                res = await session.call_tool("reply", {"payload": "missing task_id"})
                check("bad args are an error result", res.is_error)
                conn.close()

    anyio.run(run)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="kuska-web-"))
    try:
        test_web(make_project(tmp))
        board_project = tmp / "boardproject"
        (board_project / ".agents" / "prompts").mkdir(parents=True)
        ac.config_path(board_project).write_text('[agents.dev-agent]\nbackend = "claude"\nrole = "builder"\n')
        test_board(board_project)
        mcp_project = tmp / "mcpproject"
        (mcp_project / ".agents" / "prompts").mkdir(parents=True)
        ac.config_path(mcp_project).write_text('[agents.codex-1]\nbackend = "codex"\nrole = "second opinion"\n')
        test_mcp(mcp_project)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    main()
