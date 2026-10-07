# Agents, tasks and messages

How work moves through kuska: a human queues a task, an agent's daemon claims
it and runs one fresh invocation, and the result comes back as a message and
a status change. Agents never wait on each other inline; a question becomes a
task of its own.

## One task, end to end

```mermaid
sequenceDiagram
  autonumber
  actor H as You
  participant W as Web UI
  participant DB as SQLite
  participant D as Daemon (loop.py)
  participant A as Agent run (LLM + kuska tools)
  participant B as Other agent
  participant G as Git
  participant S as Supervisor (supervisor.py)

  H->>W: create task, assign agent, pick feature
  W->>DB: INSERT task (todo)
  H->>W: move to Ready
  W->>DB: status = ready
  loop every 2s
    D->>DB: claim_task — BEGIN IMMEDIATE, ready + deps done
  end
  DB-->>D: task now in_progress
  D->>DB: heartbeat working
  D->>DB: start_run (runs row, heartbeat every 30s)
  opt worktree = true
    D->>G: worktree add kuska/N-slug (rebase if requeued)
  end
  D->>DB: compose_task_prompt — dep handovers, task thread, unread inbox
  D->>A: fresh invocation (prompt, workdir)
  A-->>DB: events via Monologue (text, thinking, tool calls)
  A->>DB: docs_get / search / create_task (lands in todo)
  opt needs another agent
    A->>DB: send_message(question) creates answer task (ready) + dependency
    A->>DB: reply(blocked) turns into ready, waits on answer
    B->>DB: claims answer task, reply(done)
    Note over D,DB: asking task claimable again, answer arrives as dep context
  end
  A->>DB: reply(payload, handover, status)
  Note over DB: done becomes ready_to_merge for worktree tasks
  A-->>D: text + usage
  alt success
    D->>DB: finish_task — result msg, mark inbox read
    D->>DB: end_run (finished/failed + usage: the cost ledger)
  else error, timeout or limit
    D->>DB: fail_task — blocker msg, status blocked
    D->>DB: end_run (finished/failed + usage: the cost ledger)
  end
  opt worktree
    D->>G: commit leftovers (wip)
  end
  D->>DB: heartbeat idle
  H->>G: review + merge branch
  S->>DB: sees merged branch, sets done
  Note over DB: dependents unblocked and claimable
```

## Crash recovery

A daemon that dies mid-run stops heartbeating its `runs` row. The supervisor
(a thread of `kuska serve` and `kuska run-all`, or `kuska supervise`) ends any
run silent for 300 s as `abandoned`, sends the human a `blocker` message and
blocks the task if it is still `in_progress`. It also sets the agent offline.
The same sweep moves `ready_to_merge` tasks whose branch is merged to `done`.
A branch counts as merged when its commits are in the base branch, or when a
commit on base since the task started carries the `Kuska-Task: <id>` trailer
that `kuska merge` writes (a squash).

## Task statuses

```mermaid
stateDiagram-v2
  [*] --> todo: human, or an agent's create_task
  [*] --> ready: answer task (ask_agent)
  todo --> ready: human (board, requeue)
  ready --> in_progress: daemon claim_task
  in_progress --> done: reply(done), no worktree
  in_progress --> ready_to_merge: reply(done), worktree
  in_progress --> needs_approval: reply(needs_approval)
  in_progress --> blocked: reply(blocked), or the run failed
  in_progress --> ready: reply while waiting on an answer task
  needs_approval --> done: human approves
  needs_approval --> ready: human sends back
  ready_to_merge --> done: kuska merge, branch merged, or marked merged
  blocked --> ready: human requeues or replies
  done --> ready: human replies on the task
  done --> [*]
```

A daemon that is stopped mid-run (Ctrl+C, or `kuska run-all` shutting down)
blocks its in-flight task with an "interrupted" note.

An agent's `replicas` setting (default 1) is how many workers `kuska run-all`
starts for it. An agent's status is derived, not stored: `working ×N` while it
has N `running` runs, else `idle` if its last heartbeat is under 60 s old, else
`offline`.

Every status change goes through `store/lifecycle.transition()`, which holds
the one table of allowed moves. The events behind the arrows: `make_ready`
(todo to ready), `claim` (ready to in_progress, done inline by `claim_task` to
stay atomic), `finish` (in_progress to done, or to ready_to_merge when the
task has a worktree), `hold` (to needs_approval), `block` (to blocked),
`await_answer` (in_progress back to ready), `approve` (needs_approval to
done), `merged` (ready_to_merge to done) and `requeue` (back to ready, or to
todo when unassigned). Outside the diagram: `park` (back to todo), `close`
(todo, ready, needs_approval or blocked straight to done) and `force` (a human
sets any status; leaves a note).

- A `ready_to_merge` task reaches `done` only through a detected merge or
  "Mark merged" in the merge queue. `kuska merge <id>` squashes and marks it in one step. If git cannot see
  the merge (a squash without the trailer), "Mark merged" asks again with "Mark merged anyway".
- A task is claimable only when it is `ready`, assigned to the claiming agent,
  and every task it depends on is `done`. Anything held (`needs_approval`,
  `ready_to_merge`, `blocked`) holds back its dependents too.
- `todo` is a waiting list. Tasks agents create land there, and a human
  decides when they run.
- The status dropdown on the tasks page can set any status
  directly; the arrows above are the transitions the code makes on its own.
- The tasks page can also move several tasks at once: tick rows, pick a
  status, Move. It follows the board's rules, so `in_progress` is never a
  target, a task already in progress stays put, and `ready` needs an agent;
  tasks that cannot go are skipped and named in the toast.

## Review before merge

A dev agent configured with `reviewer = "<agent>"` gets a review task created
when its task reaches `ready_to_merge`. The review task is `kind = "review"`,
assigned to the reviewer, `ready`, and linked to the reviewed task by
`review_of`. Its description names the branch, base branch, worktree and the
author's handover doc. After `max_review_rounds` reviews (default 2) of one
task, the human gets a note and the task is left for them. The review runs in
the author's worktree (never one of its own, whatever the reviewer's
`worktree` setting) and never commits there. If that worktree is gone, the
review task is blocked with "cannot review".

When the review run finishes, kuska acts on the status the reviewer replied with:

- **`done` (passed):** the review task is `done`, `review_outcome = "passed"`, and
  you get a note on the task. You merge.
- **`needs_approval` (changes requested):** `review_outcome = "changes_requested"`.
  The findings go to the author as a note, the task is requeued to `ready`, and
  the review task is closed. The author's next run sees the findings; when it
  reaches `ready_to_merge` again a new review follows, up to `max_review_rounds`.
- **`blocked` (inconclusive):** `review_outcome = "inconclusive"`, you get a note
  ("Review could not be completed") and the task stays `ready_to_merge`.

```mermaid
flowchart TD
    A[ready_to_merge] --> B[review task]
    B -->|passed| C[you merge]
    B -->|changes requested| D[ready for the author]
    D --> A
    B -->|inconclusive| E[note to you]
```

The merge queue has a Review column linking to the latest review. Merging stays
a human step.

## Messages

| `msg_type` | Sent by | Meaning |
| --- | --- | --- |
| `result` | `reply()`, or the daemon when a run ends | The outcome of a run. Carries no cost: usage lives on the run's `runs` row. |
| `question` | `send_message` | To another agent, it also creates an answer task for them (see below). |
| `blocker` | `send_message`, or `fail_task` | Something stops the work; `fail_task` uses it to say why a run failed. |
| `note` | `send_message`, or a human's reply on a task | Information, nothing to act on by itself. |

A message to an agent stays unread until one of that agent's runs has seen it
in its prompt and succeeded; the daemon marks it read then, so a failed run
never swallows a message. A run's prompt includes only unread messages about
its own task or about no task; messages about other tasks wait for those tasks.

## Asking another agent

1. Agent A, on task N, calls `send_message(recipient=B, msg_type="question", task_id=N)`.
2. kuska creates an answer task for B (a task of kind `answer`, status `ready`) and
   makes task N depend on it.
3. A calls `reply(status="blocked")` and stops. Because N waits on an answer,
   the reply puts it back to `ready`, held by the dependency.
4. B claims the answer task, answers in its `reply` payload, and the task is `done`.
5. N is claimable again. A's next run gets B's answer as dependency context.

This goes one level deep: an answer task cannot ask a question of its own.

## Handing work on

When a task finishes, whatever depends on it gets its handover in the prompt:
the `handover` argument of its `reply` (stored as the doc
`task_<id>_<agent>_context`), or else its final result message. Every
dependency contributes, not only the latest.

## Roles

| Flavor | Does | Extra tools |
| --- | --- | --- |
| `planner` | Turns a goal into a plan doc and small tasks, with dependencies and a feature. | `add_tag`, `remove_tag`, `set_task_feature` |
| `dev` | Implements a task, usually in its own worktree, and commits there. | - |
| `reviewer` | Reads another agent's work and reports findings. | - |

Every flavor gets the base set: `get_inbox`, `send_message`, `reply`,
`docs_get`, `docs_set`, `docs_list`, `create_task`, `list_tasks`,
`list_features`, `search`, `list_tags`. See [MCP_TOOLS.md](MCP_TOOLS.md).
