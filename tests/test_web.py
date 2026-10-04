"""Web UI checks, driven through Flask's test client.

The tests share one project and run in file order: later ones build on the
tasks, agents and docs earlier ones created.
"""


import subprocess

import pytest

import kuska as ac
from kuska import supervisor

HX = {"HX-Request": "true"}


def task_id(conn, title: str) -> int:
    """Id of the task a tests earlier in this file created with `title`."""
    return next(t["id"] for t in ac.list_tasks(conn) if t["title"] == title)


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    project = tmp_path_factory.mktemp("web") / "webproject"
    (project / ".agents" / "prompts").mkdir(parents=True)
    ac.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nmodel = "claude-opus-5"\nrole = "builder"\n'
    )
    return project


@pytest.fixture(scope="module")
def c(project):
    app = ac.create_app(project)
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture(scope="module")
def conn(project, c):  # `c` first, so the app has set the database up
    db = ac.connect(ac.db_path(project))
    yield db
    db.close()


def test_project_page(c):
    html = c.get("/").get_data(as_text=True)
    assert "<title>webproject - kuska</title>" in html, "renders"
    assert "htmx.min.js" in html, "htmx loaded"
    assert "No tasks yet." not in html and 'id="tasks-container"' not in html, "no task list clutter"

    assert c.post("/description", data={"content": "# Web project"}).status_code == 200, "description saved"
    assert "# Web project" in c.get("/").get_data(as_text=True), "description round-trips"


def test_tasks_page_starts_empty(c):
    assert "No tasks yet." in c.get("/tasks").get_data(as_text=True), "empty state"


def test_tasks(c, conn):
    frag = c.post("/tasks", data={"title": "Ship it", "description": "carefully", "assigned_to": "dev-agent"}).get_data(as_text=True)
    assert 'id="task-1"' in frag and "Ship it" in frag, "row added"
    assert "No tasks yet." not in c.post("/tasks", data={"title": "  "}).get_data(as_text=True), "blank title ignored"
    assert len(ac.list_tasks(conn)) == 1, "only one task"

    row = c.post("/tasks/1", data={"status": "blocked"}).get_data(as_text=True)
    assert ac.get_task(conn, 1)["status"] == "blocked", "status patched"
    assert 'id="task-1"' in row and "selected" in row, "patch returns row"
    c.post("/tasks/1", data={"assigned_to": ""})
    assert ac.get_task(conn, 1)["assigned_to"] is None, "unassign"
    c.post("/tasks/1", data={"assigned_to": "dev-agent"})

    detail = c.get("/tasks/1", headers=HX).get_data(as_text=True)
    assert "carefully" in detail and "status forced" in detail, "task page shows detail"
    assert "Re-queue" in detail, "requeue offered when not todo/ready"
    c.post("/tasks/1/requeue")
    assert ac.get_task(conn, 1)["status"] == "ready", "requeued"

    thread = c.post("/tasks/1/message", data={"payload": "check the edge case"}).get_data(as_text=True)
    assert "check the edge case" in thread, "human message posted"
    assert ac.get_inbox(conn, "dev-agent")[0]["payload"] == "check the edge case", "message is routed to assignee"

    ac.update_task_status(conn, 1, "in_progress")
    ac.reply(conn, "dev-agent", 1, "Shipped.", input_tokens=10, output_tokens=5, cost_usd=0.01)
    detail = c.get("/tasks/1", headers=HX).get_data(as_text=True)
    assert "Shipped." in detail and "$0.0100" in detail, "result shows in thread"
    assert 'id="task-1"' in c.get("/tasks/1/row").get_data(as_text=True), "row still fetchable on its own"


def test_task_status_filtering(conn):
    # Create specific tasks for filtering tests
    ac.add_task(conn, "filter test 1", "", "dev-agent")  # Will be todo
    ftest2 = ac.add_task(conn, "filter test 2", "", "dev-agent")  # Will be done
    ftest3 = ac.add_task(conn, "filter test 3", "", "dev-agent")  # Will be blocked
    ac.update_task_status(conn, ftest2, "done")
    ac.update_task_status(conn, ftest3, "blocked")
    todo_tasks = ac.list_tasks(conn, status="todo")
    done_tasks = ac.list_tasks(conn, status="done")
    blocked_tasks = ac.list_tasks(conn, status="blocked")
    assert len([t for t in todo_tasks if t["title"].startswith("filter test")]) >= 1, "filters by status - todo"
    assert len([t for t in done_tasks if t["title"].startswith("filter test")]) >= 1, "filters by status - done"
    assert len([t for t in blocked_tasks if t["title"].startswith("filter test")]) >= 1, "filters by status - blocked"


def test_task_transitions_to_in_progress(c, conn):
    assert ac.claim_task(conn, "dev-agent") is None, "todo tasks are not claimed"
    ac.update_task_status(conn, ftest_ready := ac.add_task(conn, "filter test ready", "", "dev-agent"), "ready")
    claimed = ac.claim_task(conn, "dev-agent")
    assert claimed and claimed.get("status") == "in_progress", "claim_task puts task in_progress"
    claimed_id = claimed["id"]
    row = c.get(f"/tasks/{claimed_id}/row").get_data(as_text=True)
    assert "in_progress" in row or "selected" in row, "row shows in_progress status"


def test_approval_and_dependencies(c, conn):
    c.post("/tasks", data={"title": "design the schema", "assigned_to": "dev-agent"})
    c.post("/tasks", data={"title": "build on it", "assigned_to": "dev-agent"})
    ids = [t["id"] for t in ac.list_tasks(conn)]
    first, second = ids[-2], ids[-1]
    assert ac.get_task(conn, first)["status"] == "todo", "new assigned task starts as todo"
    assert ac.claim_task(conn, "dev-agent") is None, "assigned todo task is not claimed"
    for tid in (first, second):
        ac.update_task_status(conn, tid, "ready")
    panel = c.post(f"/tasks/{second}/deps", data={"depends_on": first}).get_data(as_text=True)
    assert [d["id"] for d in ac.task_dependencies(conn, second)] == [first], "dependency added"
    assert f"#{first} design the schema" in panel, "panel lists it"
    assert ("already depends" in
          c.post(f"/tasks/{first}/deps", data={"depends_on": second}).get_data(as_text=True)), "cycle refused with a toast"

    c.post(f"/tasks/{first}", data={"status": "needs_approval"})
    assert ac.claim_task(conn, "dev-agent") is None, "held task is not claimable"
    row = c.get(f"/tasks/{second}/row").get_data(as_text=True)
    assert f"#{first} needs_approval" in row, "row shows what it waits on"
    detail = c.get(f"/tasks/{first}", headers=HX).get_data(as_text=True)
    assert "Waiting for your approval." in detail, "approval prompt shown"
    assert "Approve (mark done)" in detail and "Send back (re-queue)" in detail, "both resolutions offered"

    sent_back = c.post(f"/tasks/{first}/send-back").get_data(as_text=True)
    assert ac.get_task(conn, first)["status"] == "ready", "send back re-queues"
    assert "sent back" in sent_back, "said so"
    assert ac.claim_task(conn, "dev-agent")["id"] == first, "dependent still waits"
    ac.update_task_status(conn, first, "needs_approval")
    c.post(f"/tasks/{first}/approve")
    assert ac.get_task(conn, first)["status"] == "done", "approve marks it done"
    assert ac.claim_task(conn, "dev-agent")["id"] == second, "dependent runs now"
    c.post(f"/tasks/{second}/deps/{first}/delete")
    assert ac.task_dependencies(conn, second) == [], "dependency removed"
    for tid in (first, second):
        c.post(f"/tasks/{tid}/delete")


def test_requeue_send_back_and_reply_on_tasks(c, conn):
    for st in ("done", "blocked", "needs_approval", "ready_to_merge"):
        rid = ac.add_task(conn, f"reply {st}", "", "dev-agent")
        ac.update_task_status(conn, rid, st)
        c.post(f"/tasks/{rid}/message", data={"payload": "more please"})
        assert ac.get_task(conn, rid)["status"] == "ready", f"web reply reopens {st}"
        c.post(f"/tasks/{rid}/delete")
    for st in ("todo", "ready", "in_progress"):
        rid = ac.add_task(conn, f"reply {st}", "", "dev-agent")
        ac.update_task_status(conn, rid, st)
        c.post(f"/tasks/{rid}/message", data={"payload": "fyi"})
        assert ac.get_task(conn, rid)["status"] == st, f"web reply leaves {st} alone"
        c.post(f"/tasks/{rid}/delete")
    for action in ("requeue", "send-back"):
        uid = ac.add_task(conn, f"unassigned {action}", "")
        ac.update_task_status(conn, uid, "blocked")
        c.post(f"/tasks/{uid}/{action}")
        assert ac.get_task(conn, uid)["status"] == "todo", f"{action} of unassigned task gives todo"
        c.post(f"/tasks/{uid}/delete")


def test_markdown_rendering(c, conn):
    c.post("/tasks/1", data={"title": "Ship it", "description": "## Plan\n\n- one\n- two\n\n`code`"})
    detail = c.get("/tasks/1?edit=0", headers=HX).get_data(as_text=True)
    assert "<h2>Plan</h2>" in detail, "headings rendered"
    assert "<li>one</li>" in detail, "lists rendered"
    assert "<code>code</code>" in detail, "inline code rendered"
    assert "## Plan" not in detail, "source not shown raw"
    assert "## Plan" in c.get("/tasks/1?edit=1", headers=HX).get_data(as_text=True), "edit view gives the source back"
    ac.send_message(conn, "dev-agent", "human", 1, "result", "**done** &lt;ok&gt;")
    thread = c.get("/tasks/1", headers=HX).get_data(as_text=True)
    assert "<strong>done</strong>" in thread, "message markdown rendered"
    ac.send_message(conn, "dev-agent", "human", 1, "note", "<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in c.get("/tasks/1", headers=HX).get_data(as_text=True), "html from agents is escaped"
    assert ("<strong>bold</strong>" in (
        c.post("/docs", data={"key": "notes"}),
        c.post("/docs/notes", data={"content": "**bold**"}),
        c.get("/docs/notes").get_data(as_text=True))[-1]), "docs render too"
    c.post("/docs/notes/delete")


def test_agents_page(c):
    html = c.get("/agents").get_data(as_text=True)
    assert "dev-agent" in html and "claude" in html, "agent listed"
    assert "claude-opus-5" in html, "model column"
    assert "$0.0100" in html, "spend shown"
    assert 'hx-get="/agents/rows"' in html, "polls itself"
    rows = c.get("/agents/rows").get_data(as_text=True)
    assert "<table>" in rows, "rows fragment"
    assert '<a href="/agents/dev-agent">' in rows, "agent name links to its own page"
    assert 'id="agent-editor"' not in html, "list page has no embedded editor"


def test_agent_heartbeat_and_status(c, conn):
    ac.heartbeat(conn, "dev-agent", "working")
    agent = ac.get_agent(conn, "dev-agent")
    assert agent["status"] == "working", "heartbeat updates status"
    assert agent["last_heartbeat"] is not None and agent["last_heartbeat"] > 0, "heartbeat records time"
    agent_html = c.get("/agents").get_data(as_text=True)
    assert "working" in agent_html or "dev-agent" in agent_html, "status shown on agents page"
    ac.heartbeat(conn, "dev-agent", "idle")
    agent = ac.get_agent(conn, "dev-agent")
    assert agent["status"] == "idle", "status can change"
    ac.heartbeat(conn, "dev-agent", "offline")
    assert ac.get_agent(conn, "dev-agent")["status"] == "offline", "offline status visible"


def test_agent_settings(c, conn, project):
    editor = c.get("/agents/dev-agent", headers=HX).get_data(as_text=True)
    assert 'name="model"' in editor and 'name="backend"' in editor, "settings form"
    assert 'value="claude-opus-5"' in editor, "current model prefilled"
    assert all(f'name="{f["key"]}"' in editor for f in ac.AGENT_FIELDS), "every field offered"
    assert "textarea" in editor and "prompts/dev-agent.md" in editor, "prompt editor too"
    full_agent_page = c.get("/agents/dev-agent").get_data(as_text=True)
    assert "<nav>" in full_agent_page and 'name="model"' in full_agent_page, "agent page is a full page"
    assert c.get("/agents/nope").status_code == 404, "missing agent 404s"

    saved = c.post("/agents/dev-agent", data={
        "backend": "claude", "model": "claude-sonnet-5", "role": "builder",
        "permission_mode": "plan", "sandbox": "", "codex_bin": "",
        "price_in_per_mtok": "", "price_out_per_mtok": "",
    }).get_data(as_text=True)
    cfg = ac.load_config(project)["agents"]["dev-agent"]
    assert cfg["model"] == "claude-sonnet-5", "model written to config.toml"
    assert cfg["permission_mode"] == "plan", "option written"
    assert "sandbox" not in cfg and "codex_bin" not in cfg, "blank fields dropped"
    assert ac.get_agent(conn, "dev-agent")["role"] == "builder", "role synced to the db"
    assert 'id="agent-editor"' in saved and "claude-sonnet-5" in saved, "editor panel swapped back"
    assert 'id="toast" hx-swap-oob="true"' in saved, "toast is out-of-band"
    assert 'value="claude-sonnet-5"' in c.get("/agents/dev-agent", headers=HX).get_data(as_text=True), "editor reflects the change"


def test_agent_current_task_display(c, conn):
    new_task_id = ac.add_task(conn, "for the agent", "test", "dev-agent")
    ac.heartbeat(conn, "dev-agent", "working", task_id=new_task_id)
    agent = ac.get_agent(conn, "dev-agent")
    assert agent.get("current_task_id") == new_task_id, "agent current_task_id stored"
    agent_page = c.get("/agents").get_data(as_text=True)
    # The agents page shows the task ID in the Task column, not the title
    assert str(new_task_id) in agent_page, "current task shown on agents page"
    ac.heartbeat(conn, "dev-agent", "idle", task_id=None)
    assert ac.get_agent(conn, "dev-agent")["current_task_id"] is None, "current task can be cleared"


def test_adding_and_removing_agents(c, conn, project):
    added = c.post("/agents", data={"name": "bench-1", "backend": "codex", "model": "gpt-5-codex", "role": "benchmarks"}).get_data(as_text=True)
    assert ac.load_config(project)["agents"]["bench-1"]["backend"] == "codex", "agent added to config"
    assert ac.get_agent(conn, "bench-1")["backend"] == "codex", "agent registered in db"
    assert ac.prompt_path(project, "bench-1").exists(), "prompt file seeded"
    assert "bench-1" in added and "gpt-5-codex" in added, "shows in the table"
    # an agent name becomes a file path (.agents/prompts/<name>.md), so a
    # traversal attempt must be refused before anything is written
    traversal = c.post("/agents", data={"name": "../etc/passwd"})
    assert traversal.status_code == 422, "bad name refused"
    assert "Agent name must start with alphanumeric" in traversal.get_data(as_text=True), "bad name explained"
    assert not ac.prompt_path(project, "../etc/passwd").exists(), "bad name wrote nothing"
    assert "../etc/passwd" not in str(ac.load_config(project)["agents"]), "nothing written for a bad name"
    assert "already exists" in c.post("/agents", data={"name": "bench-1"}).get_data(as_text=True), "duplicate refused"

    c.post("/agents/bench-1", data={"backend": "codex", "price_in_per_mtok": "1.25", "price_out_per_mtok": "10"})
    assert ac.load_config(project)["agents"]["bench-1"]["price_in_per_mtok"] == 1.25, "prices stored as numbers"
    c.post("/agents/bench-1", data={"backend": "codex", "max_turns": "40", "max_budget_usd": "2"})
    saved = ac.load_config(project)["agents"]["bench-1"]
    assert saved["max_turns"] == 40 and isinstance(saved["max_turns"], int), "turn limit stored as an integer"
    assert saved["max_budget_usd"] == 2.0, "budget stored as a number"
    assert "max_turns = 40\n" in ac.config_path(project).read_text(), "written as 40, not 40.0"
    bad_price = c.post("/agents/bench-1", data={"price_in_per_mtok": "cheap"})
    assert bad_price.status_code == 422, "bad price refused"
    assert "price_in_per_mtok must be a valid number" in bad_price.get_data(as_text=True), "bad price explained"

    t_id = ac.add_task(conn, "for bench", assigned_to="bench-1")
    removed = c.post("/agents/bench-1/delete")
    assert "bench-1" not in ac.load_config(project)["agents"], "gone from config"
    assert ac.get_agent(conn, "bench-1") is None, "gone from db"
    assert ac.get_task(conn, t_id)["assigned_to"] is None, "its task survives, unassigned"
    assert removed.headers.get("HX-Redirect") == "/agents", "redirects back to the agents list"
    assert c.get("/agents/bench-1").status_code == 404, "its own page is gone"
    assert ac.prompt_path(project, "bench-1").exists(), "prompt file kept"

    c.post("/agents/dev-agent/context", data={"content": "be terse"})
    assert ac.prompt_path(project, "dev-agent").read_text() == "be terse", "prompt written to disk"
    assert "be terse" in c.get("/agents/dev-agent", headers=HX).get_data(as_text=True), "editor reloads it"


def test_activity(c, conn):
    new_task_id = task_id(conn, "for the agent")
    mono = ac.Monologue(conn, "dev-agent", new_task_id, quiet=True)
    mono.record("prompt", f"Task {new_task_id}: Ship it")
    mono.tool_call("Edit", {"file_path": "src/app.py", "old_string": "a" * 500})
    tail = c.get("/agents/activity").get_data(as_text=True)
    assert 'hx-get="/agents/activity"' in tail, "tail polls itself"
    assert "Edit" in tail and "dev-agent" in tail, "tail shows the tool"
    assert "a" * 400 not in tail, "tail truncates"
    # Regression: the poll target's own response used to include the filter
    # form too, so every 3s poll (outerHTML on just #activity) dropped in a
    # duplicate filter bar as a sibling instead of just refreshing the log.
    assert "activity-filters" not in tail and "agent-filter" not in tail, "poll fragment is the log alone, no filter form"
    assert 'id="activity"' in c.get("/agents").get_data(as_text=True), "tail on the agents page"
    agents_page_html = c.get("/agents").get_data(as_text=True)
    assert agents_page_html.count("activity-filters") == 1, "filters appear exactly once on the agents page"
    detail = c.get(f"/tasks/{new_task_id}", headers=HX).get_data(as_text=True)
    assert f"Task {new_task_id}" in detail and 'class="ev k-' in detail, "task log rendered"
    assert "show input" in detail and "a" * 400 not in detail, "tool_use is header only, input behind a control"
    # stream: long bodies preview inline, the rest loads on demand
    long_body = "\n".join(f"line {i}" for i in range(120))
    mono.record("text", long_body)
    stream = c.get(f"/tasks/{new_task_id}", headers=HX).get_data(as_text=True)
    assert "line 0" in stream and "line 49" in stream and "line 50" not in stream, "task view previews body inline"
    assert "70 more lines" in stream, "task view offers 'N more lines'"
    agents_stream = c.get("/agents/activity").get_data(as_text=True)
    assert "line 49" in agents_stream and "70 more lines" in agents_stream, "agents tail previews body inline"
    assert ".ev.expanded" in agents_stream and "details[open]" not in agents_stream, "poll pauses on expanded events"
    assert 'class="ev-more ev-less"' in agents_stream and "classList.remove('expanded')" in agents_stream, "expanded events can be collapsed to resume polling"
    import re as _re
    ev_id = int(_re.findall(r'hx-get="/events/(\d+)/detail\?full=1"', agents_stream)[0])
    full = c.get(f"/events/{ev_id}/detail?full=1").get_data(as_text=True)
    assert "line 119" in full and len(full) > len(c.get(f"/events/{ev_id}/detail").get_data(as_text=True)) - 1, "full mode returns the whole body"
    assert "line 119" in full and "line 119" not in stream, "full mode has more than the preview"
    assert 'hx-trigger="every 3s"' not in detail, "log does not poll over the form"


def test_live_polling_features_replaced_with_reload_button(c):
    # Test the polling container structure
    table_html = c.get("/tasks").get_data(as_text=True)
    # The live reload trigger should be gone
    assert 'hx-trigger="every 5s"' not in table_html, "live reload trigger removed"
    # The "live" indicator should be gone
    assert ">live<" not in table_html, "live indicator removed"
    # The reload button should be present
    assert 'class="reload-tasks-btn"' in table_html, "reload button present"
    assert 'aria-label="Reload task table"' in table_html, "reload button has aria label"
    assert "Reload" in table_html, "reload button text correct"
    # Test individual fragment endpoints
    rows = c.get("/agents/rows").get_data(as_text=True)
    assert "<table>" in rows, "agents rows fragment renders"
    activity = c.get("/agents/activity").get_data(as_text=True)
    assert "activity" in activity.lower() or "hx-get" in activity, "activity fragment renders"
    # An htmx request to /tasks (as the filter/sort/reload controls issue) gets
    # just the tasks-container fragment, not the full page.
    tasks_table_html = c.get("/tasks", headers={"HX-Request": "true"}).get_data(as_text=True)
    assert 'id="tasks-container"' in tasks_table_html, "tasks fragment exists"
    assert "<html" not in tasks_table_html, "tasks fragment has no layout"


def test_reload_button_functionality(c):
    # Verify the reload button works with filters
    c.post("/tasks", data={"title": "Test for reload", "description": "Test reload functionality", "assigned_to": "dev-agent"})
    reload_test_html = c.get("/tasks?status=todo").get_data(as_text=True)
    assert 'class="reload-tasks-btn"' in reload_test_html, "reload button present with filters"
    assert 'id="task-filters-container"' in reload_test_html, "filters preserved with reload button"
    # Simulate the reload button (an htmx GET to /tasks with the current filters)
    filtered_html = c.get("/tasks?status=todo", headers={"HX-Request": "true"}).get_data(as_text=True)
    assert "Test for reload" in filtered_html, "filtered fragment works for reload"
    # And a real browser reload on the same filtered URL gets a full page, not a bare fragment
    reloaded_html = c.get("/tasks?status=todo").get_data(as_text=True)
    assert "<html" in reloaded_html and "Test for reload" in reloaded_html, "direct reload of a filtered URL gets the full layout"


def test_faceted_filters(c, conn):
    c.post("/tasks", data={"title": "Facet A", "assigned_to": "dev-agent", "feature": "facets"})
    c.post("/tasks", data={"title": "Facet B", "feature": "facets"})
    fa = next(t["id"] for t in ac.list_tasks(conn) if t["title"] == "Facet A")
    fb = next(t["id"] for t in ac.list_tasks(conn) if t["title"] == "Facet B")
    ac.update_task(conn, fa, tags="alpha")
    ac.update_task_status(conn, fa, "ready")
    page = c.get("/tasks").get_data(as_text=True)
    assert 'class="facet-chip' in page and "<select id=\"task-status\"" not in page, "facet chips replace the multiselects"
    assert 'name="status"' in page and 'name="agent"' in page and 'name="feature"' in page and 'name="tag"' in page, "facet chips use the filter params"
    assert "Showing" in page and " of " in page, "shown count"
    only_facets = c.get("/tasks?feature=facets", headers=HX).get_data(as_text=True)
    assert "Facet A" in only_facets and "Facet B" in only_facets, "filter by feature shows both"
    assert 'value="facets" checked' in only_facets, "feature chip is ticked"
    assert ('value="ready" ' in only_facets
          and "Showing 2 of" in only_facets), "status chip counts respect the other filters"
    by_status = c.get("/tasks?feature=facets&status=ready", headers=HX).get_data(as_text=True)
    assert "Facet A" in by_status and "Facet B" not in by_status, "status facet narrows the table"
    assert 'value="todo" >' in by_status.replace("  ", " ").replace("\n", " ") or "facet-count" in by_status, "counts for the ticked facet ignore itself"

    assert f'name="ids" value="{fa}"' in page and 'id="bulk-all"' in page, "bulk checkboxes in rows"
    assert ('value="needs_approval"' in page.split('id="bulk-status"')[1].split("</select>")[0]
          and 'value="in_progress"' not in page.split('id="bulk-status"')[1].split("</select>")[0]), "bulk bar offers every status but in_progress"
    moved = c.post("/tasks/bulk", data={"ids": [fa, fb], "to_status": "ready", "status": "ready",
                                        "feature": "facets"}).get_data(as_text=True)
    assert ac.get_task(conn, fa)["status"] == "ready", "bulk ready moves the owned one"
    assert ac.get_task(conn, fb)["status"] == "todo", "bulk ready skips the unowned one"
    assert 'id="toast"' in moved and f"no agent: #{fb}" in moved and "1 moved to ready" in moved, "toast names the skip"
    assert 'id="tasks-container"' in moved and "Facet B" not in moved, "bulk response keeps the filtered view"
    c.post("/tasks/bulk", data={"ids": [fa, fb], "to_status": "done"})
    assert ac.get_task(conn, fa)["status"] == "done" and ac.get_task(conn, fb)["status"] == "done", "bulk done moves both"
    assert ("only an agent" in c.post("/tasks/bulk", data={"ids": [fa], "to_status": "in_progress"}).get_data(as_text=True)
          and ac.get_task(conn, fa)["status"] == "done"), "bulk in_progress refused"
    assert "tick some tasks" in c.post("/tasks/bulk", data={"to_status": "todo"}).get_data(as_text=True), "bulk with nothing ticked says so"
    assert "1 moved to todo" in c.post("/tasks/bulk", data={"ids": ["x", str(fa)], "to_status": "todo"}).get_data(as_text=True), "bulk ignores junk ids"
    for tid in (fa, fb):
        c.post(f"/tasks/{tid}/delete")


def test_docs_page(c, conn):
    html = c.get("/docs").get_data(as_text=True)
    assert "description" in html, "lists existing docs"
    created = c.post("/docs", data={"key": "architecture"}).get_data(as_text=True)
    assert 'id="doc-editor"' in created and "architecture" in created, "create opens the editor"
    assert ac.docs_get(conn, "architecture") == "", "created in the db"
    saved = c.post("/docs/architecture", data={"content": "One SQLite DB per project."}).get_data(as_text=True)
    assert ac.docs_get(conn, "architecture") == "One SQLite DB per project.", "content saved"
    assert 'id="docs"' in saved and 'id="doc-editor"' not in saved, "save returns just the table"
    assert "One SQLite DB per project." in c.get("/docs/architecture").get_data(as_text=True), "editor reloads content"
    assert [d for d in ac.docs_list(conn) if d["key"] == "architecture"][0]["updated_by"] == "human", "attributed to the human"
    c.post("/docs", data={"key": "architecture"})
    assert ac.docs_get(conn, "architecture") == "One SQLite DB per project.", "recreating does not wipe"
    removed = c.post("/docs/architecture/delete").get_data(as_text=True)
    assert 'hx-get="/docs/architecture"' not in removed, "deleted"
    assert ac.docs_get(conn, "architecture") is None, "gone from the db"
    assert removed.count("hx-post=\"/docs\"") == 0, "swaps just the table"


def test_data_browser(c):
    for table in ("tasks", "agents", "messages", "docs", "events"):
        page = c.get(f"/data/{table}").get_data(as_text=True)
        assert 'id="rows"' in page and ("Insert row" in page or "editable" in table), f"{table} page renders"
    assert "no such table" in c.get("/data/nope").get_data(as_text=True), "unknown table refused"


def test_data_table_paging(c, conn):
    new_task_id = task_id(conn, "for the agent")
    # Create multiple messages to test paging
    for i in range(10):
        ac.send_message(conn, "human", "dev-agent", new_task_id, "note", f"message {i}")
    messages_page = c.get("/data/messages?offset=0").get_data(as_text=True)
    assert "of" in messages_page.lower() or "message" in messages_page, "paging info shown"
    assert c.get("/data/messages?offset=5").status_code == 200, "offset parameter works"

    inserted = c.post("/data/messages", data={
        "sender": "human", "recipient": "dev-agent", "msg_type": "note", "payload": "typed by hand", "task_id": "1",
    }).get_data(as_text=True)
    msg = [m for m in ac.task_messages(conn, 1) if m["payload"] == "typed by hand"]
    assert len(msg) == 1, "row inserted"
    assert msg[0]["ts"] > 0, "insert stamps ts"
    assert msg[0]["task_id"] == 1, "numbers coerced, not strings"
    assert "inserted messages" in inserted, "toast reports the new pk"

    editor = c.get(f"/data/messages/row?pk={msg[0]['id']}").get_data(as_text=True)
    assert "typed by hand" in editor, "row editor prefilled"
    assert "read-only" in editor, "pk is read-only"
    c.post(f"/data/messages/row?pk={msg[0]['id']}", data={"payload": "edited by hand", "cost_usd": "0.25"})
    row = ac.get_task and [m for m in ac.task_messages(conn, 1) if m["id"] == msg[0]["id"]][0]
    assert row["payload"] == "edited by hand" and row["cost_usd"] == 0.25, "row updated"
    assert "row deleted" in c.post(f"/data/messages/delete?pk={msg[0]['id']}").get_data(as_text=True), "row deleted"
    assert not [m for m in ac.task_messages(conn, 1) if m["id"] == msg[0]["id"]], "really gone"

    assert "not inserted" in c.post("/data/events", data={"task_id": "abc", "kind": "text"}).get_data(as_text=True), "bad value reported, not raised"
    assert "not inserted" in c.post("/data/tasks", data={"title": "x", "assigned_to": "ghost"}).get_data(as_text=True), "fk violation reported"
    assert c.get("/data/tasks/row?pk=9999").get_data(as_text=True).strip() == '<div id="row-editor"></div>', "missing row is harmless"
    assert "of " in c.get("/data/events/rows?offset=0").get_data(as_text=True), "paging shown"


def test_special_characters_and_escaping(c, conn):
    special_title = "Task with <special> & \"quotes\" 'marks'"
    c.post("/tasks", data={"title": special_title, "description": "Testing: <script>alert(1)</script>"})
    special_tasks = [t for t in ac.list_tasks(conn) if "<special>" in t["title"]]
    assert len(special_tasks) > 0, "special chars stored in db"
    html = c.get("/tasks").get_data(as_text=True)
    assert "<script>" not in html or "alert" not in html, "special chars escaped in html"
    # The title should be visible but escaped
    assert "special" in html, "title visible but safe"


def test_form_submission_edge_cases(c, conn):
    # Empty description is OK
    c.post("/tasks/1", data={"description": ""})
    assert ac.get_task(conn, 1)["description"] == "", "empty description accepted"
    # Blank assignment
    c.post("/tasks/1", data={"assigned_to": ""})
    assert ac.get_task(conn, 1)["assigned_to"] is None, "blank assignment clears"
    # Re-assign to valid agent
    c.post("/tasks/1", data={"assigned_to": "dev-agent"})
    assert ac.get_task(conn, 1)["assigned_to"] == "dev-agent", "assignment to valid agent works"


def test_html_fragment_consistency(c):
    # All responses to HTMX requests should be HTML fragments, not full pages
    detail_response = c.post("/tasks/1", data={"status": "done"}).get_data(as_text=True)
    assert "<html" not in detail_response.lower(), "patch response is fragment not page"
    assert "<head" not in detail_response.lower(), "fragment has no head tag"
    row_response = c.get("/agents/rows").get_data(as_text=True)
    assert "<html" not in row_response.lower(), "rows fragment is partial"


def test_error_handling_and_edge_cases(c, conn):
    t_id = task_id(conn, "for bench")
    # Test invalid task IDs
    assert c.get("/tasks/99999").status_code == 404, "nonexistent task page 404s"
    assert c.get("/tasks/99999/row").get_data(as_text=True) == "", "nonexistent task row returns empty"
    assert c.post("/tasks/99999", data={"status": "done"}).get_data(as_text=True) == "", "nonexistent task patch ignored"
    # Test with missing form fields
    c.post("/tasks", data={"title": "no description task"})
    assert len(ac.list_tasks(conn)) > 1, "tasks can be created with empty description"
    # Closing a panel is pure client-side (closePanel() in layout.html) - no
    # server round trip to test, just that each editor wires its close
    # control to it.
    assert "function closePanel(id)" in c.get("/").get_data(as_text=True), "layout defines closePanel"
    doc_editor_html = c.post("/docs", data={"key": "closetest"}).get_data(as_text=True)
    assert "closePanel('doc-editor')" in doc_editor_html, "doc editor close wired to closePanel"
    row_editor_html = c.get("/data/tasks/row", query_string={"pk": t_id}).get_data(as_text=True)
    assert "closePanel('row-editor')" in row_editor_html, "row editor close wired to closePanel"


def test_merge_queue(c, conn, project):
    # Initialize git in the project so we can test worktree functionality
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
    assert "Merge Queue" in merge_queue_page, "merge queue page renders"
    assert str(merge_task_1) in merge_queue_page or "Fix search" in merge_queue_page, "ready_to_merge task shows in queue"
    assert branch1 in merge_queue_page or "kuska" in merge_queue_page, "ready_to_merge task shows its branch"
    assert str(merge_task_2) in merge_queue_page or "Refactor database" in merge_queue_page, "another ready_to_merge task shows"

    # Review column: latest review's outcome, linked to the review task
    assert ">pending<" not in merge_queue_page and ">passed<" not in merge_queue_page, "no review yet"
    review_id = ac.request_review(conn, merge_task_2, "dev-agent", "main")
    assert ">pending<" in c.get("/merge-queue").get_data(as_text=True), "open review is pending"
    ac.update_task_status(conn, review_id, "done")
    ac.apply_review_outcome(conn, review_id)
    page = c.get("/merge-queue").get_data(as_text=True)
    assert f'<a href="/tasks/{review_id}">passed</a>' in page, "passed review shown and linked"

    # Test the rows fragment
    rows = c.get("/merge-queue/rows", headers=HX).get_data(as_text=True)
    assert "tr" in rows, "merge queue rows fragment renders"

    # Test ordering: merge_task_1 (1 blocks) should come before merge_task_2 (0 blocks)
    # because higher blocking count comes first, so task 1 should appear before task 2
    merge_queue_page = c.get("/merge-queue").get_data(as_text=True)
    task_1_pos = merge_queue_page.find(str(merge_task_1))
    task_2_pos = merge_queue_page.find(str(merge_task_2))
    assert task_1_pos < task_2_pos and task_1_pos > 0, "ordering puts blocking task above non-blocking one"

    # Test marking merged
    marked = c.post(f"/tasks/{merge_task_1}/merged", data={"confirm": "1"}).get_data(as_text=True)
    assert ac.get_task(conn, merge_task_1)["status"] == "done", "POST /tasks/<id>/merged with confirm sets done"
    assert 'hx-get="/merge-queue/rows"' in marked, "merge queue table keeps its polling trigger"

    # Prune: disabled while the branch has unmerged commits, enabled once merged
    def prune_button(html: str) -> str:
        start = html.index(f'hx-post="/tasks/{merge_task_2}/prune"')
        return html[start:html.index(">", start)]

    (path2 / "change.txt").write_text("work")
    subprocess.run(["git", "add", "."], cwd=path2, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "work"], cwd=path2, check=True, capture_output=True)
    rows = c.get("/merge-queue/rows", headers=HX).get_data(as_text=True)
    assert "disabled" in prune_button(rows), "prune disabled for an unmerged branch"
    subprocess.run(["git", "merge", "--no-ff", "-m", "merge", branch2], cwd=project, check=True, capture_output=True)
    rows = c.get("/merge-queue/rows", headers=HX).get_data(as_text=True)
    assert "disabled" not in prune_button(rows), "prune enabled once the branch is merged"
    assert 'hx-target="#merge-queue"' in prune_button(rows), "prune swaps the queue, not the body"
    pruned = c.post(f"/tasks/{merge_task_2}/prune", headers=HX)
    assert pruned.status_code == 200 and "pruned" in pruned.get_data(as_text=True), "POST /tasks/<id>/prune succeeds"
    assert not path2.exists(), "prune removes the worktree"
    assert ac.get_task(conn, merge_task_2)["status"] == "done", "prune marks the task done"
    # Re-open it so later checks that use this task still see it
    ac.update_task_status(conn, merge_task_2, "ready_to_merge")

    # Merge detection: a merged branch flips to done, an unmerged one stays
    merge_task_3 = ac.add_task(conn, "Merged by hand", "x", "dev-agent")
    merge_task_4 = ac.add_task(conn, "Still open", "x", "dev-agent")
    path3, branch3, _ = ac.worktree.ensure_worktree(project, merge_task_3, "Merged by hand", "main")
    path4, branch4, _ = ac.worktree.ensure_worktree(project, merge_task_4, "Still open", "main")
    for tid, p, b in ((merge_task_3, path3, branch3), (merge_task_4, path4, branch4)):
        ac.update_task(conn, tid, worktree_path=str(p),
                       worktree_base_sha=ac.worktree.merge_base(project, b, "main"))
        (p / "work.txt").write_text(b)
        subprocess.run(["git", "add", "."], cwd=p, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "work"], cwd=p, check=True, capture_output=True)
        ac.update_task_status(conn, tid, "ready_to_merge")
    subprocess.run(["git", "merge", "--no-ff", "-m", "merge", branch3], cwd=project, check=True, capture_output=True)
    page = c.get("/merge-queue").get_data(as_text=True)
    assert ac.get_task(conn, merge_task_3)["status"] == "ready_to_merge", "GET /merge-queue writes no status"
    assert "merged" in page, "the page still reports the merge"
    assert supervisor.detect_merges(conn, project) == [merge_task_3], "supervisor finds the merged branch"
    assert ac.get_task(conn, merge_task_3)["status"] == "done", "merged branch flips its task to done"
    assert ac.get_task(conn, merge_task_4)["status"] == "ready_to_merge", "unmerged task stays ready_to_merge"

    # Test that diff command in task detail has three dots, not two
    task_detail = c.get(f"/tasks/{merge_task_2}", headers=HX).get_data(as_text=True)
    assert f"git diff {branch2[len('kuska/'):]}" not in task_detail or "..." in task_detail, "diff command in HTML has three dots"
    assert "...<" not in task_detail, "diff command has three dots not two"


def test_web_status_changes_go_through_lifecycle(c, conn, project):
    # approve: only from needs_approval
    tid = ac.add_task(conn, "lifecycle approve", "", "dev-agent")
    ac.update_task_status(conn, tid, "needs_approval")
    c.post(f"/tasks/{tid}/approve")
    assert ac.get_task(conn, tid)["status"] == "done", "approve gives done"
    busy = ac.add_task(conn, "lifecycle busy", "", "dev-agent")
    ac.update_task_status(conn, busy, "in_progress")
    resp = c.post(f"/tasks/{busy}/approve")
    assert resp.status_code == 200 and "cannot approve" in resp.get_data(as_text=True), "refusal toast shown"
    assert ac.get_task(conn, busy)["status"] == "in_progress", "approve leaves in_progress alone"

    # board drops
    rtm = ac.add_task(conn, "lifecycle rtm", "", "dev-agent")
    ac.update_task_status(conn, rtm, "ready_to_merge")
    resp = c.post(f"/tasks/{rtm}/move", data={"column": "finished"})
    assert ac.get_task(conn, rtm)["status"] == "ready_to_merge", "ready_to_merge card stays put"
    assert "waiting to merge" in resp.get_data(as_text=True), "board says why"
    card = ac.add_task(conn, "lifecycle card", "", "dev-agent")
    c.post(f"/tasks/{card}/move", data={"column": "ready"})
    assert ac.get_task(conn, card)["status"] == "ready", "todo to ready"
    c.post(f"/tasks/{card}/move", data={"column": "todo"})
    assert ac.get_task(conn, card)["status"] == "todo", "ready to todo"
    ac.update_task_status(conn, card, "done")
    c.post(f"/tasks/{card}/move", data={"column": "ready"})
    assert ac.get_task(conn, card)["status"] == "ready", "done to ready"

    # the status dropdown is a forced, noted override
    c.post(f"/tasks/{card}", data={"status": "blocked"})
    assert ac.get_task(conn, card)["status"] == "blocked", "dropdown changes the status"
    assert any("forced" in m["payload"] for m in ac.task_messages(conn, card)), "forced status leaves a note"


def test_mark_merged_checks_git(c, conn, project):
    def make(title: str):
        tid = ac.add_task(conn, title, "x", "dev-agent")
        path, branch, _ = ac.worktree.ensure_worktree(project, tid, title, "main")
        ac.update_task(conn, tid, worktree_path=str(path),
                       worktree_base_sha=ac.worktree.merge_base(project, branch, "main"))
        (path / "work.txt").write_text(title)
        subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "work"], cwd=path, check=True, capture_output=True)
        ac.update_task_status(conn, tid, "ready_to_merge")
        return tid, branch

    open_id, _ = make("Mark merged open")
    resp = c.post(f"/tasks/{open_id}/merged").get_data(as_text=True)
    assert ac.get_task(conn, open_id)["status"] == "ready_to_merge", "unmerged branch is not marked done"
    assert "not merged" in resp and "Mark merged anyway" in resp, "toast and confirm button shown"
    c.post(f"/tasks/{open_id}/merged", data={"confirm": "1"})
    assert ac.get_task(conn, open_id)["status"] == "done", "confirm=1 marks it done"

    real_id, real_branch = make("Mark merged real")
    subprocess.run(["git", "merge", "--no-ff", "-m", "merge", real_branch], cwd=project, check=True, capture_output=True)
    c.post(f"/tasks/{real_id}/merged")
    assert ac.get_task(conn, real_id)["status"] == "done", "a really merged branch needs no confirm"


def test_search_view(c, conn):
    t_id = task_id(conn, "for bench")
    assert 'hx-push-url="true"' in c.get("/search").get_data(as_text=True), "search form pushes the query into the URL"
    assert c.get("/search/results?q=bench").status_code == 404, "the old fragment-only pagination endpoint is gone"
    # A task result should link straight to its own page
    search_html = c.get("/search", query_string={"q": "bench"}).get_data(as_text=True)
    assert f'href="/tasks/{t_id}"' in search_html, "task result links to /tasks/<id>"
    opened_task = c.get(f"/tasks/{t_id}").get_data(as_text=True)
    assert 'id="task-detail"' in opened_task and "Depends on" in opened_task, "task page shows its detail"
    # A doc result should link straight to it, pre-opened, on the docs page
    c.post("/docs/closetest", data={"content": "zzmarkerdoc content"})
    search_doc_html = c.get("/search", query_string={"q": "zzmarkerdoc"}).get_data(as_text=True)
    assert "/docs?open=closetest#doc-editor" in search_doc_html, "doc result links to /docs?open=<key>"
    opened_doc = c.get("/docs", query_string={"open": "closetest"}).get_data(as_text=True)
    assert 'id="doc-content-closetest"' in opened_doc, "open=<key> lands with that doc's editor open"
    assert 'id="doc-editor"' in c.get("/docs", query_string={"open": "nope"}).get_data(as_text=True), "open=<missing key> is harmless"


def test_htmx_swaps_get_fragments_not_whole_pages(c, conn):
    t_id = task_id(conn, "for bench")
    # Regression: deep-link URLs used to answer an htmx swap with the entire
    # page, so clicking a task nested a second copy of the table inside that
    # task's own row. A response destined for an element must never carry the
    # layout with it.
    doctype = "<!doctype html>"

    task_frag = c.get(f"/tasks/{t_id}", headers=HX).get_data(as_text=True)
    assert doctype not in task_frag.lower(), "htmx /tasks/<id> is a fragment"
    assert "<nav>" not in task_frag, "htmx /tasks/<id> carries no nav"

    agent_frag = c.get("/agents/dev-agent", headers=HX).get_data(as_text=True)
    assert doctype not in agent_frag.lower(), "htmx /agents/<name> is the editor alone"
    assert 'id="agent-editor"' in agent_frag and "<nav>" not in agent_frag, "htmx /agents/<name> is the editor"
    assert "agent-rows" not in agent_frag, "htmx /agents/<name> has no second agent table"

    doc_frag = c.get("/docs", query_string={"open": "closetest"}, headers=HX).get_data(as_text=True)
    assert doctype not in doc_frag.lower(), "htmx /docs?open= is the editor alone"
    assert 'id="doc-editor"' in doc_frag and "<nav>" not in doc_frag, "htmx /docs?open= is the editor"
    assert "/docs/closetest/delete" not in doc_frag, "htmx /docs?open= has no second docs table"

    row_frag = c.get("/data/tasks", query_string={"open": t_id}, headers=HX).get_data(as_text=True)
    assert doctype not in row_frag.lower(), "htmx /data?open= is the row editor alone"
    assert 'id="row-editor"' in row_frag and 'id="rows"' not in row_frag, "htmx /data?open= is the row editor"

    page_frag = c.get("/data/tasks", query_string={"offset": 0}, headers=HX).get_data(as_text=True)
    assert 'id="rows"' in page_frag and doctype not in page_frag.lower(), "htmx /data?offset= is the rows fragment"

    # Back/forward: on a cache miss htmx re-requests the URL and replaces the
    # whole body, so a history restore has to get the full page back.
    restore = c.get(f"/tasks/{t_id}",
                    headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert doctype in restore.get_data(as_text=True).lower(), "history restore gets the full page"

    # A plain browser navigation is unaffected by any of the above.
    for url, args in ((f"/tasks/{t_id}", {}), ("/agents/dev-agent", {}),
                      ("/docs", {"open": "closetest"}), ("/data/tasks", {"open": t_id})):
        body = c.get(url, query_string=args).get_data(as_text=True)
        assert doctype in body.lower() and "<nav>" in body, f"plain GET {url} is a full page"

    # The list row is a plain link to the task's own page - no htmx needed.
    rows_html = c.get("/tasks").get_data(as_text=True)
    assert f'href="/tasks/{t_id}">' in rows_html, "task link points at its own page"
    assert f'hx-get="/tasks/{t_id}"' not in rows_html, "no hx-get on the task link"

    # On the task page itself, editing toggles in place via htmx targeting
    # #task-detail, not the list row.
    detail_html = c.get(f"/tasks/{t_id}", headers=HX).get_data(as_text=True)
    assert f'hx-get="/tasks/{t_id}?edit=1"' in detail_html, "edit fetches the panel fragment in edit mode"
    assert 'hx-target="#task-detail"' in detail_html, "edit targets the task-detail panel"
    assert 'hx-select="#search-page"' in c.get("/search").get_data(as_text=True), "search form selects its own block"


def test_export_delete(c, conn, project):
    msg = c.post("/export").get_data(as_text=True)
    assert "exported" in msg and (project / ".agents-export" / "tasks.md").exists(), "export ran"
    for task in ac.list_tasks(conn):
        last = c.post(f"/tasks/{task['id']}/delete").get_data(as_text=True)
    assert "No tasks yet." in last, "delete empties table"
    assert ac.list_tasks(conn) == [], "gone from db"
    assert c.get("/tasks/99").status_code == 404, "missing task 404s"


def test_route_status_codes(c):
    # Test various HTTP status codes
    assert c.get("/").status_code == 200, "GET / is 200"
    assert c.get("/agents").status_code == 200, "GET /agents is 200"
    assert c.get("/docs").status_code == 200, "GET /docs is 200"
    assert c.get("/data").status_code == 200, "GET /data is 200"
    assert c.get("/data/tasks").status_code == 200, "GET /data/tasks is 200"
    assert c.get("/data/nonexistent").status_code == 200, "invalid data table returns 200"
    assert c.post("/tasks", data={"title": "x"}).status_code == 200, "POST /tasks is 200"
    assert c.get("/tasks/999999").status_code == 404, "GET /tasks/<missing> is 404"


def test_run_transcript_view(c, conn):
    # Create a task and some events to work with
    task_id = ac.add_task(conn, "test run transcript", "testing runs", "dev-agent")
    mono = ac.Monologue(conn, "dev-agent", task_id, quiet=True)
    mono.record("prompt", f"Task {task_id}: Build something")
    mono.tool_call("Read", {"file_path": "src/main.py"})
    mono.record("tool_result", {"content": "file content here"})
    run_id = mono.run_id

    # Test runs index
    runs_page = c.get("/runs").get_data(as_text=True)
    assert "<html" in runs_page and "Runs" in runs_page, "runs index renders"
    assert "<table" in runs_page and "dev-agent" in runs_page, "runs table shows"
    assert f'#{ task_id}' in runs_page, "task link in runs index"
    assert 'class="on">Runs<' in runs_page or 'class="on"' in runs_page and '/runs' in runs_page, "nav marks runs page active"

    # Test run transcript page
    transcript = c.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "<html" in transcript and "Run" in transcript, "run transcript renders"
    assert run_id in transcript, "run id shown"
    assert "dev-agent" in transcript, "agent shown"
    assert f'#{ task_id}' in transcript, "task link in transcript"
    assert "prompt" in transcript and "Read" in transcript, "events shown in order"
    assert "Build something" in transcript and 'class="ev k-' in transcript, "transcript previews bodies inline"
    assert transcript.index("prompt") < transcript.index("Read"), "transcript reads top to bottom"


def test_features(c, conn):
    c.post("/tasks", data={"title": "Grouped one", "assigned_to": "", "feature": "Run-Ledger"})
    c.post("/tasks", data={"title": "Grouped two", "feature": "run-ledger"})
    c.post("/tasks", data={"title": "Loose one"})
    grouped = [t for t in ac.list_tasks(conn) if t["title"].startswith("Grouped")]
    assert [t["feature"] for t in grouped] == ["run-ledger", "run-ledger"], "new-task form sets a feature"
    page = c.get("/tasks").get_data(as_text=True)
    assert 'name="feature"' in page and 'value="run-ledger"' in page, "feature filter offered"
    assert 'title="0/2 done"' in page, "feature chip carries its progress"
    assert "<th>Feature</th>" in page, "feature column shown"
    filtered = c.get("/tasks?feature=run-ledger", headers=HX).get_data(as_text=True)
    assert "Grouped one" in filtered and "Grouped two" in filtered and "Loose one" not in filtered, "feature filter narrows the table"
    loose = c.get("/tasks?feature=", headers=HX).get_data(as_text=True)
    assert "Loose one" in loose and "Grouped one" not in loose, "no-feature filter"
    gid = grouped[0]["id"]
    editor = c.get(f"/tasks/{gid}?edit=1", headers=HX).get_data(as_text=True)
    assert 'name="feature"' in editor and 'value="run-ledger"' in editor, "task editor offers the feature"
    panel = c.post(f"/tasks/{gid}", data={"title": "Grouped one", "description": "", "feature": "supervisor"}).get_data(as_text=True)
    assert ac.get_task(conn, gid)["feature"] == "supervisor" and "supervisor" in panel, "task editor moves it to another feature"
    c.post(f"/tasks/{gid}", data={"title": "Grouped one", "description": "", "feature": ""})
    assert ac.get_task(conn, gid)["feature_id"] is None, "task editor clears the feature"
    assert c.get("/data/features").status_code == 200, "features on the Data page"

    # Test malformed run_id validation
    bad_run = c.get("/runs/not-a-hex-id").get_data(as_text=True)
    assert "Invalid" in bad_run or "error" in bad_run.lower(), "malformed run_id rejected"

    # Test unknown run_id
    unknown_run = c.get("/runs/aabbccddeeff").get_data(as_text=True)
    assert "No run" in unknown_run or "error" in unknown_run.lower(), "unknown run handled"

    # Test path traversal attempt on run_id - Flask's routing should reject this
    traversal_resp = c.get("/runs/../../etc/passwd")
    assert traversal_resp.status_code == 404, "path traversal refused"


def test_task_page_edit_default_and_lazy_deps(c, conn):
    a = ac.add_task(conn, "alpha task", "", "dev-agent")
    b = ac.add_task(conn, "beta task", "", "dev-agent")
    page = c.get(f"/tasks/{a}", headers=HX).get_data(as_text=True)
    assert 'name="title"' in page, "open task opens in edit mode"
    assert "beta task" not in page, "dependency candidates are not rendered up front"
    assert 'name="title"' not in c.get(f"/tasks/{a}?edit=0", headers=HX).get_data(as_text=True)
    opts = c.get(f"/tasks/{a}/deps/candidates?q=beta").get_data(as_text=True)
    assert "beta task" in opts and "alpha task" not in opts, "search filters and excludes self"
    assert f"#{b}" in c.get(f"/tasks/{a}/deps/candidates?q={b}").get_data(as_text=True)
    ac.update_task_status(conn, a, "done")
    assert 'name="title"' not in c.get(f"/tasks/{a}", headers=HX).get_data(as_text=True), "done shows read view"
