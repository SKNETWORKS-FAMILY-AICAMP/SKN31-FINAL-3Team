"""Account-owned chat persistence. The only assistant SQL writes target this table.

Connections close before LLM calls. An optimistic UPDATE (never upsert) ensures
that a reply cannot resurrect a deleted session or overwrite another tab's turn.
"""
from __future__ import annotations

import json
from uuid import UUID

from psycopg.types.json import Jsonb
from procurement_db import get_connection


class SessionNotFound(Exception):
    pass


class SessionConflict(Exception):
    pass


def _view(row, *, full=True):
    result = {
        "id": str(row["session_id"]), "title": row["title"],
        "updatedAt": row["updated_at"].isoformat(), "version": row["version"],
    }
    if full:
        result.update(messages=row["messages"], dialogue=row["dialogue"])
    else:
        result["preview"] = row.get("preview") or ""
    return result


def _limits(conn):
    conn.execute("SET LOCAL statement_timeout = '5000ms'")
    conn.execute("SET LOCAL lock_timeout = '1500ms'")


class AssistantSessionStore:
    def list(self, owner: str):
        with get_connection() as conn:
            _limits(conn)
            rows = conn.execute("""
                SELECT session_id, title, updated_at, version, messages->-1->>'text' AS preview
                FROM procurement.assistant_session WHERE owner_id = %s
                ORDER BY updated_at DESC, session_id LIMIT 20
            """, (owner,)).fetchall()
        return [_view(row, full=False) for row in rows]

    def create(self, owner: str, session_id: UUID):
        with get_connection() as conn:
            _limits(conn)
            # Serialize only this account's short chat creation transaction.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 628031))", (owner,))
            existing = conn.execute("SELECT * FROM procurement.assistant_session WHERE session_id = %s AND owner_id = %s", (session_id, owner)).fetchone()
            if existing:
                return _view(existing)
            count = conn.execute("SELECT count(*) AS n FROM procurement.assistant_session WHERE owner_id = %s", (owner,)).fetchone()["n"]
            if count >= 20:
                raise SessionConflict("대화는 최대 20개까지 저장됩니다. 필요 없는 대화를 삭제한 뒤 추가해 주세요.")
            row = conn.execute("""
                INSERT INTO procurement.assistant_session(session_id, owner_id)
                VALUES (%s, %s) ON CONFLICT (session_id) DO NOTHING RETURNING *
            """, (session_id, owner)).fetchone()
            if not row:
                raise SessionConflict("대화를 만들지 못했습니다. 다시 시도해 주세요.")
        return _view(row)

    def get(self, owner: str, session_id: UUID):
        with get_connection() as conn:
            _limits(conn)
            row = conn.execute("SELECT * FROM procurement.assistant_session WHERE session_id = %s AND owner_id = %s", (session_id, owner)).fetchone()
        if not row:
            raise SessionNotFound()
        return _view(row)

    def delete(self, owner: str, session_id: UUID):
        with get_connection() as conn:
            _limits(conn)
            row = conn.execute("DELETE FROM procurement.assistant_session WHERE session_id = %s AND owner_id = %s RETURNING session_id", (session_id, owner)).fetchone()
        if not row:
            raise SessionNotFound()

    def append_turn(self, owner: str, session, question: str, response):
        payload = response.model_dump(mode="json", exclude={"dialogue"})
        messages = [*session["messages"], {"sender": "user", "text": question},
                    {"sender": "agent", "text": response.answer, "response": payload}][-80:]
        # Bound both turns and serialized bytes, including record cards.
        while len(messages) > 2 and len(json.dumps(messages, ensure_ascii=False).encode()) > 512_000:
            messages = messages[2:]
        title = session["title"] if session["messages"] else question[:60]
        with get_connection() as conn:
            _limits(conn)
            row = conn.execute("""
                UPDATE procurement.assistant_session
                SET title = %s, messages = %s, dialogue = %s, version = version + 1, updated_at = now()
                WHERE session_id = %s AND owner_id = %s AND version = %s RETURNING version
            """, (title, Jsonb(messages), Jsonb(response.dialogue.model_dump() if response.dialogue else None),
                  session["id"], owner, session["version"])).fetchone()
        if not row:
            raise SessionConflict("대화가 삭제되었거나 다른 탭에서 변경되었습니다. 세션 목록을 다시 열어 주세요.")
        return row["version"]
