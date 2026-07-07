"""ADR 0040 Batch 5 — the transcripts.db store (stdlib sqlite3 write-through)."""

import json
import sqlite3

import pytest

from omicsclaw.runtime.storage.transcript_db import TranscriptDB, dumps_message


def _db(tmp_path):
    return TranscriptDB(tmp_path / "transcripts.db")


def test_append_then_rehydrate_preserves_order(tmp_path):
    db = _db(tmp_path)
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "run clustering"},
    ]
    for m in msgs:
        db.append("chat-1", m)
    assert db.rehydrate("chat-1") == msgs


def test_replace_is_atomic_and_overwrites(tmp_path):
    db = _db(tmp_path)
    for m in [{"role": "user", "content": f"m{i}"} for i in range(5)]:
        db.append("c", m)
    # Collapse: replace with [summary, *survivors].
    collapsed = [
        {"role": "system", "content": "## Persisted Compacted Context ..."},
        {"role": "user", "content": "m4"},
    ]
    db.replace("c", collapsed)
    assert db.rehydrate("c") == collapsed


def test_replace_with_empty_list(tmp_path):
    db = _db(tmp_path)
    db.append("c", {"role": "user", "content": "x"})
    db.replace("c", [])
    assert db.rehydrate("c") == []
    assert db.has("c") is True  # chat row remains, just no messages


def test_clear_removes_the_chat(tmp_path):
    db = _db(tmp_path)
    db.append("c", {"role": "user", "content": "x"})
    assert db.has("c") is True
    db.clear("c")
    assert db.has("c") is False
    assert db.rehydrate("c") == []


def test_type_tagged_key_int_and_str_do_not_collide(tmp_path):
    db = _db(tmp_path)
    db.append(7, {"role": "user", "content": "int-seven"})
    db.append("7", {"role": "user", "content": "str-seven"})
    assert db.rehydrate(7) == [{"role": "user", "content": "int-seven"}]
    assert db.rehydrate("7") == [{"role": "user", "content": "str-seven"}]


def test_rehydrate_absent_chat_is_empty(tmp_path):
    db = _db(tmp_path)
    assert db.rehydrate("nope") == []
    assert db.has("nope") is False


def test_byte_identity_of_complex_message(tmp_path):
    # ADR 0040 S4: a message with tool_calls + a full_result_path ref round-trips to
    # an EQUAL dict AND to identical canonical bytes (so downstream request
    # serialization is byte-stable across a rehydrate).
    db = _db(tmp_path)
    msg = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "thinking about the h5ad",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "inspect_data",
                    "arguments": '{"path":"/data/study/sample.h5ad","layer":"counts"}',
                },
            }
        ],
    }
    tool_msg = {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "[tool result compacted]\nfull_result_path: /tmp/oc/r1.txt\npreview:\n...",
    }
    db.append("c", msg)
    db.append("c", tool_msg)
    rehydrated = db.rehydrate("c")
    assert rehydrated == [msg, tool_msg]
    # Canonical serialization is stable across the round-trip.
    assert dumps_message(rehydrated[0]) == dumps_message(msg)
    assert dumps_message(rehydrated[1]) == dumps_message(tool_msg)


def test_durable_across_restart(tmp_path):
    # ADR 0040: a graceful restart (a fresh TranscriptDB on the same file) rehydrates
    # the committed state — the whole point of write-through.
    path = tmp_path / "transcripts.db"
    db1 = TranscriptDB(path)
    db1.append("c", {"role": "user", "content": "before restart"})
    db1.append("c", {"role": "assistant", "content": "ok"})
    db1.close()

    db2 = TranscriptDB(path)  # simulate process restart
    assert db2.rehydrate("c") == [
        {"role": "user", "content": "before restart"},
        {"role": "assistant", "content": "ok"},
    ]


def test_append_rolls_back_on_mid_transaction_error(tmp_path):
    # ADR 0040: append() does TWO writes (bump chats.last_seq, then INSERT the
    # message). If the message INSERT fails AFTER the last_seq bump, the mutation
    # must roll back atomically — else the connection is left mid-transaction and a
    # LATER commit finalizes the orphaned last_seq bump (a silent seq desync).
    db = _db(tmp_path)
    db.append("c", {"role": "user", "content": "m0"})
    db.append("c", {"role": "user", "content": "m1"})
    # Corrupt last_seq to point BEFORE the highest existing seq so the next append
    # computes a colliding seq -> the message INSERT raises IntegrityError after
    # the chats UPDATE has already run (a natural mid-transaction failure).
    with db._lock:
        db._conn.execute("UPDATE transcript_chats SET last_seq=0 WHERE chat_key=?", ("s:c",))
        db._conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        db.append("c", {"role": "user", "content": "collision"})

    # Rolled back cleanly: no dirty transaction lingering on the connection...
    assert db._conn.in_transaction is False
    # ...and the last_seq bump was undone (not left for a later commit to finalize).
    row = db._conn.execute(
        "SELECT last_seq FROM transcript_chats WHERE chat_key=?", ("s:c",)
    ).fetchone()
    assert row[0] == 0
    # The store stays usable and consistent — the failed message never landed.
    assert db.rehydrate("c") == [
        {"role": "user", "content": "m0"},
        {"role": "user", "content": "m1"},
    ]


def test_non_ascii_content_round_trips(tmp_path):
    db = _db(tmp_path)
    msg = {"role": "user", "content": "请对 pbmc.h5ad 做聚类分析 🧬"}
    db.append("c", msg)
    assert db.rehydrate("c") == [msg]
    # ensure_ascii=False keeps the CJK/emoji literal (no \\uXXXX blow-up).
    assert "请" in dumps_message(msg)
    assert json.loads(dumps_message(msg)) == msg


# --------------------------------------------------------------------------- #
# ADR 0040 Batch 6 — wiring into TranscriptStore (mirror + lazy rehydrate)
# --------------------------------------------------------------------------- #

from omicsclaw.runtime.storage.transcript import TranscriptStore  # noqa: E402


def test_store_mirrors_and_rehydrates_across_restart(tmp_path):
    # ADR 0040: a store with a db mirrors appends; a fresh store on the same db
    # rehydrates them byte-identically on first access (cold-start / restart).
    path = tmp_path / "transcripts.db"
    store1 = TranscriptStore(db=TranscriptDB(path))
    store1.append_user_message("chat", "hello")
    store1.append_assistant_message("chat", content="hi there")
    store1.append_tool_message("chat", tool_call_id="c1", content="result")
    expected = store1.get_history("chat")
    store1.db.close()

    store2 = TranscriptStore(db=TranscriptDB(path))  # simulate restart
    assert "chat" not in store2.messages_by_chat  # cold
    assert store2.get_history("chat") == expected  # rehydrated on miss


def test_store_replace_history_mirrors(tmp_path):
    path = tmp_path / "transcripts.db"
    store = TranscriptStore(db=TranscriptDB(path))
    for i in range(4):
        store.append_user_message("c", f"m{i}")
    collapsed = [{"role": "system", "content": "summary"}, {"role": "user", "content": "m3"}]
    store.replace_history("c", collapsed)
    store.db.close()
    store2 = TranscriptStore(db=TranscriptDB(path))
    assert store2.get_history("c") == collapsed


def test_store_clear_deletes_durable_rows(tmp_path):
    path = tmp_path / "transcripts.db"
    store = TranscriptStore(db=TranscriptDB(path))
    store.append_user_message("c", "x")
    store.clear("c")
    store.db.close()
    store2 = TranscriptStore(db=TranscriptDB(path))
    assert store2.get_history("c") == []


def test_store_lru_evict_keeps_durable_rows(tmp_path):
    # ADR 0040 D6: LRU eviction clears memory but KEEPS the db rows so a revisit
    # rehydrates — eviction must NOT delete durable state.
    path = tmp_path / "transcripts.db"
    store = TranscriptStore(max_conversations=1, db=TranscriptDB(path))
    store.append_user_message("old", "old message")
    store.touch("old", at=1.0)
    store.append_user_message("new", "new message")
    store.touch("new", at=2.0)
    evicted = store.evict_lru_conversations()
    assert "old" in evicted and "old" not in store.messages_by_chat
    assert store.get_history("old") == [{"role": "user", "content": "old message"}]


def test_store_without_db_is_pure_in_memory(tmp_path):
    # Backward-compat: no db -> pre-0040 in-process behaviour, nothing persisted.
    store = TranscriptStore()
    store.append_user_message("c", "x")
    assert store.get_history("c") == [{"role": "user", "content": "x"}]
    assert TranscriptStore().get_history("c") == []
