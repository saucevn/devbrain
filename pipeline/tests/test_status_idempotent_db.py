"""P0-4: apply_status phải idempotent — replay không được double-count roadmap.

Chạy khi có TEST_DATABASE_URL (scratch container, xem Step 2):
  TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:15433/postgres \
    python -m pytest tests/test_status_idempotent_db.py -q
"""
import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL (scratch db)"
)


def _milestone_payload() -> dict:
    return {
        "action": "created",
        "milestone": {"number": 1, "title": "v9.9 test", "state": "open"},
        "repository": {"full_name": "test/repo"},
    }


def test_apply_status_twice_inserts_once():
    import asyncpg

    import pipeline

    async def run() -> int:
        conn = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
        await pipeline._init_connection(conn)
        try:
            eid = await conn.fetchval(
                """
                insert into events (source, source_event_id, event_type, actor, payload, occurred_at)
                values ('github', 'it-status-1', 'milestone.created', 'tester', $1, now())
                on conflict (source, source_event_id) do nothing
                returning id
                """,
                _milestone_payload(),
            )
            if eid is None:  # re-run: event already stored
                eid = await conn.fetchval(
                    "select id from events where source='github' and source_event_id='it-status-1'"
                )
            ev = {
                "event_type": "milestone.created",
                "payload": _milestone_payload(),
                "occurred_at": await conn.fetchval("select occurred_at from events where id=$1", eid),
                "id": eid,
            }
            await pipeline.apply_status(conn, ev, 1)
            await pipeline.apply_status(conn, ev, 1)  # replay same event
            return await conn.fetchval(
                "select count(*) from entity_status_history where source_event_id=$1", eid
            )
        finally:
            await conn.close()

    assert asyncio.run(run()) == 1
