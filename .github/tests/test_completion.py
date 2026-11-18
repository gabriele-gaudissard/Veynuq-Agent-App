import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from veyq.desktop import DesktopAPI
from veyq.engine import ModelClient, ModelFailure
from veyq.storage import Store
from veyq.tools import ToolRunner


def tool(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": name + "-" + str(id(args)), "type": "function",
         "function": {"name": name, "arguments": args}}]}


DONE = {"role": "assistant", "content": "Done."}


class CompletionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.root / "profile")
        self.store.data["settings"].update(workspace=str(self.project), permission="full",
                                          network=True, auto_update=False)
        self.api = DesktopAPI(self.store)
        self.sid = self.api.create_session()
        self.agent = self.api._agent

    def run_agent(self, replies, prompt="Create proof.txt"):
        with patch.object(ModelClient, "chat", side_effect=replies) as chat:
            self.api.start_run(self.sid, prompt)
            self.agent.worker.join(15)
            if self.agent.busy:
                self.agent.stop()
                self.agent.worker.join(15)
            self.assertFalse(self.agent.busy)
        self.session = self.api.get_session(self.sid)
        return chat

    def test_normal_text_reply_needs_no_action(self):
        chat = self.run_agent([DONE], "Explain what a port city is.")
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(self.agent.state, "completed")

    def test_created_file_is_verified_by_actual_hash(self):
        self.run_agent([tool("write_file", path="proof.txt", content="verified"), DONE])
        self.assertEqual(self.agent.state, "completed")
        self.assertEqual((self.project / "proof.txt").read_bytes(), b"verified")
        result = json.loads(next(row for row in self.session["history"] if row["role"] == "tool")["content"])
        self.assertEqual(result["verification"]["state"], "completed")
        self.assertEqual(self.session["task"]["operations"][0]["evidence"], "file_readback_sha256")
        self.assertFalse(self.session["task"]["verification"])

    def test_multiple_calls_return_results_in_order_and_continue(self):
        observed = []
        replies = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "first", "type": "function", "function": {"name": "write_file", "arguments": {"path": "a.py", "content": "a = 1\n"}}},
                {"id": "second", "type": "function", "function": {"name": "write_file", "arguments": {"path": "b.py", "content": "b = 2\n"}}}]},
            tool("read_file", path="a.py"),
            DONE,
        ]
        def respond(client, messages):
            observed.append(messages)
            return replies.pop(0)
        with patch.object(ModelClient, "chat", respond):
            self.api.start_run(self.sid, "Create a.py and b.py, then inspect a.py.")
            self.agent.worker.join(15)
        self.assertEqual(self.agent.state, "completed")
        returned = [row["tool_call_id"] for row in observed[1] if row["role"] == "tool"]
        self.assertEqual(returned, ["first", "second"])
        history = self.api.get_session(self.sid)["history"]
        calls = [call["id"] for row in history for call in row.get("tool_calls", [])]
        results = [row["tool_call_id"] for row in history if row["role"] == "tool"]
        self.assertEqual(calls, results)

    def test_write_is_not_completed_when_actual_verification_and_recovery_fail(self):
        with patch("veyq.verification.verify_file", return_value=False), \
                patch.object(ToolRunner, "tool_read_file", side_effect=OSError("Unavailable target")):
            self.run_agent([tool("write_file", path="proof.txt", content="still saved"), DONE, DONE, DONE])
        self.assertEqual(self.agent.state, "unverified")
        self.assertEqual((self.project / "proof.txt").read_text(), "still saved")
        self.assertTrue(self.session["task"]["verification"])

    def test_readback_detects_wrong_content_even_when_length_matches(self):
        from veyq.verification import verify_file
        import hashlib
        path = self.project / "sample"
        path.write_bytes(b"wrong")
        self.assertFalse(verify_file(path, 5, hashlib.sha256(b"right").hexdigest()))

    def test_execution_failure_is_reported_and_does_not_repeat_command(self):
        with patch.object(ToolRunner, "tool_exec_cmd", return_value={"exit_code": 7, "output": "configuration failed"}) as execute:
            self.run_agent([tool("exec_cmd", command="fixture"), DONE, DONE, DONE], "Run the configuration.")
        execute.assert_called_once()
        self.assertEqual(self.agent.state, "failed")
        self.assertEqual(self.session["task"]["operations"][0]["state"], "failed")
        self.assertIn("failed", self.session["history"][-1]["content"])

    def test_partial_completion_lists_both_files(self):
        (self.project / "b.py").write_text("existing")
        self.run_agent([tool("write_file", path="a.py", content="new"),
                        tool("write_file", path="b.py", content="new"), DONE],
                       "Create a.py and replace b.py.")
        self.assertEqual(self.agent.state, "partial")
        rows = self.session["task"]["operations"]
        self.assertEqual([row["state"] for row in rows], ["completed", "failed"])
        self.assertEqual((self.project / "b.py").read_text(), "existing")
        self.assertIn("Some operations failed", self.session["history"][-1]["content"])

    def test_corrected_operation_recovers_failure(self):
        (self.project / "proof.txt").write_text("old")
        self.run_agent([tool("write_file", path="proof.txt", content="new"),
                        tool("write_file", path="proof.txt", content="new", overwrite=True), DONE], "Overwrite proof.txt.")
        self.assertEqual(self.agent.state, "completed")
        self.assertTrue(self.session["task"]["operations"][0]["recovered"])
        self.assertEqual((self.project / "proof.txt").read_text(), "new")

    def test_verified_duplicate_write_reuses_result(self):
        writes = ToolRunner.tool_write_file
        seen = []
        def write(runner, *args, **kwargs):
            seen.append(args)
            return writes(runner, *args, **kwargs)
        with patch.object(ToolRunner, "tool_write_file", write):
            self.run_agent([tool("write_file", path="proof.txt", content="once"),
                            tool("write_file", path="proof.txt", content="once"), DONE])
        self.assertEqual(len(seen), 1)
        tool_results = [json.loads(row["content"]) for row in self.session["history"] if row["role"] == "tool"]
        self.assertTrue(tool_results[1]["reused_result"])
        self.assertEqual(self.agent.state, "completed")

    def test_no_action_and_empty_response_have_different_diagnostics(self):
        self.run_agent([DONE, DONE, DONE])
        self.assertEqual(self.agent.state, "failed")
        self.assertEqual(self.session["task"]["diagnostics"]["category"], "no_tool_call")
        self.run_agent([{"role": "assistant", "content": ""}] * 3, "Explain a simple concept.")
        self.assertEqual(self.agent.state, "failed")
        self.assertEqual(self.session["task"]["diagnostics"]["category"], "empty_response")

    def test_iteration_limit_preserves_performed_work(self):
        self.store.data["settings"]["max_steps"] = 1
        self.run_agent([tool("write_file", path="proof.txt", content="saved")])
        self.assertEqual(self.agent.state, "limit")
        self.assertEqual((self.project / "proof.txt").read_text(), "saved")
        self.assertEqual(self.session["task"]["diagnostics"]["category"], "iteration_limit")

    def test_truncation_and_context_keep_separate_diagnostics(self):
        for category in ("truncated", "input_too_large", "timeout"):
            with self.subTest(category=category):
                self.run_agent([ModelFailure(category, "Controlled model error")])
                self.assertEqual(self.agent.state, "failed")
                self.assertEqual(self.session["task"]["diagnostics"]["category"], category)

    def test_same_conversation_can_continue_after_failure(self):
        self.run_agent([DONE, DONE, DONE])
        previous = len(self.session["history"])
        self.run_agent([tool("write_file", path="proof.txt", content="recovered"), DONE])
        self.assertEqual(self.agent.state, "completed")
        self.assertGreater(len(self.session["history"]), previous)
        self.assertEqual((self.project / "proof.txt").read_text(), "recovered")

    def test_denied_write_is_not_repeated(self):
        self.store.data["settings"]["permission"] = "always"
        with patch.object(self.agent, "approve", return_value=False) as approve:
            self.run_agent([tool("write_file", path="proof.txt", content="denied"), DONE])
        approve.assert_called_once()
        self.assertFalse((self.project / "proof.txt").exists())
        self.assertEqual(self.agent.state, "blocked")

    def test_unfinished_plan_prevents_false_completion(self):
        self.run_agent([tool("update_plan", steps=[{"step": "Inspect files", "status": "in_progress"}]),
                        DONE, DONE, DONE], "Inspect the selected project.")
        self.assertEqual(self.agent.state, "unverified")
        self.assertEqual(self.session["plan"][0]["status"], "in_progress")

    def test_uncertain_operation_is_not_dispatched_a_second_time(self):
        with patch.object(ToolRunner, "execute", return_value={
                "ok": False, "outcome_unknown": True, "error": "Interrupted after dispatch"}) as execute:
            self.run_agent([tool("write_file", path="proof.txt", content="uncertain"),
                            tool("write_file", path="proof.txt", content="uncertain"), DONE, DONE, DONE])
        execute.assert_called_once()
        self.assertEqual(self.agent.state, "unverified")
        self.assertEqual(self.session["task"]["diagnostics"]["category"], "outcome_unknown")

    def test_native_adapter_preserves_tool_name_and_result_pairing(self):
        client = ModelClient(self.store.data["settings"], "", threading.Event(), lambda *args: None)
        client.metadata = {"capabilities": ["tools"]}
        result = Mock(status_code=200)
        result.iter_lines.return_value = iter([b'{"done":true,"done_reason":"stop","message":{"content":"Observed."}}'])
        messages = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "first", "type": "function", "function": {"name": "read_file", "arguments": {"path": "a.py"}}}]},
            {"role": "tool", "name": "read_file", "tool_call_id": "first", "content": "actual evidence"}]
        try:
            with patch.object(client.session, "post", return_value=result) as post:
                client.chat(messages)
            sent = post.call_args.kwargs["json"]["messages"][-1]
            self.assertEqual(sent["tool_name"], "read_file")
            self.assertEqual(sent["tool_call_id"], "first")
            self.assertEqual(sent["content"], "actual evidence")
            self.assertNotIn("tool_name", messages[-1])
        finally:
            client.close()
