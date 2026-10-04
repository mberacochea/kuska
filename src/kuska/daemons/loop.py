"""The daemon loop every backend shares.

    kuska daemon <agent-name>

Poll for a task, set up where it runs, compose its prompt, run one fresh
invocation, record the result and its cost, go back to polling. Nothing here
knows which model is on the other end: a backend module supplies
`make_runner(db, project, agent_name, cfg)`, which returns

    async run(prompt, workdir, mono) -> (text, usage)

where `usage` holds the ledger's keyword arguments (input_tokens,
output_tokens, cache_read_tokens, cache_write_tokens, tool_rounds, cost_usd -
any it does not report can be left out). A run that does not finish its task
raises; `core.RunAborted` carries the usage it spent getting there.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import kuska as core
from kuska import worktree


def log(line: str, error: bool = False) -> None:
    """Daemons usually run under nohup or systemd, so never buffer their log."""
    print(line, file=sys.stderr if error else sys.stdout, flush=True)


# how often a running run proves it is alive; a supervisor reads a much older
# heartbeat_at as a dead daemon
RUN_HEARTBEAT_S = 30.0


class RunHeartbeat:
    """Touches a run's heartbeat_at every `interval` seconds on its own thread, so a run that is busy
    (a long tool call, or the codex backend blocking the event loop) still reads as alive."""

    def __init__(self, db_path, run_id: str, interval: float = RUN_HEARTBEAT_S):
        self.db_path, self.run_id, self.interval = db_path, run_id, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name=f"heartbeat-{run_id}", daemon=True)

    def _beat(self) -> None:
        # its own connection: the daemon's is not safe to share across threads
        conn = None
        try:
            conn = core.connect(self.db_path)
            while not self._stop.wait(self.interval):
                try:
                    core.touch_run(conn, self.run_id)
                except Exception as exc:  # a locked database must not end the heartbeat
                    log(f"run {self.run_id} heartbeat failed: {exc}", error=True)
        except Exception as exc:
            log(f"run {self.run_id} heartbeat stopped: {exc}", error=True)
        finally:
            if conn is not None:
                conn.close()

    def __enter__(self) -> RunHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def check_git(project: Path, agent_name: str) -> None:
    """Refuse to start a worktree agent where worktrees cannot exist."""
    if not worktree.is_git_repo(project):
        problem = f"{project} is not inside a git work tree"
    elif not worktree.has_commits(project):
        problem = f"{project} has no commits"
    else:
        return
    raise SystemExit(
        f"[{agent_name}] worktree=true but {problem}. "
        f"Run `git init && git commit` or turn worktree off in .agents/config.toml"
    )


def prepare_workdir(db, project: Path, agent_name: str, task: dict, mono) -> tuple[Path, str]:
    """This task's own worktree, as (path, branch).

    A worktree that cannot be set up fails the task rather than falling back
    to the project checkout: agents running side by side in one checkout
    overwrite each other's work, which is what worktrees are there to stop."""
    base = worktree.base_branch(project)
    try:
        path, branch, created = worktree.ensure_worktree(project, task["id"], task["title"], base)
    except RuntimeError as exc:
        raise core.RunAborted(f"could not set up a worktree for this task: {exc}") from None
    base_sha = None
    if created:
        base_sha = worktree.merge_base(project, branch, base)
    else:  # re-queued task: its base may be stale
        ok, detail = worktree.rebase_onto(path, base)
        if ok:
            base_sha = worktree.merge_base(project, branch, base)
        else:
            mono.record("warning", detail, label=f"rebase onto {base} failed - continuing on the old base")
            core.send_message(
                db, agent_name, core.HUMAN, task["id"], "note",
                f"branch {branch} could not be rebased onto {base}: {detail}. "
                f"Working from the old base; resolve by hand before merging."
            )
    # set before the run: reply() reads it to hold the task for review
    fields = {"worktree_path": str(path)}
    if base_sha:  # a failed rebase leaves the recorded base unchanged
        fields["worktree_base_sha"] = base_sha
    core.update_task(db, task["id"], **fields)
    return path, branch


def _next_task(db, agent_name: str, poll_interval: float, stop: threading.Event | None) -> dict | None:
    """Claim the next ready task, polling until one turns up; None once `stop` is set."""
    while True:
        if stop is not None and stop.is_set():
            return None
        task = core.claim_task(db, agent_name)
        if task:
            return task
        if stop is not None:
            if stop.wait(poll_interval):
                return None
        else:
            time.sleep(poll_interval)


async def serve(
    project: Path,
    agent_name: str,
    backend: str,
    make_runner,
    poll_interval: float = 2.0,
    max_tasks: int | None = None,
    quiet: bool = False,
    heartbeat_interval: float = RUN_HEARTBEAT_S,
    stop: threading.Event | None = None,
) -> None:
    db = core.connect(core.db_path(project))
    core.init_db(db)
    core.sync_agents_from_config(db, project)
    cfg = core.agent_config(project, agent_name)
    limits = core.run_limits(cfg)
    if cfg.get("worktree"):
        check_git(project, agent_name)
    run = make_runner(db, project, agent_name, cfg)

    log(f"[{agent_name}] {backend} daemon up on {project} (model={cfg.get('model') or 'default'})")
    core.heartbeat(db, agent_name, "idle")
    handled = 0
    try:
        while max_tasks is None or handled < max_tasks:
            task = _next_task(db, agent_name, poll_interval, stop)
            if task is None:
                break
            handled += 1
            log(f"[{agent_name}] task {task['id']}: {task['title']}")
            core.heartbeat(db, agent_name, "working", task["id"])
            started = core.now()
            mono = core.Monologue(db, agent_name, task["id"], quiet=quiet)
            core.start_run(db, mono.run_id, task["id"], agent_name)

            workdir, branch, finished = project, None, False
            with RunHeartbeat(core.db_path(project), mono.run_id, heartbeat_interval):
                try:
                    if task.get("kind") == "review":
                        # a review reads the author's worktree and never commits
                        # there: branch stays None, so no commit_all afterwards
                        source = core.get_task(db, task["review_of"]) if task.get("review_of") else None
                        src_path = source.get("worktree_path") if source else None
                        if not src_path or not Path(src_path).exists():
                            raise core.RunAborted(
                                f"cannot review: task {task.get('review_of')} has no worktree to review"
                            )
                        workdir = Path(src_path)
                    # an answer is a message, not code: it needs no branch to review
                    elif cfg.get("worktree") and not core.is_answer_task(task):
                        workdir, branch = prepare_workdir(db, project, agent_name, task, mono)
                    prompt, inbox_message_ids = core.compose_task_prompt(db, agent_name, task)
                    mono.record("prompt", prompt)
                    try:
                        text, usage = await asyncio.wait_for(run(prompt, workdir, mono), limits["timeout_s"])
                    except TimeoutError:
                        # cancelled mid-stream: the run's final usage never came,
                        # so record what the backend had reported so far
                        raise core.RunAborted(
                            f"timed out after {limits['timeout_s'] / 60:g} minutes", dict(mono.spent)
                        ) from None
                except (KeyboardInterrupt, asyncio.CancelledError):
                    # stopped mid-run: say so on the task rather than leave it in_progress
                    core.fail_task(db, agent_name, task["id"], "interrupted: the daemon was stopped mid-run", **mono.spent)
                    core.end_run(db, mono.run_id, "failed", exit_reason="interrupted", **mono.spent)
                    raise
                except Exception as exc:
                    mono.record("error", f"run failed: {exc}")
                    core.fail_task(db, agent_name, task["id"], str(exc), **getattr(exc, "usage", {}))
                    core.end_run(db, mono.run_id, "failed", exit_reason=str(exc), **getattr(exc, "usage", {}))
                    log(f"[{agent_name}] task {task['id']} failed: {exc}", error=True)
                else:
                    finished = True
                    text = text.strip() or "(no output)"
                    msg_id = core.finish_task(db, agent_name, task["id"], text, started, run_id=mono.run_id, **usage)
                    core.end_run(db, mono.run_id, "finished", result_message_id=msg_id, **usage)
                    # read only once a run has actually used them
                    core.mark_messages_read(db, inbox_message_ids)
                    if task.get("kind") == "review":
                        outcome = core.apply_review_outcome(db, task["id"])
                        mono.record("system", outcome or "-", label="review outcome")
                    final = (core.get_task(db, task["id"]) or task)["status"]
                    if final == "ready_to_merge" and branch is not None:
                        mono.record("system", branch, label="ready to merge")
                        if cfg.get("reviewer") and core.is_work_task(task):
                            review_id = core.request_review(
                                db, task["id"], cfg["reviewer"], worktree.base_branch(project),
                                int(cfg.get("max_review_rounds") or 2),
                            )
                            if review_id:
                                mono.record("system", f"task {review_id} for {cfg['reviewer']}",
                                            label="review requested")
                    # cost and round count are the honest summary; token volume is
                    # dominated by cache reads at a tenth the price
                    summary = (
                        f"${usage.get('cost_usd', 0.0):.4f}, {usage.get('tool_rounds', 0)} rounds, "
                        f"{usage.get('input_tokens', 0)}+{usage.get('cache_read_tokens', 0)}c/"
                        f"{usage.get('output_tokens', 0)} tok"
                    )
                    mono.record("result", text, label=f"{final} - {summary}")
                    log(f"[{agent_name}] task {task['id']} {final} ({summary})")
                finally:
                    # leftovers are committed either way - an uncommitted tree would
                    # fail the next run's rebase - but a failed run's are labelled
                    # as such so a reviewer never mistakes them for finished work
                    if branch is not None:
                        worktree.commit_all(workdir, (
                            f"wip: task {task['id']} uncommitted changes" if finished
                            else f"wip: task {task['id']} partial work from a failed run"
                        ))

            core.heartbeat(db, agent_name, "idle")
            if stop is not None and stop.is_set():
                break
    finally:
        core.heartbeat(db, agent_name, "offline")
        db.close()


def run_daemon(
    project: Path,
    agent_name: str,
    backend: str,
    make_runner,
    poll_interval: float = 2.0,
    max_tasks: int | None = None,
    quiet: bool = False,
    heartbeat_interval: float = RUN_HEARTBEAT_S,
    stop: threading.Event | None = None,
) -> None:
    asyncio.run(serve(
        project, agent_name, backend, make_runner, poll_interval, max_tasks, quiet, heartbeat_interval, stop
    ))
