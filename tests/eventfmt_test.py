"""Checks for kuska.eventfmt - no DB, no tempdir."""

import json

from kuska.db import EVENT_KINDS
from kuska.eventfmt import (
    CLAMP_MAX_CHARS,
    NOISE_SUBTYPES,
    PREVIEW_LINES,
    QUIET_KINDS,
    detail_html,
    full_html,
    glyph,
    is_quiet,
    preview_html,
    summarize,
)


def ev(kind="tool_use", body="", label=None, **extra) -> dict:
    return {"id": 1, "ts": 0.0, "agent": "dev-agent", "task_id": 1, "run_id": "abc123",
            "kind": kind, "label": label, "body": body, **extra}


def test_glyph():
    for kind in EVENT_KINDS:
        g = glyph(kind)
        assert isinstance(g, str) and len(g) == 1 and g != " ", f"glyph({kind!r}) is a single non-blank char"
    assert glyph("no-such-kind") == " ", "unknown kind glyph defaults to blank"
    assert glyph("warning") != " ", "warning has a glyph (runtime.py:366 gap)"


def test_is_quiet():
    assert is_quiet(ev(kind="system")) is True, "system is quiet"
    for kind in EVENT_KINDS:
        if kind not in QUIET_KINDS:
            assert is_quiet(ev(kind=kind)) is False, f"{kind} is not quiet"
    assert is_quiet({}) is False, "is_quiet on empty dict does not raise"
    assert is_quiet(None) is False, "is_quiet on None does not raise"
    assert "thinking_tokens" in NOISE_SUBTYPES, "NOISE_SUBTYPES pins thinking_tokens"


def test_summarize_signature_table():
    assert summarize(ev(label="Read", body=json.dumps({"file_path": "src/kuska/web.py"}))) == "src/kuska/web.py", "Read reads as the path"
    assert summarize(ev(label="Bash", body=json.dumps({"command": "ls -la"}))) == "ls -la", "Bash without description reads as the command"
    assert (summarize(ev(label="Bash", body=json.dumps({"command": "ls -la", "description": "List root"})))
        == "List root"), "Bash with description prefers it"
    assert (summarize(ev(label="Grep", body=json.dumps({"pattern": "normalize_path", "path": "tests/"})))
        == '"normalize_path" in tests/'), "Grep reads as a call signature"
    assert summarize(ev(label="Grep", body=json.dumps({"pattern": "TODO"}))) == '"TODO" in .', "Grep with no path defaults to ."
    assert (summarize(ev(label="Edit", body=json.dumps({"file_path": "src/kuska/store.py", "old_string": "a", "new_string": "b"})))
        == "src/kuska/store.py"), "Edit reads as the path"
    assert summarize(ev(label="Write", body=json.dumps({"file_path": "notes.md", "content": "hi"}))) == "notes.md", "Write reads as the path"
    assert (summarize(ev(label="Task", body=json.dumps({"description": "Explore architecture", "prompt": "..."})))
        == "Explore architecture"), "Task reads as the description"
    assert (summarize(ev(label="Agent", body=json.dumps({"description": "Explore architecture", "prompt": "..."})))
        == "Explore architecture"), "Agent reads as the description"
    mcp_sig = summarize(ev(
        label="mcp__kuska__create_task",
        body=json.dumps({"title": "A. Guardrails module", "assigned_to": "dev-agent", "description": "long " * 50}),
    ))
    assert "title=A. Guardrails module" in mcp_sig and "assigned_to=dev-agent" in mcp_sig, "mcp__kuska__* reads as k=v args"
    assert summarize(ev(label="ToolSearch", body=json.dumps({"query": "select:foo", "max_results": 5}))) == "select:foo", "unknown tool falls back to its first scalar arg"
    assert summarize(ev(label="Weird", body=json.dumps({"nested": {"a": 1}}))) == json.dumps({"nested": {"a": 1}}), "unknown tool with no scalar args falls back to raw one_line"
    assert (summarize(ev(kind="tool_result", label="mcp__kuska__get_inbox", body=json.dumps([{"type": "text", "text": "[]"}])))
        == "[]"), "tool_result content-block list unwraps to its text"
    assert summarize(ev(kind="text", label=None, body="line one\nline two")) == "line one line two", "non-tool kinds are just one-lined"


def test_never_raises_on_hostile_bodies():
    hostile_bodies = [
        None,
        "",
        "   ",
        "not json at all, just text",
        '{"incomplete": "json", "trailing":',  # truncated mid-JSON
        "[1, 2, 3]",  # valid JSON, not a dict
        "42",  # valid JSON, not a dict
        '"just a json string"',  # valid JSON, not a dict
        "x" * 40_000,  # one huge line, no newlines: needs the char clamp
        "\n".join(f"line {i} of a long tool_result" for i in range(4000)),  # ~35 KB, needs the line clamp
        "<script>alert(1)</script>",
        json.dumps({"file_path": "<script>evil</script>"}),
        json.dumps({"weird_key": None, "nested": {"a": 1}, "list": [1, 2]}),
        json.dumps([{"type": "text"}]),  # block with no "text" key
        json.dumps([{"type": "tool_reference", "tool_name": "mcp__kuska__get_inbox"}]),
        {"not": "a string body"},  # wrong type entirely - a dict, not str
        12345,  # wrong type entirely - an int
    ]
    kinds = list(EVENT_KINDS)
    labels = [None, "Read", "Bash", "Grep", "Edit", "Write", "Task", "Agent",
              "mcp__kuska__create_task", "ToolSearch", "UnknownTool"]

    exploded = 0
    for body in hostile_bodies:
        for kind in kinds:
            for label in labels:
                event = ev(kind=kind, label=label, body=body)
                s = summarize(event)
                d = detail_html(event)
                if not isinstance(s, str) or not isinstance(d, str):
                    exploded += 1
    assert exploded == 0, (f"summarize()/detail_html() returned a str for every one of "
          f"{len(hostile_bodies) * len(kinds) * len(labels)} hostile combinations")


def test_detail_html_clamps_long_bodies():
    huge_35kb = "line\n" * 7000  # ~35 KB, matches the live DB's max tool_result size
    html_out = detail_html(ev(kind="tool_result", label="Bash", body=huge_35kb))
    assert len(html_out) < len(huge_35kb), "clamped output is bounded"
    assert "more lines" in html_out, "clamp marker present"

    huge_one_line = "x" * 40_000
    html_out2 = detail_html(ev(kind="tool_result", label="Bash", body=huge_one_line))
    assert len(html_out2) < CLAMP_MAX_CHARS + 1000, "char-clamp bounds a single huge line"


def test_detail_html_escapes_model_authored_html():
    raw_script = detail_html(ev(kind="tool_result", label="Bash", body="<script>alert(1)</script>"))
    assert "<script>" not in raw_script and "&lt;script&gt;" in raw_script, "raw pre branch escapes <script>"

    prose_script = detail_html(ev(kind="text", label=None, body="<script>alert(1)</script>"))
    assert "<script>" not in prose_script, "prose branch escapes <script>"

    json_script = detail_html(ev(kind="tool_use", label="Read", body=json.dumps({"file_path": "<script>x</script>"})))
    assert "<script>" not in json_script, "json-as-markdown branch escapes <script>"


def test_detail_html_renders_prose_and_json_sections():
    prose = detail_html(ev(kind="result", label=None, body="**done**"))
    assert "<strong>done</strong>" in prose or "<em>" in prose or "done" in prose, "prose renders markdown"

    section = detail_html(ev(kind="tool_use", label="Edit", body=json.dumps({"file_path": "a.py", "old_string": "x", "new_string": "y"})))
    assert "raw" not in section, "dict body becomes sections, not a raw blob"


def test_empty_none_bodies():
    assert summarize(ev(kind="text", body="")) == "", "empty body summarizes to empty string"
    assert isinstance(summarize(ev(kind="text", body=None)), str), "None body does not raise and returns a string"
    assert detail_html(ev(kind="text", body="")) == "", "empty body detail is empty string"


def test_preview_html_full_html():
    big = "\n".join(f"row {i}" for i in range(120))
    h, hidden = preview_html(ev(kind="tool_result", label=None, body=big))
    assert PREVIEW_LINES == 50 and hidden == 70 and "row 49" in h and "row 50" not in h, "preview cut at 50 lines, hidden count right"
    h, hidden = preview_html(ev(kind="text", label=None, body="a\nb\nc"))
    assert hidden == 0 and "c" in h, "short body not cut"
    assert preview_html(ev(kind="tool_use", label="Read", body='{"file_path": "a"}')) == ("", 0), "tool_use has no inline body"
    assert preview_html(ev(kind="text", body="")) == ("", 0), "empty body is empty"
    h, hidden = preview_html(ev(kind="text", label=None, body="x" * 100_000))
    assert hidden >= 1 and len(h) < 20_000, "giant single line is backstopped"
    huge = "\n".join("y" * 5000 for _ in range(4))
    h, hidden = preview_html(ev(kind="tool_result", label=None, body=huge))
    assert hidden == 3, f"char backstop counts the cut lines: {hidden}"
    for hostile in (None, 5, b"\xff", "\x00\ud800" if False else "\x00", "{" * 50, "[1,"):
        for kind in ("text", "tool_result", "bogus"):
            r = preview_html({"kind": kind, "body": hostile})
            f = full_html({"kind": kind, "body": hostile})
            assert isinstance(r[0], str) and isinstance(f, str), f"never raises on {hostile!r}/{kind}"
    assert isinstance(preview_html(None)[0], str), "preview_html tolerates non-dict"
    for fn in (lambda b: preview_html(ev(kind="tool_result", label=None, body=b))[0], lambda b: full_html(ev(kind="text", label=None, body=b))):
        out = fn("<script>alert(1)</script>")
        assert "<script>" not in out, "script escaped in preview/full"
    fence = "intro\n```py\n" + "\n".join(f"x{i} = {i}" for i in range(80))
    h, hidden = preview_html(ev(kind="text", label=None, body=fence))
    assert hidden > 0 and h.count("<pre") == h.count("</pre>") == 1 and "x47" in h and "x60" not in h, "markdown cut mid-fence closes the block"
    jb = json.dumps({"k": [f"item{i}" for i in range(100)]})
    h, hidden = preview_html(ev(kind="tool_result", label=None, body=jb))
    assert hidden > 0 and "item0" in h, "JSON body cut on rendered lines"
    f = full_html(ev(kind="tool_result", label=None, body=big))
    assert "row 119" in f and "more lines" not in f, "full has every line"
    f = full_html(ev(kind="tool_result", label=None, body="y" * 300_000))
    assert len(f) < 250_000 and "clamped" in f, "full has a char backstop"
