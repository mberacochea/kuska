"""Backfill runs from the usage booked on messages, so runs can be the only ledger.

Until now a run's tokens and cost were booked on its result message, and the
stats read them there. They now read `runs`. Each message that carries usage
and is not the `result_message_id` of some run (those runs already hold the
usage) gets a `finished` run of its own: id `m<message id>`, the message's
task and sender, its usage, and started_at = ended_at = the message's ts.

The usage columns stay on `messages`: older running kuska processes still read
them, and nothing here clears them. Re-running is harmless: ids are unique.

(Numbered 020: 018 and 019 were taken by the time this was written.)
"""


def up(migrator, db):
    """Insert one finished run per message that carries usage and has no run."""
    db.execute_sql(
        'INSERT OR IGNORE INTO "runs" ('
        '"id", "task_id", "agent", "status", "started_at", "heartbeat_at", "ended_at", '
        '"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", '
        '"tool_rounds", "cost_usd", "result_message_id") '
        "SELECT 'm' || m.id, m.task_id, m.sender, 'finished', m.ts, m.ts, m.ts, "
        "COALESCE(m.input_tokens, 0), COALESCE(m.output_tokens, 0), "
        "COALESCE(m.cache_read_tokens, 0), COALESCE(m.cache_write_tokens, 0), "
        "COALESCE(m.tool_rounds, 0), COALESCE(m.cost_usd, 0.0), m.id "
        'FROM "messages" m '
        "WHERE (COALESCE(m.cost_usd, 0) > 0 OR COALESCE(m.input_tokens, 0) > 0 "
        "OR COALESCE(m.output_tokens, 0) > 0 OR COALESCE(m.cache_read_tokens, 0) > 0 "
        "OR COALESCE(m.cache_write_tokens, 0) > 0) "
        'AND NOT EXISTS (SELECT 1 FROM "runs" r WHERE r."result_message_id" = m.id)'
    )


def down(migrator, db):
    """Remove the runs this migration made (ids `m<message id>`)."""
    db.execute_sql("DELETE FROM \"runs\" WHERE \"id\" GLOB 'm[0-9]*'")
