"""Kanban board checks, driven through Flask's test client.

The tests share one project and run in file order: later ones build on the
cards earlier ones created.
"""

import pytest

import kuska as ac


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    project = tmp_path_factory.mktemp("board") / "boardproject"
    (project / ".agents" / "prompts").mkdir(parents=True)
    ac.config_path(project).write_text('[agents.dev-agent]\nbackend = "claude"\nrole = "builder"\n')
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


@pytest.fixture(scope="module")
def status(conn):
    def status(tid: int) -> str:
        return ac.get_task(conn, tid)["status"]

    return status


@pytest.fixture(scope="module")
def move(c):
    def move(tid: int, column: str, **extra) -> str:
        return c.post(f"/tasks/{tid}/move", data={"column": column, **extra}).get_data(as_text=True)

    return move


def test_board(c, conn, status, move):
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
    assert "<h1>Board</h1>" in page and 'href="/board"' in page, "board renders"
    for key in ("todo", "ready", "in_progress", "finished"):
        assert f'data-column="{key}"' in page, f"column {key}"
    assert "data-drop" not in page.split('data-column="in_progress"')[1].split(">")[0], "in progress column takes no drops"
    assert "data-drop" in page.split('data-column="todo"')[1].split(">")[0], "todo column takes drops"
    assert f'href="/tasks/{plain}"' in page and "Plain card" in page, "card links to task page"
    assert "draggable" not in page.split(f'id="card-{running}"')[1].split(">")[0], "running card not draggable"
    assert "draggable" in page.split(f'id="card-{plain}"')[1].split(">")[0], "other cards draggable"
    assert "needs_approval" in page, "finished shows status label"
    assert page.index("Waiting card") < page.index("Done 21"), "waiting card sits above done cards"
    assert page.count("Done ") == 20 and "Done 0<" not in page and "Done 1<" not in page, "only 20 done cards"
    assert page.count('style="--sub:') == 4, "columns carry a sub-column share"
    assert 'style="--sub:3" data-column="finished"' in page, "a long column gets three sub-columns"
    assert 'style="--sub:1" data-column="in_progress"' in page, "a short column gets one"
    assert c.get("/board", headers={"HX-Request": "true"}).get_data(as_text=True).lstrip().startswith('<div id="board"'), "fragment is bare board"

    # board moves
    move(plain, "ready")
    assert "choose an agent" in move(plain, "ready") and status(plain) == "todo", "unassigned to ready shows picker"
    assert 'name="assigned_to"' in move(plain, "ready") and "dev-agent" in move(plain, "ready"), "picker lists agents"
    html = move(plain, "ready", assigned_to="dev-agent")
    assert status(plain) == "ready" and ac.get_task(conn, plain)["assigned_to"] == "dev-agent", "assigned_to assigns and readies"
    assert "does not exist" in move(owned, "ready", assigned_to="nobody") and status(owned) == "todo", "unknown agent refused"
    assert status(owned) == "todo" and "choose an agent" in move(owned, "ready", assigned_to=""), "empty pick refused"
    move(owned, "ready")
    assert status(owned) == "ready", "todo -> ready with agent"
    move(owned, "todo")
    assert status(owned) == "todo", "ready -> todo"
    move(owned, "ready")
    move(owned, "finished")
    assert status(owned) == "done", "ready -> finished is done"
    move(owned, "todo")
    assert status(owned) == "todo", "finished -> todo"
    move(owned, "finished")
    assert status(owned) == "done", "todo -> finished is done"
    move(owned, "ready")
    assert status(owned) == "ready", "finished -> ready with agent"
    move(waiting, "finished")
    assert status(waiting) == "done", "waiting -> finished is done"
    ac.update_task_status(conn, waiting, "blocked")
    move(waiting, "todo")
    assert status(waiting) == "todo", "waiting -> todo"
    ac.update_task_status(conn, waiting, "blocked")
    unowned_done = ac.add_task(conn, "Unowned done", "", None)
    ac.update_task_status(conn, unowned_done, "done")
    assert "choose an agent" in move(unowned_done, "ready") and status(unowned_done) == "done", "finished unassigned -> ready needs picker"

    # board refused moves
    toast = move(owned, "in_progress")
    assert status(owned) == "ready" and 'id="toast"' in toast and "does not accept" in toast, "move into in_progress refused"
    toast = move(running, "todo")
    assert status(running) == "in_progress" and 'id="toast"' in toast, "in_progress card cannot move"
    assert "only an agent" in move(running, "finished") and status(running) == "in_progress", "in_progress card cannot finish"
    assert 'id="board"' in toast and "Running card" in toast, "refusal still returns board"
    assert "does not accept" in move(owned, "bogus") and status(owned) == "ready", "unknown column refused"
    assert c.post("/tasks/9999/move", data={"column": "todo"}).status_code == 404, "missing task is 404"

    # board feature filter
    a1 = ac.add_task(conn, "Alpha one", "", "dev-agent", feature="alpha")
    ac.add_task(conn, "Alpha two", "", "dev-agent", feature="alpha")
    ac.add_task(conn, "Beta one", "", "dev-agent", feature="beta")
    hx = {"HX-Request": "true"}
    html = c.get("/board?feature=alpha", headers=hx).get_data(as_text=True)
    assert "Alpha one" in html and "Alpha two" in html, "filter shows alpha cards"
    assert "Beta one" not in html and "Plain card" not in html, "filter hides beta card"
    html = move(a1, "ready", assigned_to="dev-agent", feature="alpha")
    assert status(a1) == "ready" and "Alpha one" in html and "Beta one" not in html, "move keeps filter"
    html = c.get("/board").get_data(as_text=True)
    assert all(t in html for t in ("Alpha one", "Alpha two", "Beta one")), "no filter shows all"
    assert 'id="board-feature"' in html and ">All features<" in html and 'value="beta"' in html, "select on page"
    page = c.get("/board?feature=beta").get_data(as_text=True)
    assert 'value="beta" selected' in page and "Alpha one" not in page, "current feature selected"
