"""Tests for the derived personal work-memory layer."""

import os
import json
import shutil
import tempfile
import unittest


class WorkMemoryFixture(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import app.database as db

        db._db_path = os.path.join(self.tmpdir, "work.db")
        db.init_db()
        self.db = db

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def add_session(self, session_id="codex-local-work-1", source="codex", title="Qwen export"):
        self.db.upsert_session({
            "id": session_id,
            "source": source,
            "server_name": "local",
            "title": title,
            "cwd": "/work/models",
            "start_time": "2026-07-01T10:00:00Z",
            "end_time": "2026-07-01T11:00:00Z",
            "message_count": 2,
        })
        self.db.insert_messages(session_id, [
            {
                "role": "user",
                "content": "Qwen3.5 122B 模型导出后 dump golden 显存 OOM",
                "msg_type": "user_message",
                "timestamp": "2026-07-01T10:00:00Z",
            },
            {
                "role": "assistant",
                "content": "根因是一次性保留张量，改成分块落盘后峰值显存恢复正常。",
                "msg_type": "agent_message",
                "timestamp": "2026-07-01T11:00:00Z",
            },
        ])
        with self.db.get_db() as conn:
            return [
                row[0] for row in conn.execute(
                    "SELECT id FROM messages WHERE session_id=? ORDER BY id",
                    (session_id,),
                )
            ]

    @staticmethod
    def analysis(message_ids, title="Qwen3.5 122B 导出后 golden 显存 OOM"):
        return {
            "session_summary": "解决模型导出时的显存溢出",
            "primary_project": "模型导出",
            "session_tags": ["Qwen", "显存"],
            "items": [{
                "kind": "task",
                "title": title,
                "project": "模型导出",
                "category": "model-debugging",
                "status": "done",
                "priority": "high",
                "summary": "定位并解决 dump golden 导致的显存峰值",
                "problem": "导出 Qwen3.5 122B 时 GPU OOM",
                "root_cause": "golden 张量一次性驻留显存",
                "solution": "分块生成并及时落盘释放",
                "outcome": "峰值显存恢复正常，导出完成",
                "next_steps": ["补充回归测试"],
                "keywords": ["Qwen3.5", "122B", "golden", "export", "OOM"],
                "entities": [{"type": "model", "name": "Qwen3.5-122B"}],
                "evidence_message_ids": message_ids,
                "confidence": 0.97,
                "dedupe_key": "qwen3.5-122b|export-golden|gpu-oom",
            }],
        }


class TestWorkMemory(WorkMemoryFixture):
    def test_message_import_enqueues_enrichment(self):
        self.add_session()
        with self.db.get_db() as conn:
            job = conn.execute(
                "SELECT * FROM enrichment_jobs WHERE session_id='codex-local-work-1'"
            ).fetchone()
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "pending")
        self.assertTrue(job["source_hash"])
        self.assertTrue(job["not_before"], "live session enrichment must be debounced")

    def test_guardian_session_is_kept_but_skipped_by_enrichment(self):
        session_id = "codex-local-guardian"
        message_ids = self.add_session(session_id)
        from app.work_memory import apply_session_analysis

        item_id = apply_session_analysis(
            session_id, self.analysis(message_ids), 0, "v1", "fake"
        )[0]
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE sessions SET metadata_json=? WHERE id=?",
                (json.dumps({"source": {"subagent": {"other": "guardian"}}}), session_id),
            )
            from app.work_memory import mark_session_pending

            self.assertFalse(mark_session_pending(conn, session_id))
            job = conn.execute(
                "SELECT status,reason FROM enrichment_jobs WHERE session_id=?",
                (session_id,),
            ).fetchone()
            message_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            derived_count = conn.execute(
                "SELECT COUNT(*) FROM work_items WHERE id=?", (item_id,)
            ).fetchone()[0]
        self.assertEqual(dict(job), {"status": "skipped", "reason": "guardian"})
        self.assertEqual(message_count, 2)
        self.assertEqual(derived_count, 0)

    def test_guardian_cleanup_preserves_user_curated_item_and_provenance(self):
        session_id = "codex-local-guardian-curated"
        message_ids = self.add_session(session_id)
        from app.work_memory import apply_session_analysis, mark_session_pending

        item_id = apply_session_analysis(
            session_id, self.analysis(message_ids), 0, "v1", "fake"
        )[0]
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE work_items SET review_state='approved' WHERE id=?",
                (item_id,),
            )
            conn.execute(
                "UPDATE sessions SET metadata_json=? WHERE id=?",
                (json.dumps({"source": {"subagent": {"other": "guardian"}}}), session_id),
            )
            self.assertFalse(mark_session_pending(conn, session_id))
            item = conn.execute(
                "SELECT review_state FROM work_items WHERE id=?", (item_id,)
            ).fetchone()
            source_count = conn.execute(
                """SELECT COUNT(*) FROM work_item_sources
                   WHERE work_item_id=? AND session_id=?""",
                (item_id, session_id),
            ).fetchone()[0]
        self.assertEqual(item["review_state"], "approved")
        self.assertEqual(source_count, 1)

    def test_new_source_version_resets_failed_attempts(self):
        self.add_session()
        with self.db.get_db() as conn:
            conn.execute(
                """UPDATE enrichment_jobs SET status='failed',attempts=11,
                           source_hash='old',error_message='timeout'
                   WHERE session_id='codex-local-work-1'"""
            )
            from app.work_memory import mark_session_pending

            mark_session_pending(conn, "codex-local-work-1", "messages_appended")
            job = conn.execute(
                """SELECT status,attempts,error_message FROM enrichment_jobs
                   WHERE session_id='codex-local-work-1'"""
            ).fetchone()
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["attempts"], 0)
        self.assertIsNone(job["error_message"])

    def test_legacy_cursor_requires_a_complete_successful_segment_batch(self):
        self.add_session()
        from app.work_memory import _infer_processed_message_count

        with self.db.get_db() as conn:
            conn.execute(
                """UPDATE messages SET created_at='2026-07-01T10:00:00Z'
                   WHERE session_id='codex-local-work-1'"""
            )
            conn.execute(
                """INSERT INTO agent_runs(
                       id,run_type,provider,status,session_id,request_json,
                       started_at,finished_at)
                   VALUES ('partial','session_enrichment','fake','success',?,
                           '{"segment_index":0,"segment_count":2}',?,?)""",
                (
                    "codex-local-work-1",
                    "2026-07-01T10:30:00Z",
                    "2026-07-01T10:31:00Z",
                ),
            )
            self.assertEqual(
                _infer_processed_message_count(conn, "codex-local-work-1"), 0
            )
            conn.execute(
                """INSERT INTO agent_runs(
                       id,run_type,provider,status,session_id,request_json,
                       started_at,finished_at)
                   VALUES ('complete','session_enrichment','fake','success',?,
                           '{"segment_index":1,"segment_count":2}',?,?)""",
                (
                    "codex-local-work-1",
                    "2026-07-01T10:40:00Z",
                    "2026-07-01T10:41:00Z",
                ),
            )
            self.assertEqual(
                _infer_processed_message_count(conn, "codex-local-work-1"), 0
            )

    def test_legacy_cursor_accepts_contiguous_segment_runs(self):
        self.add_session()
        from app.work_memory import _infer_processed_message_count

        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE messages SET created_at='2026-07-01T10:00:00Z'"
            )
            conn.executemany(
                """INSERT INTO agent_runs(
                       id,run_type,provider,status,session_id,request_json,
                       started_at,finished_at)
                   VALUES (?,'session_enrichment','fake','success',?,?,?,?)""",
                [
                    (
                        "segment-0", "codex-local-work-1",
                        '{"segment_index":0,"segment_count":2}',
                        "2026-07-01T10:30:00Z", "2026-07-01T10:31:00Z",
                    ),
                    (
                        "segment-1", "codex-local-work-1",
                        '{"segment_index":1,"segment_count":2}',
                        "2026-07-01T10:31:00Z", "2026-07-01T10:32:00Z",
                    ),
                ],
            )
            self.assertEqual(
                _infer_processed_message_count(conn, "codex-local-work-1"), 2
            )

    def test_incremental_context_only_loads_new_messages_with_small_overlap(self):
        self.add_session()
        appended = [
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"incremental-{index}",
                "msg_type": "user_message" if index % 2 == 0 else "agent_message",
                "timestamp": f"2026-07-01T11:{index:02d}:00Z",
            }
            for index in range(20)
        ]
        with self.db.get_db() as conn:
            conn.executemany(
                """INSERT INTO messages(session_id,role,content,msg_type,timestamp)
                   VALUES ('codex-local-work-1',?,?,?,?)""",
                [
                    (item["role"], item["content"], item["msg_type"], item["timestamp"])
                    for item in appended
                ],
            )

        from app.enrichment import _load_session_context

        session, segments, _source_hash, message_count = _load_session_context(
            "codex-local-work-1", processed_message_count=18
        )
        rendered = "\n".join(segments)
        self.assertEqual(message_count, 22)
        self.assertEqual(session["incremental_update"]["new_messages"], 4)
        self.assertNotIn("message_id=1 ", rendered)
        self.assertIn("incremental-16", rendered)
        self.assertIn("incremental-19", rendered)

    def test_context_cursor_stops_at_last_included_segment(self):
        from unittest.mock import patch
        from app.config import AgentConfig
        from app.enrichment import _load_session_context

        self.add_session()
        with self.db.get_db() as conn:
            conn.executemany(
                """INSERT INTO messages(session_id,role,content,msg_type,timestamp)
                   VALUES ('codex-local-work-1','user',?,'user_message',?)""",
                [
                    (f"message-{index}-" + "x" * 5000, f"2026-07-02T00:{index:02d}:00Z")
                    for index in range(10)
                ],
            )
        config = AgentConfig(max_input_chars=12000, max_segments_per_session=2)
        with patch("app.enrichment.get_config", return_value=type("C", (), {"agent": config})()):
            _session, segments, _source_hash, processed = _load_session_context(
                "codex-local-work-1"
            )
        self.assertEqual(len(segments), 2)
        self.assertLess(processed, 12)
        self.assertIn("message_id=", segments[-1])

    def test_timeout_retry_uses_one_smaller_segment(self):
        from unittest.mock import patch
        from app.config import AgentConfig
        from app.enrichment import _load_session_context

        self.add_session()
        with self.db.get_db() as conn:
            conn.executemany(
                """INSERT INTO messages(session_id,role,content,msg_type)
                   VALUES ('codex-local-work-1','user',?,'user_message')""",
                [(f"large-{index}-" + "x" * 5000,) for index in range(8)],
            )
        config = AgentConfig(max_input_chars=12000, max_segments_per_session=2)
        with patch(
            "app.enrichment.get_config",
            return_value=type("C", (), {"agent": config})(),
        ):
            session, segments, _source_hash, processed = _load_session_context(
                "codex-local-work-1", retry_level=1
            )
        self.assertEqual(len(segments), 1)
        self.assertLessEqual(len(segments[0]), 6000)
        self.assertEqual(session["retry_compaction"]["max_chars"], 6000)
        self.assertGreater(processed, 0)

    def test_session_hash_ignores_metadata_only_refresh(self):
        self.add_session()
        from app.work_memory import compute_session_hash

        with self.db.get_db() as conn:
            before = compute_session_hash(conn, "codex-local-work-1")
            conn.execute(
                "UPDATE sessions SET updated_at='2099-01-01 00:00:00' WHERE id=?",
                ("codex-local-work-1",),
            )
            after = compute_session_hash(conn, "codex-local-work-1")
        self.assertEqual(before, after)

    def test_recovery_is_explicit_so_cli_init_cannot_reset_live_worker(self):
        self.add_session()
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE enrichment_jobs SET status='running' WHERE session_id=?",
                ("codex-local-work-1",),
            )
            from app.work_memory import mark_session_pending

            mark_session_pending(
                conn, "codex-local-work-1", reason="messages_appended"
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM enrichment_jobs WHERE session_id=?",
                    ("codex-local-work-1",),
                ).fetchone()[0],
                "running",
            )
        self.db.init_db()
        with self.db.get_db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM enrichment_jobs WHERE session_id=?",
                    ("codex-local-work-1",),
                ).fetchone()[0],
                "running",
            )
        from app.work_memory import recover_interrupted_agent_state

        recover_interrupted_agent_state()
        with self.db.get_db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM enrichment_jobs WHERE session_id=?",
                    ("codex-local-work-1",),
                ).fetchone()[0],
                "pending",
            )

    def test_analysis_creates_searchable_evidence_backed_item(self):
        ids = self.add_session()
        from app.work_memory import (
            apply_session_analysis,
            create_work_item,
            get_work_item,
            search_work_items,
        )

        item_ids = apply_session_analysis(
            "codex-local-work-1", self.analysis(ids), 0, "test-v1", "fake"
        )
        self.assertEqual(len(item_ids), 1)
        results, total = search_work_items(
            "我记得 qwen3.5 122b 导出 golden 显存炸掉", source="codex"
        )
        self.assertEqual(total, 1)
        self.assertEqual(results[0]["id"], item_ids[0])
        self.assertIn("qwen3.5", [value.casefold() for value in results[0]["matched_terms"]])
        detail = get_work_item(item_ids[0])
        self.assertEqual(len(detail["evidence"]), 2)
        self.assertEqual(detail["entities"][0]["canonical_name"], "Qwen3.5-122B")
        self.assertIn("resume_command", detail["evidence"][0])

        meta_item = create_work_item({
            "kind": "solution",
            "title": "修复 Qwen3.5 122B golden 显存任务搜索",
            "category": "search",
            "summary": "解决以前的任务找不到的问题",
        })
        ranked, _ = search_work_items("Qwen3.5 122B golden 显存")
        self.assertEqual(ranked[0]["id"], item_ids[0])
        self.assertIn(meta_item["id"], {item["id"] for item in ranked})

    def test_major_task_groups_derived_items_and_rolls_up_knowledge(self):
        ids = self.add_session()
        from app.work_memory import (
            _major_task_identity,
            apply_session_analysis,
            get_knowledge_dashboard,
            get_major_task,
            get_work_item,
            list_major_tasks,
            search_work_items,
        )

        base = self.analysis(ids)["items"][0]
        legacy_parent_key = _major_task_identity({
            **base,
            "dedupe_key": "qwen38|m50|legacy-finding",
        })[0]
        explicit_parent_key = _major_task_identity({
            **base,
            "major_task_key": "模型导出|qwen38 m50",
            "major_task_title": "完成 Qwen3.8 M50 量化评测",
        })[0]
        self.assertEqual(legacy_parent_key, explicit_parent_key)
        task = dict(base)
        task.update({
            "kind": "task",
            "title": "完成 M50 量化精度回归",
            "status": "in_progress",
            "dedupe_key": "qwen38|m50|accuracy-regression",
            "major_task_key": "模型导出|Qwen3.8 M50 量化评测",
            "major_task_title": "完成 Qwen3.8 M50 量化评测",
            "major_task_summary": "完成导出、精度与性能验证并形成可复用结论",
            "next_steps": ["补齐长序列性能数据"],
        })
        knowledge = dict(base)
        knowledge.update({
            "kind": "knowledge",
            "title": "M50 校准集必须覆盖长序列",
            "status": "done",
            "dedupe_key": "qwen38|m50|long-sequence-calibration",
            "major_task_key": task["major_task_key"],
            "major_task_title": task["major_task_title"],
            "major_task_summary": task["major_task_summary"],
            "summary": "短序列校准会遗漏长上下文精度退化",
            "outcome": "将长序列样本加入默认校准集",
        })
        item_ids = apply_session_analysis(
            "codex-local-work-1",
            {
                "session_summary": "推进 Qwen3.8 M50 量化评测",
                "primary_project": "模型导出",
                "items": [task, knowledge],
            },
            0,
            "major-v1",
            "fake",
        )

        details = [get_work_item(item_id) for item_id in item_ids]
        self.assertEqual(len({item["major_task_id"] for item in details}), 1)
        self.assertEqual(details[0]["major_task_title"], task["major_task_title"])

        parents, total = list_major_tasks("Qwen3.8 M50")
        self.assertEqual(total, 1)
        self.assertEqual(parents[0]["item_count"], 2)
        self.assertEqual(parents[0]["knowledge_count"], 1)
        self.assertIn("下一步", {
            section["label"] for section in parents[0]["summary_sections"]
        })
        parent = get_major_task(parents[0]["id"])
        self.assertEqual({item["id"] for item in parent["items"]}, set(item_ids))

        child_hits, child_total = search_work_items(task["major_task_title"])
        self.assertEqual(child_total, 2)
        self.assertEqual({item["id"] for item in child_hits}, set(item_ids))
        dashboard = get_knowledge_dashboard(query="长序列")
        self.assertEqual(dashboard["topic_total"], 1)
        self.assertEqual(dashboard["topics"][0]["knowledge_count"], 1)
        self.assertIn("经验与结论", {
            section["label"] for section in dashboard["topics"][0]["summary_sections"]
        })

    def test_same_dedupe_key_merges_cross_session_evidence(self):
        first_ids = self.add_session()
        second_ids = self.add_session("claude-local-work-2", "claude", "follow up")
        from app.work_memory import apply_session_analysis, get_work_item, search_work_items

        first = apply_session_analysis(
            "codex-local-work-1", self.analysis(first_ids), 0, "v1", "fake"
        )[0]
        second = apply_session_analysis(
            "claude-local-work-2", self.analysis(second_ids), 0, "v1", "fake"
        )[0]
        self.assertEqual(first, second)
        detail = get_work_item(first)
        self.assertEqual(detail["session_count"], 2)
        results, total = search_work_items("Qwen3.5 golden")
        self.assertEqual(total, 1)
        self.assertEqual(set(results[0]["sources"]), {"codex", "claude"})

    def test_manual_edit_is_not_overwritten_by_reanalysis(self):
        ids = self.add_session()
        from app.work_memory import apply_session_analysis, get_work_item, update_work_item

        item_id = apply_session_analysis(
            "codex-local-work-1", self.analysis(ids), 0, "v1", "fake"
        )[0]
        update_work_item(item_id, {"title": "我的人工标题", "status": "in_progress"})
        changed = self.analysis(ids, title="Agent 新标题")
        apply_session_analysis("codex-local-work-1", changed, 0, "v2", "fake")
        item = get_work_item(item_id)
        self.assertEqual(item["title"], "我的人工标题")
        self.assertEqual(item["status"], "in_progress")
        self.assertEqual(item["review_state"], "edited")

    def test_reordered_analysis_items_follow_dedupe_identity_not_position(self):
        ids = self.add_session()
        from app.work_memory import apply_session_analysis, get_work_item

        first = self.analysis(ids, "Task A")
        first["items"][0]["dedupe_key"] = "project|task-a"
        second_item = dict(first["items"][0])
        second_item.update({"title": "Task B", "dedupe_key": "project|task-b"})
        first["items"].append(second_item)
        item_ids = apply_session_analysis(
            "codex-local-work-1", first, 0, "v1", "fake", extraction_scope="same"
        )

        reordered = dict(first)
        reordered["items"] = [dict(second_item), dict(first["items"][0])]
        reapplied = apply_session_analysis(
            "codex-local-work-1", reordered, 0, "v1", "fake", extraction_scope="same"
        )
        self.assertEqual(set(reapplied), set(item_ids))
        self.assertEqual({get_work_item(item_id)["title"] for item_id in item_ids}, {
            "Task A", "Task B",
        })

    def test_review_projects_merge_and_split(self):
        ids = self.add_session()
        from app.work_memory import (
            apply_session_analysis,
            generate_review,
            list_projects,
            merge_work_items,
            split_work_item,
        )

        item_id = apply_session_analysis(
            "codex-local-work-1", self.analysis(ids), 0, "v1", "fake"
        )[0]
        projects = list_projects()
        self.assertEqual(projects[0]["name"], "模型导出")
        review = generate_review("month", "2026-07-15")
        self.assertIn("Qwen3.5", review["content_markdown"])
        new_item = split_work_item(
            item_id, {"title": "补充回归测试", "status": "planned"}, []
        )
        self.assertNotEqual(new_item["id"], item_id)
        merged = merge_work_items(item_id, [new_item["id"]])
        self.assertEqual(merged["id"], item_id)

    def test_context_pack_timeline_and_knowledge_dashboard(self):
        ids = self.add_session()
        from app.work_memory import (
            apply_session_analysis,
            build_context_pack,
            get_knowledge_dashboard,
            get_timeline,
        )

        analysis = self.analysis(ids)
        analysis["items"][0]["kind"] = "solution"
        item_id = apply_session_analysis(
            "codex-local-work-1", analysis, 0, "v1", "fake"
        )[0]
        pack = build_context_pack(item_id)
        self.assertIn("Qwen3.5", pack["markdown"])
        self.assertIn("原始证据", pack["markdown"])
        timeline = get_timeline(days=3650)
        self.assertTrue(any(event["id"] == item_id for event in timeline["events"]))
        knowledge = get_knowledge_dashboard()
        self.assertEqual(knowledge["solutions"][0]["id"], item_id)

    def test_today_keeps_old_active_work_after_many_recent_completions(self):
        from app.work_memory import create_work_item, get_today_dashboard

        active = create_work_item({
            "title": "仍需继续的旧任务",
            "status": "open",
            "summary": "不能被最近完成的工作挤出今日页",
        })
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE work_items SET updated_at='2025-01-01T00:00:00Z' WHERE id=?",
                (active["id"],),
            )
        for index in range(105):
            create_work_item({
                "title": f"最近完成事项 {index}",
                "status": "done",
            })

        dashboard = get_today_dashboard(compact=True)
        self.assertIn(active["id"], {item["id"] for item in dashboard["active"]})
        unlinked = next(group for group in dashboard["active_groups"] if group["session"] is None)
        self.assertIn(active["id"], set(unlinked["item_ids"]))

    def test_today_shows_synced_session_before_agent_creates_tasks(self):
        message_ids = self.add_session(
            "codex-local-live-session", "codex", "刚开始的新任务"
        )
        from app.work_memory import apply_session_analysis, get_today_dashboard

        dashboard = get_today_dashboard(compact=True)
        group = next(
            value for value in dashboard["active_groups"]
            if value["session"] and value["session"]["id"] == "codex-local-live-session"
        )
        self.assertEqual(group["item_ids"], [])
        self.assertEqual(group["pipeline"]["stage"], "understanding")
        self.assertEqual(group["pipeline"]["label"], "正在理解")

        analysis = self.analysis(message_ids, "刚开始的新任务")
        analysis["items"][0]["status"] = "in_progress"
        item_id = apply_session_analysis(
            "codex-local-live-session", analysis, 0, "v1", "fake"
        )[0]
        with self.db.get_db() as conn:
            conn.execute(
                """UPDATE enrichment_jobs SET status='completed',
                          processed_hash=source_hash,not_before=NULL
                   WHERE session_id=?""",
                ("codex-local-live-session",),
            )

        dashboard = get_today_dashboard(compact=True)
        group = next(
            value for value in dashboard["active_groups"]
            if value["session"] and value["session"]["id"] == "codex-local-live-session"
        )
        self.assertEqual(group["item_ids"], [item_id])
        self.assertEqual(group["pipeline"]["stage"], "ready")
        self.assertEqual(group["pipeline"]["label"], "已形成任务")

    def test_today_compact_collapses_a_concurrent_refresh_burst(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import app.work_memory as memory

        with memory._today_compact_condition:
            memory._today_compact_cache = None
            memory._today_compact_cache_key = None
            memory._today_compact_cache_expires_at = 0.0
            memory._today_compact_building = False

        calls = 0

        def slow_build(compact=False):
            nonlocal calls
            calls += 1
            time.sleep(0.08)
            return {"date": "2026-08-02", "compact": compact}

        with patch("app.work_memory._build_today_dashboard", side_effect=slow_build):
            with ThreadPoolExecutor(max_workers=16) as executor:
                results = list(
                    executor.map(lambda _: memory.get_today_dashboard(compact=True), range(32))
                )

        self.assertEqual(calls, 1)
        self.assertTrue(all(result["compact"] for result in results))

    def test_agent_queue_prefers_latest_active_session_with_same_priority(self):
        self.add_session("codex-local-older", "codex", "较早会话")
        self.add_session("codex-local-newer", "codex", "最新活跃会话")
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE sessions SET end_time='2026-07-01T11:00:00Z' WHERE id=?",
                ("codex-local-older",),
            )
            conn.execute(
                "UPDATE sessions SET end_time='2026-07-01T13:00:00Z' WHERE id=?",
                ("codex-local-newer",),
            )
            conn.execute(
                "UPDATE enrichment_jobs SET not_before=NULL,priority=10"
            )

        from app.enrichment import _claim_next_job

        claimed = _claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["session_id"], "codex-local-newer")

    def test_successful_incremental_completion_advances_cursor_and_resets_attempts(self):
        self.add_session()
        from app.enrichment import _claim_next_job, _complete_job

        with self.db.get_db() as conn:
            conn.execute(
                """UPDATE enrichment_jobs SET not_before=NULL,attempts=8
                   WHERE session_id='codex-local-work-1'"""
            )
        job = _claim_next_job()
        self.assertIsNotNone(job)
        _complete_job(job, {
            "source_hash": job["source_hash"],
            "processed_message_count": 2,
        })
        with self.db.get_db() as conn:
            current = conn.execute(
                """SELECT status,attempts,processed_message_count
                   FROM enrichment_jobs WHERE id=?""",
                (job["id"],),
            ).fetchone()
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["attempts"], 0)
        self.assertEqual(current["processed_message_count"], 2)

    def test_partial_batch_completion_stays_pending(self):
        self.add_session()
        from app.enrichment import _claim_next_job, _complete_job

        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE enrichment_jobs SET not_before=NULL WHERE session_id=?",
                ("codex-local-work-1",),
            )
        job = _claim_next_job()
        _complete_job(job, {
            "source_hash": job["source_hash"],
            "processed_message_count": 1,
        })
        with self.db.get_db() as conn:
            current = conn.execute(
                "SELECT status,processed_hash FROM enrichment_jobs WHERE id=?",
                (job["id"],),
            ).fetchone()
        self.assertEqual(current["status"], "pending")
        self.assertIsNone(current["processed_hash"])

    def test_provider_timeout_retries_once_then_fails(self):
        from app.agent_provider import AgentProviderError
        from app.enrichment import _claim_next_job, _fail_job

        self.add_session()
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE enrichment_jobs SET not_before=NULL WHERE session_id=?",
                ("codex-local-work-1",),
            )
        first = _claim_next_job()
        _fail_job(first, AgentProviderError("Agent 调用超时"))
        with self.db.get_db() as conn:
            after_first = conn.execute(
                "SELECT status,attempts,not_before FROM enrichment_jobs WHERE id=?",
                (first["id"],),
            ).fetchone()
            conn.execute(
                "UPDATE enrichment_jobs SET not_before=NULL WHERE id=?",
                (first["id"],),
            )
        self.assertEqual(after_first["status"], "pending")
        self.assertEqual(after_first["attempts"], 1)
        self.assertIsNotNone(after_first["not_before"])

        second = _claim_next_job()
        _fail_job(second, AgentProviderError("Agent 调用 timeout"))
        with self.db.get_db() as conn:
            after_second = conn.execute(
                "SELECT status,attempts FROM enrichment_jobs WHERE id=?",
                (first["id"],),
            ).fetchone()
        self.assertEqual(after_second["status"], "failed")
        self.assertEqual(after_second["attempts"], 2)

    def test_stale_analysis_is_rejected_before_derived_writes(self):
        message_ids = self.add_session()
        from app.work_memory import StaleSessionAnalysisError, apply_session_analysis

        with self.db.get_db() as conn:
            job = conn.execute(
                "SELECT source_hash FROM enrichment_jobs WHERE session_id=?",
                ("codex-local-work-1",),
            ).fetchone()
            conn.execute(
                "UPDATE enrichment_jobs SET status='running' WHERE session_id=?",
                ("codex-local-work-1",),
            )
            conn.execute(
                """INSERT INTO messages(session_id,role,content,msg_type)
                   VALUES ('codex-local-work-1','user','changed','user_message')"""
            )
        with self.assertRaises(StaleSessionAnalysisError):
            apply_session_analysis(
                "codex-local-work-1",
                self.analysis(message_ids),
                0,
                "v2",
                "fake",
                expected_source_hash=job["source_hash"],
            )
        with self.db.get_db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM work_item_sources WHERE session_id=?",
                    ("codex-local-work-1",),
                ).fetchone()[0],
                0,
            )

    def test_direct_batch_apply_rejects_skipped_guardian_job(self):
        message_ids = self.add_session("codex-local-guardian-direct")
        from app.work_memory import (
            StaleSessionAnalysisError,
            apply_session_analyses,
            compute_session_hash,
        )

        with self.db.get_db() as conn:
            source_hash = compute_session_hash(conn, "codex-local-guardian-direct")
            conn.execute(
                """UPDATE enrichment_jobs SET status='skipped',reason='guardian',
                           source_hash=? WHERE session_id=?""",
                (source_hash, "codex-local-guardian-direct"),
            )
        with self.assertRaises(StaleSessionAnalysisError):
            apply_session_analyses(
                "codex-local-guardian-direct",
                [(0, self.analysis(message_ids), "fake")],
                "v2",
                0.92,
                "direct",
                source_hash,
                require_running_job=False,
            )

    def test_today_groups_tasks_under_their_latest_source_session(self):
        ids = self.add_session()
        from app.work_memory import apply_session_analysis, get_today_dashboard

        analysis = self.analysis(ids)
        analysis["items"][0]["status"] = "in_progress"
        second = dict(analysis["items"][0])
        second.update({
            "title": "为导出显存修复补充回归测试",
            "status": "open",
            "dedupe_key": "qwen3.5-122b|export-golden|regression",
        })
        analysis["items"].append(second)
        item_ids = apply_session_analysis(
            "codex-local-work-1", analysis, 0, "v1", "fake"
        )

        dashboard = get_today_dashboard(compact=True)
        group = next(
            value for value in dashboard["active_groups"]
            if value["session"] and value["session"]["id"] == "codex-local-work-1"
        )
        self.assertEqual(set(group["item_ids"]), set(item_ids))
        self.assertEqual(group["session"]["source"], "codex")

    def test_today_places_cross_session_task_under_latest_context(self):
        first_ids = self.add_session()
        second_ids = self.add_session("codex-local-work-2", "codex", "Qwen follow-up")
        from app.work_memory import apply_session_analysis, get_today_dashboard

        first_analysis = self.analysis(first_ids)
        first_analysis["items"][0]["status"] = "in_progress"
        item_id = apply_session_analysis(
            "codex-local-work-1", first_analysis, 0, "v1", "fake"
        )[0]
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE sessions SET end_time='2026-07-02T11:00:00Z' WHERE id=?",
                ("codex-local-work-2",),
            )
        second_analysis = self.analysis(second_ids)
        second_analysis["items"][0]["status"] = "in_progress"
        self.assertEqual(
            apply_session_analysis(
                "codex-local-work-2", second_analysis, 0, "v1", "fake"
            )[0],
            item_id,
        )

        dashboard = get_today_dashboard(compact=True)
        group = next(
            value for value in dashboard["active_groups"]
            if item_id in value["item_ids"]
        )
        self.assertEqual(group["session"]["id"], "codex-local-work-2")
        item = next(value for value in dashboard["active"] if value["id"] == item_id)
        self.assertEqual(item["session_count"], 2)


class TestAgentProvider(WorkMemoryFixture):
    def test_successful_run_records_effective_provider_model(self):
        from app.enrichment import _create_run, _finish_run

        run_id = _create_run(
            "session_enrichment",
            "codex_cli",
            "codex-default",
            None,
            "hash",
            "v1",
            {},
        )
        _finish_run(
            run_id,
            "success",
            12,
            {"ok": True},
            model="gpt-5.6-terra",
        )
        with self.db.get_db() as conn:
            row = conn.execute(
                "SELECT model,status FROM agent_runs WHERE id=?", (run_id,)
            ).fetchone()
        self.assertEqual(row["model"], "gpt-5.6-terra")
        self.assertEqual(row["status"], "success")

    def test_codex_inherit_uses_local_model_and_reasoning_preferences(self):
        from unittest.mock import patch
        from app.agent_provider import _codex_effective_preferences
        from app.config import AgentConfig

        config = AgentConfig(
            provider="codex_cli",
            executable="codex",
            model="",
            reasoning_effort="inherit",
        )
        with patch("builtins.open"), patch("tomllib.load", return_value={
            "model": "gpt-5.6-sol",
            "model_reasoning_effort": "max",
        }):
            model, effort = _codex_effective_preferences(config)
        self.assertEqual(model, "gpt-5.6-sol")
        self.assertEqual(effort, "max")

    def test_redaction_and_structured_prompt(self):
        from app.agent_provider import AgentProvider, AgentResult, analyze_session_segment

        class FakeProvider(AgentProvider):
            name = "fake"

            def __init__(self):
                self.prompt = ""

            def available(self):
                return True, "fake"

            def structured(self, prompt, schema):
                self.prompt = prompt
                return AgentResult(
                    data={
                        "session_summary": "",
                        "primary_project": "",
                        "session_tags": [],
                        "items": [],
                    },
                    provider="fake",
                    model="fake",
                    duration_ms=1,
                )

        provider = FakeProvider()
        analyze_session_segment(
            provider,
            {"id": "x"},
            "[message_id=1 role=user]\npassword: super-secret-value\nBearer abcdefghijklmnopqrstuvwxyz",
            0,
            1,
        )
        self.assertNotIn("super-secret-value", provider.prompt)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", provider.prompt)
        self.assertIn("[REDACTED]", provider.prompt)

    def test_compact_status_reuses_provider_probe_and_skips_history(self):
        from unittest.mock import patch
        from app.enrichment import EnrichmentManager

        provider = {
            "enabled": True,
            "provider": "codex_cli",
            "available": True,
            "detail": "codex-cli 1.0",
            "model": "default",
        }
        manager = EnrichmentManager()
        with patch("app.enrichment.provider_status", return_value=provider) as probe:
            first = manager.status(compact=True)
            second = manager.status(compact=True)
            manager.invalidate_provider_status()
            third = manager.status(compact=True)

        self.assertEqual(probe.call_count, 2)
        self.assertNotIn("jobs", first)
        self.assertNotIn("recent_runs", second)
        self.assertEqual(third["provider"]["detail"], "codex-cli 1.0")

    def test_parallel_status_polling_never_stampedes_provider_probe(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from app.enrichment import EnrichmentManager

        provider = {
            "enabled": True,
            "provider": "codex_cli",
            "available": True,
            "detail": "codex-cli 1.0",
            "model": "default",
        }
        manager = EnrichmentManager()

        def slow_probe():
            time.sleep(0.08)
            return provider

        with patch("app.enrichment.provider_status", side_effect=slow_probe) as probe:
            with ThreadPoolExecutor(max_workers=12) as executor:
                results = list(executor.map(lambda _: manager.status(compact=True), range(24)))

        self.assertEqual(probe.call_count, 1)
        self.assertTrue(all("provider" in result for result in results))

    def test_retry_failed_uses_worker_database_timeout(self):
        from unittest.mock import patch
        from app.enrichment import EnrichmentManager, _WORKER_DB_BUSY_TIMEOUT_MS

        real_get_db = self.db.get_db
        with patch(
            "app.enrichment.get_db",
            side_effect=lambda **kwargs: real_get_db(**kwargs),
        ) as get_db:
            EnrichmentManager().retry_failed()
        self.assertEqual(
            get_db.call_args.kwargs["busy_timeout_ms"],
            _WORKER_DB_BUSY_TIMEOUT_MS,
        )

    def test_worker_supervisor_restarts_after_unexpected_crash(self):
        import threading
        from unittest.mock import patch

        from app.enrichment import EnrichmentManager

        manager = EnrichmentManager()
        restarted = threading.Event()
        calls = 0

        def flaky_loop():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("one-off worker crash")
            restarted.set()
            with manager._condition:
                while not manager._stopping:
                    manager._condition.wait(timeout=0.05)

        config = type("C", (), {
            "agent": type("A", (), {
                "enabled": True,
                "auto_start_worker": True,
            })(),
        })()
        with patch("app.enrichment.get_config", return_value=config), patch.object(
            manager, "_loop", side_effect=flaky_loop
        ):
            manager.start()
            self.assertTrue(restarted.wait(timeout=2.5))
            runtime = manager.runtime_snapshot()
            self.assertTrue(runtime["worker_alive"])
            self.assertEqual(runtime["restart_count"], 1)
            manager.stop(timeout=2)


class TestSmartSearch(WorkMemoryFixture):
    def test_quick_search_uses_independent_raw_session_offset(self):
        from unittest.mock import patch

        from app.smart_search import quick_search_payload

        with patch("app.smart_search.search_work_items", return_value=([], 0)), patch(
            "app.smart_search.search_sessions",
            return_value=(
                [
                    {
                        "id": 931,
                        "session_id": "session-pi05",
                        "display_title": "PI05 原始会话",
                        "source": "opencode",
                    }
                ],
                22,
            ),
        ) as raw_search:
            result = quick_search_payload(
                "PI05",
                {},
                limit=30,
                offset=30,
                include_raw=True,
                raw_offset=20,
            )

        self.assertEqual(raw_search.call_args.kwargs["offset"], 20)
        self.assertEqual(result["raw_offset"], 20)
        self.assertEqual(result["raw_next_offset"], 21)
        self.assertTrue(result["raw_has_more"])
        self.assertEqual(result["raw_sessions"][0]["session_id"], "session-pi05")

    def test_agent_plan_reranks_evidence_backed_candidates(self):
        ids = self.add_session()
        from app.work_memory import apply_session_analysis
        item_id = apply_session_analysis(
            "codex-local-work-1", self.analysis(ids), 0, "v1", "fake"
        )[0]
        from app.agent_provider import AgentProvider, AgentResult

        class FakeSearchProvider(AgentProvider):
            name = "fake"

            def available(self):
                return True, "ready"

            def structured(self, prompt, schema):
                if "intent" in schema.get("properties", {}):
                    data = {
                        "intent": "find_task",
                        "rewritten_query": "Qwen3.5 122B golden OOM",
                        "required_terms": ["Qwen3.5", "122B"],
                        "optional_terms": ["golden", "OOM"],
                        "aliases": ["显存溢出", "export"],
                        "source": "codex",
                        "project": "模型导出",
                        "status": "",
                        "date_from": "",
                        "date_to": "",
                        "explanation": "technical entities",
                    }
                else:
                    data = {
                        "answer": "最可能是 Qwen3.5 导出显存任务。",
                        "best_item_ids": [item_id],
                        "why_matched": [{"item_id": item_id, "reason": "模型、规格和故障均匹配"}],
                        "suggested_queries": [],
                        "uncertainty": "",
                    }
                return AgentResult(data, "fake", "fake", 1)

        from unittest.mock import patch
        from app.smart_search import SmartSearchManager

        with patch("app.smart_search.get_agent_provider", return_value=FakeSearchProvider()):
            result = SmartSearchManager()._execute(
                "我记得 qwen3.5 122b golden 显存炸掉", {"source": "codex"}
            )
        self.assertEqual(result["mode"], "agent")
        self.assertEqual(result["results"][0]["id"], item_id)
        self.assertIn("模型、规格", result["results"][0]["agent_match_reason"])
        self.assertTrue(result["raw_sessions"])
        self.assertLessEqual(
            max(len(row.get("content", "")) for row in result["raw_sessions"]),
            800,
        )


class TestAgentProviders(unittest.TestCase):
    def test_cli_process_group_is_terminated_and_reaped_on_timeout(self):
        import subprocess
        from unittest.mock import Mock, patch
        from app.agent_provider import _run_cli_process_group

        process = Mock()
        process.pid = 4321
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["codex"], 1),
            subprocess.TimeoutExpired(["codex"], 5),
            ("", ""),
        ]
        with patch("app.agent_provider.subprocess.Popen", return_value=process) as popen, \
             patch("app.agent_provider.os.killpg") as killpg:
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_cli_process_group(
                    ["codex"], input="prompt", timeout=1, env={}
                )

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(killpg.call_args_list[0].args[0], 4321)
        self.assertEqual(killpg.call_args_list[1].args[0], 4321)
        self.assertEqual(process.communicate.call_count, 3)

    def test_cli_timeout_kills_group_even_when_leader_exits_on_term(self):
        import subprocess
        from unittest.mock import Mock, patch
        from app.agent_provider import _run_cli_process_group

        process = Mock()
        process.pid = 4321
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["codex"], 1),
            ("", ""),
            ("", ""),
        ]
        with patch("app.agent_provider.subprocess.Popen", return_value=process), \
             patch("app.agent_provider.os.killpg") as killpg:
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_cli_process_group(
                    ["codex"], input="prompt", timeout=1, env={}
                )
        self.assertEqual(len(killpg.call_args_list), 2)

    def test_codex_connection_test_checks_login_and_catalog_without_inference(self):
        import json
        from unittest.mock import patch
        from app.agent_provider import CodexCLIProvider
        from app.config import AgentConfig

        provider = CodexCLIProvider(AgentConfig(
            executable="codex",
            model="gpt-test",
            reasoning_effort="high",
        ))
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if command[-1] == "--version":
                return type("Completed", (), {
                    "returncode": 0, "stdout": "codex-cli 1.0", "stderr": "",
                })()
            if command[-2:] == ["login", "status"]:
                return type("Completed", (), {
                    "returncode": 0, "stdout": "Logged in", "stderr": "",
                })()
            return type("Completed", (), {
                "returncode": 0,
                "stdout": json.dumps({"models": [{
                    "slug": "gpt-test",
                    "supported_reasoning_levels": [{"effort": "high"}],
                }]}),
                "stderr": "",
            })()

        with patch("app.agent_provider.shutil.which", return_value="/usr/bin/codex"), \
             patch("app.agent_provider.subprocess.run", side_effect=fake_run):
            result = provider.diagnose()

        self.assertTrue(result.available)
        self.assertTrue(result.model_available)
        self.assertIn("gpt-test / high", result.detail)
        self.assertFalse(any("exec" in command for command in commands))

    def test_opencode_provider_parses_text_events_and_isolates_data_home(self):
        import json
        import os
        import tempfile
        from unittest.mock import patch
        from app.agent_provider import OpenCodeCLIProvider
        from app.config import AgentConfig

        provider = OpenCodeCLIProvider(AgentConfig(
            provider="opencode_cli",
            executable="opencode",
            model="newapi/gpt-test",
            reasoning_effort="low",
            timeout_seconds=30,
        ))
        events = "\n".join([
            json.dumps({
                "type": "text",
                "part": {"id": "part-1", "type": "text", "text": "{\"ok\":"},
            }),
            json.dumps({
                "type": "text",
                "part": {"id": "part-1", "type": "text", "text": "{\"ok\":true}"},
            }),
        ])
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            captured["input"] = kwargs["input"]
            return type("Completed", (), {
                "returncode": 0,
                "stdout": events,
                "stderr": "",
            })()

        with tempfile.TemporaryDirectory() as tmpdir:
            auth_path = os.path.join(tmpdir, "auth.json")
            with open(auth_path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            with patch.object(provider, "_auth_path", return_value=auth_path), \
                 patch("app.agent_provider.shutil.which", return_value="/usr/bin/opencode"), \
                 patch("app.agent_provider.subprocess.run", side_effect=fake_run):
                result = provider.structured(
                    "return JSON",
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                )

        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(result.provider, "opencode_cli")
        self.assertIn("--pure", captured["command"])
        self.assertIn("work-memory", captured["command"])
        self.assertIn("return JSON", captured["input"])
        self.assertIn("OPENCODE_CONFIG_CONTENT", captured["env"])
        self.assertIn("agent-memory-opencode-", captured["env"]["XDG_DATA_HOME"])


class TestWorkAPI(WorkMemoryFixture):
    def setUp(self):
        super().setUp()
        ids = self.add_session()
        from app.work_memory import apply_session_analysis

        self.item_id = apply_session_analysis(
            "codex-local-work-1", self.analysis(ids), 0, "v1", "fake"
        )[0]
        from fastapi.testclient import TestClient
        from app.main import app

        self.client = TestClient(app, raise_server_exceptions=False)

    def test_today_items_search_projects_and_mcp(self):
        for url in (
            "/api/work/today",
            "/api/work/items?q=Qwen3.5",
            "/api/work/major-tasks?q=Qwen3.5",
            "/api/work/search?q=Qwen3.5&source=codex",
            "/api/work/projects",
            "/api/work/reviews",
            "/api/work/timeline?days=3650",
            "/api/work/knowledge",
            "/api/work/agent/status",
            "/api/mcp/work/search?q=Qwen3.5",
            f"/api/mcp/work/item/{self.item_id}",
            f"/api/work/items/{self.item_id}/context-pack",
            f"/api/mcp/work/item/{self.item_id}/context-pack",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, (url, response.text))
        search = self.client.get("/api/work/search?q=Qwen3.5").json()
        self.assertEqual(search["result_unit"], "work_item")
        self.assertEqual(search["results"][0]["id"], self.item_id)
        self.assertEqual(search["major_total"], 1)
        self.assertEqual(search["major_tasks"][0]["item_count"], 1)
        major_task_id = search["major_tasks"][0]["id"]
        major_detail = self.client.get(
            f"/api/work/major-tasks/{major_task_id}"
        )
        self.assertEqual(major_detail.status_code, 200)
        self.assertEqual(major_detail.json()["items"][0]["id"], self.item_id)
        search_schema = self.client.get("/openapi.json").json()["paths"][
            "/api/work/search"
        ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            search_schema["$ref"],
            "#/components/schemas/QuickSearchResponse",
        )
        session_schema = self.client.get("/openapi.json").json()["paths"][
            "/api/sessions/{session_id}"
        ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(
            session_schema["$ref"],
            "#/components/schemas/SessionDetailResponse",
        )

        compact_today = self.client.get("/api/work/today?compact=true").json()
        self.assertNotIn("completed_today", compact_today)
        self.assertNotIn("inbox", compact_today)
        self.assertIn("completed_today_count", compact_today)
        allowed_today_fields = {
            "id", "title", "kind", "status", "summary", "problem", "project_name",
            "source_end_time", "updated_at", "next_steps", "session_count", "sources",
        }
        for item in compact_today["active"]:
            self.assertLessEqual(set(item), allowed_today_fields)
        self.assertIn("active_groups", compact_today)

        compact_agent = self.client.get("/api/work/agent/status?compact=true").json()
        self.assertEqual(set(compact_agent), {"counts", "runtime", "provider"})
        self.assertNotIn("jobs", compact_agent)

    def test_review_crud_and_queue_endpoints(self):
        approved = self.client.post(f"/api/work/items/{self.item_id}/approve")
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["review_state"], "approved")
        edited = self.client.patch(
            f"/api/work/items/{self.item_id}", json={"status": "in_progress"}
        )
        self.assertEqual(edited.json()["status"], "in_progress")
        review = self.client.post("/api/work/reviews/generate?period=month")
        self.assertEqual(review.status_code, 200)
        queued = self.client.post(
            "/api/work/agent/enqueue/codex-local-work-1?force=true"
        )
        self.assertEqual(queued.status_code, 200)

    def test_bulk_approve_selected_or_all_pending_items_atomically(self):
        from app.work_memory import create_work_item

        second = create_work_item({"title": "第二条待确认事项", "kind": "task"})
        with self.db.get_db() as conn:
            conn.execute(
                "UPDATE work_items SET review_state='pending' WHERE id IN (?,?)",
                (self.item_id, second["id"]),
            )

        unsafe_all = self.client.post(
            "/api/work/items/bulk-approve", json={"all_pending": True}
        )
        self.assertEqual(unsafe_all.status_code, 400)
        conflict = self.client.post(
            "/api/work/items/bulk-approve",
            json={"all_pending": True, "expected_total": 1},
        )
        self.assertEqual(conflict.status_code, 409)
        with self.db.get_db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM work_items WHERE review_state='pending'"
                ).fetchone()[0],
                2,
            )

        selected = self.client.post(
            "/api/work/items/bulk-approve",
            json={"item_ids": [self.item_id, second["id"], "missing-item"]},
        )
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.json()["updated_count"], 2)
        self.assertEqual(selected.json()["skipped_count"], 1)
        self.assertEqual(selected.json()["remaining"], 0)
        with self.db.get_db() as conn:
            self.assertEqual(
                conn.execute(
                    """SELECT COUNT(*) FROM work_feedback
                       WHERE action='approve' AND work_item_id IN (?,?)""",
                    (self.item_id, second["id"]),
                ).fetchone()[0],
                2,
            )
            conn.execute(
                "UPDATE work_items SET review_state='pending' WHERE id IN (?,?)",
                (self.item_id, second["id"]),
            )

        all_pending = self.client.post(
            "/api/work/items/bulk-approve",
            json={"all_pending": True, "expected_total": 2},
        )
        self.assertEqual(all_pending.status_code, 200)
        self.assertEqual(all_pending.json()["updated_count"], 2)
        self.assertEqual(all_pending.json()["remaining"], 0)

    def test_saved_search_views_can_be_created_filtered_and_deleted(self):
        created = self.client.post(
            "/api/work/saved-searches",
            json={
                "name": "近期 Codex 故障",
                "query": "Qwen3.5 显存",
                "filters": {
                    "source": "codex",
                    "kind": "task",
                    "status": "active",
                    "date_range": "90",
                },
            },
        )
        self.assertEqual(created.status_code, 200)
        saved = created.json()
        listed = self.client.get("/api/work/saved-searches").json()["saved_searches"]
        self.assertEqual(listed[0]["id"], saved["id"])
        self.assertEqual(listed[0]["filters"]["status"], "active")

        deleted = self.client.delete(f"/api/work/saved-searches/{saved['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual(
            self.client.delete(f"/api/work/saved-searches/{saved['id']}").status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
