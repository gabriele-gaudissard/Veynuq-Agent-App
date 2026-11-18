import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from veyq.engine import ModelClient, ModelFailure, action_intent
from veyq.storage import DEFAULTS, Store
from veyq.tools import TOOLS, ToolRunner
from veyq.tool_routing import select_tools, continued_scope
from veyq.local_backend import worker_from_snapshot, verify_worker, discover_worker
from veyq.progress import ProgressGuard
from veyq.desktop import DesktopAPI

def reply(name=None, args=None, text="Done."):
    message = {"content": text}
    if name:
        message["tool_calls"] = [{"id": "fixture", "type": "function",
                                  "function": {"name": name, "arguments": json.dumps(args)}}]
    result = Mock(status_code=200)
    result.content = json.dumps({"choices": [{"message": message,
                                  "finish_reason": "tool_calls" if name else "stop"}]}).encode()
    return result

class RoutingTests(unittest.TestCase):
    def test_text_only_requests_do_not_force_tools(self):
        for text in ["Hello", "Write a poem", "Write a Python function", "Scrivimi una funzione",
                     "Show the code here in chat", "How can I create a file?",
                     "Write a Python function that reads files", "Explain how tools work",
                     "Come posso modificare le impostazioni?"]:
            with self.subTest(text=text):
                tools, mode = select_tools(text, TOOLS, action_intent(text))
                self.assertEqual((tools, mode), ([], "none"))
                self.assertFalse(action_intent(text))

    def test_displaying_existing_file_keeps_read_tools(self):
        selected, mode=select_tools("Show the content of proof.py here",TOOLS)
        self.assertEqual(mode,"auto")
        self.assertIn("read_file",[t["function"]["name"] for t in selected])

    def test_file_action_is_required_and_reduced(self):
        for prompt in ["Create proof.py", "Replace proof.py.", "Modifica il file proof.py",
                       "Write code and save it in proof.py"]:
            with self.subTest(prompt=prompt):
                selected, mode = select_tools(prompt, TOOLS, action_intent(prompt))
                names = {t["function"]["name"] for t in selected}
                self.assertEqual(mode, "required")
                self.assertIn("write_file", names)
                self.assertIn("read_file", names)
                self.assertNotIn("computer_action", names)
                self.assertLess(len(selected), len(TOOLS))

    def test_short_resume_keeps_previous_tool_scope_without_forcing_action(self):
        history=[{"role":"user","content":"Create proof.py"},{"role":"user","content":"continua"}]
        scope=continued_scope("continua",history)
        selected,mode=select_tools(scope,TOOLS,operational=action_intent(scope))
        self.assertEqual(mode,"auto")
        self.assertIn("write_file",[t["function"]["name"] for t in selected])
        self.assertEqual(continued_scope("Explain what changed",history),"Explain what changed")

    def test_domains_and_prior_tools_are_preserved(self):
        for prompt, name in [("Set the keyboard layout", "keyboard_layout"),
                              ("Download that GitHub repository and configure it", "clone_repository"),
                              ("Create an image of a dog", "generate_image"),
                              ("Inspect project files", "read_file")]:
            selected, mode = select_tools(prompt, TOOLS, action_intent(prompt), ["web_search"])
            self.assertIn(name, [t["function"]["name"] for t in selected])
            self.assertIn("web_search", [t["function"]["name"] for t in selected])
        selected, mode = select_tools("Inspect project files", TOOLS)
        self.assertEqual(mode, "auto")
        self.assertNotIn("write_file", [t["function"]["name"] for t in selected])

    def test_tools_for_later_mutations_remain_available_after_first_action(self):
        selected, mode = select_tools("Read input.txt, modify it and create proof.txt using tools.",
                                     TOOLS, False, ["read_file", "edit_file"], operational=True)
        self.assertEqual(mode, "auto")
        self.assertIn("write_file", [t["function"]["name"] for t in selected])
        self.assertNotIn("plugin_marketplace", [t["function"]["name"] for t in selected])

    def test_selection_never_reenables_excluded_tools(self):
        available = [t for t in TOOLS if t["function"]["name"] == "read_file"]
        selected, mode = select_tools("Create proof.py", available, True, ["write_file"])
        self.assertEqual(selected, available)

    def test_unknown_action_keeps_available_capabilities(self):
        self.assertEqual(select_tools("Do it for me", TOOLS, True), (TOOLS, "required"))

class TransportTests(unittest.TestCase):
    def client(self, **changes):
        c = ModelClient({**DEFAULTS, "provider":"compatible", "model_streaming":False, **changes},
                        "", threading.Event(), lambda *args: None)
        c.tools = [t for t in TOOLS if t["function"]["name"] == "read_file"]
        self.addCleanup(c.close)
        return c

    def test_each_choice_is_explicit_and_none_has_no_definitions(self):
        for mode in ("none", "auto", "required"):
            c = self.client()
            c.tool_choice = mode
            with patch.object(c.session, "post", return_value=reply()) as post:
                c.chat([{"role":"user","content":"Fixture"}])
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["tool_choice"], mode)
            self.assertEqual("tools" in payload, mode != "none")

    def test_local_worker_uses_compatible_protocol_and_pairing(self):
        c = self.client(provider="local")
        c.metadata = {"capabilities":["tools"]}
        c.tool_choice = "required"
        bridge = {"root":"http://127.0.0.1:59999","capacity":16384,"slots":1}
        messages = [{"role":"assistant","content":"","tool_calls":[
            {"id":"previous","function":{"name":"read_file","arguments":{"path":"proof.py"}}}]},
            {"role":"tool","name":"read_file","tool_call_id":"previous","tool_name":"read_file","content":"actual"}]
        with patch.object(c,"local_bridge",return_value=bridge), patch.object(c.session,"post",return_value=reply("read_file",{"path":"proof.py"})) as post:
            result=c.chat(messages)
        self.assertEqual(post.call_args.args[0],"http://127.0.0.1:59999/v1/chat/completions")
        payload=post.call_args.kwargs["json"]
        self.assertEqual(payload["tool_choice"],"required")
        self.assertEqual(json.loads(payload["messages"][0]["tool_calls"][0]["function"]["arguments"]),{"path":"proof.py"})
        self.assertNotIn("tool_name",payload["messages"][1])
        self.assertEqual(json.loads(result["tool_calls"][0]["function"]["arguments"]),{"path":"proof.py"})
        self.assertTrue(c.choice_enforced)

    def test_known_unadvertised_tool_is_regenerated_not_dispatched(self):
        c=self.client()
        c.all_tools=[t for t in TOOLS if t["function"]["name"] in {"read_file","write_file"}]
        responses=[reply("write_file",{"path":"proof.py","content":"safe"}),
                   reply("write_file",{"path":"proof.py","content":"safe"})]
        with patch.object(c.session,"post",side_effect=responses) as post, self.assertLogs("veyq.engine"):
            result=c.chat([{"role":"user","content":"Create proof.py"}])
        self.assertEqual(post.call_count,2)
        self.assertEqual(result["tool_calls"][0]["function"]["name"],"write_file")
        self.assertIn("write_file",[t["function"]["name"] for t in post.call_args.kwargs["json"]["tools"]])

    def test_read_result_has_literal_text_without_changing_saved_history(self):
        c=self.client()
        original={"role":"tool","name":"read_file","tool_call_id":"previous",
                  "content":json.dumps({"ok":True,"result":{"path":"a.py","total_lines":12,
                                                         "content":"10: 1: actual file text\n11: two"}})}
        messages=[copy.deepcopy(original)]
        with patch.object(c.session,"post",return_value=reply()) as post:
            c.chat(messages)
        supplied=json.loads(post.call_args.kwargs["json"]["messages"][0]["content"])["result"]
        self.assertEqual(supplied["content"],"1: actual file text\ntwo")
        self.assertEqual(supplied["start_line"],10)
        self.assertTrue(supplied["literal_excerpt"])
        self.assertEqual(messages[0],original)

    def test_native_loader_keeps_actual_context_and_starts_lease(self):
        c=self.client(provider="local")
        metadata={"template_source":"gguf_chat_template"}
        bridge={"root":"http://127.0.0.1:59999","capacity":16384,"slots":1}
        with patch("veyq.engine.os.name","nt"), patch("veyq.engine.discover_worker",return_value={"root":bridge["root"]}), \
             patch("veyq.engine.verify_worker",return_value=bridge), patch("veyq.engine.threading.Thread") as thread, \
             patch.object(c.session,"post",return_value=Mock(status_code=200)) as post:
            self.assertEqual(c.local_bridge(metadata,16384),bridge)
            payload=post.call_args.kwargs["json"]
            self.assertEqual(payload["messages"],[])
            self.assertEqual(payload["options"],{"num_ctx":16384})
            self.assertEqual(payload["keep_alive"],"10m")
            thread.return_value.start.assert_called_once()
            c.close()
            self.assertTrue(c.bridge_stop.is_set())

    def test_unsupported_choice_fails_before_dispatch(self):
        c=self.client();c.tool_choice="required"
        failure=Mock(status_code=400,text='unsupported tool_choice required')
        with patch.object(c.session,"post",return_value=failure), self.assertRaises(ModelFailure) as error:
            c.chat([{"role":"user","content":"Fixture"}])
        self.assertEqual(error.exception.category,"tool_choice_unsupported")

    def test_native_api_does_not_claim_required_is_supported(self):
        c=self.client(provider="local",model_streaming=True)
        c.metadata={"capabilities":["tools"]}
        c.tool_choice="required"
        response=Mock(status_code=200)
        response.iter_lines.return_value=iter([b'{"message":{"content":"Done."},"done":true,"done_reason":"stop"}'])
        with patch.object(c.session,"post",return_value=response) as post:
            c.chat([{"role":"user","content":"Fixture"}])
        self.assertNotIn("tool_choice",post.call_args.kwargs["json"])
        self.assertFalse(c.choice_enforced)

class WorkerIdentityTests(unittest.TestCase):
    def test_worker_must_match_api_parent_executable_model_and_listener(self):
        with tempfile.TemporaryDirectory() as root:
            parent=str(Path(root)/"ollama.exe")
            worker=str(Path(root)/"lib"/"ollama"/"llama-server.exe")
            model=str(Path(root)/"model.gguf")
            snapshot={"processes":[{"ProcessId":1,"Name":"ollama.exe","ExecutablePath":parent},
                {"ProcessId":2,"ParentProcessId":1,"Name":"llama-server.exe","ExecutablePath":worker,"CommandLine":"fixture"}],
                "listeners":[{"LocalPort":11434,"OwningProcess":1},{"LocalPort":59999,"OwningProcess":2}]}
            argv=[worker,"--model",model,"--port","59999","--host","127.0.0.1"]
            with patch("veyq.local_backend.split_command",return_value=argv):
                self.assertEqual(worker_from_snapshot(snapshot,model,11434),"http://127.0.0.1:59999")
                for field,value in [("ParentProcessId",3),("ExecutablePath",str(Path(root)/"foreign.exe"))]:
                    changed=copy.deepcopy(snapshot);changed["processes"][1][field]=value
                    self.assertIsNone(worker_from_snapshot(changed,model,11434))
                self.assertIsNone(worker_from_snapshot(snapshot,model+"other",11434))
                changed=copy.deepcopy(snapshot);changed["listeners"][1]["OwningProcess"]=9
                self.assertIsNone(worker_from_snapshot(changed,model,11434))

    def test_authenticated_remote_and_nonroot_endpoints_are_not_inspected(self):
        for base,token in [("https://example.com",""),("http://127.0.0.1:11434","secret"),
                           ("http://127.0.0.1:11434/proxy","")]:
            with patch("veyq.local_backend.subprocess.run") as run:
                self.assertIsNone(discover_worker(base,{"template_source":"gguf_chat_template"},token))
                run.assert_not_called()

    def test_props_must_match_model_and_actual_capacity(self):
        result=Mock()
        result.json.return_value={"chat_template":"native","model_path":str(Path("model.gguf").resolve()),
                                  "default_generation_settings":{"n_ctx":16384},"total_slots":1}
        session=Mock();session.get.return_value=result
        candidate={"root":"http://127.0.0.1:59999","model_path":str(Path("model.gguf").resolve())}
        self.assertEqual(verify_worker(session,candidate)["capacity"],16384)
        result.json.return_value["model_path"]=str(Path("other.gguf").resolve())
        self.assertIsNone(verify_worker(session,candidate))

class ProgressTests(unittest.TestCase):
    def test_repeated_unchanged_reads_get_one_recovery_then_stop(self):
        g=ProgressGuard()
        for n in range(9):
            g.observe("read_file",{"path":"a.py"},{"ok":True,"result":"same"})
        self.assertTrue(g.stopped)
        self.assertEqual(g.warnings,2)

    def test_distinct_ranges_and_changed_results_remain_progress(self):
        g=ProgressGuard()
        for n in range(2000):
            g.observe("read_file",{"path":"a.py","start_line":n},{"ok":True,"result":str(n)})
        self.assertFalse(g.stopped)

    def test_repeated_file_states_are_not_new_progress(self):
        g=ProgressGuard()
        for n in range(9):
            g.observe("write_file",{"path":"a.py","content":"same"},
                      {"ok":True,"result":{"path":"a.py","published":True,"sha256":"fixture","backup":str(n)}})
        self.assertTrue(g.stopped)

    def test_mutation_resets_observations(self):
        g=ProgressGuard()
        for n in range(5):g.observe("read_file",{"path":"a.py"},{"ok":True,"result":"same"})
        g.observe("write_file",{"path":"a.py"},{"ok":True,"result":{"published":True}})
        g.observe("read_file",{"path":"a.py"},{"ok":True,"result":"same"})
        self.assertEqual(g.redundant,0)
        self.assertFalse(g.stopped)

class AgentProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.project=self.root/"project";self.project.mkdir()
        (self.project/"input.txt").write_text("unchanged")
        self.store=Store(self.root/"profile")
        self.store.data["settings"].update(workspace=str(self.project),permission="full",auto_update=False,max_steps=0)
        self.api=DesktopAPI(self.store);self.sid=self.api.create_session()

    def run_agent(self, side_effect, prompt="Modify input.txt"):
        with patch.object(ModelClient,"chat",side_effect=side_effect) as chat:
            self.api.start_run(self.sid,prompt)
            self.api._agent.worker.join(20)
            if self.api._agent.busy:
                self.api._agent.stop();self.api._agent.worker.join(5)
            self.assertFalse(self.api._agent.busy)
        return chat

    def test_real_read_loop_stops_and_preserves_history(self):
        read={"role":"assistant","content":"","tool_calls":[
            {"id":"read","function":{"name":"read_file","arguments":{"path":"input.txt"}}}]}
        chat=self.run_agent(lambda messages:copy.deepcopy(read))
        session=self.api.get_session(self.sid)
        self.assertEqual(chat.call_count,9)
        self.assertEqual(session["task"]["diagnostics"]["category"],"no_progress")
        self.assertEqual((self.project/"input.txt").read_text(),"unchanged")
        self.assertEqual(len([m for m in session["history"] if m["role"]=="tool"]),9)

    def test_recovery_can_move_from_repeated_reads_to_verified_edit(self):
        read={"role":"assistant","content":"","tool_calls":[
            {"id":"read","function":{"name":"read_file","arguments":{"path":"input.txt"}}}]}
        write={"role":"assistant","content":"","tool_calls":[
            {"id":"write","function":{"name":"write_file","arguments":{"path":"input.txt","content":"fixed","overwrite":True}}}]}
        self.run_agent([copy.deepcopy(read) for _ in range(5)]+[write,{"role":"assistant","content":"Done."}])
        self.assertEqual(self.api._agent.state,"completed")
        self.assertEqual((self.project/"input.txt").read_text(),"fixed")

    def test_unlimited_exceeds_old_1000_step_cap_and_is_stoppable(self):
        count=0
        # Steering creates a new turn without filesystem tool side effects.
        def steering(messages):
            nonlocal count
            count+=1
            if count==1002:self.api._agent.cancel.set()
            from veyq.engine import Steered
            raise Steered()
        with patch.object(self.store,"save"), patch.object(self.api._agent,"emit"):
            self.run_agent(steering)
        self.assertEqual(count,1002)
        self.assertEqual(self.api._agent.state,"cancelled")

    def test_steps_setting_zero_and_large_values_persist(self):
        for value in [0,5000]:
            result=self.api.save_settings({"max_steps":value,"permission":"auto"})
            self.assertTrue(result.get("ok",True))
            self.assertEqual(Store(self.store.root).data["settings"]["max_steps"],value)
        with self.assertRaises(ValueError):self.api.save_settings({"max_steps":-1})
