"""Web UI checks, driven through Flask's test client.

The tests share one project and run in file order: later ones build on the
tasks, agents and docs earlier ones created.
"""


import json
import re
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

    thread = c.post("/tasks/1/messages", data={"payload": "check the edge case"}).get_data(as_text=True)
    assert "check the edge case" in thread, "human message posted"
    assert ac.get_inbox(conn, "dev-agent")[0]["payload"] == "check the edge case", "message is routed to assignee"

    ac.update_task_status(conn, 1, "in_progress")
    ac.reply(conn, "dev-agent", 1, "Shipped.", input_tokens=10, output_tokens=5, cost_usd=0.01)
    ac.start_run(conn, "webrun000001", 1, "dev-agent")
    ac.end_run(conn, "webrun000001", "finished", input_tokens=10, output_tokens=5, cost_usd=0.01)
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

    sent_back = c.post(f"/tasks/{first}/requeue").get_data(as_text=True)
    assert ac.get_task(conn, first)["status"] == "ready", "send back re-queues"
    assert "re-queued" in sent_back, "said so"
    assert ac.claim_task(conn, "dev-agent")["id"] == first, "dependent still waits"
    ac.update_task_status(conn, first, "needs_approval")
    c.post(f"/tasks/{first}/approve")
    assert ac.get_task(conn, first)["status"] == "done", "approve marks it done"
    assert ac.claim_task(conn, "dev-agent")["id"] == second, "dependent runs now"
    c.post(f"/tasks/{second}/deps/{first}/delete")
    assert ac.task_dependencies(conn, second) == [], "dependency removed"
    for tid in (first, second):
        c.delete(f"/tasks/{tid}")


def test_requeue_send_back_and_reply_on_tasks(c, conn):
    for st in ("done", "blocked", "needs_approval", "ready_to_merge"):
        rid = ac.add_task(conn, f"reply {st}", "", "dev-agent")
        ac.update_task_status(conn, rid, st)
        c.post(f"/tasks/{rid}/messages", data={"payload": "more please"})
        assert ac.get_task(conn, rid)["status"] == "ready", f"web reply reopens {st}"
        c.delete(f"/tasks/{rid}")
    for st in ("todo", "ready", "in_progress"):
        rid = ac.add_task(conn, f"reply {st}", "", "dev-agent")
        ac.update_task_status(conn, rid, st)
        c.post(f"/tasks/{rid}/messages", data={"payload": "fyi"})
        assert ac.get_task(conn, rid)["status"] == st, f"web reply leaves {st} alone"
        c.delete(f"/tasks/{rid}")
    for action in ("requeue",):
        uid = ac.add_task(conn, f"unassigned {action}", "")
        ac.update_task_status(conn, uid, "blocked")
        c.post(f"/tasks/{uid}/{action}")
        assert ac.get_task(conn, uid)["status"] == "todo", f"{action} of unassigned task gives todo"
        c.delete(f"/tasks/{uid}")


def test_markdown_rendering(c, conn):
    tid = ac.add_task(conn, "md task", "", "dev-agent")
    c.post(f"/tasks/{tid}", data={"title": "Ship md", "description": "## Plan\n\n- one\n- two\n\n`code`"})
    open_task = c.get(f"/tasks/{tid}", headers=HX).get_data(as_text=True)
    assert "## Plan" in open_task and f'class="mdf-radio mdf-write" id="task-desc-{tid}-tab-write"' in open_task, "open task opens on Write"
    assert "<h2>Plan</h2>" not in open_task, "open task renders nothing server-side"
    ac.update_task_status(conn, tid, "done")
    detail = c.get(f"/tasks/{tid}?edit=0", headers=HX).get_data(as_text=True)
    assert "<h2>Plan</h2>" in detail, "done task opens on View with headings rendered"
    assert "<li>one</li>" in detail, "lists rendered"
    assert "<code>code</code>" in detail, "inline code rendered"
    assert "## Plan" in detail and 'name="title"' in detail, "the form is always there; ?edit= is ignored"
    ac.send_message(conn, "dev-agent", "human", tid, "result", "**done** &lt;ok&gt;")
    thread = c.get(f"/tasks/{tid}", headers=HX).get_data(as_text=True)
    assert "<strong>done</strong>" in thread, "message markdown rendered"
    ac.send_message(conn, "dev-agent", "human", tid, "note", "<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in c.get(f"/tasks/{tid}", headers=HX).get_data(as_text=True), "html from agents is escaped"
    c.post("/docs", data={"key": "notes"})
    c.post("/docs/notes", data={"content": "**bold**"})
    doc = c.get("/docs/notes").get_data(as_text=True)
    assert "**bold**" in doc and "<strong>bold</strong>" not in doc, "doc editor has no second rendered copy"
    assert "<strong>bold</strong>" in c.post("/markdown", data={"field": "content", "content": "**bold**"}).get_data(as_text=True)
    c.delete("/docs/notes")


def test_agents_page(c):
    html = c.get("/agents").get_data(as_text=True)
    assert "dev-agent" in html and "claude" in html, "agent listed"
    assert "claude-opus-5" in html, "model column"
    assert "$0.0100" in html, "spend shown"
    assert 'hx-get="/agents"' in html, "polls itself"
    rows = c.get("/agents", headers={**HX, "HX-Target": "agent-rows"}).get_data(as_text=True)
    assert "<table>" in rows and "<h2>Agents</h2>" not in rows, "rows fragment"
    assert '<a href="/agents/dev-agent">' in rows, "agent name links to its own page"
    assert 'id="agent-editor"' not in html, "list page has no embedded editor"


def test_agent_heartbeat_and_status(c, conn):
    assert ac.get_agent(conn, "dev-agent")["status"] == "offline", "never heard from"
    ac.heartbeat(conn, "dev-agent")
    agent = ac.get_agent(conn, "dev-agent")
    assert agent["status"] == "idle", "fresh heartbeat reads as idle"
    assert agent["last_heartbeat"] is not None and agent["last_heartbeat"] > 0, "heartbeat records time"
    assert "idle" in c.get("/agents").get_data(as_text=True), "status shown on agents page"
    conn.execute_sql("UPDATE agents SET last_heartbeat = ? WHERE name = 'dev-agent'", (ac.now() - 120,))
    assert ac.get_agent(conn, "dev-agent")["status"] == "offline", "old heartbeat reads as offline"


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


def test_agent_working_count(c, conn):
    ac.heartbeat(conn, "dev-agent")
    for run_id, title in (("run-a", "for the agent"), ("run-b", "second")):
        task_id = ac.add_task(conn, title, "test", "dev-agent")
        ac.start_run(conn, run_id, task_id, "dev-agent")
    agent = ac.get_agent(conn, "dev-agent")
    assert agent["status"] == "working" and agent["running"] == 2, "two running runs"
    assert "working ×2" in c.get("/agents/rows").get_data(as_text=True), "count shown on the agents page"
    ac.end_run(conn, "run-a", "finished")
    assert ac.get_agent(conn, "dev-agent")["running"] == 1, "finished run no longer counts"
    ac.end_run(conn, "run-b", "finished")
    assert ac.get_agent(conn, "dev-agent")["status"] == "idle", "back to idle"


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
    removed = c.delete("/agents/bench-1")
    assert "bench-1" not in ac.load_config(project)["agents"], "gone from config"
    assert ac.get_agent(conn, "bench-1") is None, "gone from db"
    assert ac.get_task(conn, t_id)["assigned_to"] is None, "its task survives, unassigned"
    assert removed.headers.get("HX-Redirect") == "/agents", "redirects back to the agents list"
    assert c.get("/agents/bench-1").status_code == 404, "its own page is gone"
    assert ac.prompt_path(project, "bench-1").exists(), "prompt file kept"

    c.post("/agents/dev-agent/prompt", data={"content": "be terse"})
    assert ac.prompt_path(project, "dev-agent").read_text() == "be terse", "prompt written to disk"
    assert "be terse" in c.get("/agents/dev-agent", headers=HX).get_data(as_text=True), "editor reloads it"


def test_activity(c, conn):
    new_task_id = task_id(conn, "for the agent")
    mono = ac.Monologue(conn, "dev-agent", new_task_id, quiet=True)
    mono.record("prompt", f"Task {new_task_id}: Ship it")
    mono.tool_call("Edit", {"file_path": "src/app.py", "old_string": "a" * 500})
    tail = c.get("/agents", headers={**HX, "HX-Target": "activity"}).get_data(as_text=True)
    assert 'hx-get="/agents"' in tail, "tail polls itself"
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
    agents_stream = c.get("/agents", headers={**HX, "HX-Target": "activity"}).get_data(as_text=True)
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
    rows = c.get("/agents", headers={**HX, "HX-Target": "agent-rows"}).get_data(as_text=True)
    assert "<table>" in rows, "agents rows fragment renders"
    activity = c.get("/agents", headers={**HX, "HX-Target": "activity"}).get_data(as_text=True)
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
        c.delete(f"/tasks/{tid}")


def test_docs_page(c, conn):
    html = c.get("/docs").get_data(as_text=True)
    assert "description" in html, "lists existing docs"
    assert 'id="doc-editor"' not in html and 'href="/docs/description"' in html, "list has no editor, plain links"
    created = c.post("/docs", data={"key": "architecture"}, headers=HX)
    assert created.headers["HX-Redirect"] == "/docs/architecture", "create sends the browser to the doc's page"
    assert c.post("/docs", data={"key": "plain"}).headers["Location"].endswith("/docs/plain"), "no htmx: 303"
    c.delete("/docs/plain")
    assert ac.docs_get(conn, "architecture") == "", "created in the db"
    assert c.post("/docs", data={"key": "bad key!"}).status_code == 422, "invalid key is a 422"
    page = c.get("/docs/architecture").get_data(as_text=True)
    assert 'id="doc-editor"' in page and "<nav>" in page and 'href="/docs"' in page, "doc page is a full page with a back link"
    assert "closePanel" not in page, "no close control"
    frag = c.get("/docs/architecture", headers=HX).get_data(as_text=True)
    assert 'id="doc-editor"' in frag and "<nav>" not in frag, "htmx gets the editor alone"
    assert c.get("/docs/nope").status_code == 404, "missing doc 404s"
    saved = c.post("/docs/architecture", data={"content": "One SQLite DB per project."}).get_data(as_text=True)
    assert ac.docs_get(conn, "architecture") == "One SQLite DB per project.", "content saved"
    assert 'id="doc-editor"' in saved and 'id="docs"' not in saved and "saved" in saved, "save returns the editor and a toast"
    assert [d for d in ac.docs_list(conn) if d["key"] == "architecture"][0]["updated_by"] == "human", "attributed to the human"
    c.post("/docs", data={"key": "architecture"})
    assert ac.docs_get(conn, "architecture") == "One SQLite DB per project.", "recreating does not wipe"
    moved = c.get("/docs?open=architecture")
    assert moved.status_code == 301 and moved.headers["Location"].endswith("/docs/architecture"), "old ?open= link redirects"
    from_list = c.delete("/docs/architecture", headers={**HX, "HX-Target": "docs"}).get_data(as_text=True)
    assert 'id="docs"' in from_list and 'href="/docs/architecture"' not in from_list, "list delete swaps the table"
    assert ac.docs_get(conn, "architecture") is None, "gone from the db"
    c.post("/docs", data={"key": "tmpdoc"})
    on_page = c.delete("/docs/tmpdoc", headers={**HX, "HX-Target": "doc-editor"})
    assert on_page.headers["HX-Redirect"] == "/docs" and ac.docs_get(conn, "tmpdoc") is None, "page delete goes back to the list"
    assert c.post("/docs/architecture/delete").status_code in (404, 405), "old delete route is gone"


def test_data_browser(c, conn):
    from kuska import tables as tbl
    for table in tbl.TABLES:
        page = c.get(f"/data/{table}").get_data(as_text=True)
        assert 'id="rows"' in page and "Insert" not in page, f"{table} page renders read-only"
        first = tbl.list_rows(conn, table, 1)
        if not first:
            continue
        pk = first[0][tbl.pk_name(table)]
        detail = c.get(f"/data/{table}/{pk}")
        assert detail.status_code == 200, f"/data/{table}/<pk> works"
        assert f'href="/data/{table}/{pk}"' in page, f"{table} pk cell links to its page"
        assert "<form" not in detail.get_data(as_text=True).split("<main>")[1], f"{table} row page is read-only"
        assert c.get(f"/data/{table}/zz-missing-9999").status_code == 404, f"{table} missing row 404s"
    nope = c.get("/data/nope")
    assert nope.status_code == 404 and "no such table" in nope.get_data(as_text=True), "unknown table refused"
    assert c.get("/data/nope/1").status_code == 404, "unknown table row 404s"
    moved = c.get("/data/tasks?open=1")
    assert moved.status_code == 301 and moved.headers["Location"].endswith("/data/tasks/1"), "old ?open= link redirects"
    for route in ("/data/tasks/row", "/data/tasks/rows", "/data/tasks/markdown-preview"):
        assert c.get(route).status_code == 404, f"{route} is gone"
    assert c.post("/data/tasks", data={"title": "x"}).status_code == 405, "no insert"
    assert c.post("/data/tasks/delete?pk=1").status_code == 405, "no delete"


def test_data_table_paging(c, conn):
    new_task_id = task_id(conn, "for the agent")
    # Create multiple messages to test paging
    for i in range(10):
        ac.send_message(conn, "human", "dev-agent", new_task_id, "note", f"message {i}")
    messages_page = c.get("/data/messages?offset=0").get_data(as_text=True)
    assert "of" in messages_page.lower() or "message" in messages_page, "paging info shown"
    assert c.get("/data/messages?offset=5").status_code == 200, "offset parameter works"

    msg_id = ac.send_message(conn, "dev-agent", "human", 1, "note", "**typed** by hand")
    row = c.get(f"/data/messages/{msg_id}").get_data(as_text=True)
    assert "<strong>typed</strong>" in row and "raw text" in row, "markdown column rendered with a raw toggle"
    assert 'href="/agents/dev-agent"' in row and 'href="/tasks/1"' in row, "agent and task columns link"
    assert 'href="/agents/human"' not in row, "human has no page"
    assert "of " in c.get("/data/events?page=1", headers=HX).get_data(as_text=True), "paging shown"
    assert 'id="rows"' in c.get("/data/messages?page=2", headers=HX).get_data(as_text=True), "page=N works"
    assert 'href="/tasks/1"' in c.get("/data/tasks/1").get_data(as_text=True), "task row links to its own page"

    # Test with missing form fields
    c.post("/tasks", data={"title": "no description task"})
    assert len(ac.list_tasks(conn)) > 1, "tasks can be created with empty description"
    assert "closePanel" not in c.get("/").get_data(as_text=True), "closePanel is gone"
    c.post("/docs", data={"key": "closetest"})


def test_search_view(c, conn):
    t_id = task_id(conn, "for bench")
    assert 'hx-push-url="true"' in c.get("/search").get_data(as_text=True), "search form pushes the query into the URL"
    assert c.get("/search/results?q=bench").status_code == 404, "the old fragment-only pagination endpoint is gone"
    # A task result should link straight to its own page
    search_html = c.get("/search", query_string={"q": "bench"}).get_data(as_text=True)
    assert f'href="/tasks/{t_id}"' in search_html, "task result links to /tasks/<id>"
    opened_task = c.get(f"/tasks/{t_id}").get_data(as_text=True)
    assert 'id="task-detail"' in opened_task and "Depends on" in opened_task, "task page shows its detail"
    # A doc result should link straight to the doc's own page
    c.post("/docs/closetest", data={"content": "zzmarkerdoc content"})
    search_doc_html = c.get("/search", query_string={"q": "zzmarkerdoc"}).get_data(as_text=True)
    assert 'href="/docs/closetest"' in search_doc_html, "doc result links to /docs/<key>"
    assert "open=" not in search_doc_html, "no ?open= links"
    opened_doc = c.get("/docs/closetest").get_data(as_text=True)
    assert 'id="doc-content-closetest"' in opened_doc, "the doc page has its editor"
    ac.send_message(conn, "human", "dev-agent", t_id, "note", "zzmarkermsg words")
    msg_html = c.get("/search", query_string={"q": "zzmarkermsg"}).get_data(as_text=True)
    assert 'href="/data/messages/' in msg_html, "message result links to its data page"


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

    doc_frag = c.get("/docs/closetest", headers=HX).get_data(as_text=True)
    assert doctype not in doc_frag.lower(), "htmx /docs/<key> is the editor alone"
    assert 'id="doc-editor"' in doc_frag and "<nav>" not in doc_frag, "htmx /docs/<key> is the editor"

    page_frag = c.get("/data/tasks", query_string={"page": 1}, headers=HX).get_data(as_text=True)
    assert 'id="rows"' in page_frag and doctype not in page_frag.lower(), "htmx /data?page= is the rows fragment"

    # Back/forward: on a cache miss htmx re-requests the URL and replaces the
    # whole body, so a history restore has to get the full page back.
    restore = c.get(f"/tasks/{t_id}",
                    headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert doctype in restore.get_data(as_text=True).lower(), "history restore gets the full page"

    # A plain browser navigation is unaffected by any of the above.
    for url, args in ((f"/tasks/{t_id}", {}), ("/agents/dev-agent", {}),
                      ("/docs/closetest", {}), ("/data/tasks/%s" % t_id, {})):
        body = c.get(url, query_string=args).get_data(as_text=True)
        assert doctype in body.lower() and "<nav>" in body, f"plain GET {url} is a full page"

    # The list row is a plain link to the task's own page - no htmx needed.
    rows_html = c.get("/tasks").get_data(as_text=True)
    assert f'href="/tasks/{t_id}">' in rows_html, "task link points at its own page"
    assert f'hx-get="/tasks/{t_id}"' not in rows_html, "no hx-get on the task link"

    # On the task page itself, editing toggles in place via htmx targeting
    # #task-detail, not the list row.
    detail_html = c.get(f"/tasks/{t_id}", headers=HX).get_data(as_text=True)
    assert "?edit=" not in detail_html, "no edit/cancel links on the single-mode page"
    assert 'hx-target="#task-detail"' in detail_html, "saving targets the task-detail panel"
    assert 'hx-select="#search-page"' in c.get("/search").get_data(as_text=True), "search form selects its own block"


def test_export_delete(c, conn, project):
    msg = c.post("/export").get_data(as_text=True)
    assert "exported" in msg and (project / ".agents-export" / "tasks.md").exists(), "export ran"
    for task in ac.list_tasks(conn):
        last = c.delete(f"/tasks/{task['id']}").get_data(as_text=True)
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
    assert c.get("/data/nonexistent").status_code == 404, "invalid data table returns 404"
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
    editor = c.get(f"/tasks/{gid}", headers=HX).get_data(as_text=True)
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
    assert 'name="title"' in page, "task page always renders the form"
    assert "beta task" not in page, "dependency candidates are not rendered up front"
    assert 'name="title"' in c.get(f"/tasks/{a}?edit=0", headers=HX).get_data(as_text=True), "?edit= is ignored"
    opts = c.get(f"/tasks/{a}/deps/candidates?q=beta").get_data(as_text=True)
    assert "beta task" in opts and "alpha task" not in opts, "search filters and excludes self"
    assert f"#{b}" in c.get(f"/tasks/{a}/deps/candidates?q={b}").get_data(as_text=True)
    ac.update_task_status(conn, a, "done")
    assert 'name="title"' in c.get(f"/tasks/{a}", headers=HX).get_data(as_text=True), "done task still shows the form"


# ---------- per-session project, per-request connection ----------


@pytest.fixture()
def two_projects(tmp_path):
    """Two registered projects, A and B, each with one task; REGISTRY restored after."""
    import kuska.project as kproject

    paths = {}
    for name in ("alpha", "beta"):
        p = tmp_path / name
        (p / ".agents" / "prompts").mkdir(parents=True)
        ac.config_path(p).write_text('[agents.dev-agent]\nbackend = "claude"\nmodel = "m"\nrole = "builder"\n')
        paths[name] = p
    saved = kproject.REGISTRY
    kproject.REGISTRY = tmp_path / "projects.toml"
    try:
        for name, p in paths.items():
            kproject.registry_add(name, p)
        for name, p in paths.items():
            app = ac.create_app(p)
            with app.app_context():
                ac.add_task(ac.connect(ac.db_path(p)), f"{name} task", "", "dev-agent")
        yield paths
    finally:
        kproject.REGISTRY = saved


def test_sessions_pick_their_own_project(two_projects):
    app = ac.create_app(two_projects["alpha"])
    app.config.update(TESTING=True)
    c1, c2 = app.test_client(), app.test_client()
    assert c1.post("/switch", data={"project": "beta"}).status_code == 303
    assert "beta task" in c1.get("/tasks").get_data(as_text=True), "client 1 sees B"
    html2 = c2.get("/tasks").get_data(as_text=True)
    assert "alpha task" in html2 and "beta task" not in html2, "client 2 still sees A"


def test_threaded_requests_with_switching(two_projects):
    import threading

    app = ac.create_app(two_projects["alpha"])
    app.config.update(TESTING=True)
    failures: list = []

    def reader():
        try:
            cl = app.test_client()
            for _ in range(20):
                for url in ("/tasks", "/agents"):
                    r = cl.get(url)
                    if r.status_code >= 500:
                        failures.append((url, r.status_code))
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)

    def switcher():
        try:
            cl = app.test_client()
            for i in range(20):
                r = cl.post("/switch", data={"project": "beta" if i % 2 else "alpha"})
                if r.status_code >= 500:
                    failures.append(("/switch", r.status_code))
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(8)] + [threading.Thread(target=switcher)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures, failures


def test_connection_closed_at_teardown(two_projects, monkeypatch):
    from kuska.web import context

    closed = []
    real = context.connect

    def tracking(path):
        conn = real(path)
        orig = conn.close
        conn.close = lambda: (closed.append(1), orig())[1]
        return conn

    app = ac.create_app(two_projects["alpha"])
    app.config.update(TESTING=True)
    monkeypatch.setattr(context, "connect", tracking)
    before = len(closed)
    assert app.test_client().get("/tasks").status_code == 200
    assert len(closed) > before, "request connection closed"


def test_markdown_route_renders_and_escapes(c):
    from kuska.markdown import render
    text = "## Hi\n\n<script>alert(1)</script> **bold**"
    got = c.post("/markdown", data={"field": "description", "description": text}).get_data(as_text=True)
    assert got == render(text) and "<script>" not in got and "<h2>Hi</h2>" in got
    assert got == c.post("/markdown", data={"text": text}).get_data(as_text=True), "default field is text"
    empty = c.post("/markdown", data={"field": "content", "content": ""}).get_data(as_text=True)
    assert "Nothing to preview." in empty


def test_markdown_fields_render_tabs(c, conn):
    tid = ac.add_task(conn, "tabs", "body", "dev-agent")
    ac.docs_set(conn, "tabdoc", "# D", "human")
    pages = {
        f"/tasks/{tid}": [f"task-desc-{tid}-tab-view", f"task-reply-{tid}-tab-view"],
        "/docs/tabdoc": ["doc-content-tabdoc-tab-view"],
        "/": ["project-description-tab-view"],
        "/agents/dev-agent": ["agent-prompt-dev-agent-tab-view"],
    }
    for url, ids in pages.items():
        body = c.get(url, headers=HX).get_data(as_text=True)
        for i in ids:
            assert i in body, f"{url} renders the {i} tab"
        assert 'form="_none"' in body and "Write" in body and "View" in body



def test_htmx_config_swaps_422_only(c):
    html = c.get("/").get_data(as_text=True)
    meta = re.search(r"<meta name=\"htmx-config\" content='([^']+)'>", html)
    assert meta, "htmx-config meta present"
    handling = json.loads(meta.group(1))["responseHandling"]
    rule = next(r for r in handling if r["code"] == "422")
    assert rule["swap"] and not rule["error"], "422 is swapped"
    assert handling.index(rule) < next(i for i, r in enumerate(handling) if r["code"] == "[45].."), "422 wins over other 4xx"
    assert "afterSwap" not in html and "editTags" not in html, "layout carries no custom error/tag JS"


def test_refused_create_shows_toast(c):
    c.post("/tasks", data={"title": "Dup title"})
    r = c.post("/tasks", data={"title": "Dup title"}, headers=HX)
    body = r.get_data(as_text=True)
    assert r.status_code == 422, "duplicate refused"
    assert 'id="toast" hx-swap-oob="true"' in body and "already exists" in body.lower(), "message shipped as a toast"
    r = c.post("/agents", data={"name": "bad name!", "backend": "claude", "model": "m", "role": "r"}, headers=HX)
    assert r.status_code == 422 and 'id="toast"' in r.get_data(as_text=True), "agent errors use the same toast"


def test_quick_add_keeps_filters(c):
    c.post("/tasks", data={"title": "Seen alpha"})
    c.post("/tasks", data={"title": "Other beta"})
    r = c.post("/tasks", data={"title": "Seen gamma"}, headers={**HX, "HX-Current-URL": "http://x/tasks?search=Seen"})
    html = r.get_data(as_text=True)
    assert "Seen gamma" in html and "Seen alpha" in html and "Other beta" not in html, "table stays filtered"
    assert 'id="task-filters-container"' in html and 'value="Seen"' in html, "filter form comes back with the search"


def test_agent_named_rows_has_its_own_page(c):
    r = c.post("/agents", data={"name": "rows", "backend": "claude", "model": "m", "role": "r"}, headers=HX)
    assert r.status_code == 200, "agent named rows is allowed"
    page = c.get("/agents/rows")
    assert page.status_code == 200 and "rows" in page.get_data(as_text=True), "its own page, not the rows fragment"
    c.post("/agents", data={"name": "activity", "backend": "claude", "model": "m", "role": "r"}, headers=HX)
    assert c.get("/agents/activity").status_code == 200, "same for activity"


def test_tag_edit_is_click_to_edit(c, conn):
    tid = ac.add_task(conn, "tag me", "")
    plain = c.get(f"/tasks/{tid}/row").get_data(as_text=True)
    assert f'hx-get="/tasks/{tid}/row?edit=tags"' in plain and "onclick" not in plain, "edit button is htmx"
    edit = c.get(f"/tasks/{tid}/row?edit=tags").get_data(as_text=True)
    assert 'name="tags"' in edit and "Escape" in edit, "input with Escape handler"
    assert f'hx-get="/tasks/{tid}/row"' in edit, "Escape fetches the plain row"
    c.post(f"/tasks/{tid}", data={"tags": "a,b"})
    assert ac.get_task(conn, tid)["tags"] == "a,b", "tags saved"


def test_switch_redirects_to_section(two_projects):
    app = ac.create_app(two_projects["alpha"])
    app.config.update(TESTING=True)
    cl = app.test_client()
    r = cl.post("/switch", data={"project": "beta"}, headers={"Referer": "http://localhost/tasks/42"})
    assert r.status_code == 303 and r.headers["Location"] == "/tasks", "back to the section"
    r = cl.post("/switch", data={"project": "alpha"})
    assert r.status_code == 303 and r.headers["Location"] == "/", "falls back to /"


def test_search_table_param(c):
    html = c.get("/search?q=x&table=docs").get_data(as_text=True)
    assert 'name="table"' in html and "tables[]" not in html, "search filters use table="


def test_merge_queue_polls_its_own_url(c):
    page = c.get("/merge-queue").get_data(as_text=True)
    assert 'hx-get="/merge-queue"' in page and "<header>" in page, "full page polls /merge-queue"
    frag = c.get("/merge-queue", headers=HX).get_data(as_text=True)
    assert "<header>" not in frag and 'id="merge-queue"' in frag, "htmx gets only the rows"
    assert c.get("/merge-queue/rows").status_code == 404, "old route is gone"
