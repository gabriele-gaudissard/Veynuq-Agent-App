import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from veyq.context import ContextManager, MEMORY_PREFIX
from veyq.engine import ModelClient
from veyq.generation import GenerationError
from veyq.storage import DEFAULTS


class ContextTests(unittest.TestCase):
    def manager(self, **settings):
        config = {**DEFAULTS, **settings}
        events = []
        manager = ContextManager(config, lambda *event: events.append(event), Mock(),
                                 "http://localhost:11434")
        manager.events = events
        return manager

    def history(self, count):
        rows = [{"role": "system", "content": "Complete the user's current request."}]
        for n in range(count // 2):
            rows += [
                {"role": "user", "content": (
                    "Always preserve the database. Use src/storage.py. " if n == 0 else ""
                ) + f"Analysis request {n}. " + "observation " * 180},
                {"role": "assistant", "content": f"Observed result {n}. " + "evidence " * 80},
            ]
        rows += [{"role": "user", "content": "Continue the latest analysis."}]
        return rows

    def test_fresh_request_fits_with_configured_response(self):
        manager = self.manager(response_tokens=8192)
        rows = self.history(0)
        prepared, capacity, output = manager.prepare(rows, [], 16384)
        self.assertEqual(prepared, rows)
        self.assertEqual(output, 8192)
        self.assertFalse(manager.last["compacted"])
        self.assertEqual(capacity, 16384)

    def test_50_and_200_messages_preserve_goal_constraints_and_transcript(self):
        for count in (50, 200):
            with self.subTest(count=count):
                manager = self.manager()
                rows = self.history(count)
                original = copy.deepcopy(rows)
                prepared, capacity, output = manager.prepare(rows, [], 16384)
                self.assertEqual(rows, original)
                self.assertEqual(prepared[-1], rows[-1])
                self.assertTrue(manager.last["compacted"])
                self.assertIn("Always preserve the database", json.dumps(manager.memory))
                self.assertIn("src/storage.py", json.dumps(manager.memory))
                self.assertLessEqual(manager.last["input"] + output + 256, capacity)
                self.assertGreaterEqual(output, 1024)

    def test_thresholds_75_95_and_full_trigger_compaction_before_inference(self):
        for count, expected in ((65, False), (83, True), (88, True), (120, True)):
            with self.subTest(count=count):
                manager = self.manager(response_tokens=1000)
                # Count messages as 100 units, isolating the threshold from the
                # heuristic tokenizer while still exercising selection.
                manager.count = lambda rows, tools: (100 * len(rows), "tokenizer")
                rows = [{"role": "user", "content": f"Old request {n}"} for n in range(count)]
                rows.append({"role": "user", "content": "Current request"})
                prepared, cap, output = manager.prepare(rows, [], 10000)
                self.assertEqual(manager.last["compacted"], expected)
                self.assertEqual(prepared[-1]["content"], "Current request")
                self.assertLessEqual(manager.last["input"] + output + 256, cap)

    def test_large_previous_user_is_compacted_and_current_input_is_not_truncated(self):
        manager = self.manager()
        rows = [{"role": "system", "content": "Explain."},
                {"role": "user", "content": "Earlier source\n" + "x" * 100000},
                {"role": "assistant", "content": "Earlier answer."},
                {"role": "user", "content": "What was the next step?"}]
        original = copy.deepcopy(rows)
        prepared, _, _ = manager.prepare(rows, [], 16384)
        self.assertEqual(rows, original)
        self.assertEqual(prepared[-1], rows[-1])
        self.assertTrue(any(row["content"].startswith(MEMORY_PREFIX) for row in prepared))

    def test_single_oversized_request_fails_without_committing_memory(self):
        manager = self.manager()
        manager.memory = {"summary": "previous valid memory"}
        manager.commit = Mock()
        rows = [{"role": "user", "content": "JSON input " + "1234 " * 20000}]
        with self.assertRaises(GenerationError) as error:
            manager.prepare(rows, [], 8192)
        self.assertEqual(error.exception.reason, "input_too_large")
        manager.commit.assert_not_called()
        self.assertEqual(manager.memory["summary"], "previous valid memory")
        self.assertEqual(len(rows[0]["content"]), len("JSON input " + "1234 " * 20000))

    def test_all_request_overhead_is_counted(self):
        manager = self.manager(response_tokens=8192)
        rows = self.history(50)
        rows.insert(-1, {"role": "user", "content":
                    "Application-generated status, includes untrusted task state " + "status " * 500})
        rows[0]["content"] += " system instructions " * 450
        tools = [{"type": "function", "function": {"name": "existing_tool", "description": "unchanged " * 300}}]
        prepared, cap, output = manager.prepare(rows, tools, 16384)
        self.assertEqual(prepared[0], rows[0])
        self.assertEqual(prepared[-1], rows[-1])
        self.assertIn(rows[-2], prepared)
        self.assertGreater(manager.last["tool_estimate"], 0)
        self.assertGreater(manager.last["system_estimate"], 1000)
        self.assertLessEqual(manager.last["input"] + output + 256, cap)

    def test_compaction_failure_keeps_previous_memory(self):
        manager = self.manager()
        manager.memory = {"summary": "valid previous decision"}
        manager.commit = Mock()
        with patch("veyq.context.source_memory", side_effect=ValueError("invalid summary")):
            with self.assertRaises(ValueError):
                manager.prepare(self.history(50), [], 8192)
        self.assertEqual(manager.memory["summary"], "valid previous decision")
        manager.commit.assert_not_called()

    def test_resume_compaction_keeps_all_source_messages(self):
        manager = self.manager()
        rows = self.history(200)
        prepared, _, _ = manager.prepare(rows, [], 16384)
        saved = copy.deepcopy(manager.memory)
        rows += [{"role": "assistant", "content": "Verified."},
                 {"role": "user", "content": "Now explain the final decision."}]
        other = self.manager()
        other.memory = saved
        continuation, _, _ = other.prepare(rows, [], 16384)
        self.assertEqual(continuation[-1], rows[-1])
        self.assertIn("database", json.dumps(other.memory))
        self.assertEqual(len(rows), 204)

    def test_complete_tool_exchanges_remain_paired(self):
        rows = [{"role": "user", "content": "Continue the task."}]
        for n in range(20):
            rows += [
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": str(n), "type": "function", "function": {"name": "read_file", "arguments": {"path": "src/a.py"}}}]},
                {"role": "tool", "tool_call_id": str(n), "name": "read_file", "content": "observed " * 500},
            ]
        manager = self.manager()
        prepared, _, _ = manager.prepare(rows, [], 8192)
        calls = {call["id"] for row in prepared for call in row.get("tool_calls", [])}
        answers = {row["tool_call_id"] for row in prepared if row["role"] == "tool"}
        self.assertEqual(calls, answers)
        self.assertEqual(prepared[-1], rows[-1])

    def test_external_slot_capacity_and_tokenizer_are_used(self):
        config = {**DEFAULTS, "provider": "compatible", "context_tokens": 65536}
        http = Mock()
        def result(value):
            response = Mock()
            response.json.return_value = value
            return response
        http.get.side_effect = [
            result({"chat_template": "native", "total_slots": 4, "default_generation_settings": {"n_ctx": 32768}}),
            result([{"n_ctx": 8192}] * 4)]
        http.post.side_effect = [result({"prompt": "rendered by loaded model"}), result({"tokens": [1, 2, 3]})]
        manager = ContextManager(config, lambda *args: None, http, "http://127.0.0.1:8080/v1")
        rows = [{"role": "user", "content": "Hello"}]
        _, cap, _ = manager.prepare(rows, [], 65536)
        self.assertEqual(cap, 8192)
        self.assertEqual(manager.last["slots"], 4)
        self.assertEqual(manager.last["method"], "tokenizer")
        self.assertEqual(manager.last["input"], 3)
        self.assertEqual(http.post.call_count, 2)
        for call in http.post.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_oom_does_not_increase_context_or_retry_model(self):
        client = ModelClient({**DEFAULTS}, "", threading.Event(), lambda *args: None)
        client.metadata = {"capabilities": ["tools"]}
        client.tools = []
        failed = Mock(status_code=500, text='{"error":"CUDA out of memory"}')
        try:
            with patch.object(client.session, "post", return_value=failed) as post, self.assertLogs("veyq.engine"):
                with self.assertRaisesRegex(RuntimeError, "out of memory"):
                    client.chat([{"role": "user", "content": "Explain this."}])
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs["json"]["options"]["num_ctx"], 16384)
        finally:
            client.close()

    def test_native_runtime_measurement_updates_effective_capacity(self):
        manager = self.manager()
        manager.prepare(self.history(0), [], 16384)
        observed = Mock()
        observed.json.return_value = {"models": [{"name": DEFAULTS["model"], "context_length": 8192}]}
        manager.http.get.return_value = observed
        manager.observe({"prompt_eval_count": 100}, True)
        self.assertEqual(manager.events[-1][1]["capacity"], 8192)
        self.assertEqual(manager.events[-1][1]["method"], "observed")

    def test_long_output_reserve_after_large_history(self):
        manager = self.manager(response_tokens=8192, min_response_tokens=2048)
        prepared, capacity, output = manager.prepare(self.history(200), [], 32768)
        self.assertGreaterEqual(output, 8192)
        self.assertLessEqual(manager.last["input"] + output + 256, capacity)
        self.assertEqual(prepared[-1]["content"], "Continue the latest analysis.")
