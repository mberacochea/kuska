"""Markdown rendering for text agents and humans wrote.

Agents write Markdown, so the web UI renders it rather than showing the
source. Raw HTML inside that text is escaped, never passed through: the text
comes from a model, and a page that renders it should not be a place where a
model can inject markup.
"""

from __future__ import annotations

import json

import mistune

_render = mistune.create_markdown(escape=True, plugins=["strikethrough", "table", "url"])

# kinds of event whose body is prose worth rendering; the rest is raw payload
PROSE_KINDS = ("text", "thinking", "result", "prompt")


def render(text: str | None) -> str:
    """Markdown -> HTML, with any embedded HTML escaped."""
    if not text:
        return ""
    return _render(text)


# --- coercing an agent's doc into Markdown ------------------------------


def _heading(depth: int) -> str:
    """Heading marker for a nesting depth, never deeper than h4."""
    return "#" * min(depth + 2, 4)


def _label(key: str) -> str:
    """'files_modified' -> 'Files modified'."""
    words = str(key).replace("_", " ").replace("-", " ").split()
    if not words:
        return "Section"
    return " ".join(words)[0].upper() + " ".join(words)[1:]


def _lines(value: object, depth: int) -> list[str]:
    """Render one JSON value as Markdown lines."""
    if value is None or value == "" or value == [] or value == {}:
        return ["_none_"]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out += [f"{_heading(depth)} {_label(key)}", "", *_lines(item, depth + 1), ""]
        return out[:-1]  # the caller adds its own trailing blank
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, (dict, list)):
                out += [*_lines(item, depth), ""]
            else:
                out.append(f"- {item}")
        return out
    return [str(value).strip()]


def as_markdown(content: str, title: str | None = None) -> str:
    """Coerce a doc body to Markdown, rewriting a JSON dump into prose.

    Docs are rendered as Markdown in the web UI and concatenated into
    `plan.md` on export, so a doc whose body is a JSON object reads as a
    wall of escaped quotes and `\\n`. Models do that anyway, now and then,
    whatever the prompt says - so the doc tools run their content through
    here on the way in.

    Text that is already prose is returned untouched, byte for byte: only a
    body that parses *whole* as a JSON object or array is rewritten, and a
    JSON snippet inside a fenced code block is prose by that rule.

    Args:
        content: The doc body an agent (or a human) wants to store.
        title: Optional `# ` heading, used only when content was rewritten.

    Returns:
        str: Markdown - the original text, or the JSON rendered as sections.

    Examples:
        >>> as_markdown('{"summary": "did a thing", "files": ["a.py"]}')
        '## Summary\\n\\ndid a thing\\n\\n## Files\\n\\n- a.py\\n'
        >>> as_markdown("# A report\\n\\nAlready markdown.")
        '# A report\\n\\nAlready markdown.'
    """
    text = (content or "").strip()
    if not text.startswith(("{", "[")):
        return content
    try:
        parsed = json.loads(text)
    except ValueError:
        return content
    if not isinstance(parsed, (dict, list)):
        return content

    head = [f"# {title}", ""] if title else []
    body = "\n".join(head + _lines(parsed, 0)).strip()
    while "\n\n\n" in body:
        body = body.replace("\n\n\n", "\n\n")
    return body + "\n"
