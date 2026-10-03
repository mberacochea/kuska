"""tasks.worktree_base_sha: the commit a task's branch started from.

Merge detection needs it: once a branch is merged into base it is no longer
ahead of base, so "merged and ahead" never holds. A branch counts as merged
when its tip is in base and differs from this sha (it had commits of its own).
"""


def up(migrator, db):
    """Add the nullable column; existing tasks have no recorded base."""
    db.execute_sql('ALTER TABLE "tasks" ADD COLUMN "worktree_base_sha" TEXT')


def down(migrator, db):
    """Drop the column."""
    migrator.drop_column('tasks', 'worktree_base_sha').run()
