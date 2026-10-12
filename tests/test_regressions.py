import copy
import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from veyq.storage import Store
from veyq.desktop import DesktopAPI
from veyq.engine import ModelClient, Steered, action_intent
from veyq.computer import Computer
from veyq.tools import ToolRunner


class Regressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'data')
        self.workspace = self.root/'project'; self.workspace.mkdir()
        self.store.data['settings'].update(workspace=str(self.workspace), permission='full', network=True)
        self.api = DesktopAPI(self.store); self.sid = self.api.create_session()
        self.agent = self.api._agent
    def tearDown(self):
        self.agent.stop(); self.temp.cleanup()
    def wait(self):
        deadline=time.monotonic()+5
        while self.agent.busy and time.monotonic()<deadline: time.sleep(.01)
        self.assertFalse(self.agent.busy)
    def test_delete_removes_chat_and_persisted_state(self):
        other=self.api.create_session(); self.agent.current_session=self.sid
        self.agent.emit('text', {'text':'old'})
        self.api.delete_session(self.sid)
        self.assertIsNone(self.api.get_session(self.sid))
        self.assertEqual([s['id'] for s in self.api.get_sessions()], [other])
        self.assertEqual([s['id'] for s in Store(self.store.root).data['sessions']], [other])
        self.assertEqual(self.agent.current_session, '')
        self.assertFalse(self.agent.events)
    def test_legacy_auto_title_expands_without_rewriting_manual_title(self):
        s=self.store.data['sessions'][0]; text='Impostami come layout di tastiera quello inglese americano per favore'
        s.update(title=text[:25], history=[{'role':'user','content':text}]);self.store.save()
        self.assertEqual(self.api.get_sessions()[0]['title'], text)
        self.api.rename_session(self.sid,text[:25]);self.assertEqual(self.api.get_sessions()[0]['title'],text[:25].strip())
    def test_execution_intent_distinguishes_tutorial_and_negative_instruction(self):
        for text in ['Impostami la tastiera US','Modificami questo file','Can you create the file?','Non darmi istruzioni: fallo tu']:
            self.assertTrue(action_intent(text),text)
        for text in ['Come posso impostare la tastiera?','Puoi spiegarmi come cambiare il layout?','Do not change my settings','How do I create a file?']:
            self.assertFalse(action_intent(text),text)
    def test_generic_image_capability_question_does_not_force_generation(self):
        for text in ['puoi creare immagini?', 'Sai generare immagini?', 'Can you create images?', 'Are you able to generate images?']:
            self.assertFalse(action_intent(text), text)
        for text in ['Puoi creare un’immagine di un cane?', 'Can you create an image of a dog?', 'Crea immagini di un cane', 'Puoi creare immagini? Fallo tu: disegna un cane']:
            self.assertTrue(action_intent(text), text)
        with patch.object(ModelClient, 'chat', return_value={'role':'assistant','content':'Yes, a local image engine is available.'}) as chat:
            self.agent.start(self.sid, 'puoi creare immagini?'); self.wait()
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(self.agent.state, 'completed')
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(self.api.get_session(self.sid)['history'][-1]['content'], 'Yes, a local image engine is available.')

    def test_instruction_only_reply_is_retried_until_actual_action(self):
        messages=[{'role':'assistant','content':'You can create the file yourself.'}, {'role':'assistant','content':'','tool_calls':[{'id':'a','function':{'name':'write_file','arguments':{'path':'proof.txt','content':'actual'}}}]}, {'role':'assistant','content':'','tool_calls':[{'id':'verify','function':{'name':'read_file','arguments':{'path':'proof.txt'}}}]}, {'role':'assistant','content':'Done.'}]
        with patch.object(ModelClient,'chat',side_effect=messages) as chat:
            self.agent.start(self.sid,'Create proof.txt for me');self.wait()
        self.assertEqual((self.workspace/'proof.txt').read_text(),'actual')
        self.assertEqual(chat.call_count,4)
        self.assertFalse(any('yourself' in m.get('content','') for m in self.api.get_session(self.sid)['history']))
    def test_incapable_model_is_not_reported_as_successful_execution(self):
        with patch.object(ModelClient,'chat',return_value={'role':'assistant','content':'Follow these instructions.'}) as chat:
            self.agent.start(self.sid,'Create proof.txt');self.wait()
        self.assertEqual(chat.call_count,3)
        self.assertFalse((self.workspace/'proof.txt').exists())
        self.assertIn('could not complete and verify',self.api.get_session(self.sid)['history'][-1]['content'])
    def test_followup_interrupts_model_generation_without_stop(self):
        entered=threading.Event(); captured=[]
        def chat(client,messages):
            captured.append(copy.deepcopy(messages))
            if len(captured)==1:
                entered.set()
                if not client.steer.wait(3): raise AssertionError('Generation was not steered')
                raise Steered('partial')
            return {'role':'assistant','content':'Updated answer'}
        with patch.object(ModelClient,'chat',chat):
            self.agent.start(self.sid,'Explain this project');self.assertTrue(entered.wait(2))
            result=self.agent.follow_up(self.sid,'Use Italian and be concise');self.assertTrue(result['steering']);self.wait()
        self.assertFalse(self.agent.cancel.is_set())
        self.assertEqual(captured[-1][-1]['content'],'Use Italian and be concise')
        self.assertEqual(self.agent.state,'completed')
    def test_keyboard_write_denied_does_not_run_powershell(self):
        settings=self.store.snapshot()['settings'];settings['permission']='auto'
        runner=ToolRunner(self.store,settings,'keyboard',threading.Event(),lambda *a:None,lambda *a:False,self.root,{})
        with patch.object(runner,'run_process') as process:
            self.assertFalse(runner.execute('keyboard_layout',{'action':'set','layout':'us'})['ok']);process.assert_not_called()
    def test_keyboard_set_verifies_output_and_preserves_existing_languages(self):
        runner=ToolRunner(self.store,self.store.snapshot()['settings'],'keyboard',threading.Event(),lambda *a:None,lambda *a:True,self.root,{})
        output={'default_tip':'0409:00000409','languages':[{'language':'it-IT','input_tips':['0410:00000410']},{'language':'en-US','input_tips':['0409:00000409']}]}
        with patch.object(runner,'run_process',return_value={'exit_code':0,'output':'VEYNUQ_KEYBOARD_JSON='+json.dumps(output)}) as process:
            result=runner.execute('keyboard_layout',{'action':'set','layout':'us'})
        self.assertTrue(result['ok']);self.assertTrue(result['result']['verified'])
        import base64
        script=base64.b64decode(process.call_args.args[0][-1]).decode('utf-16-le')
        self.assertNotIn('InputMethodTips.Clear',script);self.assertIn('Set-WinDefaultInputMethodOverride',script)
    def test_failed_typing_does_not_consume_snapshot(self):
        computer=Computer(self.store.root);element=Mock();element.element_info.control_type='Button'
        snapshot={'elements':[(element,())]};window=Mock()
        computer.snapshots['s']=snapshot
        with patch.object(computer,'checked_element',return_value=(snapshot,window,element,False)):
            with self.assertRaises(ValueError): computer.action('s',0,'type','text')
        self.assertIn('s',computer.snapshots);window.set_focus.assert_not_called()
    def test_old_snapshot_refreshes_only_live_same_identity(self):
        computer=Computer(self.store.root);element=Mock();element.element_info.runtime_id=[1];element.element_info.control_type='Button';element.element_info.name='Same'
        window=Mock();window.process_id.return_value=123
        computer.snapshots['s']={'time':time.monotonic()-130,'window_id':1,'pid':123,'elements':[(element,((1,),'Button','Same'))]}
        with patch.object(computer,'target',return_value=window),patch.object(computer,'password',return_value=False):
            snapshot, live_window, live_element, refreshed=computer.checked_element('s',0)
        self.assertTrue(refreshed);self.assertIs(live_element,element);self.assertIn('s',computer.snapshots)
    def test_pointer_rejects_outside_control_and_does_not_consume_snapshot(self):
        computer=Computer(self.store.root);rect=types.SimpleNamespace(left=100,top=100,right=500,bottom=500)
        window=Mock();window.rectangle.return_value=rect;element=Mock();element.rectangle.return_value=types.SimpleNamespace(left=110,top=110,right=130,bottom=130)
        snapshot={'rect':[100,100,500,500]};computer.snapshots['s']=snapshot
        with patch.object(computer,'checked_element',return_value=(snapshot,window,element,False)):
            with self.assertRaises(ValueError):computer.pointer('s',0,'click',x=200,y=200)
        self.assertIn('s',computer.snapshots);window.set_focus.assert_not_called()
