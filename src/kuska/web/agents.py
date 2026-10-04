"""Routes: agents and events."""

from __future__ import annotations

from flask import make_response, render_template, request

from .. import eventfmt
from ..project import (
    remove_agent_config,
    set_agent_config,
    sync_agents_from_config,
    write_prompt,
)
from ..store import delete_agent, get_event, list_agents, token_usage_by_agent
from .helpers import (
    BACKENDS,
    _bad_request,
    validate_agent_backend,
    validate_agent_model,
    validate_agent_name,
    validate_agent_prices,
    validate_agent_role,
    wants_fragment,
)


def register(app, ctx) -> None:
    """Add this area's routes to `app`; `ctx` is the namespace make_context() built."""
    _toast = ctx._toast
    activity_log = ctx.activity_log
    activity_tail = ctx.activity_tail
    agent_rows = ctx.agent_rows
    db = ctx.db
    editor = ctx.editor
    rows_with_toast = ctx.rows_with_toast
    project = ctx.project

    # ========== ROUTES: Agents ==========

    @app.get("/agents")
    def agents_page() -> str:
        """GET /agents - Display the agents page with status and activity."""
        return render_template(
            "agents.html",
            page="agents",
            agent_rows=agent_rows(),
            activity=activity_tail(),
            backends=BACKENDS,
            usage=token_usage_by_agent(db()),
        )

    @app.get("/agents/<name>")
    def agent_page(name: str) -> tuple[str, int] | str:
        """GET /agents/<name> - the standalone agent settings + prompt page.

        Same htmx-vs-browser split as /tasks/<id>: an htmx request (e.g. the
        response target of a save or delete) gets just the editor panel, a
        plain navigation gets the whole page.
        """
        if not any(a["name"] == name for a in list_agents(db())):
            return "", 404
        panel = editor(name)
        if wants_fragment():
            return panel
        return render_template("agent.html", page="agents", name=name, agent_panel=panel)

    @app.get("/agents/rows")
    def agents_rows() -> str:
        """GET /agents/rows - Get the agent list rows fragment."""
        return agent_rows()

    @app.get("/agents/activity")
    def agents_activity() -> str:
        """GET /agents/activity - Get the activity log fragment (poll target and filter-change target)."""
        return activity_log()

    @app.post("/agents")
    def create_agent() -> tuple[str, int]:
        """POST /agents - Create a new agent with the given configuration."""
        name = request.form.get("name", "").strip()
        existing = {a["name"] for a in list_agents(db())}

        # Validate agent name
        name_error = validate_agent_name(name, existing)
        if name_error:
            return _bad_request(agent_rows(), "name", name_error)

        # Validate required fields
        backend = request.form.get("backend", "").strip()
        model = request.form.get("model", "").strip()
        role = request.form.get("role", "").strip()

        backend_error = validate_agent_backend(backend)
        if backend_error:
            return _bad_request(agent_rows(), "backend", backend_error)

        model_error = validate_agent_model(model)
        if model_error:
            return _bad_request(agent_rows(), "model", model_error)

        role_error = validate_agent_role(role)
        if role_error:
            return _bad_request(agent_rows(), "role", role_error)

        # Validate prices if provided
        price_error = validate_agent_prices(request.form.to_dict())
        if price_error:
            return _bad_request(agent_rows(), "prices", price_error)

        try:
            set_agent_config(project(), name, request.form.to_dict())
            sync_agents_from_config(db(), project())
        except (ValueError, OSError) as exc:
            return _bad_request(agent_rows(), "form", f"Failed to create agent: {exc}")

        return rows_with_toast(f"added {name}"), 200

    @app.post("/agents/<name>")
    def save_agent(name: str) -> tuple[str, int]:
        """POST /agents/<name> - Save agent configuration (backend, model, role, etc)."""
        # Validate required fields if provided
        backend = request.form.get("backend", "").strip()
        model = request.form.get("model", "").strip()
        role = request.form.get("role", "").strip()

        if backend:
            backend_error = validate_agent_backend(backend)
            if backend_error:
                return _bad_request(editor(name), "backend", backend_error)

        if model:
            model_error = validate_agent_model(model)
            if model_error:
                return _bad_request(editor(name), "model", model_error)

        if role:
            role_error = validate_agent_role(role)
            if role_error:
                return _bad_request(editor(name), "role", role_error)

        # Validate prices if provided
        price_error = validate_agent_prices(request.form.to_dict())
        if price_error:
            return _bad_request(editor(name), "prices", price_error)

        try:
            set_agent_config(project(), name, request.form.to_dict())
        except (ValueError, OSError) as exc:
            return _bad_request(editor(name), "form", f"Failed to save agent settings: {exc}")

        sync_agents_from_config(db(), project())
        return editor(name) + _toast(f"{name} settings saved - restart its daemon to pick them up"), 200

    @app.post("/agents/<name>/delete")
    def remove_agent(name: str):
        """POST /agents/<name>/delete - Delete an agent, unassign its tasks, and
        redirect back to the agents list (its own page no longer exists)."""
        remove_agent_config(project(), name)
        delete_agent(db(), name)
        resp = make_response("")
        resp.headers["HX-Redirect"] = "/agents"
        return resp

    @app.post("/agents/<name>/context")
    def set_context(name: str) -> str:
        """POST /agents/<name>/context - Save an agent's prompt content."""
        write_prompt(project(), name, request.form.get("content", ""))
        return f"{name} prompt saved"

    # ========== ROUTES: Events ==========

    @app.get("/events/<int:event_id>/detail")
    def get_event_detail(event_id: int) -> str:
        """GET /events/<id>/detail - Fetch the expanded detail for an event."""
        event = get_event(db(), event_id)
        if not event:
            return "", 404
        if request.args.get("full"):
            return eventfmt.full_html(event)
        return eventfmt.detail_html(event)
