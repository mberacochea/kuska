"""Drop the file_claims table.

Worktrees replace advisory claims. The claim system was also never working:
`active_claims` only counts a claim whose agent heartbeated within ~180 seconds,
and no daemon heartbeats mid-run — only at task boundaries. Any invocation longer
than three minutes silently dropped its own claims. Nothing of value is lost.
"""

from peewee import *


def up(migrator, db):
    """Drop the file_claims table."""
    migrator.drop_table('file_claims').run()


def down(migrator, db):
    """Rollback is not supported - the table cannot be recreated."""
    pass
