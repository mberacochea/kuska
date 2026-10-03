"""How one event reads - the single place that decides it.

Events are model-authored: `tool_use`/`tool_result` bodies are JSON, `system`
bodies vary by backend, and any body can be truncated mid-write or just
plain garbage. This module turns that into (a) one readable line and (b) a
safe expanded view, for three surfaces that must not drift apart - the
terminal monologue (`runtime.Monologue.record`), the web feeds, and
`kuska export`.

Pure functions only: no DB, no Flask, so this is trivially testable and
callable from any daemon. **The public API here is pinned** - other tasks
are written against these exact names; message the other holders before
changing a signature.

    NOISE_SUBTYPES  events a daemon should never bother logging (a `system`
                    event whose label is one of these is pure heartbeat).
    QUIET_KINDS     kinds that stay in the audit trail but are hidden from a
                    feed by default (a "show system" toggle reveals them).
    summarize()     the one-line row body.
    detail_html()   the expanded payload, as safe (already-escaped) HTML.
    glyph()         one character for a kind, blank if unknown.
    is_quiet()      whether an event's kind is in QUIET_KINDS.

An `event` is a plain dict as the store returns it (`models.py` `Event`):
keys `id`, `ts`, `agent`, `task_id`, `run_id`, `kind`, `label`, `body`.

Nothing in here raises. A formatter that throws takes the whole activity
feed down with it, which is worse than an ugly row - every public function
catches broadly and degrades to something plain instead.
"""

from __future__ import annotations

import html
import json

from .markdown import PROSE_KINDS, as_markdown, render

# `system` events with this label are pure telemetry (e.g. a "used N more
# thinking tokens" heartbeat) - callers should not even call log_event() for
# them. Enforced by the daemons, not here; this is the shared list so they
# agree on what counts as noise.
NOISE_SUBTYPES = frozenset({"thinking_tokens"})

# Kinds that are logged (they have audit value) but excluded from a feed
# unless a "show system" toggle is on.
QUIET_KINDS = ("system",)

# One character per event kind, for the terminal monologue and the web feeds.
GLYPHS = {
    "prompt": "▸",
    "thinking": "·",
    "text": "▪",
    "tool_use": "⚙",
    "tool_result": "←",
    "system": "┈",
    "error": "✘",
    "result": "✔",
    "warning": "⚠",
}

TERMINAL_WIDTH = 160
# Width used for the one-line row body (summarize()) and the tool call-sig
# fallback - matches the clamp the web activity tail already used.
ROW_WIDTH = 110

# Clamp for detail_html()'s raw/markdown view: tool_result bodies average
# 4.2 KB and top out at 35 KB, so an un-clamped <pre> would make the page.
CLAMP_HEAD_LINES = 60
CLAMP_TAIL_LINES = 10
CLAMP_MAX_CHARS = 20_000


def one_line(text, width: int = TERMINAL_WIDTH) -> str:
    """Collapse a block of output to one readable line.

    Joins multiple lines into one (collapsing whitespace) and truncates if
    needed, adding an ellipsis to indicate truncation.

    Args:
        text: Text to collapse. Coerced to `str` if not already one, so a
              non-string body degrades instead of raising.
        width: Maximum width before truncation (default TERMINAL_WIDTH).

    Returns:
        str: Single-line summary.

    Examples:
        >>> one_line("this is\\na long\\ntext", width=10)
        'this is a…'
    """
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def glyph(kind: str) -> str:
    """One character for an event kind, or a blank if the kind is unknown."""
    return GLYPHS.get(kind, " ")


def is_quiet(event: dict) -> bool:
    """Whether this event's kind is hidden from a feed by default."""
    return bool(event) and event.get("kind") in QUIET_KINDS


# --------------------------------------------------------------------------
# summarize() - the one-line row body
# --------------------------------------------------------------------------


def _is_scalar(value: object) -> bool:
    """A JSON value worth showing inline: a non-empty str/int/float/bool."""
    return isinstance(value, (str, int, float, bool)) and value != ""


def _first_scalar(args: dict) -> str | None:
    for value in args.values():
        if _is_scalar(value):
            return str(value)
    return None


def _sig_read(args: dict) -> str | None:
    path = args.get("file_path")
    return one_line(path, ROW_WIDTH) if isinstance(path, str) and path else None


def _sig_bash(args: dict) -> str | None:
    desc = args.get("description")
    if isinstance(desc, str) and desc.strip():
        return one_line(desc, ROW_WIDTH)
    cmd = args.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return one_line(cmd, ROW_WIDTH)
    return None


def _sig_grep(args: dict) -> str | None:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return None
    path = args.get("path")
    path = path if isinstance(path, str) and path else "."
    return one_line(f'"{pattern}" in {path}', ROW_WIDTH)


def _sig_path_arg(args: dict) -> str | None:
    """Edit / Write: the file being touched."""
    path = args.get("file_path")
    return one_line(path, ROW_WIDTH) if isinstance(path, str) and path else None


def _sig_description(args: dict) -> str | None:
    """Task / Agent: what the sub-agent was asked to do."""
    desc = args.get("description")
    return one_line(desc, ROW_WIDTH) if isinstance(desc, str) and desc.strip() else None


def _sig_mcp(args: dict) -> str | None:
    """mcp__kuska__*: first 2-3 scalar args as k=v."""
    parts = []
    for key, value in args.items():
        if _is_scalar(value):
            parts.append(f"{key}={value}")
        if len(parts) == 3:
            break
    return one_line(" ".join(parts), ROW_WIDTH) if parts else None


# Keyed on the tool's `label`. Each entry is a small callable over the
# parsed argument dict, returning a signature string or None to mean "does
# not apply here, fall through". Adding a tool is a one-line change.
SIGNATURES = {
    "Read": _sig_read,
    "Edit": _sig_path_arg,
    "Write": _sig_path_arg,
    "Grep": _sig_grep,
    "Bash": _sig_bash,
    "Task": _sig_description,
    "Agent": _sig_description,
}


def _tool_signature(label: object, args: dict) -> str | None:
    """Render a call signature for a tool_use/tool_result's parsed args dict."""
    if not isinstance(args, dict):
        return None
    fn = SIGNATURES.get(label) if isinstance(label, str) else None
    if fn is not None:
        try:
            sig = fn(args)
        except Exception:
            sig = None
        if sig:
            return sig
    if isinstance(label, str) and label.startswith("mcp__kuska__"):
        sig = _sig_mcp(args)
        if sig:
            return sig
    scalar = _first_scalar(args)
    return one_line(scalar, ROW_WIDTH) if scalar is not None else None


def _try_json(text: str):
    """`json.loads`, degrading to None on anything that is not clean JSON."""
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_text_blocks(parsed: object) -> str | None:
    """Pull the text out of a list of SDK-style content blocks, if that is
    what this is - `[{"type": "text", "text": "..."}]` is the common shape
    of an MCP tool_result body."""
    if not isinstance(parsed, list):
        return None
    texts = [b.get("text") for b in parsed if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(texts) if texts else None


def _body_text(event: dict) -> str:
    body = (event or {}).get("body")
    if isinstance(body, str):
        return body
    if body is None:
        return ""
    return str(body)  # a body should always be a string by the time it is stored; degrade if not


def summarize(event: dict) -> str:
    """The one-line row body for an event - never raises.

    `tool_use`/`tool_result` bodies are JSON; this renders a call signature
    from the tool's significant argument via `SIGNATURES`. Anything else
    (unknown tool, malformed/truncated JSON, non-dict JSON, any other kind)
    degrades to `one_line(body, ROW_WIDTH)` - today's plain behaviour.
    """
    try:
        return _summarize(event)
    except Exception:
        return one_line(_body_text(event), ROW_WIDTH)


def _summarize(event: dict) -> str:
    event = event or {}
    kind = event.get("kind")
    label = event.get("label")
    text = _body_text(event)
    if not text:
        return ""
    if kind in ("tool_use", "tool_result"):
        parsed = _try_json(text)
        if isinstance(parsed, dict):
            sig = _tool_signature(label, parsed)
            if sig:
                return sig
        else:
            extracted = _extract_text_blocks(parsed)
            if extracted:
                return one_line(extracted, ROW_WIDTH)
    return one_line(text, ROW_WIDTH)


# --------------------------------------------------------------------------
# detail_html() - the expanded payload, as safe HTML
# --------------------------------------------------------------------------


def _clamp(text: str) -> str:
    """Bound a body's size before it goes into an expanded view.

    Clamps by line count first (the common case - a long tool_result), then
    by raw character count as a backstop against a single huge line (e.g.
    minified JSON with no newlines at all).
    """
    if not text:
        return text
    lines = text.split("\n")
    if len(lines) > CLAMP_HEAD_LINES + CLAMP_TAIL_LINES:
        hidden = len(lines) - CLAMP_HEAD_LINES - CLAMP_TAIL_LINES
        lines = lines[:CLAMP_HEAD_LINES] + [f"… {hidden} more lines …"] + lines[-CLAMP_TAIL_LINES:]
        text = "\n".join(lines)
    if len(text) > CLAMP_MAX_CHARS:
        half = CLAMP_MAX_CHARS // 2
        text = text[:half] + f"\n… clamped, {len(text)} chars total …\n" + text[-half:]
    return text


def detail_html(event: dict) -> str:
    """The expanded payload for an event, as safe (pre-escaped) HTML.

    Prose kinds (`markdown.PROSE_KINDS`) render as Markdown. A body that
    parses whole as JSON is rewritten into `## Section` + bullets by
    `as_markdown` first - that is exactly what a JSON event body needs, and
    it is already used for doc handovers. Anything else is an escaped
    `<pre class="raw">`. Never raises - the fallback is a plain, safe string.
    """
    try:
        return _detail_html(event)
    except Exception:
        return '<pre class="raw">(unreadable event)</pre>'


def _detail_html(event: dict) -> str:
    event = event or {}
    kind = event.get("kind")
    text = _body_text(event)
    if not text:
        return ""

    if kind in PROSE_KINDS:
        return render(_clamp(text))

    parsed = _try_json(text)
    if isinstance(parsed, (dict, list)):
        try:
            md_text = as_markdown(text)
        except Exception:
            md_text = None
        if md_text and md_text != text:
            return render(_clamp(md_text))

    return f'<pre class="raw">{html.escape(_clamp(text))}</pre>'


# --------------------------------------------------------------------------
# preview_html() / full_html() - the inline stream view
# --------------------------------------------------------------------------

# Lines of a body shown inline in the activity stream before "show all".
PREVIEW_LINES = 50
# Backstop for a preview of a few gigantic lines (minified JSON etc.).
PREVIEW_MAX_CHARS = 8_000
# The un-clamped-by-lines view: observed bodies top out ~35 KB.
FULL_MAX_CHARS = 200_000


def _display_source(event: dict) -> tuple[str, bool]:
    """(text that will be rendered, whether it is Markdown) for an event.

    Same rules as detail_html: prose kinds are Markdown, whole-JSON bodies are
    rewritten with `as_markdown`, everything else is a raw escaped <pre>.
    """
    event = event or {}
    text = _body_text(event)
    if not text:
        return "", False
    if event.get("kind") in PROSE_KINDS:
        return text, True
    parsed = _try_json(text)
    if isinstance(parsed, (dict, list)):
        try:
            md_text = as_markdown(text)
        except Exception:
            md_text = None
        if md_text and md_text != text:
            return md_text, True
    return text, False


def _emit(text: str, is_md: bool) -> str:
    if not text:
        return ""
    return render(text) if is_md else f'<pre class="raw">{html.escape(text)}</pre>'


def preview_html(event: dict, max_lines: int = PREVIEW_LINES) -> tuple[str, int]:
    """The first `max_lines` lines of an event body as safe HTML.

    Returns `(html, hidden_lines)`; `hidden_lines` is 0 when nothing was cut.
    Lines are counted on the text that is actually rendered (after JSON ->
    Markdown). `tool_use` has no inline body (header only), so it yields
    `("", 0)`. Never raises.
    """
    try:
        if (event or {}).get("kind") == "tool_use":
            return "", 0
        text, is_md = _display_source(event)
        if not text:
            return "", 0
        all_lines = text.split("\n")
        lines = all_lines[:max_lines]
        shown = "\n".join(lines)
        if len(shown) > PREVIEW_MAX_CHARS:
            # Char backstop: count lines not fully shown (a partly shown
            # last line counts as hidden).
            fully = shown.count("\n", 0, PREVIEW_MAX_CHARS)
            if shown[PREVIEW_MAX_CHARS] == "\n":
                fully += 1
            shown = shown[:PREVIEW_MAX_CHARS]
            hidden = max(len(all_lines) - fully, 1)
        else:
            hidden = len(all_lines) - len(lines)
        return _emit(shown, is_md), hidden
    except Exception:
        return '<pre class="raw">(unreadable event)</pre>', 0


def full_html(event: dict) -> str:
    """The whole body as safe HTML - no line clamp, ~200 KB char backstop.
    Never raises."""
    try:
        text, is_md = _display_source(event)
        if len(text) > FULL_MAX_CHARS:
            text = text[:FULL_MAX_CHARS] + f"\n… clamped, {len(text)} chars total …"
        return _emit(text, is_md)
    except Exception:
        return '<pre class="raw">(unreadable event)</pre>'
