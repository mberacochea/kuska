"""Flask + HTMX web UI - where the human writes.

The job is rendering HTML fragments, not validating JSON, and Jinja2 comes
bundled. HTMX loads from a CDN, so there is no npm, no build step and no
bundler.

create_app() opens the project (context.py) and registers each page area's
routes on it (pages, agents, docs, data, insights).

Route map (GET returns a full page, or just the fragment to an htmx request):

    GET  /                                   project description + export
    POST /description, /export, /switch      save description, export, switch project (303 back to the section)
    GET  /tasks                              task table (?search status agent feature tag sort direction)
    POST /tasks                              create; DELETE /tasks/<id> delete; POST /tasks/bulk bulk move
    GET  /tasks/<id>                         task page; POST /tasks/<id> update
    GET  /tasks/<id>/row                     one table row (?edit=tags for click-to-edit)
    POST /tasks/<id>/{requeue,approve,move,merged,prune}   lifecycle actions
    POST /tasks/<id>/messages                reply on the thread
    /tasks/<id>/deps...                      dependencies (still the old paths; task 65 renames them)
    GET  /board                              kanban (same filters as /tasks, plus cols=1..4)
    GET  /agents                             list; HX-Target agent-rows or activity picks the fragment
    POST /agents                             create
    GET  /agents/<name>                      settings + prompt; POST saves settings; DELETE removes
    POST /agents/<name>/prompt               save the prompt
    GET  /runs, /runs/<run_id>               runs
    GET  /merge-queue                        merge queue (htmx polls the same URL)
    GET  /docs, /docs/<key>                  list + create form; one doc (POST create/save, DELETE)
    GET  /data, /data/<table>, /data/<table>/<pk>   read-only browser (?page=N)
    GET  /search                             full-text search (?q page table=, repeatable)
    GET  /stats                              stats
    POST /markdown                           render markdown for a View tab
    GET  /events/<id>/detail                 expanded event body
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from . import agents, data, docs, insights, pages
from .context import make_context
from .helpers import htmx

# templates/ and static/ live in the kuska package, one level up
PACKAGE_DIR = Path(__file__).resolve().parent.parent


def create_app(project_dir: Path) -> Flask:
    app = Flask(__name__, root_path=str(PACKAGE_DIR))
    if not app.secret_key:
        app.secret_key = os.urandom(32)
    htmx.init_app(app)
    ctx = make_context(app, project_dir)
    for module in (pages, agents, docs, data, insights):
        module.register(app, ctx)
    return app
