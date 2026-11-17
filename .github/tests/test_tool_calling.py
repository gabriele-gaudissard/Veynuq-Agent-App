import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from veyq.desktop import DesktopAPI
from veyq.durability import compact
from veyq.engine import Cancelled, ModelClient
from veyq.storage import Store
from veyq.tools import ToolRunner


def response(chunks=(), *, status=200, error="", sse=False, content=None):
    result = Mock(status_code=status, text=error)
    if content is not None:
        result.content = json.dumps(content).encode()
    prefix = b"data: " if sse else b""
    result.iter_lines.return_value = iter(
        value if isinstance(value, bytes) else prefix + json.dumps(value).encode() for value in chunks
    )
    return result


def call(name, args, index=0):
    return {"function": {"index": index, "name": name, "arguments": args}}


def native(*calls, text="", reason="stop"):
    return response([
        {"message": {"content": text, "tool_calls": list(calls)}},
        {"done": True, "done_reason": reason},
    ])


class ToolCallingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.root / "profile")
        self.store.data["settings"].update(workspace=str(self.project), max_steps=12)
        self.cancel = threading.Event()
        self.events = []
        self.runner = ToolRunner(self.store, self.store.data["settings"], "test", self.cancel,
                                 lambda *args: self.events.append(args), lambda preview: True, self.root / "app", {})
        self.addCleanup(self.runner.close)

    def client(self, **settings):
        client = ModelClient({**self.runner.settings, **settings}, "", self.cancel,
                             lambda *args: self.events.append(args))
        client.metadata = {"capabilities": ["tools", "completion"]}
        self.addCleanup(client.close)
        return client

    def write(self, content, **kwargs):
        return self.runner.execute("write_file", {"path": "proof.py", "content": content, **kwargs})

    def test_empty_and_special_text_written_exactly_and_audited(self):
        self.assertTrue(self.write("", final=True)["ok"])
        target = self.project / "proof.py"
        self.assertEqual(target.read_bytes(), b"")
        text = '# caf\u00e9 \U0001F680\npath = r"C:\\temp\\foo"\nquote = "\"literal\""\r\n'
        self.assertTrue(self.write(text, overwrite=True)["result"]["published"])
        self.assertEqual(target.read_bytes(), text.encode("utf-8"))
        audit = (self.store.root / "audit.jsonl").read_text()
        self.assertNotIn("literal", audit)
        self.assertIn("completed", audit)

    def test_explicit_line_breaks_add_only_missing_boundaries(self):
        first = self.write("first", final=False, newline="lf")
        self.assertEqual(first["result"]["next_part"], 2)
        self.assertFalse((self.project / "proof.py").exists())
        self.assertTrue(self.write("second\n", part=2, newline="lf")["ok"])
        self.assertTrue(self.write("third", part=3, final=True, newline="crlf")["ok"])
        target = self.project / "proof.py"
        self.assertEqual(target.read_bytes(), b"first\nsecond\nthird\r\n")
        result = self.write("first\nsecond\nthird\r\n", overwrite=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["result"]["modified"])
        self.assertIsNone(result["result"]["backup"])

    def test_staged_order_duplicate_preservation_and_publish_of_long_code(self):
        target = self.project / "proof.py"
        first = "".join(f"v{i} = {i}\n" for i in range(20000))
        self.assertGreater(len(first), 200000)
        second = "\nassert v19999 == 19999\n"
        self.assertEqual(self.write(first, part=1, final=False)["result"]["next_part"], 2)
        self.assertEqual(self.write(first, part=1, final=False)["result"]["staged_bytes"], len(first))
        self.assertFalse(self.write("different", part=1, final=False)["ok"])
        self.assertFalse(self.write(second, part=3, final=True)["ok"])
        self.assertFalse(target.exists())
        self.assertTrue(self.write(second, part=2, final=True)["result"]["published"])
        self.assertEqual(target.read_bytes(), (first + second).encode())
        compile(target.read_bytes(), str(target), "exec")
        self.assertFalse(list(self.project.glob(".veynuq-*.tmp")))

    def test_existing_file_requires_overwrite_and_interrupted_draft_keeps_it(self):
        target = self.project / "proof.py"
        target.write_bytes(b"original")
        self.assertFalse(self.write("oops")["ok"])
        self.assertTrue(self.write("incomplete", part=1, final=False, overwrite=True)["ok"])
        self.assertEqual(target.read_bytes(), b"original")
        self.runner.close()
        self.assertEqual(target.read_bytes(), b"original")
        self.assertFalse(list(self.project.glob(".veynuq-*.tmp")))

    def test_changed_destination_and_failed_publish_do_not_lose_data(self):
        target = self.project / "proof.py"
        target.write_bytes(b"original")
        self.assertTrue(self.write("first", part=1, final=False, overwrite=True)["ok"])
        target.write_bytes(b"external edit")
        result = self.write("last", part=2, final=True)
        self.assertFalse(result["ok"])
        self.assertIn("changed", result["error"])
        self.assertEqual(target.read_bytes(), b"external edit")
        with patch("veyq.file_write.os.replace", side_effect=PermissionError("locked")):
            # Call directly to avoid patching the state store's own atomic writes.
            with self.assertRaises(PermissionError):
                self.runner.tool_write_file("proof.py", "complete", overwrite=True)
        self.assertEqual(target.read_bytes(), b"external edit")
        self.assertFalse(list(self.project.glob(".veynuq-*.tmp")))

    def test_large_destination_can_be_backed_up_and_restored(self):
        target = self.project / "proof.py"
        original = b"# hello\n" * 650000
        target.write_bytes(original)
        result = self.write("complete", overwrite=True)
        self.assertTrue(result["ok"])
        self.assertEqual(target.read_bytes(), b"complete")
        self.assertTrue(self.runner.execute("restore_backup", {"backup_id": result["result"]["backup"]})["ok"])
        self.assertEqual(target.read_bytes(), original)

    def test_schema_and_json_validation_precede_dispatch(self):
        cases = [
            ('{"path":"proof.py","content":', "tool_json"),
            ('{"path":"proof.py","path":"other","content":""}', "tool_json"),
            ('{"path":"proof.py","content":"\\ud800"}', "tool_json"),
            ({}, "tool_schema"), ({"path":"proof.py","content":7}, "tool_schema"),
            ({"path":"proof.py","content":"","part":0}, "tool_schema"),
            ({"path":"proof.py","content":"","final":None}, "tool_schema"),
            ({"path":"proof.py","content":"","extra":7}, "tool_schema"),
        ]
        for args, reason in cases:
            with self.subTest(args=args):
                client = self.client(tool_retries=0)
                result = native(call("write_file", args))
                with patch.object(client.session, "post", return_value=result) as post, self.assertLogs("veyq.engine") as logs:
                    with self.assertRaisesRegex(RuntimeError, "No action"):
                        client.chat([{"role":"user","content":"Create proof.py."}])
                self.assertIn(reason, logs.output[0])
                self.assertNotIn("proof.py", logs.output[0])
                post.assert_called_once()
                result.close.assert_called_once()
        self.assertFalse((self.project / "proof.py").exists())

    def test_upstream_eof_retries_with_short_parts_without_repeating_history(self):
        client = self.client(tool_retries=1)
        detail = 'llama-server returned invalid tool call arguments for "write_file": unexpected end of JSON input'
        initial = response(status=500, error=json.dumps({"error": detail}))
        accepted = native(call("write_file", {"path":"proof.py","content":"test", "part":1, "final":False}))
        history = [{"role":"user","content":"Create proof.py."}, {"role":"tool","tool_call_id":"id","content":"Existing observed result."}]
        with patch.object(client.session, "post", side_effect=[initial, accepted]) as post, self.assertLogs("veyq.engine") as logs:
            self.assertEqual(client.chat(history)["tool_calls"][0]["function"]["arguments"]["part"], 1)
        payloads = [req.kwargs["json"] for req in post.call_args_list]
        self.assertEqual(payloads[0]["messages"], history)
        self.assertEqual(payloads[1]["messages"][1:-1], history)
        self.assertIn("substantially shorter", payloads[1]["messages"][0]["content"])
        self.assertIn("Call write_file once", payloads[1]["messages"][-1]["content"])
        self.assertIn("source=llama-server reason=backend_tool_eof tool=write_file http=500 retry=True", logs.output[0])
        initial.close.assert_called_once()

    def test_compatible_batch_assembled_by_index_and_finish_reason(self):
        client = self.client(provider="compatible")
        chunks = [
            {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"id-1","function":{"name":"read_","arguments":'{"path":'}}]}}]},
            {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"id-0","function":{"name":"list_dir","arguments":'{"path":"."}'}},
                                               {"index":1,"function":{"name":"file","arguments":'"proof.py"}'}}]}}]},
            {"choices":[{"delta":{},"finish_reason":"tool_calls"}]},
        ]
        with patch.object(client.session, "post", return_value=response(chunks, sse=True)):
            calls = client.chat([])["tool_calls"]
        self.assertEqual([c["id"] for c in calls], ["id-0", "id-1"])
        self.assertEqual(calls[1]["function"]["name"], "read_file")
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"path":"proof.py"})

    def test_finish_length_and_done_without_finish_discard_even_valid_fragments(self):
        fragment = {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"id","function":{"name":"list_dir","arguments":'{"path":"."}'}}]}}]}
        for last, category in [
            (b"data: [DONE]", "stream_incomplete"),
            ({"choices":[{"delta":{},"finish_reason":"length"}]}, "reason=length"),
        ]:
            with self.subTest(category=category):
                client = self.client(provider="compatible", tool_retries=0)
                with patch.object(client.session, "post", return_value=response([fragment,last], sse=True)), self.assertLogs("veyq.engine") as logs:
                    with self.assertRaisesRegex(RuntimeError, "No action"):
                        client.chat([])
                self.assertIn(category, logs.output[0])

    def test_ndjson_error_then_valid_response_and_bounded_persistent_error(self):
        detail = 'llama-server returned invalid tool call arguments for "write_file": unexpected end of JSON input'
        def failed():
            return response([{"message":{"content":"discard this"}}, {"error":detail}])
        client = self.client(tool_retries=1)
        with patch.object(client.session, "post", side_effect=[failed(), native(text="new")]) as post, self.assertLogs("veyq.engine"):
            self.assertEqual(client.chat([])["content"], "new")
        self.assertEqual(post.call_count, 2)
        self.assertIn(("discard_text", {}), self.events)
        with patch.object(client.session, "post", side_effect=[failed(), failed()]) as post, self.assertLogs("veyq.engine"):
            with self.assertRaisesRegex(RuntimeError, "No action"):
                client.chat([])
        self.assertEqual(post.call_count, 2)

    def test_transport_timeout_drops_draft_and_stop_cancels_retry(self):
        def interrupted():
            yield b'{"message":{"content":"partial"}}'
            raise requests.Timeout()
        failed = response()
        failed.iter_lines.return_value = interrupted()
        client = self.client()
        with patch.object(client.session, "post", side_effect=[failed, native(text="new")]) as post, \
                patch.object(self.cancel, "wait", return_value=False), self.assertLogs("veyq.engine"):
            self.assertEqual(client.chat([])["content"], "new")
        self.assertEqual(post.call_count, 2)
        failed.close.assert_called_once()
        self.assertIn(("discard_text", {}), self.events)
        stopped = response()
        def cancelled_stream():
            yield b'{"message":{"content":"partial"}}'
            self.cancel.set()
            raise requests.ConnectionError()
        stopped.iter_lines.return_value = cancelled_stream()
        with patch.object(client.session, "post", return_value=stopped) as post, self.assertRaises(Cancelled):
            client.chat([])
        post.assert_called_once()

    def test_settings_persist_and_limit_retries(self):
        api = DesktopAPI(self.store)
        api.save_settings({"model_streaming": False, "tool_retries": 0})
        settings = Store(self.store.root).data["settings"]
        self.assertIs(settings["model_streaming"], False)
        self.assertEqual(settings["tool_retries"], 0)
        with self.assertRaises(ValueError):
            api.save_settings({"tool_retries": 4})

    def test_non_streaming_mode_validates_calls(self):
        for provider, payload in [
            ("local", {"done":True,"done_reason":"stop","message":{"tool_calls":[call("list_dir", {"path":"."})]}}),
            ("compatible", {"choices":[{"finish_reason":"tool_calls","message":{"tool_calls":[call("list_dir", '{"path":"."}')]}}]}),
        ]:
            client = self.client(provider=provider, model_streaming=False)
            result = response(content=payload)
            with patch.object(client.session, "post", return_value=result) as post:
                self.assertEqual(client.chat([])["tool_calls"][0]["function"]["name"], "list_dir")
                self.assertFalse(post.call_args.kwargs["json"]["stream"])
                result.iter_lines.assert_not_called()

    def test_legacy_bad_calls_remain_in_transcript_but_not_provider_context(self):
        session = {"history":[{"role":"user","content":"Create a file."},
            {"role":"assistant","content":"","tool_calls":[{"id":"broken","function":{"name":"write_file","arguments":"{"}}]},
            {"role":"tool","name":"write_file","tool_call_id":"broken","content":"Not executed"},
            {"role":"user","content":"Continue."}]}
        context = compact(session, 2000)
        self.assertEqual(len(session["history"]), 4)
        self.assertIn("Not executed", context[1]["content"])
        self.assertNotIn("tool_calls", context[1])

    def test_agent_rejects_whole_batch_then_continues_same_chat_without_duplicate_tools(self):
        api = DesktopAPI(self.store)
        sid = api.create_session()
        api.set_workspace(sid, str(self.project))
        self.store.data["settings"]["tool_retries"] = 0
        good = call("write_file", {"path":"proof.py","content":"value = 1\n"})
        bad = call("read_file", '{"path":', 1)
        with patch.object(ModelClient, "local_metadata", return_value={"capabilities":["tools"]}):
            with patch("requests.Session.post", return_value=native(good, bad)), self.assertLogs("veyq.engine"):
                api.start_run(sid, "Create proof.py.")
                api._agent.worker.join(10)
            self.assertEqual(api._agent.state, "failed")
            self.assertFalse((self.project / "proof.py").exists())
            self.assertEqual([m["role"] for m in api.get_session(sid)["history"]], ["user"])
            self.store.data["settings"]["tool_retries"] = 1
            with patch("requests.Session.post", side_effect=[
                native(good, bad), native(good), native(call("read_file", {"path":"proof.py"})), native(text="Verified.")]), \
                    self.assertLogs("veyq.engine"):
                api.start_run(sid, "Create proof.py and verify it.")
                api._agent.worker.join(10)
        self.assertEqual(api._agent.state, "completed")
        self.assertEqual((self.project / "proof.py").read_bytes(), b"value = 1\n")
        history = api.get_session(sid)["history"]
        call_ids = [c["id"] for message in history for c in message.get("tool_calls", [])]
        self.assertEqual(call_ids, [m["tool_call_id"] for m in history if m["role"] == "tool"])
        self.assertEqual(len(call_ids), 2)

    def test_agent_treats_interrupted_final_part_as_unpublished(self):
        api = DesktopAPI(self.store)
        sid = api.create_session()
        api.set_workspace(sid, str(self.project))
        self.store.data["settings"]["tool_retries"] = 0
        original = self.project / "proof.py"
        original.write_bytes(b"old")
        first = native(call("write_file", {"path":"proof.py","content":"part one",
                                          "overwrite":True,"part":1,"final":False}))
        partial = response([b'{"message":{"tool_calls":[{"function":{"name":"write_file",'])
        with patch.object(ModelClient, "local_metadata", return_value={"capabilities":["tools"]}), \
                patch("requests.Session.post", side_effect=[first, partial]), self.assertLogs("veyq.engine"):
            api.start_run(sid, "Replace proof.py.")
            api._agent.worker.join(10)
        self.assertEqual(api._agent.state, "failed")
        self.assertEqual(original.read_bytes(), b"old")
        self.assertFalse(list(self.project.glob(".veynuq-*.tmp")))
        self.assertIn("staged_discarded", (self.store.root / "audit.jsonl").read_text())
