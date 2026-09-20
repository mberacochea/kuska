"""Markdown rendering for text agents and humans wrote.

Agents write Markdown, so the web UI renders it rather than showing the
source. Raw HTML inside that text is escaped, never passed through: the text
comes from a model, and a page that renders it should not be a place where a
model can inject markup.
"""

from __future__ import annotations

import mistune

_render = mistune.create_markdown(escape=True, plugins=["strikethrough", "table", "url"])

# kinds of event whose body is prose worth rendering; the rest is raw payload
PROSE_KINDS = ("text", "thinking", "result", "prompt")


def render(text: str | None) -> str:
    """Markdown -> HTML, with any embedded HTML escaped."""
    if not text:
        return ""
    return _render(text)
