#!/usr/bin/env python3
"""Standalone checks for kuska.eventfmt - no test framework, no DB, no tempdir.

Follows the idiom of test_core.py: `uv run tests/eventfmt_test.py`.
"""

import json
import sys

from kuska.db import EVENT_KINDS
from kuska.eventfmt import (
    CLAMP_MAX_CHARS,
    NOISE_SUBTYPES,
    QUIET_KINDS,
    detail_html,
    glyph,
    is_quiet,
    summarize,
)

PASSED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        sys.exit(1)


def ev(kind="tool_use", body="", label=None, **extra) -> dict:
    return {"id": 1, "ts": 0.0, "agent": "dev-agent", "task_id": 1, "run_id": "abc123",
            "kind": kind, "label": label, "body": body, **extra}


def main() -> None:
    print("glyph()")
    for kind in EVENT_KINDS:
        g = glyph(kind)
        check(f"glyph({kind!r}) is a single non-blank char", isinstance(g, str) and len(g) == 1 and g != " ", g)
    check("unknown kind glyph defaults to blank", glyph("no-such-kind") == " ")
    check("warning has a glyph (runtime.py:366 gap)", glyph("warning") != " ")

    print("is_quiet()")
    check("system is quiet", is_quiet(ev(kind="system")) is True)
    for kind in EVENT_KINDS:
        if kind not in QUIET_KINDS:
            check(f"{kind} is not quiet", is_quiet(ev(kind=kind)) is False)
    check("is_quiet on empty dict does not raise", is_quiet({}) is False)
    check("is_quiet on None does not raise", is_quiet(None) is False)
    check("NOISE_SUBTYPES pins thinking_tokens", "thinking_tokens" in NOISE_SUBTYPES)

    print("summarize() - the signature table")
    check(
        "Read reads as the path",
        summarize(ev(label="Read", body=json.dumps({"file_path": "src/kuska/web.py"}))) == "src/kuska/web.py",
    )
    check(
        "Bash without description reads as the command",
        summarize(ev(label="Bash", body=json.dumps({"command": "ls -la"}))) == "ls -la",
    )
    check(
        "Bash with description prefers it",
        summarize(ev(label="Bash", body=json.dumps({"command": "ls -la", "description": "List root"})))
        == "List root",
    )
    check(
        "Grep reads as a call signature",
        summarize(ev(label="Grep", body=json.dumps({"pattern": "normalize_path", "path": "tests/"})))
        == '"normalize_path" in tests/',
    )
    check(
        "Grep with no path defaults to .",
        summarize(ev(label="Grep", body=json.dumps({"pattern": "TODO"}))) == '"TODO" in .',
    )
    check(
        "Edit reads as the path",
        summarize(ev(label="Edit", body=json.dumps({"file_path": "src/kuska/store.py", "old_string": "a", "new_string": "b"})))
        == "src/kuska/store.py",
    )
    check(
        "Write reads as the path",
        summarize(ev(label="Write", body=json.dumps({"file_path": "notes.md", "content": "hi"}))) == "notes.md",
    )
    check(
        "Task reads as the description",
        summarize(ev(label="Task", body=json.dumps({"description": "Explore architecture", "prompt": "..."})))
        == "Explore architecture",
    )
    check(
        "Agent reads as the description",
        summarize(ev(label="Agent", body=json.dumps({"description": "Explore architecture", "prompt": "..."})))
        == "Explore architecture",
    )
    mcp_sig = summarize(ev(
        label="mcp__kuska__create_task",
        body=json.dumps({"title": "A. Guardrails module", "assigned_to": "dev-agent", "description": "long " * 50}),
    ))
    check("mcp__kuska__* reads as k=v args", "title=A. Guardrails module" in mcp_sig and "assigned_to=dev-agent" in mcp_sig, mcp_sig)
    check(
        "unknown tool falls back to its first scalar arg",
        summarize(ev(label="ToolSearch", body=json.dumps({"query": "select:foo", "max_results": 5}))) == "select:foo",
    )
    check(
        "unknown tool with no scalar args falls back to raw one_line",
        summarize(ev(label="Weird", body=json.dumps({"nested": {"a": 1}}))) == json.dumps({"nested": {"a": 1}}),
    )
    check(
        "tool_result content-block list unwraps to its text",
        summarize(ev(kind="tool_result", label="mcp__kuska__get_inbox", body=json.dumps([{"type": "text", "text": "[]"}])))
        == "[]",
    )
    check(
        "non-tool kinds are just one-lined",
        summarize(ev(kind="text", label=None, body="line one\nline two")) == "line one line two",
    )

    print("summarize()/detail_html() never raise - hostile bodies")
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
    check(f"summarize()/detail_html() returned a str for every one of "
          f"{len(hostile_bodies) * len(kinds) * len(labels)} hostile combinations", exploded == 0, exploded)

    print("detail_html() clamps long bodies")
    huge_35kb = "line\n" * 7000  # ~35 KB, matches the live DB's max tool_result size
    html_out = detail_html(ev(kind="tool_result", label="Bash", body=huge_35kb))
    check("clamped output is bounded", len(html_out) < len(huge_35kb), (len(html_out), len(huge_35kb)))
    check("clamp marker present", "more lines" in html_out, html_out[:200])

    huge_one_line = "x" * 40_000
    html_out2 = detail_html(ev(kind="tool_result", label="Bash", body=huge_one_line))
    check("char-clamp bounds a single huge line", len(html_out2) < CLAMP_MAX_CHARS + 1000, len(html_out2))

    print("detail_html() escapes model-authored HTML")
    raw_script = detail_html(ev(kind="tool_result", label="Bash", body="<script>alert(1)</script>"))
    check("raw pre branch escapes <script>", "<script>" not in raw_script and "&lt;script&gt;" in raw_script, raw_script)

    prose_script = detail_html(ev(kind="text", label=None, body="<script>alert(1)</script>"))
    check("prose branch escapes <script>", "<script>" not in prose_script, prose_script)

    json_script = detail_html(ev(kind="tool_use", label="Read", body=json.dumps({"file_path": "<script>x</script>"})))
    check("json-as-markdown branch escapes <script>", "<script>" not in json_script, json_script)

    print("detail_html() renders prose and JSON sections")
    prose = detail_html(ev(kind="result", label=None, body="**done**"))
    check("prose renders markdown", "<strong>done</strong>" in prose or "<em>" in prose or "done" in prose, prose)

    section = detail_html(ev(kind="tool_use", label="Edit", body=json.dumps({"file_path": "a.py", "old_string": "x", "new_string": "y"})))
    check("dict body becomes sections, not a raw blob", "raw" not in section, section)

    print("empty/None bodies")
    check("empty body summarizes to empty string", summarize(ev(kind="text", body="")) == "")
    check("None body does not raise and returns a string", isinstance(summarize(ev(kind="text", body=None)), str))
    check("empty body detail is empty string", detail_html(ev(kind="text", body="")) == "")

    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    main()
