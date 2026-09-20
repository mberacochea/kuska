"""Split cache tokens out of the conflated input_tokens column.

Until now the Claude daemon added `cache_read_input_tokens` into `input_tokens`
before storing it, which merged two prices that differ by 10x into one number.
Cost analysis was impossible: a turn reading 3M tokens from cache looked
identical to one paying full price for 3M fresh tokens.

This adds the columns needed to tell them apart, plus `tool_rounds` - the number
of API round-trips in the turn, which is what actually drives cost in an agentic
loop.

Rows written before this migration keep their conflated `input_tokens` and get 0
for the new columns; there is no way to recover the split retroactively, so the
stats page labels them.
"""
from peewee import *

COLUMNS = ("cache_read_tokens", "cache_write_tokens", "tool_rounds")


def up(migrator, db):
    """Add the cache-token and round-count columns to messages."""
    for name in COLUMNS:
        op = migrator.add_column('messages', name, IntegerField(null=True, default=0))
        op.run()


def down(migrator, db):
    """Drop the columns again.

    SQLite cannot drop a column in place; the migrator recreates the table
    without it, preserving the remaining data.
    """
    for name in COLUMNS:
        op = migrator.drop_column('messages', name)
        op.run()
