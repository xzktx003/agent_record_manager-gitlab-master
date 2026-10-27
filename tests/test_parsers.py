"""Unit tests for Copilot and Codex parsers."""
import json
import os
import sqlite3
import tempfile
import unittest

class TestCopilotParser(unittest.TestCase):
    def test_parse_copilot_session(self):
        from app.parsers.copilot import parse_copilot_session
        # Create a mock session directory with events.jsonl
        with tempfile.TemporaryDirectory() as tmpdir:
            events = [
                {"type": "session.start", "data": {"sessionId": "test-001",
                 "copilotVersion": "1.0.0", "startTime": "2026-04-08T10:00:00Z",
                 "context": {"cwd": "/tmp/test"}}, "timestamp": "2026-04-08T10:00:00Z"},
                {"type": "user.message", "data": {"content": "Hello, how are you?"},
                 "timestamp": "2026-04-08T10:00:01Z"},
                {"type": "assistant.message", "data": {"content": "I am fine, thanks!",
                 "messageId": "msg-1", "toolRequests": []},
                 "timestamp": "2026-04-08T10:00:02Z"},
                {"type": "user.message", "data": {"content": "Can you help me with Python?"},
                 "timestamp": "2026-04-08T10:00:03Z"},
                {"type": "assistant.message", "data": {"content": "Sure! What do you need?",
                 "messageId": "msg-2", "toolRequests": [
                     {"name": "bash", "arguments": {"command": "python3 --version"},
                      "toolCallId": "tc-1"}
                 ]}, "timestamp": "2026-04-08T10:00:04Z"},
                {"type": "tool.execution_complete", "data": {"toolCallId": "tc-1",
                 "result": "Python 3.12.0"}, "timestamp": "2026-04-08T10:00:05Z"},
            ]
            with open(os.path.join(tmpdir, "events.jsonl"), "w") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")

            result = parse_copilot_session(tmpdir, "test-server")
            self.assertIsNotNone(result)
            session = result["session"]
            messages = result["messages"]

            self.assertEqual(session["source"], "copilot")
            self.assertEqual(session["server_name"], "test-server")
            self.assertEqual(session["cwd"], "/tmp/test")
            self.assertIn("Hello, how are you?", session["title"])
            self.assertGreater(len(messages), 0)

            user_msgs = [m for m in messages if m["role"] == "user"]
            asst_msgs = [m for m in messages if m["role"] == "assistant"]
            tool_msgs = [m for m in messages if m["role"] == "tool"]
            self.assertEqual(len(user_msgs), 2)
            self.assertGreaterEqual(len(asst_msgs), 2)  # text + tool_call
            self.assertEqual(len(tool_msgs), 1)
            self.assertIn("Python 3.12.0", tool_msgs[0]["content"])

class TestCodexParser(unittest.TestCase):
    def test_parse_codex_session(self):
        from app.parsers.codex import parse_codex_session
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False,
                prefix="rollout-2026-04-08T10-00-00-019d0001-0002-0003-0004-000000000005") as f:
            events = [
                {"timestamp": "2026-04-08T10:00:00Z", "type": "session_meta",
                 "payload": {"id": "019d0001-0002-0003-0004-000000000005",
                  "timestamp": "2026-04-08T10:00:00Z", "cwd": "/tmp/codex",
                  "cli_version": "0.118.0", "source": "vscode", "model_provider": "openai"}},
                {"timestamp": "2026-04-08T10:00:01Z", "type": "event_msg",
                 "payload": {"type": "user_message", "message": "帮我写一个排序算法"}},
                {"timestamp": "2026-04-08T10:00:02Z", "type": "response_item",
                 "payload": {"role": "assistant", "content": [
                     {"type": "output_text", "text": "好的，我来帮你实现快速排序。"}
                 ], "phase": "commentary"}},
                {"timestamp": "2026-04-08T10:00:03Z", "type": "function_call",
                 "payload": {"name": "write_file", "arguments": "def quicksort(arr): ..."}},
            ]
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            tmpfile = f.name

        try:
            result = parse_codex_session(tmpfile, "test-server")
            self.assertIsNotNone(result)
            session = result["session"]
            messages = result["messages"]

            self.assertEqual(session["source"], "codex")
            self.assertEqual(session["cwd"], "/tmp/codex")
            self.assertGreater(len(messages), 0)

            user_msgs = [m for m in messages if m["role"] == "user"]
            asst_msgs = [m for m in messages if m["role"] == "assistant"]
            self.assertGreaterEqual(len(user_msgs), 1)
            self.assertIn("排序", user_msgs[0]["content"])
            self.assertGreaterEqual(len(asst_msgs), 1)
        finally:
            os.unlink(tmpfile)

    def test_parse_codex_index(self):
        from app.parsers.codex import parse_codex_index
        with tempfile.TemporaryDirectory() as tmpdir:
            index_data = [
                {"id": "019d-aaa", "thread_name": "测试会话1", "updated_at": "2026-04-08T10:00:00Z"},
                {"id": "019d-bbb", "thread_name": "测试会话2", "updated_at": "2026-04-08T11:00:00Z"},
            ]
            with open(os.path.join(tmpdir, "session_index.jsonl"), "w") as f:
                for e in index_data:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            entries = parse_codex_index(tmpdir)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["thread_name"], "测试会话1")

    def test_parse_codex_response_items_and_skip_internal_prompts(self):
        from app.parsers.codex import parse_codex_session
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False,
                prefix="rollout-2026-04-08T10-00-00-019d0001-0002-0003-0004-000000000006") as f:
            events = [
                {"timestamp": "2026-04-08T10:00:00Z", "type": "session_meta",
                 "payload": {"id": "019d0001-0002-0003-0004-000000000006",
                  "timestamp": "2026-04-08T10:00:00Z", "cwd": "/tmp/codex",
                  "cli_version": "0.118.0", "source": "vscode", "model_provider": "openai"}},
                {"timestamp": "2026-04-08T10:00:01Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user", "content": [
                     {"type": "input_text", "text": "<hook_prompt hook_run_id=\"stop:4\">internal"}
                 ]}},
                {"timestamp": "2026-04-08T10:00:02Z", "type": "event_msg",
                 "payload": {"type": "user_message", "message": "真实用户问题"}},
                {"timestamp": "2026-04-08T10:00:03Z", "type": "event_msg",
                 "payload": {"type": "agent_message", "message": "助手回复 A"}},
                {"timestamp": "2026-04-08T10:00:04Z", "type": "response_item",
                 "payload": {"type": "message", "role": "assistant", "content": [
                     {"type": "output_text", "text": "助手回复 A"}
                 ]}},
                {"timestamp": "2026-04-08T10:00:04.500Z", "type": "event_msg",
                 "payload": {"type": "agent_message", "message": "这是一个很长的重复回复。" * 30}},
                {"timestamp": "2026-04-08T10:00:04.800Z", "type": "event_msg",
                 "payload": {"type": "agent_message", "message": "这是一个很长的重复回复。" * 30}},
                {"timestamp": "2026-04-08T10:00:05Z", "type": "response_item",
                 "payload": {"type": "function_call", "name": "read_file", "arguments": {"path": "/tmp/a"}, "call_id": "call-1"}},
                {"timestamp": "2026-04-08T10:00:06Z", "type": "response_item",
                 "payload": {"type": "function_call_output", "output": {"ok": True}, "call_id": "call-1"}},
                {"timestamp": "2026-04-08T10:00:07Z", "type": "response_item",
                 "payload": {"type": "reasoning", "summary": [
                     {"type": "summary_text", "text": "推理摘要"}
                 ]}},
            ]
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            tmpfile = f.name

        try:
            result = parse_codex_session(tmpfile, "test-server")
            messages = result["messages"]
            user_contents = [m["content"] for m in messages if m["role"] == "user"]
            assistant_contents = [m["content"] for m in messages if m["role"] == "assistant"]
            tool_msgs = [m for m in messages if m["role"] == "tool"]

            self.assertEqual(user_contents, ["真实用户问题"])
            self.assertEqual(assistant_contents.count("助手回复 A"), 1)
            self.assertEqual(sum(1 for text in assistant_contents if text.startswith("这是一个很长的重复回复")), 1)
            self.assertTrue(any(m["msg_type"] == "function_call" for m in messages))
            self.assertTrue(any(m["msg_type"] == "reasoning" and "推理摘要" in m["content"] for m in messages))
            self.assertTrue(any(m["msg_type"] == "function_call_output" for m in tool_msgs))
        finally:
            os.unlink(tmpfile)

    def test_parse_codex_deduplicates_duplicate_user_messages(self):
        from app.parsers.codex import parse_codex_session
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False,
                prefix="rollout-2026-04-08T10-00-00-019d0001-0002-0003-0004-000000000007") as f:
            events = [
                {"timestamp": "2026-04-08T10:00:00Z", "type": "session_meta",
                 "payload": {"id": "019d0001-0002-0003-0004-000000000007",
                  "timestamp": "2026-04-08T10:00:00Z", "cwd": "/tmp/codex",
                  "cli_version": "0.118.0", "source": "vscode", "model_provider": "openai"}},
                {"timestamp": "2026-04-08T10:00:01Z", "type": "response_item",
                 "payload": {"type": "message", "role": "user", "content": [
                     {"type": "input_text", "text": "重复用户问题"}
                 ]}},
                {"timestamp": "2026-04-08T10:00:02Z", "type": "event_msg",
                 "payload": {"type": "user_message", "message": "重复用户问题"}},
                {"timestamp": "2026-04-08T10:00:03Z", "type": "event_msg",
                 "payload": {"type": "agent_message", "message": "只保留一条用户消息"}},
            ]
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            tmpfile = f.name

        try:
            result = parse_codex_session(tmpfile, "test-server")
            user_contents = [m["content"] for m in result["messages"] if m["role"] == "user"]
            self.assertEqual(user_contents, ["重复用户问题"])
        finally:
            os.unlink(tmpfile)

    def test_merge_codex_team_sessions_without_parent_metadata(self):
        from app.parsers.codex import merge_codex_subagents

        controller = {
            "session": {
                "id": "codex-local-main",
                "source": "codex",
                "server_name": "local",
                "title": "$pua $autopilot $team - 总结：",
                "cwd": "/data01/home/yujy/work/xh2modelzoo",
                "start_time": "2026-04-20T14:07:51Z",
                "end_time": "2026-04-20T14:20:00Z",
                "message_count": 2,
                "metadata": {
                    "originator": "codex-tui",
                    "source": "cli",
                    "codex_id": "main-id",
                    "subagent": None,
                },
            },
            "messages": [
                {"role": "user", "content": "$pua $autopilot $team - 总结：", "msg_type": "user_message", "timestamp": "2026-04-20T14:08:00Z"},
                {"role": "assistant", "content": "主会话回复", "msg_type": "agent_message", "timestamp": "2026-04-20T14:19:00Z"},
            ],
            "_source_files": [{"path": "/tmp/main.jsonl", "mtime": 1, "size": 100}],
        }
        worker = {
            "session": {
                "id": "codex-local-worker-1",
                "source": "codex",
                "server_name": "local",
                "title": "Read $OMX_TEAM_STATE_ROOT/.../worker-1/inbox.md, work now, report progress, continue assigned work or next feasible task.",
                "cwd": "/data01/home/yujy/work/xh2modelzoo__team_clean/.omx/team/spec/worktrees/worker-1",
                "start_time": "2026-04-20T14:12:55Z",
                "end_time": "2026-04-20T14:18:56Z",
                "message_count": 2,
                "metadata": {
                    "originator": "codex-tui",
                    "source": "cli",
                    "codex_id": "worker-id",
                    "subagent": None,
                },
            },
            "messages": [
                {"role": "user", "content": "Read $OMX_TEAM_STATE_ROOT/.../worker-1/inbox.md", "msg_type": "user_message", "timestamp": "2026-04-20T14:13:00Z"},
                {"role": "assistant", "content": "worker 已完成", "msg_type": "agent_message", "timestamp": "2026-04-20T14:18:00Z"},
            ],
            "_source_files": [{"path": "/tmp/worker.jsonl", "mtime": 2, "size": 200}],
        }
        helper = {
            "session": {
                "id": "codex-local-helper-1",
                "source": "codex",
                "server_name": "local",
                "title": "You summarize shell command output. Return markdown bullets only.",
                "cwd": "/data01/home/yujy/work/xh2modelzoo__team_clean",
                "start_time": "2026-04-20T14:17:12Z",
                "end_time": "2026-04-20T14:17:14Z",
                "message_count": 2,
                "metadata": {
                    "originator": "codex_exec",
                    "source": "exec",
                    "codex_id": "helper-id",
                    "subagent": None,
                },
            },
            "messages": [
                {"role": "user", "content": "You summarize shell command output. Return markdown bullets only.", "msg_type": "user_message", "timestamp": "2026-04-20T14:17:13Z"},
                {"role": "assistant", "content": "summary bullets", "msg_type": "agent_message", "timestamp": "2026-04-20T14:17:14Z"},
            ],
            "_source_files": [{"path": "/tmp/helper.jsonl", "mtime": 3, "size": 50}],
        }
        unrelated = {
            "session": {
                "id": "codex-local-other",
                "source": "codex",
                "server_name": "local",
                "title": "无关会话",
                "cwd": "/data01/home/yujy/work/another_project",
                "start_time": "2026-04-20T14:10:00Z",
                "end_time": "2026-04-20T14:11:00Z",
                "message_count": 1,
                "metadata": {
                    "originator": "codex-tui",
                    "source": "cli",
                    "codex_id": "other-id",
                    "subagent": None,
                },
            },
            "messages": [
                {"role": "user", "content": "无关内容", "msg_type": "user_message", "timestamp": "2026-04-20T14:10:00Z"},
            ],
            "_source_files": [{"path": "/tmp/other.jsonl", "mtime": 4, "size": 10}],
        }

        merged = merge_codex_subagents([controller, worker, helper, unrelated])

        self.assertEqual(len(merged), 2)
        merged_main = next(item for item in merged if item["session"]["id"] == "codex-local-main")
        merged_ids = [item["session_id"] for item in merged_main["session"]["metadata"]["merged_child_sessions"]]
        self.assertEqual(set(merged_ids), {"codex-local-worker-1", "codex-local-helper-1"})
        self.assertTrue(any(msg["msg_type"] == "team_merge_start" for msg in merged_main["messages"]))
        self.assertTrue(any("[team-worker-1] worker 已完成" in msg["content"] for msg in merged_main["messages"] if msg["role"] == "assistant"))
        timestamps = [msg["timestamp"] or "" for msg in merged_main["messages"]]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertTrue(any(item["session"]["id"] == "codex-local-other" for item in merged))

    def test_merge_omx_cli_sessions_without_team_keyword(self):
        from app.parsers.codex import merge_codex_subagents

        controller = {
            "session": {
                "id": "codex-local-omx-main",
                "source": "codex",
                "server_name": "local",
                "title": "继续处理模型导出",
                "cwd": "/data01/home/yujy/work/xh2modelzoo",
                "start_time": "2026-06-03T01:19:55Z",
                "end_time": "2026-06-03T01:40:00Z",
                "message_count": 2,
                "metadata": {
                    "originator": "codex-tui",
                    "source": "cli",
                    "codex_id": "omx-main-id",
                    "subagent": None,
                    "omx": {"managed": True, "markers": ["oh-my-codex"]},
                },
            },
            "messages": [
                {"role": "user", "content": "继续处理模型导出", "msg_type": "user_message", "timestamp": "2026-06-03T01:20:00Z"},
                {"role": "assistant", "content": "主会话回复", "msg_type": "agent_message", "timestamp": "2026-06-03T01:39:00Z"},
            ],
            "_source_files": [{"path": "/tmp/omx-main.jsonl", "mtime": 1, "size": 100}],
        }
        worker = {
            "session": {
                "id": "codex-local-omx-worker-1",
                "source": "codex",
                "server_name": "local",
                "title": "Read $OMX_TEAM_STATE_ROOT/.../worker-2/inbox.md and report progress.",
                "cwd": "/data01/home/yujy/work/xh2modelzoo__team_run/.omx/team/spec/worktrees/worker-2",
                "start_time": "2026-06-03T01:24:50Z",
                "end_time": "2026-06-03T01:38:00Z",
                "message_count": 2,
                "metadata": {
                    "originator": "codex-tui",
                    "source": "cli",
                    "codex_id": "omx-worker-id",
                    "subagent": None,
                },
            },
            "messages": [
                {"role": "user", "content": "Read $OMX_TEAM_STATE_ROOT/.../worker-2/inbox.md", "msg_type": "user_message", "timestamp": "2026-06-03T01:25:00Z"},
                {"role": "assistant", "content": "worker 汇报", "msg_type": "agent_message", "timestamp": "2026-06-03T01:37:00Z"},
            ],
            "_source_files": [{"path": "/tmp/omx-worker.jsonl", "mtime": 2, "size": 200}],
        }

        merged = merge_codex_subagents([controller, worker])

        self.assertEqual(len(merged), 1)
        merged_main = merged[0]
        merged_ids = [
            item["session_id"]
            for item in merged_main["session"]["metadata"]["merged_child_sessions"]
        ]
        self.assertEqual(merged_ids, ["codex-local-omx-worker-1"])
        self.assertTrue(any("[team-worker-2] worker 汇报" in msg["content"] for msg in merged_main["messages"]))

    def test_parse_codex_session_records_omx_metadata_marker(self):
        from app.parsers.codex import parse_codex_session

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False,
                prefix="rollout-2026-06-03T01-19-55-019e8b10-d098-7ff2-bfcb-5e1c67f97d87") as f:
            events = [
                {"timestamp": "2026-06-03T01:19:55Z", "type": "session_meta",
                 "payload": {"id": "019e8b10-d098-7ff2-bfcb-5e1c67f97d87",
                  "timestamp": "2026-06-03T01:19:55Z", "cwd": "/tmp/codex",
                  "originator": "codex-tui", "cli_version": "0.118.0",
                  "source": "cli", "model_provider": "openai",
                  "base_instructions": {"text": "You are running with oh-my-codex (OMX)."}}},
                {"timestamp": "2026-06-03T01:20:00Z", "type": "event_msg",
                 "payload": {"type": "user_message", "message": "真实任务"}},
            ]
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            tmpfile = f.name

        try:
            result = parse_codex_session(tmpfile, "test-server")
            self.assertTrue(result["session"]["metadata"]["omx"]["managed"])
            self.assertIn("oh-my-codex", result["session"]["metadata"]["omx"]["markers"])
        finally:
            os.unlink(tmpfile)

class TestClaudeParser(unittest.TestCase):
    def test_parse_claude_session_content_and_filters(self):
        from app.parsers.claude import parse_claude_session

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = os.path.join(tmpdir, "projects", "-tmp-claude-project")
            os.makedirs(project_dir)
            session_file = os.path.join(project_dir, "session-001.jsonl")
            events = [
                {"type": "user", "message": {"role": "user", "content": "Hello Claude"},
                 "uuid": "u1", "timestamp": "2026-05-13T09:29:20.857Z", "cwd": "/tmp/claude-project", "version": "2.1.140"},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "I can help."},
                    {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "echo searchable-tool"}},
                    {"type": "attachment", "content": "DO NOT IMPORT PART"},
                ]}, "uuid": "a1", "timestamp": "2026-05-13T09:29:21.000Z"},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1", "content": "searchable result"},
                    {"type": "text", "text": "Thanks"},
                ]}, "uuid": "u2", "timestamp": "2026-05-13T09:29:22.000Z"},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "content", "content": "List content text"},
                ]}, "uuid": "a2", "timestamp": "2026-05-13T09:29:23.000Z"},
                {"type": "attachment", "message": {"role": "user", "content": "DO NOT IMPORT ATTACHMENT"},
                 "uuid": "att1", "timestamp": "2026-05-13T09:29:24.000Z"},
            ]
            with open(session_file, "w", encoding="utf-8") as f:
                for event in events:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")

            result = parse_claude_session(session_file, "test-server")

        self.assertIsNotNone(result)
        session = result["session"]
        messages = result["messages"]
        contents = [m["content"] for m in messages]

        self.assertEqual(session["id"], "claude-test-server-session-001")
        self.assertEqual(session["source"], "claude")
        self.assertEqual(session["server_name"], "test-server")
        self.assertEqual(session["cwd"], "/tmp/claude-project")
        self.assertEqual(session["cli_version"], "2.1.140")
        self.assertIn("Hello Claude", session["title"])
        self.assertIn("Hello Claude", contents)
        self.assertIn("I can help.", contents)
        self.assertIn("Thanks", contents)
        self.assertIn("List content text", contents)
        self.assertTrue(any(m["msg_type"] == "tool_use" and "searchable-tool" in m["content"] for m in messages))
        self.assertTrue(any(m["role"] == "tool" and m["msg_type"] == "tool_result" and "searchable result" in m["content"] for m in messages))
        joined = "\n".join(contents)
        self.assertNotIn("DO NOT IMPORT ATTACHMENT", joined)
        self.assertNotIn("DO NOT IMPORT PART", joined)

    def test_find_claude_session_files(self):
        from app.parsers.claude import find_claude_session_files

        with tempfile.TemporaryDirectory() as tmpdir:
            projects = os.path.join(tmpdir, "projects")
            project_dir = os.path.join(projects, "project-a")
            os.makedirs(project_dir)
            wanted = os.path.join(project_dir, "session.jsonl")
            ignored = os.path.join(projects, "top-level.jsonl")
            with open(wanted, "w", encoding="utf-8") as f:
                f.write("{}\n")
            with open(ignored, "w", encoding="utf-8") as f:
                f.write("{}\n")

            self.assertEqual(find_claude_session_files(projects), [wanted])


class TestOpenCodeParser(unittest.TestCase):
    @staticmethod
    def _create_sqlite_database(path: str):
        conn = sqlite3.connect(path)
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE project (
                id TEXT PRIMARY KEY, worktree TEXT NOT NULL, name TEXT,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
            );
            CREATE TABLE session (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
                slug TEXT NOT NULL, directory TEXT NOT NULL, title TEXT NOT NULL,
                version TEXT NOT NULL, agent TEXT, model TEXT,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                time_archived INTEGER
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL, data TEXT NOT NULL
            );
        """)
        now = 1786074175000
        conn.execute(
            "INSERT INTO project VALUES (?,?,?,?,?)",
            ("project-1", "/tmp/open project", "Open Project", now, now + 5000),
        )
        sessions = [
            (
                "ses_root", "project-1", None, "root-slug", "/tmp/open project",
                "OpenCode root", "1.18.14", "build",
                json.dumps({"id": "gpt-test", "providerID": "test"}),
                now, now + 5000, None,
            ),
            (
                "ses_child", "project-1", "ses_root", "child-slug",
                "/tmp/open project", "Research child", "1.18.14", "general",
                json.dumps({"id": "gpt-test", "providerID": "test"}),
                now + 2000, now + 4000, None,
            ),
        ]
        conn.executemany(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", sessions
        )
        messages = [
            (
                "msg_user", "ses_root", now + 100, now + 100,
                json.dumps({"role": "user", "time": {"created": now + 100}}),
            ),
            (
                "msg_assistant", "ses_root", now + 200, now + 1000,
                json.dumps({
                    "role": "assistant", "agent": "build",
                    "providerID": "test", "modelID": "gpt-test",
                    "parentID": "msg_user", "tokens": {"input": 12},
                }),
            ),
            (
                "msg_child", "ses_child", now + 2100, now + 3000,
                json.dumps({
                    "role": "assistant", "agent": "general",
                    "providerID": "test", "modelID": "gpt-test",
                }),
            ),
        ]
        conn.executemany("INSERT INTO message VALUES (?,?,?,?,?)", messages)
        parts = [
            (
                "prt_user", "msg_user", "ses_root", now + 100, now + 100,
                json.dumps({"type": "text", "text": "实现 OpenCode 捕获"}, ensure_ascii=False),
            ),
            (
                "prt_synthetic", "msg_user", "ses_root", now + 150, now + 150,
                json.dumps({
                    "type": "text", "text": "opened editor context",
                    "synthetic": True,
                }),
            ),
            (
                "prt_reasoning", "msg_assistant", "ses_root", now + 200, now + 200,
                json.dumps({"type": "reasoning", "text": "检查存储格式"}, ensure_ascii=False),
            ),
            (
                "prt_tool", "msg_assistant", "ses_root", now + 300, now + 1000,
                json.dumps({
                    "type": "tool", "tool": "bash", "callID": "call-1",
                    "state": {
                        "status": "completed", "input": {"command": "pytest"},
                        "output": "12 passed", "time": {"start": now + 300, "end": now + 1000},
                    },
                }),
            ),
            (
                "prt_patch", "msg_assistant", "ses_root", now + 1100, now + 1100,
                json.dumps({"type": "patch", "hash": "abc", "files": ["app.py"]}),
            ),
            (
                "prt_child", "msg_child", "ses_child", now + 2100, now + 3000,
                json.dumps({"type": "text", "text": "子 Agent 结论"}, ensure_ascii=False),
            ),
            (
                "prt_task", "msg_assistant", "ses_root", now + 1200, now + 3200,
                json.dumps({
                    "type": "tool", "tool": "task", "callID": "call-child",
                    "state": {
                        "status": "completed", "input": {"prompt": "research"},
                        "output": "<task_result>子 Agent 结论</task_result>",
                        "metadata": {"sessionId": "ses_child"},
                        "time": {"start": now + 1200, "end": now + 3200},
                    },
                }, ensure_ascii=False),
            ),
        ]
        conn.executemany("INSERT INTO part VALUES (?,?,?,?,?,?)", parts)
        conn.commit()
        return conn

    def test_parse_current_sqlite_with_wal_and_merge_child(self):
        from app.parsers.opencode import parse_opencode_groups, scan_opencode_source

        with tempfile.TemporaryDirectory() as tmpdir:
            database = os.path.join(tmpdir, "opencode.db")
            writer = self._create_sqlite_database(database)
            try:
                # Keep the writer open and WAL uncheckpointed. The read-only
                # parser must still see committed data through SQLite itself.
                source = scan_opencode_source(tmpdir)
                self.assertEqual(source["kind"], "sqlite")
                self.assertEqual(len(source["groups"]), 1)
                self.assertEqual(
                    source["groups"][0]["member_ids"],
                    ["ses_root", "ses_child"],
                )

                result = parse_opencode_groups(source, "test-server")[0]
            finally:
                writer.close()

        session = result["session"]
        messages = result["messages"]
        self.assertEqual(session["id"], "opencode-test-server-ses_root")
        self.assertEqual(session["source"], "opencode")
        self.assertEqual(session["cwd"], "/tmp/open project")
        self.assertEqual(session["model"], "test/gpt-test")
        self.assertEqual(session["cli_version"], "1.18.14")
        self.assertEqual(
            session["metadata"]["merged_child_sessions"][0]["session_id"],
            "ses_child",
        )
        self.assertTrue(any(m["msg_type"] == "reasoning" for m in messages))
        self.assertTrue(any(m["msg_type"] == "tool_call" and "pytest" in m["content"] for m in messages))
        self.assertTrue(any(m["msg_type"] == "tool_result" and "12 passed" in m["content"] for m in messages))
        self.assertTrue(any(m["msg_type"] == "patch_apply" for m in messages))
        self.assertTrue(any("[subagent:general] 子 Agent 结论" in m["content"] for m in messages))
        synthetic = next(m for m in messages if "opened editor context" in m["content"])
        self.assertEqual(synthetic["role"], "system")
        self.assertEqual(synthetic["msg_type"], "synthetic_context")
        task_result = next(m for m in messages if m["msg_type"] == "subagent_result")
        self.assertEqual(task_result["content"], "[Subagent session: ses_child] completed")
        self.assertNotIn("<task_result>", "\n".join(m["content"] for m in messages))

    def test_parse_legacy_json_storage(self):
        from app.parsers.opencode import parse_opencode_groups, scan_opencode_source

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = os.path.join(tmpdir, "storage")
            session_dir = os.path.join(storage, "session", "project-1")
            message_dir = os.path.join(storage, "message", "ses_legacy")
            part_dir = os.path.join(storage, "part", "msg_legacy")
            os.makedirs(session_dir)
            os.makedirs(message_dir)
            os.makedirs(part_dir)
            with open(os.path.join(session_dir, "ses_legacy.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "id": "ses_legacy", "projectID": "project-1",
                    "slug": "legacy", "directory": "/tmp/legacy",
                    "title": "Legacy OpenCode", "version": "0.9.0",
                    "time": {"created": 1000, "updated": 3000},
                }, handle)
            with open(os.path.join(message_dir, "msg_legacy.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "id": "msg_legacy", "sessionID": "ses_legacy", "role": "user",
                    "time": {"created": 2000, "updated": 2000},
                }, handle)
            with open(os.path.join(part_dir, "prt_legacy.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "id": "prt_legacy", "messageID": "msg_legacy",
                    "sessionID": "ses_legacy", "type": "text",
                    "text": "legacy searchable", "time": {"created": 2000},
                }, handle)

            source = scan_opencode_source(tmpdir)
            result = parse_opencode_groups(source)[0]

        self.assertEqual(source["kind"], "json")
        self.assertEqual(result["session"]["id"], "opencode-local-ses_legacy")
        self.assertEqual(result["messages"][0]["content"], "legacy searchable")

    def test_rejects_incompatible_sqlite_schema(self):
        from app.parsers.opencode import OpenCodeFormatError, scan_opencode_source

        with tempfile.TemporaryDirectory() as tmpdir:
            conn = sqlite3.connect(os.path.join(tmpdir, "opencode.db"))
            conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
            conn.commit()
            conn.close()
            with self.assertRaises(OpenCodeFormatError):
                scan_opencode_source(tmpdir)

if __name__ == "__main__":
    unittest.main()
