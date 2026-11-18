import copy
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from veyq.desktop import DesktopAPI
from veyq.engine import ModelClient, Cancelled
from veyq.parallel import worker_slot
from veyq.storage import Store
from veyq.tools import ToolRunner


def tool(name, **args):
    return {"role":"assistant","content":"","tool_calls":[
        {"id":str(time.monotonic_ns()),"function":{"name":name,"arguments":args}}]}


def wait_for(predicate, timeout=5):
    deadline=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>deadline:raise AssertionError("Controlled activity did not reach expected state.")
        time.sleep(.01)


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.project=self.root/"project";self.project.mkdir()
        (self.project/"input.txt").write_text("fixture")
        self.store=Store(self.root/"profile")
        self.store.data["settings"].update(workspace=str(self.project),permission="full",network=True,
                                          auto_update=False,max_subagents=2,max_parallel_runs=2)
        self.api=DesktopAPI(self.store)
        self.a=self.api.create_session();self.b=self.api.create_session()
        self.addCleanup(self.finish)

    def finish(self):
        self.api.stop_run()
        for agent in self.api._runs.snapshot():
            worker=getattr(agent,"worker",None)
            if worker:worker.join(5)

    def test_real_parallel_execution_and_targeted_stop(self):
        entered=[];lock=threading.Lock();release=threading.Event()
        def chat(client,messages):
            with lock:entered.append(client)
            client.emit("text",{"text":client.settings["workspace"]})
            while not release.wait(.01):
                if client.cancel.is_set():raise Cancelled()
            if client.cancel.is_set():raise Cancelled()
            return {"role":"assistant","content":"Finished independent request."}
        with patch.object(ModelClient,"chat",chat):
            self.api.start_run(self.a,"Explain the first concept.")
            wait_for(lambda:len(entered)==1)
            self.api.start_run(self.b,"Explain the second concept.")
            wait_for(lambda:len(entered)==2)
            a=self.api._runs.find(self.a);b=self.api._runs.find(self.b)
            self.assertTrue(a.busy and b.busy)
            third=self.api.create_session()
            with self.assertRaisesRegex(RuntimeError,"limit reached"):
                self.api.start_run(third,"Explain a third concept.")
            pa=self.api.get_events(0,self.a)
            pb=self.api.get_events(pa["sequence"],self.b)
            self.assertEqual(pa["session_id"],self.a)
            self.assertEqual(pb["session_id"],self.b)
            self.assertEqual(len([r for r in pa["runs"] if r["busy"]]),2)
            self.assertEqual(len({e["seq"] for e in pa["events"]}),len(pa["events"]))
            self.api.stop_run(self.a);a.worker.join(5)
            self.assertEqual(a.state,"cancelled")
            self.assertTrue(b.busy)
            with self.assertRaises(RuntimeError):self.api.save_settings({"max_subagents":1})
            release.set();b.worker.join(5)
            self.assertEqual(b.state,"completed")
        ha=self.api.get_session(self.a)["history"];hb=self.api.get_session(self.b)["history"]
        self.assertFalse(any(m.get("content")=="Finished independent request." for m in ha))
        self.assertTrue(any(m.get("content")=="Finished independent request." for m in hb))
        self.assertEqual(self.api.get_events(pb["sequence"],self.b)["state"],"completed")

    def test_approvals_have_correct_owner_and_do_not_block_other_chat(self):
        self.store.data["settings"]["permission"]="always"
        def chat(client,messages):
            results=[m for m in messages if m["role"]=="tool"]
            if results:return {"role":"assistant","content":"Observed outcome."}
            request=next(m["content"] for m in messages if m["role"]=="user" and m["content"].startswith("Create"))
            return tool("write_file",path="a.txt" if "a.txt" in request else "b.txt",content="fixture")
        with patch.object(ModelClient,"chat",chat):
            self.api.start_run(self.a,"Create a.txt")
            self.api.start_run(self.b,"Create b.txt")
            a=self.api._runs.find(self.a);b=self.api._runs.find(self.b)
            wait_for(lambda:a.pending is not None and b.pending is not None)
            pa=self.api.get_events(0,self.a)["approval"];pb=self.api.get_events(0,self.b)["approval"]
            self.assertNotEqual(pa["id"],pb["id"])
            self.assertEqual(pa["arguments"]["path"],"a.txt")
            self.assertEqual(pb["arguments"]["path"],"b.txt")
            self.api.resolve_approval(pb["id"],True);b.worker.join(5)
            self.assertEqual(b.state,"completed")
            self.assertTrue(a.busy)
            self.assertFalse((self.project/"a.txt").exists())
            self.api.resolve_approval(pa["id"],False);a.worker.join(5)
            self.assertEqual(a.state,"blocked")
        self.assertEqual((self.project/"b.txt").read_text(),"fixture")

    def test_questions_are_routed_by_identity(self):
        def chat(client,messages):
            if any(m["role"]=="tool" for m in messages):return {"role":"assistant","content":"Answered."}
            return tool("ask_user",question="Required fixture detail?",options=["One","Two"])
        with patch.object(ModelClient,"chat",chat):
            self.api.start_run(self.a,"Analyze input.txt")
            self.api.start_run(self.b,"Analyze input.txt")
            a=self.api._runs.find(self.a);b=self.api._runs.find(self.b)
            wait_for(lambda:a.pending_question is not None and b.pending_question is not None)
            qa=a.pending_question["id"];qb=b.pending_question["id"]
            self.api.answer_question(qa,"One");a.worker.join(5)
            self.assertEqual(a.state,"completed")
            self.assertTrue(b.busy)
            self.assertEqual(b.pending_question["id"],qb)
            self.api.answer_question(qb,"Two");b.worker.join(5)
            self.assertEqual(b.state,"completed")

    def test_limits_persist_and_zero_disables_delegation(self):
        self.api.save_settings({"permission":"auto","max_subagents":0,"max_parallel_runs":4})
        saved=Store(self.store.root).data["settings"]
        self.assertEqual((saved["max_subagents"],saved["max_parallel_runs"]),(0,4))
        for field,value in [("max_subagents",-1),("max_subagents",9),("max_parallel_runs",0),
                            ("max_parallel_runs",9),("max_subagents",True)]:
            with self.assertRaises(ValueError):self.api.save_settings({field:value})
        runner=ToolRunner(self.store,saved,"fixture",threading.Event(),lambda *a:None,Mock(),self.root,{})
        self.addCleanup(runner.close)
        result=runner.execute("delegate_tasks",{"tasks":["Read input.txt"]})
        self.assertFalse(result["ok"]);runner.approve.assert_not_called()

    def test_global_subagent_budget_is_shared_between_requests(self):
        self.store.data["settings"]["max_subagents"]=2
        runner=SimpleNamespace(store=self.store,settings=self.store.data["settings"],cancel=threading.Event())
        lock=threading.Lock();release=threading.Event();active=peak=started=0
        def job(n):
            nonlocal active,peak,started
            with worker_slot(runner):
                with lock:active+=1;started+=1;peak=max(peak,active)
                release.wait(3)
                with lock:active-=1
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures=[executor.submit(job,n) for n in range(6)]
            wait_for(lambda:started==2)
            self.assertEqual(peak,2)
            release.set()
            for future in futures:future.result(5)
        self.assertEqual(started,6)
        self.assertLessEqual(peak,2)

    def test_cancelled_subagent_waiter_does_not_leak_slot(self):
        self.store.data["settings"]["max_subagents"]=1
        first=SimpleNamespace(store=self.store,settings=self.store.data["settings"],cancel=threading.Event())
        second=SimpleNamespace(store=self.store,settings=self.store.data["settings"],cancel=threading.Event())
        result=[]
        def waiting():
            try:
                with worker_slot(second):result.append("started")
            except RuntimeError:result.append("cancelled")
        with worker_slot(first):
            thread=threading.Thread(target=waiting);thread.start()
            second.cancel.set();thread.join(2)
            self.assertEqual(result,["cancelled"])
        with worker_slot(first):pass

    def test_host_effects_are_serialized_without_serializing_reads(self):
        runners=[ToolRunner(self.store,self.store.data["settings"],str(n),threading.Event(),
                            lambda *a:None,lambda *a:True,self.root,{}) for n in range(2)]
        for runner in runners:self.addCleanup(runner.close)
        active=peak=0;lock=threading.Lock()
        def command(runner,command,**kwargs):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            self.assertTrue(runners[1].execute("read_file",{"path":"input.txt"})["ok"])
            time.sleep(.05)
            with lock:active-=1
            return {"exit_code":0,"output":"fixture"}
        with patch.object(ToolRunner,"tool_exec_cmd",command),ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(lambda r:r.execute("exec_cmd",{"command":"fixture"}),runners))
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(peak,1)

    def test_approval_is_rechecked_after_waiting_for_shared_host_execution(self):
        settings={**self.store.data["settings"],"permission":"always"}
        approved=threading.Event()
        runner=ToolRunner(self.store,settings,"fixture",threading.Event(),lambda *a:None,
                          lambda preview:(approved.set() or True),self.root,{})
        self.addCleanup(runner.close)
        # Install the same shared resource lock used by real concurrent runs.
        self.store._host_action_lock=threading.RLock()
        result=[]
        with self.store._host_action_lock:
            thread=threading.Thread(target=lambda:result.append(runner.execute(
                "write_file",{"path":"input.txt","content":"replacement","overwrite":True})))
            thread.start();self.assertTrue(approved.wait(3))
            (self.project/"input.txt").write_text("newer independent change")
        thread.join(3)
        self.assertFalse(result[0]["ok"])
        self.assertEqual((self.project/"input.txt").read_text(),"newer independent change")

    def test_scheduler_completion_is_recorded_before_reusing_primary_activity(self):
        job=self.api.save_automation(self.a,"Read input.txt",1)
        self.api._scheduler.active=job["id"]
        self.api._agent.state="completed"
        with patch.object(ModelClient,"chat",return_value={"role":"assistant","content":"Independent answer."}):
            self.api.start_run(self.b,"Explain a simple concept.")
            self.api._runs.find(self.b).worker.join(5)
        self.assertEqual(job["last_state"],"completed")
        self.assertIsNone(self.api._scheduler.active)

    def test_model_can_choose_delegation_with_configured_limit(self):
        self.store.data["settings"]["max_subagents"]=1
        seen=[]
        def chat(client,messages):
            if messages[0]["content"].startswith("You are a read-only project investigator"):
                if any(m["role"]=="tool" for m in messages):
                    return {"role":"assistant","content":"input.txt contains fixture"}
                return tool("read_file",path="input.txt")
            seen.append({t["function"]["name"] for t in client.tools})
            if any(m["role"]=="tool" for m in messages):
                return {"role":"assistant","content":"Combined verified observations from both investigations."}
            return tool("delegate_tasks",tasks=["Read input.txt","Independently inspect input.txt"])
        with patch.object(ModelClient,"chat",chat):
            self.api.start_run(self.a,"Analyze the project files.")
            self.api._agent.worker.join(10)
        self.assertEqual(self.api._agent.state,"completed")
        self.assertIn("delegate_tasks",seen[0])
        result=next(json.loads(m["content"]) for m in self.api.get_session(self.a)["history"] if m["role"]=="tool")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["result"]),2)
        self.assertTrue(all(r["reads"]==1 and r["state"]=="completed" for r in result["result"]))
        self.assertEqual((self.project/"input.txt").read_text(),"fixture")
