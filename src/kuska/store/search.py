"""Full-text search over tasks, docs, messages and events (FTS5)."""

from __future__ import annotations

from peewee import SqliteDatabase

from .common import bound


def _parse_fts_snippet(snippet_text: str) -> dict:
    """Parse FTS5 snippet with markers into structured parts.

    FTS5's snippet() function returns text with start/end markers around
    matched content. This function splits the snippet into before/match/after
    parts so the template can render each escaped, preventing both injection
    and broken highlighting.

    Args:
        snippet_text: Snippet from FTS5 with <MARK> delimiters.

    Returns:
        dict with keys: before, match, after (all escaped strings).
    """
    if not snippet_text:
        return {"before": "", "match": "", "after": ""}

    # Split on the markers FTS5 used
    parts = snippet_text.split("<MARK>")
    if len(parts) < 2:
        # No match found (shouldn't happen, but handle it)
        return {"before": snippet_text, "match": "", "after": ""}

    before = parts[0]
    rest = "<MARK>".join(parts[1:])

    match_parts = rest.split("</MARK>")
    if len(match_parts) < 2:
        # Malformed, treat all as before
        return {"before": snippet_text, "match": "", "after": ""}

    match = match_parts[0]
    after = "</MARK>".join(match_parts[1:])

    return {
        "before": before,
        "match": match,
        "after": after,
    }


def _fts_search_table(
    db: SqliteDatabase,
    table: str,
    query: str,
    limit: int | None = None,
) -> list[dict]:
    """Search a single FTS5 table and return results with full context.

    Uses FTS5's snippet() and bm25() functions for highlighting and scoring.
    Returns all matching results (no limit), allowing the caller to handle
    pagination across multiple tables.

    Args:
        db: SqliteDatabase instance for this project.
        table: Table name ('docs', 'messages', 'tasks', 'events').
        query: FTS5 query string (supports AND, OR, NOT, "phrase").
        limit: Optional maximum results per table (for performance tuning).

    Returns:
        list[dict]: Result dicts with keys: table, id, title, snippet_before,
                    snippet_match, snippet_after, rank, metadata.
    """
    fts_table = f"{table}_fts"
    results = []

    # Map of table to (join_table, search_columns, text_column)
    table_specs = {
        "docs": {
            "join_table": "docs",
            "join_on": "d.rowid = f.rowid",
            "search_col": "content",
            "join_select": "d.key, d.updated_by, d.updated_at",
        },
        "messages": {
            "join_table": "messages",
            "join_on": "m.id = f.rowid",
            "search_col": "payload",
            "join_select": "m.sender, m.recipient, m.task_id, m.msg_type, m.ts, m.input_tokens, m.output_tokens, m.cache_read_tokens, m.cache_write_tokens, m.tool_rounds, m.cost_usd, m.read_at",
        },
        "tasks": {
            "join_table": "tasks",
            "join_on": "t.id = f.rowid",
            "search_col": "title",
            "join_select": "t.title, t.assigned_to, t.status, t.created_at, t.updated_at",
        },
        "events": {
            "join_table": "events",
            "join_on": "e.id = f.rowid",
            "search_col": "body",
            "join_select": "e.ts, e.agent, e.task_id, e.run_id, e.kind, e.label",
        },
    }

    if table not in table_specs:
        return results

    spec = table_specs[table]

    # Use FTS5's snippet() and bm25() functions
    # Note: FTS5 functions require the actual table name, not an alias, so we use fts_table directly
    fts_query = f"""
        SELECT f.rowid, bm25({fts_table}) as rank,
               snippet({fts_table}, -1, '<MARK>', '</MARK>', '…', 32) as snippet,
               {spec['join_select']}
        FROM {fts_table} f
        JOIN {spec['join_table']} {spec['join_table'][0]} ON {spec['join_on']}
        WHERE f.{fts_table} MATCH ?
        ORDER BY rank
    """
    if limit:
        fts_query += f" LIMIT {limit}"

    fts_results = db.execute_sql(fts_query, (query,)).fetchall()

    for row in fts_results:
        row_id = row[0]
        bm25_score = row[1]
        snippet_text = row[2]

        # Parse remaining columns based on table
        if table == "docs":
            key, updated_by, updated_at = row[3], row[4], row[5]
            title = key if key else f"Doc #{row_id}"
            metadata = {
                "key": key,
                "updated_by": updated_by,
                "updated_at": updated_at,
            }
        elif table == "messages":
            sender, recipient, task_id, msg_type, ts, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, tool_rounds, cost_usd, read_at = row[3:15]
            sender_name = sender if sender else "unknown"
            first_50 = (snippet_text or "")[:50]
            title = f"From {sender_name}: {first_50}"
            metadata = {
                "sender": sender,
                "recipient": recipient,
                "task_id": task_id,
                "msg_type": msg_type,
                "ts": ts,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "tool_rounds": tool_rounds,
                "cost_usd": cost_usd,
                "read_at": read_at,
            }
        elif table == "tasks":
            title_text, assigned_to, status, created_at, updated_at = row[3], row[4], row[5], row[6], row[7]
            title = title_text if title_text else f"Task #{row_id}"
            metadata = {
                "assigned_to": assigned_to,
                "status": status,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        elif table == "events":
            ts, agent, task_id, run_id, kind, label = row[3:9]
            first_50 = (snippet_text or "")[:50]
            if task_id:
                title = f"Task #{task_id}: {first_50}"
            else:
                title = f"Event #{row_id}: {first_50}"
            metadata = {
                "ts": ts,
                "agent": agent,
                "task_id": task_id,
                "run_id": run_id,
                "kind": kind,
                "label": label,
            }

        snippet_parts = _parse_fts_snippet(snippet_text or "")

        results.append({
            "table": table,
            "id": row_id,
            "title": title,
            "snippet_before": snippet_parts["before"],
            "snippet_match": snippet_parts["match"],
            "snippet_after": snippet_parts["after"],
            "rank": bm25_score,
            "metadata": metadata,
        })

    return results


@bound
def full_text_search(
    db: SqliteDatabase,
    query: str,
    tables: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Search across FTS5 indexes for query terms.

    Main search function supporting FTS5 query syntax (AND, OR, NOT, "phrase").
    Searches multiple tables simultaneously, combines and ranks results by
    relevance (BM25 score).

    Args:
        db: SqliteDatabase instance for this project.
        query: FTS5 query string (e.g., "agent AND task", '"exact phrase"', "NOT archived").
        tables: Optional filter by table names (['docs', 'messages', 'tasks', 'events']).
                If None, searches all tables.
        limit: Maximum results to return (default 50).
        offset: Number of results to skip for pagination (default 0).

    Returns:
        list[dict]: Combined results from all searched tables, ranked by relevance.
                    Each dict has keys: table, id, title, snippet, rank, metadata.

    Raises:
        ValueError: If query is too short (less than 2 characters).

    Examples:
        >>> results = full_text_search(db, "agent AND task", tables=["tasks", "messages"])
        >>> for result in results:
        ...     print(f"{result['table']}: {result['title']} (rank: {result['rank']})")

        >>> # Search all tables
        >>> results = full_text_search(db, '"exact phrase"')

        >>> # Pagination
        >>> page1 = full_text_search(db, "query", limit=10, offset=0)
        >>> page2 = full_text_search(db, "query", limit=10, offset=10)
    """
    # Validate query length
    if len(query.strip()) < 2:
        raise ValueError("Query must be at least 2 characters")

    # Default to all tables if not specified
    search_tables = tables if tables else ["docs", "messages", "tasks", "events"]

    # Validate table names
    valid_tables = {"docs", "messages", "tasks", "events"}
    search_tables = [t for t in search_tables if t in valid_tables]
    if not search_tables:
        return []

    # Search each table and collect results
    # Use a larger per-table limit to ensure we get enough results after combining and ranking
    # This helps avoid edge cases where offset skips too many results
    per_table_limit = max(500, limit * len(search_tables))

    all_results = []
    for table in search_tables:
        table_results = _fts_search_table(db, table, query, limit=per_table_limit)
        all_results.extend(table_results)

    # Sort by rank ascending across all tables (negative scores, more negative = better match)
    all_results.sort(key=lambda x: x["rank"])

    # Normalize BM25 scores (negative, more negative = better match) to a 0-1
    # range for display: best match -> 1.0, worst -> 0.0.
    if all_results:
        best_rank = all_results[0]["rank"]
        worst_rank = all_results[-1]["rank"]
        rank_range = worst_rank - best_rank
        for result in all_results:
            if rank_range == 0:
                result["rank_normalized"] = 1.0
            else:
                result["rank_normalized"] = max(
                    0.0, min(1.0, (worst_rank - result["rank"]) / rank_range)
                )

    # Apply offset and limit across combined results
    end_idx = offset + limit
    return all_results[offset:end_idx]
