"""Unit tests for database operations."""
import os
import tempfile
import unittest

class TestDatabase(unittest.TestCase):
    def setUp(self):
        import app.database as db
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        db._db_path = self.db_path
        db.init_db()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_wal_is_persistent_and_not_renegotiated_per_request(self):
        from unittest.mock import patch
        import app.database as db

        with db.get_db() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

        statements = []
        original_connect = db.sqlite3.connect

        def recording_connect(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(db.sqlite3, "connect", side_effect=recording_connect):
            with db.get_db() as conn:
                conn.execute("SELECT 1").fetchone()

        self.assertFalse(
            any("journal_mode" in statement.casefold() for statement in statements),
            statements,
        )

    def test_secure_db_files_tolerates_wal_disappearing(self):
        """A concurrent WAL checkpoint must never kill a background worker."""
        from unittest.mock import patch
        import app.database as db

        with patch.object(db.os, "chmod", side_effect=FileNotFoundError):
            db._secure_db_files()

    def test_upsert_and_get_session(self):
        from app.database import upsert_session, get_session_detail, insert_messages
        session_data = {
            "id": "test-session-1",
            "source": "copilot",
            "server_name": "local",
            "title": "Test Session",
            "cwd": "/tmp",
            "start_time": "2026-04-08T10:00:00Z",
            "message_count": 2,
        }
        is_new = upsert_session(session_data)
        self.assertTrue(is_new)

        messages = [
            {"role": "user", "content": "Hello world", "msg_type": "user.message",
             "timestamp": "2026-04-08T10:00:01Z"},
            {"role": "assistant", "content": "Hi there!", "msg_type": "assistant.message",
             "timestamp": "2026-04-08T10:00:02Z"},
        ]
        insert_messages("test-session-1", messages)

        detail = get_session_detail("test-session-1")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["title"], "Test Session")
        self.assertEqual(len(detail["messages"]), 2)

    def test_full_text_search(self):
        from app.database import upsert_session, insert_messages, search_messages
        upsert_session({
            "id": "search-test-1", "source": "codex", "server_name": "local",
            "title": "Python排序", "start_time": "2026-04-08T10:00:00Z", "message_count": 1,
        })
        insert_messages("search-test-1", [
            {"role": "user", "content": "帮我实现一个快速排序算法 quicksort",
             "msg_type": "user_message", "timestamp": "2026-04-08T10:00:01Z"},
            {"role": "assistant", "content": "好的，这是快速排序的实现：def quicksort(arr)...",
             "msg_type": "output_text", "timestamp": "2026-04-08T10:00:02Z"},
        ])
        results = search_messages("quicksort")
        self.assertGreater(len(results), 0)
        self.assertIn("quicksort", results[0]["content"])

    def test_search_prioritizes_recent_message_hit_over_old_title_hit(self):
        from app.database import insert_messages, search_messages, upsert_session

        upsert_session({
            "id": "old-title-match",
            "source": "codex",
            "server_name": "local",
            "title": "Old Qwen3.5 planning",
            "start_time": "2026-05-01T00:00:00Z",
            "end_time": "2026-05-01T01:00:00Z",
            "message_count": 1,
        })
        insert_messages("old-title-match", [{
            "role": "assistant",
            "content": "unrelated historical content",
            "timestamp": "2026-05-01T01:00:00Z",
        }])

        upsert_session({
            "id": "recent-content-match",
            "source": "codex",
            "server_name": "local",
            "title": "Recent model work",
            "start_time": "2026-07-31T00:00:00Z",
            "end_time": "2026-07-31T01:00:00Z",
            "message_count": 1,
        })
        insert_messages("recent-content-match", [{
            "role": "user",
            "content": "The current implementation supports Qwen3.5.",
            "timestamp": "2026-07-31T01:00:00Z",
        }])

        upsert_session({
            "id": "newest-title-match",
            "source": "codex",
            "server_name": "local",
            "title": "Newest Qwen3.5 overview",
            "start_time": "2026-08-01T00:00:00Z",
            "end_time": "2026-08-01T01:00:00Z",
            "message_count": 1,
        })
        insert_messages("newest-title-match", [{
            "role": "assistant",
            "content": "metadata-only match",
            "timestamp": "2026-08-01T01:00:00Z",
        }])

        results = search_messages("Qwen3.5")
        self.assertEqual(results[0]["session_id"], "newest-title-match")
        self.assertEqual(results[1]["session_id"], "recent-content-match")
        self.assertIn("<mark>Qwen3.5</mark>", results[1]["snippet"])

    def test_search_sessions_groups_hits_and_paginates_by_session(self):
        from app.database import (
            get_sessions,
            insert_messages,
            search_sessions,
            upsert_session,
        )

        fixtures = [
            ("grouped-a", "Newest", "2026-08-03T00:00:00Z", 2),
            ("grouped-b", "Middle", "2026-08-02T00:00:00Z", 1),
            ("grouped-c", "Old grouped-token title", "2026-08-01T00:00:00Z", 0),
        ]
        for session_id, title, end_time, hit_count in fixtures:
            upsert_session({
                "id": session_id,
                "source": "codex",
                "server_name": "local",
                "title": title,
                "start_time": end_time,
                "end_time": end_time,
                "message_count": max(1, hit_count),
            })
            messages = [
                {
                    "role": "user",
                    "content": f"grouped-token occurrence {index}",
                    "timestamp": end_time,
                }
                for index in range(hit_count)
            ] or [{
                "role": "assistant",
                "content": "metadata match only",
                "timestamp": end_time,
            }]
            insert_messages(session_id, messages)

        first_page, total = search_sessions("grouped-token", limit=2)
        second_page, second_total = search_sessions(
            "grouped-token", limit=2, offset=2
        )
        self.assertEqual(total, 3)
        self.assertEqual(second_total, 3)
        self.assertEqual(
            [row["session_id"] for row in first_page],
            ["grouped-a", "grouped-b"],
        )
        self.assertEqual(
            [row["session_id"] for row in second_page],
            ["grouped-c"],
        )
        self.assertEqual(first_page[0]["match_count"], 2)
        self.assertEqual(second_page[0]["match_type"], "session_metadata")

        sidebar_sessions, sidebar_total = get_sessions(
            search="grouped-token"
        )
        self.assertEqual(sidebar_total, 3)
        self.assertEqual(
            [row["id"] for row in sidebar_sessions],
            ["grouped-a", "grouped-b", "grouped-c"],
        )

    def test_precise_session_search_requires_the_complete_phrase(self):
        from app.database import insert_messages, search_sessions, upsert_session

        for session_id, content in (
            ("precise-phrase", "the work hub search is ready"),
            ("separate-terms", "the hub now has a separate work search"),
        ):
            upsert_session({
                "id": session_id,
                "source": "codex",
                "server_name": "local",
                "title": "Unrelated title",
                "message_count": 1,
            })
            insert_messages(session_id, [{
                "role": "user",
                "content": content,
                "msg_type": "input_text",
            }])

        precise, precise_total = search_sessions(
            "work hub", match_mode="precise"
        )
        self.assertEqual(precise_total, 1)
        self.assertEqual(precise[0]["session_id"], "precise-phrase")
        expanded, expanded_total = search_sessions(
            "work hub", match_mode="expanded"
        )
        self.assertGreaterEqual(expanded_total, 2)
        self.assertEqual(
            {row["session_id"] for row in expanded},
            {"precise-phrase", "separate-terms"},
        )

    def test_smart_search_ranks_remembered_task_and_filters_source(self):
        from app.database import insert_messages, search_sessions, upsert_session

        upsert_session({
            "id": "remembered-codex-task",
            "source": "codex",
            "server_name": "local",
            "cwd": "/work/xh2modelzoo",
            "start_time": "2026-07-24T12:00:00Z",
            "end_time": "2026-07-24T13:00:00Z",
            "message_count": 1,
        })
        insert_messages("remembered-codex-task", [{
            "role": "user",
            "content": (
                "python qwen3_5_122B_workflow.py --dump-golden\n"
                "dump-golden时挂了，显存炸了，请定位并修复。"
            ),
            "timestamp": "2026-07-24T12:01:00Z",
        }])

        upsert_session({
            "id": "newer-weak-codex-match",
            "source": "codex",
            "server_name": "local",
            "start_time": "2026-08-01T00:00:00Z",
            "end_time": "2026-08-01T01:00:00Z",
            "message_count": 1,
        })
        insert_messages("newer-weak-codex-match", [{
            "role": "assistant",
            "content": "Qwen3.5 golden documentation",
            "timestamp": "2026-08-01T01:00:00Z",
        }])

        upsert_session({
            "id": "wrong-source-exact-match",
            "source": "claude",
            "server_name": "local",
            "start_time": "2026-08-02T00:00:00Z",
            "end_time": "2026-08-02T01:00:00Z",
            "message_count": 1,
        })
        insert_messages("wrong-source-exact-match", [{
            "role": "user",
            "content": "qwen3.5 122B golden 显存炸掉",
            "timestamp": "2026-08-02T01:00:00Z",
        }])

        results, total = search_sessions(
            "qwen3.5 122b golden 显存炸掉",
            source="codex",
        )
        self.assertEqual(total, 2)
        self.assertEqual(results[0]["session_id"], "remembered-codex-task")
        self.assertNotIn(
            "wrong-source-exact-match",
            {row["session_id"] for row in results},
        )
        self.assertIn("qwen3.5", results[0]["matched_terms"])
        self.assertIn("122b", results[0]["matched_terms"])
        self.assertIn("golden", results[0]["matched_terms"])
        self.assertIn("显存", results[0]["matched_terms"])
        self.assertIn("崩溃/OOM", results[0]["matched_terms"])
        self.assertIn("显存炸了", results[0]["display_title"])

    def test_search_by_session_tag(self):
        from app.database import upsert_session, insert_messages, search_messages, get_sessions
        session_id = "vscode-memory-Server-17-cab059bae2c410de3cf145d3f3b6e5f5-youxidian"
        upsert_session({
            "id": session_id,
            "source": "vscode-memory",
            "server_name": "Server-17",
            "title": "成交分析价格口径",
            "raw_path": "/tmp/youxidian.md",
            "start_time": "2026-04-21T10:00:00Z",
            "message_count": 1,
        })
        insert_messages(session_id, [
            {"role": "system", "content": "memory body", "msg_type": "memory", "timestamp": "2026-04-21T10:00:00Z"}
        ])

        sessions, total = get_sessions(search="5F5YOUXIDIAN")
        self.assertEqual(total, 1)
        self.assertEqual(sessions[0]["session_tag"], "VME-5F5YOUXIDIAN")

        results = search_messages("VME-5F5YOUXIDIAN")
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["session_tag"], "VME-5F5YOUXIDIAN")
        self.assertIn("[Session Match]", results[0]["snippet"])

    def test_claude_session_tag_prefix(self):
        from app.database import build_session_tag

        tag = build_session_tag("claude-local-12345678-90ab-cdef", "claude")
        self.assertTrue(tag.startswith("CLD-"))

    def test_get_sessions_with_filters(self):
        from app.database import upsert_session, get_sessions
        for i in range(5):
            upsert_session({
                "id": f"filter-test-{i}", "source": "copilot" if i % 2 == 0 else "codex",
                "server_name": "server-a" if i < 3 else "server-b",
                "title": f"Session {i}", "start_time": f"2026-04-0{i+1}T10:00:00Z",
                "message_count": i,
            })
        sessions, total = get_sessions(source="copilot")
        self.assertEqual(total, 3)
        sessions, total = get_sessions(server="server-b")
        self.assertEqual(total, 2)

    def test_stats(self):
        from app.database import upsert_session, insert_messages, get_stats
        upsert_session({"id": "stats-1", "source": "copilot", "server_name": "local",
                       "start_time": "2026-04-08T10:00:00Z", "message_count": 1})
        insert_messages("stats-1", [{"role": "user", "content": "test",
                                     "msg_type": "test", "timestamp": "2026-04-08T10:00:00Z"}])
        stats = get_stats()
        self.assertGreaterEqual(stats["total_sessions"], 1)
        self.assertGreaterEqual(stats["total_messages"], 1)

    def test_message_window_preview_anchor_and_full_content(self):
        from app.database import (
            get_db,
            get_message_content,
            get_session_detail,
            insert_messages,
            upsert_session,
        )

        session_id = "windowed-session"
        upsert_session({
            "id": session_id,
            "source": "codex",
            "server_name": "local",
            "start_time": "2026-04-08T10:00:00Z",
            "message_count": 205,
        })
        messages = [
            {
                "role": "assistant",
                "content": ("x" * 2500) if i == 149 else f"message-{i}",
                "msg_type": "agent_message",
                "timestamp": f"2026-04-08T10:{i // 60:02d}:{i % 60:02d}Z",
            }
            for i in range(205)
        ]
        insert_messages(session_id, messages)
        with get_db() as conn:
            anchor_id = conn.execute(
                """SELECT id FROM messages WHERE session_id = ?
                   ORDER BY id LIMIT 1 OFFSET 149""",
                (session_id,),
            ).fetchone()[0]

        detail = get_session_detail(
            session_id,
            msg_limit=100,
            content_limit=128,
            anchor_message_id=anchor_id,
        )
        self.assertEqual(detail["msg_offset"], 100)
        self.assertEqual(detail["msg_page"], 2)
        self.assertEqual(detail["msg_total_pages"], 3)
        self.assertEqual(len(detail["messages"]), 100)
        anchor = next(m for m in detail["messages"] if m["id"] == anchor_id)
        self.assertEqual(len(anchor["content"]), 128)
        self.assertTrue(anchor["content_truncated"])
        self.assertEqual(anchor["content_length"], 2500)

        full = get_message_content(session_id, anchor_id)
        self.assertEqual(len(full["content"]), 2500)

        last = get_session_detail(
            session_id,
            msg_offset=999999,
            msg_limit=100,
            content_limit=128,
        )
        self.assertEqual(last["msg_offset"], 200)
        self.assertEqual(len(last["messages"]), 5)

    def test_message_views_filter_before_pagination_and_report_counts(self):
        from app.database import get_session_detail, insert_messages, upsert_session

        session_id = "message-view-session"
        upsert_session({
            "id": session_id,
            "source": "codex",
            "server_name": "local",
            "message_count": 7,
        })
        insert_messages(session_id, [
            {"role": "user", "content": "question", "msg_type": "input_text"},
            {"role": "assistant", "content": "thinking", "msg_type": "reasoning"},
            {"role": "assistant", "content": "call", "msg_type": "function_call"},
            {"role": "tool", "content": "result", "msg_type": "function_call_output"},
            {"role": "system", "content": "compact", "msg_type": "compaction"},
            {"role": "assistant", "content": "failed step", "msg_type": "error"},
            {"role": "assistant", "content": "final answer", "msg_type": "agent_message"},
        ])

        assistant = get_session_detail(session_id, message_view="assistant")
        self.assertEqual([m["content"] for m in assistant["messages"]], ["final answer"])
        self.assertEqual(assistant["msg_total_unfiltered"], 7)
        self.assertEqual(assistant["message_view_counts"], {
            "all": 7,
            "conversation": 2,
            "assistant": 1,
            "user": 1,
            "process": 5,
        })

        conversation = get_session_detail(session_id, message_view="conversation")
        self.assertEqual(
            [m["content"] for m in conversation["messages"]],
            ["question", "final answer"],
        )
        process = get_session_detail(
            session_id, message_view="process", msg_limit=2, msg_offset=2
        )
        self.assertEqual(process["msg_total"], 5)
        self.assertEqual(process["msg_total_pages"], 3)
        self.assertEqual(
            [m["content"] for m in process["messages"]],
            ["result", "compact"],
        )

    def test_incremental_message_refresh_appends_then_replaces_rewrite(self):
        from app.database import get_db, insert_messages, upsert_session

        session_id = "incremental-session"
        upsert_session({
            "id": session_id,
            "source": "codex",
            "server_name": "local",
            "message_count": 3,
        })
        original = [
            {"role": "user", "content": f"message-{i}", "msg_type": "text"}
            for i in range(3)
        ]
        insert_messages(session_id, original)
        with get_db() as conn:
            original_ids = [
                row[0] for row in conn.execute(
                    "SELECT id FROM messages WHERE session_id = ? ORDER BY id",
                    (session_id,),
                )
            ]

        grown = original + [
            {"role": "assistant", "content": "new-3", "msg_type": "text"},
            {"role": "assistant", "content": "new-4", "msg_type": "text"},
        ]
        inserted = insert_messages(
            session_id, grown, force=True, incremental=True
        )
        self.assertEqual(inserted, 2)
        with get_db() as conn:
            grown_ids = [
                row[0] for row in conn.execute(
                    "SELECT id FROM messages WHERE session_id = ? ORDER BY id",
                    (session_id,),
                )
            ]
        self.assertEqual(grown_ids[:3], original_ids)

        rewritten = [dict(message) for message in grown]
        rewritten[2]["content"] = "rewritten"
        insert_messages(session_id, rewritten, force=True, incremental=True)
        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, content FROM messages
                   WHERE session_id = ? ORDER BY id""",
                (session_id,),
            ).fetchall()
        self.assertTrue(all(row["id"] > grown_ids[-1] for row in rows))
        self.assertEqual(rows[2]["content"], "rewritten")

    def test_partial_session_update_preserves_unsupplied_fields(self):
        from app.database import get_db, upsert_session

        session_id = "partial-update-session"
        upsert_session({
            "id": session_id,
            "source": "copilot",
            "server_name": "local",
            "title": "Initial",
            "cwd": "/work/preserved",
            "model": "provider/model",
            "cli_version": "1.2.3",
            "start_time": "2026-08-07T01:00:00Z",
            "raw_path": "/tmp/source.jsonl",
            "message_count": 1,
        })
        upsert_session({
            "id": session_id,
            "title": "Updated",
            "end_time": "2026-08-07T02:00:00Z",
            "message_count": 2,
        })
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        self.assertEqual(row["title"], "Updated")
        self.assertEqual(row["cwd"], "/work/preserved")
        self.assertEqual(row["model"], "provider/model")
        self.assertEqual(row["cli_version"], "1.2.3")
        self.assertEqual(row["start_time"], "2026-08-07T01:00:00Z")
        self.assertEqual(row["raw_path"], "/tmp/source.jsonl")

    def test_online_compact_fts_rebuild_preserves_search_and_triggers(self):
        from app.database import (
            compact_messages_fts,
            get_db,
            insert_messages,
            search_messages,
            upsert_session,
        )

        session_id = "compact-fts-session"
        upsert_session({
            "id": session_id,
            "source": "codex",
            "server_name": "local",
            "message_count": 1,
        })
        insert_messages(session_id, [{
            "role": "user",
            "content": "compact-index-before",
            "msg_type": "text",
        }])
        inserted_during_backfill = False

        def insert_concurrently(_done, _total):
            nonlocal inserted_during_backfill
            if inserted_during_backfill:
                return
            inserted_during_backfill = True
            concurrent_id = "compact-fts-concurrent"
            upsert_session({
                "id": concurrent_id,
                "source": "codex",
                "server_name": "local",
                "message_count": 1,
            })
            insert_messages(concurrent_id, [{
                "role": "assistant",
                "content": "compact-index-concurrent",
                "msg_type": "text",
            }])

        result = compact_messages_fts(
            batch_size=100,
            progress_callback=insert_concurrently,
        )
        self.assertIn("detail='none'", result["schema"])
        self.assertTrue(search_messages("index-before"))
        self.assertTrue(search_messages("index-concurrent"))

        insert_messages(session_id, [{
            "role": "user",
            "content": "compact-index-after",
            "msg_type": "text",
        }], force=True)
        self.assertTrue(search_messages("index-after"))
        with get_db() as conn:
            trigger_names = {
                row[0] for row in conn.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type = 'trigger' AND name LIKE 'messages_a%'"""
                )
            }
        self.assertEqual(
            trigger_names,
            {"messages_ai", "messages_ad", "messages_au"},
        )

    def test_short_content_query_does_not_fall_back_to_message_scan(self):
        from app.database import (
            get_sessions,
            insert_messages,
            search_messages,
            upsert_session,
        )

        session_id = "short-query-session"
        upsert_session({
            "id": session_id,
            "source": "codex",
            "server_name": "local",
            "title": "Unrelated metadata",
            "message_count": 1,
        })
        insert_messages(session_id, [{
            "role": "user",
            "content": "message-only token: xy",
            "msg_type": "text",
        }])
        self.assertEqual(search_messages("xy"), [])
        self.assertEqual(search_messages("___"), [])
        sessions, total = get_sessions(search="xy")
        self.assertEqual((sessions, total), ([], 0))
        sessions, total = get_sessions(search="message-only")
        self.assertEqual(total, 1)
        self.assertEqual(sessions[0]["id"], session_id)

    def test_search_display_title_ignores_transcript_control_tags(self):
        from app.database import _derive_search_display_title

        concepts = [{"label": "golden", "variants": ["golden"]}]
        self.assertEqual(
            _derive_search_display_title(
                "<subagent_notification>\n</subagent_notification>",
                concepts,
                "/work/qwen-export",
            ),
            "qwen-export",
        )
        self.assertEqual(
            _derive_search_display_title(
                "<command-message>修复 golden 显存 OOM</command-message>",
                concepts,
                None,
            ),
            "修复 golden 显存 OOM",
        )

if __name__ == "__main__":
    unittest.main()
