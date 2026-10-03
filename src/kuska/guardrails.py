"""Shell-command and tool-call guardrails, independent of any agent SDK.

This module takes a string (or a tool name + input dict) and returns a plain
dict verdict - no ``claude_agent_sdk`` type crosses its boundary. That is
deliberate: `daemons/claude.py` wraps the verdict in whatever the Claude SDK
wants (a `PreToolUse` hook denial today), but codex and openai can call
`check_command`/`check_tool` the same way the day their backends grow
somewhere to plug a check in, without this module or its tests changing at
all.

The core discipline is matching a *parsed* command, never the raw string.
Substring matching reads `touch "rm -rf.txt"` as a recursive delete and
`git push --force-with-lease` (the careful, safe variant) as the dangerous
one. `segments()` lexes the command with `shlex` (punctuation-aware, so
`;`/`&&`/`||`/`|`/`&` land as their own tokens instead of gluing onto
adjacent words) and every rule below matches on the resulting argv, never on
`command` as a string.

`rm -rf` is refused unconditionally, including on paths inside the project.
A guardrail whose boundary an agent has to reason about ("is this rm -rf
still inside the sandbox?") is one it will reason its way around; the
`needs_approval` escape hatch exists precisely so a strict rule doesn't have
to also be a smart one.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile

from .store import normalize_path

# --------------------------------------------------------------------------
# Vocabulary the predicate rules below share.
# --------------------------------------------------------------------------

# Control operators that end one command and start the next. `>`/`>>` are
# deliberately NOT here - they stay inside their segment so a rule can read
# the redirect target as this segment's operand.
_SPLIT_OPS = {";", "&&", "||", "|", "&"}

# Commands that mutate something a path names - relevant to the
# outside-project and project-db checks. `sed` only counts with `-i`
# (in-place); everything else here always writes to its operands.
MUTATORS = {
    "rm", "mv", "cp", "dd", "truncate", "tee", "shred",
    "install", "chmod", "chown", "ln", "mkdir", "rmdir",
}

DOWNLOADERS = {"curl", "wget", "fetch"}
INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "python", "python3", "perl", "ruby", "node", "source"}

_SAFE_SINKS = {"/dev/null", "/dev/stdout", "/dev/stderr"}

_WRAPPER_PROGRAMS = {"nohup", "time"}
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_DB_LEAF_NAMES = {"project.db", "project.db-wal", "project.db-shm"}


# --------------------------------------------------------------------------
# Parsing: string -> segments -> (program, flags, operands).
# --------------------------------------------------------------------------


def segments(command: str) -> list[dict]:
    """Split a shell command into segments joined by control operators.

    Each segment is ``{"argv": [...], "op": "|" | "&&" | ... | None}`` where
    ``op`` is the operator that *follows* this segment (``None`` for the
    last one) - that is what lets `check_download_pipe` ask "is this
    segment's head a downloader, and does it feed a pipe into the next
    segment's interpreter?" without re-parsing.

    Uses `shlex` with `punctuation_chars=True` so operators lex as their own
    tokens instead of gluing onto adjacent words: plain `shlex.split` turns
    ``"ls;rm -rf /"`` into a single ``'ls;rm'`` token and silently misses the
    `rm` entirely. An unbalanced quote raises `ValueError` from `shlex`; on
    that we fall back to a naive whitespace split rather than give up on the
    command - worst case a rule that needed real parsing misses, but nothing
    fires on input it misread.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return [{"argv": command.split(), "op": None}]

    result: list[dict] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SPLIT_OPS:
            result.append({"argv": current, "op": tok})
            current = []
        else:
            current.append(tok)
    result.append({"argv": current, "op": None})
    return [seg for seg in result if seg["argv"]]


def strip_wrappers(argv: list[str]) -> list[str]:
    """Peel off `env VAR=x`, bare `VAR=x`, `nohup` and `time` prefixes.

    Without this, ``env FOO=1 sudo rm -rf /`` (or even bare ``FOO=1 sudo
    ...``) presents "env"/"FOO=1" as the program name and walks straight
    past the sudo rule - a bypass an agent would find by accident, not even
    on purpose.
    """
    argv = list(argv)
    while argv:
        head = argv[0]
        if head == "env":
            argv = argv[1:]
            while argv and _ASSIGNMENT_RE.match(argv[0]):
                argv = argv[1:]
            continue
        if head in _WRAPPER_PROGRAMS:
            argv = argv[1:]
            continue
        if _ASSIGNMENT_RE.match(head):
            argv = argv[1:]
            continue
        break
    return argv


def flags(argv: list[str]) -> list[str]:
    """Expand short-flag bundles (``-rf`` -> ``-r``, ``-f``); collect long
    flags verbatim; stop at a bare ``--``. ``argv[0]`` is the program name
    and is not itself scanned.
    """
    out: list[str] = []
    for tok in argv[1:]:
        if tok == "--":
            break
        if tok.startswith("--") and len(tok) > 2:
            out.append(tok)
        elif tok.startswith("-") and len(tok) > 1:
            out.extend(f"-{ch}" for ch in tok[1:])
    return out


def operands(argv: list[str]) -> list[str]:
    """Return argv's non-flag arguments (everything after the program name
    that isn't a flag), honoring a bare ``--`` as "everything after this is
    literal".
    """
    out: list[str] = []
    past_dashdash = False
    for tok in argv[1:]:
        if not past_dashdash and tok == "--":
            past_dashdash = True
            continue
        if not past_dashdash and tok.startswith("-") and len(tok) > 1:
            continue
        out.append(tok)
    return out


def _sed_inplace(argv: list[str]) -> bool:
    """``sed -i`` (optionally with an attached backup suffix like ``-i.bak``,
    which `flags()`'s bundle expansion would otherwise mangle) or
    ``--in-place``.
    """
    for tok in argv[1:]:
        if tok == "--":
            break
        if tok == "-i" or (tok.startswith("-i") and not tok.startswith("--")):
            return True
        if tok == "--in-place" or tok.startswith("--in-place="):
            return True
    return False


def _write_targets(raw_argv: list[str]) -> list[str]:
    """Every path this segment might write to: a mutator's operands, plus
    any ``>``/``>>`` redirect target. Shared by the outside-project and
    project-db checks so they scan a command the same way.
    """
    argv = strip_wrappers(raw_argv)
    if not argv:
        return []
    program = os.path.basename(argv[0])
    targets: list[str] = []
    if program in MUTATORS or (program == "sed" and _sed_inplace(argv)):
        targets.extend(operands(argv))
    for i, tok in enumerate(argv):
        if tok in (">", ">>") and i + 1 < len(argv):
            target = argv[i + 1]
            # `2>&1` lexes as `2`, `>&`/`>`, `1`/`&1` depending on spelling -
            # an `&`-prefixed "target" is a file-descriptor dup, not a path.
            if not target.startswith("&"):
                targets.append(target)
    return list(dict.fromkeys(targets))  # de-dupe, keep order


def _contains_ordered_subsequence(haystack: list[str], needle: list[str]) -> bool:
    """Do ``needle``'s tokens all appear in ``haystack``, in order, not
    necessarily adjacent? (``git reset --quiet --hard`` still counts as
    "reset ... --hard" - contiguous-only matching is a bypass waiting to be
    found.)
    """
    idx = 0
    for tok in haystack:
        if idx < len(needle) and tok == needle[idx]:
            idx += 1
    return idx == len(needle)


# --------------------------------------------------------------------------
# The declarative table. Every entry here is data, not code - adding a rule
# means adding a dict, never a new branch.
#
# Optional keys:
#   program:      str, or list of str - matched against os.path.basename(argv[0])
#   argv:         list[str] - must appear in order (not necessarily contiguous)
#                 among the tokens after the program name
#   flags_all:    list where each item is either a flag string, or a tuple of
#                 alternative flags of which at least one must be present -
#                 ALL items must be satisfied
#   flags_any:    list of flags - at least one must be present
#   unless:       list of flags - if ANY is present, the rule does NOT match,
#                 even if everything else matched
#   operand_any:  list of literal operand strings - at least one must appear
#   reason:       shown to the agent, composed by refusal_text()
# --------------------------------------------------------------------------

RULES: list[dict] = [
    {
        "id": "rm-rf",
        "program": "rm",
        "flags_all": [("-r", "-R", "--recursive"), ("-f", "--force")],
        "reason": (
            "recursively force-deletes with no prompt and no undo - refused "
            "even for paths inside the project"
        ),
    },
    {
        "id": "git-reset-hard",
        "program": "git",
        "argv": ["reset", "--hard"],
        "reason": "discards uncommitted work and overwrites the working tree with no way back",
    },
    {
        "id": "git-push-force",
        "program": "git",
        "argv": ["push"],
        "flags_any": ["-f", "--force"],
        "unless": ["--force-with-lease", "--force-if-includes"],
        "reason": "overwrites remote history other agents or people may already be building on",
    },
    {
        "id": "git-clean-force",
        "program": "git",
        "argv": ["clean"],
        "flags_any": ["-f", "--force"],
        "reason": "permanently deletes untracked files with no undo",
    },
    {
        "id": "sudo",
        "program": ["sudo", "doas", "su"],
        "reason": "escalates privileges outside the agent's intended sandbox",
    },
    {
        "id": "chmod-777",
        "program": "chmod",
        "operand_any": ["777", "0777"],
        "reason": "opens the file to world write/execute - a common attack vector",
    },
    {
        "id": "git-rebase",
        "program": "git",
        "argv": ["rebase"],
        "reason": (
            "rewrites the branch's history; the daemon rebases onto the base branch "
            "when a task is re-queued, and a human resolves anything that conflicts"
        ),
    },
    {
        "id": "git-merge",
        "program": "git",
        "argv": ["merge"],
        "reason": "a human reviews and merges every branch; nothing is merged from inside a run",
    },
    {
        "id": "git-checkout-branch",
        "program": "git",
        "argv": ["checkout"],
        "reason": (
            "each task has its own worktree already checked out; switching branches "
            "inside it strands the work. Use `git restore <path>` to discard changes "
            "to individual files"
        ),
    },
    {
        "id": "git-switch",
        "program": "git",
        "argv": ["switch"],
        "reason": (
            "each task has its own worktree already checked out; switching branches "
            "inside it strands the work"
        ),
    },
    {
        "id": "git-worktree",
        "program": "git",
        "argv": ["worktree"],
        "reason": "worktrees are created and removed by kuska, not from inside a run",
    },
]


def matches(rule: dict, seg: dict) -> bool:
    """Does one segment match one rule? The only place the optional-key
    vocabulary above gets interpreted.
    """
    argv = strip_wrappers(seg.get("argv") or [])
    if not argv:
        return False

    if "program" in rule:
        wanted = rule["program"]
        wanted = wanted if isinstance(wanted, (list, tuple, set)) else (wanted,)
        if os.path.basename(argv[0]) not in wanted:
            return False

    rest = argv[1:]
    if "argv" in rule and not _contains_ordered_subsequence(rest, rule["argv"]):
        return False

    fl = flags(argv)

    if "flags_all" in rule:
        for group in rule["flags_all"]:
            alternatives = group if isinstance(group, (list, tuple)) else (group,)
            if not any(alt in fl for alt in alternatives):
                return False

    if "flags_any" in rule and not any(f in fl for f in rule["flags_any"]):
        return False

    if "unless" in rule and any(f in fl for f in rule["unless"]):
        return False

    if "operand_any" in rule:
        opnds = operands(argv)
        if not any(o in rule["operand_any"] for o in opnds):
            return False

    return True


# --------------------------------------------------------------------------
# The three checks the table can't express.
# --------------------------------------------------------------------------


def check_download_pipe(segs: list[dict]) -> dict | None:
    """A downloader piped straight into an interpreter: whatever the remote
    end serves today runs immediately, with no chance to read it first.
    Pipeline-shaped on purpose - ``curl ... | jq`` is unaffected because
    `jq` is not in `INTERPRETERS`.
    """
    for i in range(len(segs) - 1):
        head = strip_wrappers(segs[i]["argv"])
        if not head or segs[i]["op"] != "|":
            continue
        if os.path.basename(head[0]) not in DOWNLOADERS:
            continue
        nxt = strip_wrappers(segs[i + 1]["argv"])
        if nxt and os.path.basename(nxt[0]) in INTERPRETERS:
            return {
                "allowed": False,
                "rule": "download-pipe",
                "reason": (
                    "pipes a download straight into an interpreter - whatever the "
                    "remote end serves today runs immediately, unread"
                ),
                "command": shlex.join(segs[i]["argv"]) + " | " + shlex.join(segs[i + 1]["argv"]),
            }
    return None


def _outside(raw: str, project) -> bool:
    if raw.startswith("~") or raw.startswith("$HOME"):
        # normalize_path doesn't expand ~ (or $HOME), so it would never see
        # these as absolute - treat the prefix itself as the outside signal.
        return True
    normalized = normalize_path(raw, project)
    if not os.path.isabs(normalized):
        return False
    if normalized in _SAFE_SINKS:
        return False
    tmp_real = os.path.realpath(tempfile.gettempdir())
    if normalized == tmp_real or normalized.startswith(tmp_real + os.sep):
        return False
    return True


def check_outside_project(seg: dict, project) -> dict | None:
    """A mutator's operand, or a `>`/`>>` target, that resolves outside the
    project root.

    Reuses `core.normalize_path` (`store.py`), which already collapses
    ``..`` and returns an absolute string once a path escapes the project
    root - `os.path.isabs()` of that return value is the whole test. No
    check runs when `project` is None: "outside the project" is undefined
    without a project.
    """
    if project is None:
        return None
    for raw in _write_targets(seg["argv"]):
        if _outside(raw, project):
            return {
                "allowed": False,
                "rule": "outside-project",
                "reason": f"{raw} resolves outside the project root",
                "command": shlex.join(seg["argv"]),
            }
    return None


def _is_project_db_path(norm: str) -> bool:
    if norm == ".agents":
        return True
    parts = norm.split("/")
    return len(parts) == 2 and parts[0] == ".agents" and parts[1] in _DB_LEAF_NAMES


def _readonly_sql(text: str) -> bool:
    s = text.strip().lower()
    return s == "" or s.startswith("select") or s.startswith(".")


def check_project_db(seg: dict, project) -> dict | None:
    """A write target that lands on kuska's own coordination database
    (``.agents/project.db``, its ``-wal``/``-shm`` siblings, or the
    ``.agents`` directory itself), or a `sqlite3` invocation running
    non-read-only SQL against it.

    Read-only inspection (``select``, ``.schema``, ``.dump``, and friends)
    stays allowed on purpose - refusing it just pushes an agent that wants
    to look toward a worse idea, like editing the file directly.
    """
    if project is None:
        return None

    for raw in _write_targets(seg["argv"]):
        if _is_project_db_path(normalize_path(raw, project)):
            return {
                "allowed": False,
                "rule": "project-db",
                "reason": (
                    f"{raw} is kuska's own coordination database - corrupting or "
                    "truncating it takes every agent down, not just this one"
                ),
                "command": shlex.join(seg["argv"]),
            }

    argv = strip_wrappers(seg["argv"])
    if argv and os.path.basename(argv[0]) == "sqlite3":
        opnds = operands(argv)
        db_hits = {o for o in opnds if _is_project_db_path(normalize_path(o, project))}
        if db_hits:
            for o in opnds:
                if o in db_hits or _readonly_sql(o):
                    continue
                return {
                    "allowed": False,
                    "rule": "project-db",
                    "reason": f"runs non-read-only SQL against kuska's coordination database: {o}",
                    "command": shlex.join(seg["argv"]),
                }
    return None


# --------------------------------------------------------------------------
# Public entry points.
# --------------------------------------------------------------------------


def check_command(command: str, project=None) -> dict:
    """Check one shell command line against every rule.

    Returns ``{"allowed": True}`` or ``{"allowed": False, "rule": str,
    "reason": str, "command": str}`` - "command" is the offending *segment*
    (or, for the download-pipe check, the offending two-segment pipe
    fragment), never the whole pipeline, so the agent sees exactly what
    tripped the check.

    ``project`` gates the two path-aware checks (`check_outside_project`,
    `check_project_db`); pass the project root to enable them, or leave it
    None to run only the project-agnostic table rules and download-pipe
    check.
    """
    if not command or not command.strip():
        return {"allowed": True}

    segs = segments(command)

    verdict = check_download_pipe(segs)
    if verdict:
        return verdict

    for seg in segs:
        if not seg["argv"]:
            continue
        for rule in RULES:
            if matches(rule, seg):
                return {
                    "allowed": False,
                    "rule": rule["id"],
                    "reason": rule["reason"],
                    "command": shlex.join(seg["argv"]),
                }
        verdict = check_outside_project(seg, project)
        if verdict:
            return verdict
        verdict = check_project_db(seg, project)
        if verdict:
            return verdict

    return {"allowed": True}


def check_tool(tool_name: str, tool_input: dict, project=None) -> dict:
    """Entry point for a PreToolUse-style hook: dispatch by tool name.

    Only ``Bash`` carries a shell command for `check_command` to parse.
    Every other tool kuska's daemons expose is either the in-process
    `mcp__kuska__*` server (pre-approved - nothing to parse) or a structured
    file edit, whose reach the sandbox and the claude daemon's tool_guard
    already govern; there is nothing for this module to add there today.
    """
    if tool_name == "Bash":
        return check_command((tool_input or {}).get("command", ""), project)
    return {"allowed": True}


def refusal_text(verdict: dict) -> str:
    """Compose the message an agent sees when a command is refused.

    Same voice as tool_guard's redundant-read refusal: say what was refused,
    say why, and name the one acceptable next move - reply ``needs_approval``
    if the command is genuinely necessary, or stop and try something else.
    Deliberately does not suggest a rephrasing, because the fastest way to
    make a strict rule useless is to also make it a hint.
    """
    if verdict.get("allowed", True):
        return ""
    return (
        f"Refused: `{verdict['command']}` ({verdict['rule']}). {verdict['reason']}. "
        "This is not a wording problem, so do not retry it rephrased, escaped, or "
        "split across steps to slip past the check. If it is genuinely necessary, "
        "reply with status 'needs_approval' and say why; otherwise stop and use a "
        "different approach, or reply with status 'blocked' if you cannot proceed "
        "without it."
    )
