"""Features: named groups of related tasks."""

from __future__ import annotations

from peewee import JOIN, Case, SqliteDatabase, fn

from ..db import now
from ..models import Feature, Task, row, rows
from .common import bound


def norm_feature_name(value: str | None) -> str | None:
    """Free text in, a stable feature name out. None means no feature."""
    return ((value or "").strip().lower())[:40] or None


@bound
def ensure_feature(db: SqliteDatabase, name: str | None, description: str | None = None) -> int | None:
    """The id of the feature called `name`, creating it if there is none.

    The name is normalised first, so "Search " and "search" are one feature.
    `description` is set on a new feature, or on an existing one that has
    none yet; it never overwrites one already written. An empty name means
    no feature and returns None.
    """
    name = norm_feature_name(name)
    if name is None:
        return None
    existing = Feature.get_or_none(Feature.name == name)
    if existing is not None:
        if description and not existing.description:
            Feature.update(description=description, updated_at=now()).where(
                Feature.id == existing.id
            ).execute()
        return int(existing.id)
    ts = now()
    Feature.insert(name=name, description=description or None, created_at=ts, updated_at=ts).on_conflict_ignore().execute()
    # re-read rather than trust the insert's id: another process may have won
    return int(Feature.get(Feature.name == name).id)


@bound
def get_feature(db: SqliteDatabase, feature_id: int) -> dict | None:
    """One feature by id, or None."""
    return row(Feature.select().where(Feature.id == feature_id))


@bound
def get_feature_by_name(db: SqliteDatabase, name: str) -> dict | None:
    """One feature by (normalised) name, or None."""
    return row(Feature.select().where(Feature.name == norm_feature_name(name)))


@bound
def list_features(db: SqliteDatabase) -> list[dict]:
    """Every feature, by name, with how many tasks it has and how many are done.

    Returns:
        list[dict]: id, name, description, created_at, updated_at, total, done.
    """
    query = (
        Feature.select(
            Feature,
            fn.COUNT(Task.id).alias("total"),
            fn.COALESCE(fn.SUM(Case(None, [(Task.status == "done", 1)], 0)), 0).alias("done"),
        )
        .join(Task, JOIN.LEFT_OUTER, on=(Task.feature_id == Feature.id))
        .group_by(Feature.id)
        .order_by(Feature.name)
    )
    return rows(query)


@bound
def update_feature(
    db: SqliteDatabase, feature_id: int, name: str | None = None, description: str | None = None
) -> None:
    """Rename a feature and/or change its description. Every task in it
    follows, since tasks point at the feature, not its name.

    Raises:
        ValueError: the new name is empty or already taken by another feature.
    """
    sets: dict = {}
    if name is not None:
        normalised = norm_feature_name(name)
        if normalised is None:
            raise ValueError("a feature needs a name")
        clash = Feature.get_or_none((Feature.name == normalised) & (Feature.id != feature_id))
        if clash is not None:
            raise ValueError(f"there is already a feature called {normalised!r}")
        sets["name"] = normalised
    if description is not None:
        sets["description"] = description or None
    if sets:
        sets["updated_at"] = now()
        Feature.update(**sets).where(Feature.id == feature_id).execute()


@bound
def delete_feature(db: SqliteDatabase, feature_id: int) -> int:
    """Delete a feature. Its tasks stay, ungrouped. Returns how many were."""
    freed = (
        Task.update(feature_id=None, updated_at=now()).where(Task.feature_id == feature_id).execute()
    )
    Feature.delete().where(Feature.id == feature_id).execute()
    return freed
