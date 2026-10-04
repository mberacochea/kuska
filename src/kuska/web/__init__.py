"""Flask + HTMX web UI - where the human writes.

The job is rendering HTML fragments, not validating JSON, and Jinja2 comes
bundled. HTMX loads from a CDN, so there is no npm, no build step and no
bundler.

create_app() opens the project (context.py) and registers each page area's
routes on it (pages, agents, docs, data, insights).
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
