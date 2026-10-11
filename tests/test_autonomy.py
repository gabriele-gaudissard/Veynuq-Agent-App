import base64
import hashlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from veyq.runtime import ProfileLease, profile_root
from veyq.background import task_xml, task_name, run as background_run
from veyq.windows_sandbox import prepare, open_desktop
from veyq.image_generation import generate, worker
from veyq.storage import Store
from veyq.desktop import DesktopAPI
from veyq.engine import ModelClient,TransientModelError,Cancelled
from veyq.storage import activity_title
from veyq.tools import ToolRunner
from veyq.computer import Computer

def call(name, **args):
    return {'role':'assistant','content':'','tool_calls':[{'id':name+str(time.monotonic_ns()),'function':{'name':name,'arguments':args}}]}

def png():
    stream=io.BytesIO(); Image.new('RGB',(256,256),(31,51,89)).save(stream,format='PNG'); return stream.getvalue()

class Autonomy(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.workspace=self.root/'project';self.workspace.mkdir()
        self.store=Store(self.root/'profile')
        self.store.data['settings'].update(workspace=str(self.workspace),permission='full',network=True)
        self.api=DesktopAPI(self.store);self.sid=self.api.create_session();self.agent=self.api._agent
        self.session=self.store.data['sessions'][0];self.cancel=threading.Event()
        self.runner=ToolRunner(self.store,self.store.snapshot()['settings'],'test',self.cancel,lambda *a:None,lambda *a:True,self.root/'app',self.session)
        self.addCleanup(self.runner.close);self.addCleanup(self.agent.stop)

    def agent_run(self,replies):
        with patch.object(ModelClient,'chat',side_effect=replies):
            self.agent.start(self.sid,'Create proof.txt')
            deadline=time.monotonic()+5
            while self.agent.busy and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.agent.busy)

    def test_profile_owner_prevents_second_writer_and_releases(self):
        lease=ProfileLease(self.root/'exclusive').acquire(foreground=True)
        try:
            with self.assertRaises(RuntimeError):ProfileLease(lease.root).acquire()
            with self.assertRaisesRegex(RuntimeError,'already open'):ProfileLease(lease.root).acquire(foreground=True,timeout=1)
            self.assertFalse((lease.root/'background-stop').exists())
        finally:lease.close()
        with ProfileLease(self.root/'exclusive').acquire():pass

    def test_foreground_signals_background_before_waiting(self):
        lease=ProfileLease(self.root/'exclusive').acquire()
        try:
            with self.assertRaises(RuntimeError):ProfileLease(lease.root).acquire(foreground=True)
            self.assertGreater(float((lease.root/'background-stop').read_text()),time.time()-5)
        finally:lease.close()

    def test_profile_anchor_is_absolute_and_override_takes_precedence(self):
        app=self.root/'app';app.mkdir();(app/'profile.json').write_text(json.dumps({'data_dir':str(self.store.root)}))
        with patch.dict(os.environ,{'VEYNUQ_DATA_DIR':'','VEYQ_DATA_DIR':''}):self.assertEqual(profile_root(app),self.store.root.resolve())
        with patch.dict(os.environ,{'VEYNUQ_DATA_DIR':str(self.workspace)}):self.assertEqual(profile_root(app),self.workspace.resolve())
        (app/'profile.json').write_text('{"data_dir":"relative"}')
        with patch.dict(os.environ,{'VEYNUQ_DATA_DIR':'','VEYQ_DATA_DIR':''}),self.assertRaises(ValueError):profile_root(app)

    def test_schedule_xml_has_no_elevation_password_or_wake(self):
        text=task_xml(self.root/'app space',self.store.root,self.root/'app space/pythonw.exe','S-1-5-21-123-456-789-1000')
        node=ET.fromstring(text);n={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        for name,value in [('RunLevel','LeastPrivilege'),('LogonType','InteractiveToken'),('WakeToRun','false'),('MultipleInstancesPolicy','IgnoreNew'),('Interval','PT5M'),('ExecutionTimeLimit','PT6M')]:
            self.assertEqual(node.find('.//t:'+name,n).text,value)
        self.assertIn('--background-profile',node.find('.//t:Arguments',n).text)
        self.assertIn(str(self.store.root),node.find('.//t:Arguments',n).text)
        self.assertNotIn('Password',text);self.assertNotEqual(task_name(self.root),task_name(self.workspace))

    def test_background_does_not_load_store_without_registration(self):
        app=self.root/'app';app.mkdir()
        with patch('veyq.storage.Store') as constructor:background_run(app,self.store.root)
        constructor.assert_not_called()

    def test_background_refuses_modified_installation_before_store_load(self):
        app=self.root/'app';app.mkdir();(app/'app.py').write_text('changed')
        (self.store.root/'background-task.json').write_text(json.dumps({'app_root':str(app.resolve())}))
        (self.store.root/'installation.json').write_text(json.dumps({'root':str(app.resolve()),'files':{'app.py':hashlib.sha256(b'original').hexdigest()}}))
        with patch('veyq.storage.Store') as constructor:background_run(app,self.store.root)
        constructor.assert_not_called()

    def test_sandbox_copy_filters_secrets_and_private_profile(self):
        (self.workspace/'hello.py').write_text('print(1)');(self.workspace/'.env').write_text('secret')
        (self.workspace/'key.pem').write_text('secret');(self.workspace/'.git').mkdir();(self.workspace/'.git/config').write_text('secret')
        data=self.workspace/'profile';data.mkdir();(data/'state.json').write_text('private')
        result=prepare(self.workspace,data,self.cancel)
        node=ET.parse(result['configuration']).getroot()
        self.assertEqual(result['copied_files'],1)
        for tag in ['Networking','ClipboardRedirection','AudioInput','VideoInput','PrinterRedirection']:self.assertEqual(node.find(tag).text,'Disable')
        self.assertEqual(node.find('MappedFolders/MappedFolder/ReadOnly').text,'true');self.assertIsNone(node.find('LogonCommand'))
        source=Path(node.find('MappedFolders/MappedFolder/HostFolder').text)
        self.assertEqual([p.name for p in source.rglob('*') if p.is_file()],['hello.py'])

    def test_unavailable_sandbox_never_falls_back_to_host(self):
        with patch('veyq.windows_sandbox.executable',return_value=None),patch('veyq.windows_sandbox.subprocess.Popen') as process:
            with self.assertRaisesRegex(RuntimeError,'No host task'):open_desktop(self.workspace,self.store.root,self.cancel)
        process.assert_not_called()

    def test_sandbox_copy_cancelled_before_config_is_written(self):
        self.cancel.set()
        with self.assertRaises(RuntimeError):prepare(self.workspace,self.store.root,self.cancel)
        self.assertFalse(list(self.store.root.rglob('*.wsb')))

    def test_canvas_typing_denies_host_without_input(self):
        computer=Computer(self.store.root)
        with patch.object(computer,'checked_element') as checked,self.assertRaises(PermissionError):computer.sandbox_type('s',0,'hello')
        checked.assert_not_called()

    def test_canvas_typing_requires_fresh_visual_observation(self):
        computer=Computer(self.store.root);computer.sandbox_only=True;window=Mock();element=Mock()
        for snapshot,refreshed in [({'visual':False},False),({'visual':True},True)]:
            with patch.object(computer,'checked_element',return_value=(snapshot,window,element,refreshed)),self.assertRaises(ValueError):computer.sandbox_type('s',0,'hello')
        window.set_focus.assert_not_called()

    def test_fake_sandbox_process_is_denied(self):
        computer=Computer(self.store.root);computer.sandbox_only=True;window=Mock();window.process_id.return_value=123
        desktop=Mock();desktop.window.return_value.wrapper_object.return_value=window
        with patch.object(computer,'desktop',return_value=desktop),patch('veyq.computer.process_path',return_value=self.workspace/'WindowsSandbox.exe'),self.assertRaises(PermissionError):computer.target(123)

    def test_wait_requires_observed_window_and_returns_fresh_match(self):
        with self.assertRaises(ValueError):self.runner.tool_computer_wait(100,'Ready')
        self.runner.computer.snapshots['s']={'window_id':100}
        with patch.object(self.runner.computer,'inspect',return_value={'snapshot_id':'fresh','elements':[{'name':'Ready','value':''}]}) as inspect:
            result=self.runner.tool_computer_wait(100,'Ready')
        self.assertTrue(result['matched']);self.assertEqual(result['snapshot_id'],'fresh');inspect.assert_called_once_with(100)

    def test_wait_cancelled_before_inspecting_or_input(self):
        self.runner.computer.snapshots['s']={'window_id':100};self.cancel.set()
        with patch.object(self.runner.computer,'inspect') as inspect,self.assertRaises(RuntimeError):self.runner.tool_computer_wait(100,'Ready')
        inspect.assert_not_called()

    def test_installed_capabilities_override_catalog_for_action(self):
        client=ModelClient(self.store.data['settings'],'',self.cancel,lambda *a:None);self.addCleanup(client.close)
        client.metadata={'capabilities':['completion']};client.require_action=True
        with patch.object(client.session,'post') as post,self.assertRaisesRegex(RuntimeError,'does not support native tools'):client.chat([])
        post.assert_not_called()

    def test_transient_response_retry_preserves_completed_tool_history(self):
        client=ModelClient(self.store.data['settings'],'',self.cancel,lambda *a:None);self.addCleanup(client.close)
        messages=[{'role':'tool','tool_call_id':'already-done','content':'file was written'}]
        with patch.object(client,'_chat',side_effect=[TransientModelError('connection reset'),{'role':'assistant','content':'continue'}]) as chat,patch.object(self.cancel,'wait',return_value=False):
            self.assertEqual(client.chat(messages)['content'],'continue')
        self.assertEqual(chat.call_count,2);self.assertEqual(chat.call_args_list[0].args,chat.call_args_list[1].args)

    def test_authentication_failure_is_not_retried(self):
        client=ModelClient(self.store.data['settings'],'',self.cancel,lambda *a:None);self.addCleanup(client.close)
        with patch.object(client,'_chat',side_effect=RuntimeError('Provider HTTP 401')) as chat,self.assertRaises(RuntimeError):client.chat([])
        self.assertEqual(chat.call_count,1)

    def test_local_health_http500_retries_without_replaying_accepted_tools(self):
        client=ModelClient(self.store.data['settings'],'',self.cancel,lambda *a:None);self.addCleanup(client.close)
        client.metadata={'capabilities':['completion','tools']}
        failed=Mock(status_code=500,text='{"error":"health resp: Get http://127.0.0.1/health: wsarecv: connection closed"}')
        success=Mock(status_code=200);success.iter_lines.return_value=iter([b'{"message":{"content":"continued"},"done":true}'])
        messages=[{'role':'tool','tool_call_id':'already-done','content':'file was written'}]
        with patch.object(client.session,'post',side_effect=[failed,success]) as post,patch.object(self.cancel,'wait',return_value=False):
            self.assertEqual(client.chat(messages)['content'],'continued')
        self.assertEqual(post.call_count,2)
        self.assertEqual(post.call_args_list[0].kwargs['json']['messages'],messages)
        self.assertEqual(post.call_args_list[1].kwargs['json']['messages'],messages)
        failed.close.assert_called_once()

    def test_unrelated_local_http500_is_not_retried(self):
        client=ModelClient(self.store.data['settings'],'',self.cancel,lambda *a:None);self.addCleanup(client.close)
        client.metadata={'capabilities':['completion','tools']}
        failed=Mock(status_code=500,text='{"error":"invalid model configuration"}')
        with patch.object(client.session,'post',return_value=failed) as post,self.assertRaisesRegex(RuntimeError,'invalid model'):
            client.chat([])
        self.assertEqual(post.call_count,1)

    def test_persistent_local_health_failure_has_bounded_retries(self):
        client=ModelClient(self.store.data['settings'],'',self.cancel,lambda *a:None);self.addCleanup(client.close)
        client.metadata={'capabilities':['completion','tools']}
        failed=Mock(status_code=500,text='{"error":"health resp: unavailable"}')
        with patch.object(client.session,'post',return_value=failed) as post,patch.object(self.cancel,'wait',return_value=False),self.assertRaises(TransientModelError):
            client.chat([])
        self.assertEqual(post.call_count,3)

    def test_stop_during_retry_prevents_another_model_request(self):
        client=ModelClient(self.store.data['settings'],'',self.cancel,lambda *a:None);self.addCleanup(client.close)
        with patch.object(client,'_chat',side_effect=TransientModelError('connection reset')) as chat,patch.object(self.cancel,'wait',return_value=True),self.assertRaises(Cancelled):client.chat([])
        self.assertEqual(chat.call_count,1)

    def test_automatic_title_preserves_complete_words_and_manual_title(self):
        text='Inspect the actual controls and type this message safely into the disposable test window before verifying the result'
        self.assertLessEqual(len(activity_title(text)),100);self.assertTrue(activity_title(text).endswith('…'))
        self.assertTrue(text.startswith(activity_title(text)[:-1]+' '))
        self.session.update(title=text[:100],history=[{'role':'user','content':text}],auto_title=True)
        self.assertEqual(self.api.get_sessions()[0]['title'],activity_title(text))
        self.session['auto_title']=False;self.assertEqual(self.api.get_sessions()[0]['title'],text[:100])

    def test_model_context_is_clamped_to_actual_metadata(self):
        client=ModelClient(self.store.data['settings'],'',self.cancel,lambda *a:None);self.addCleanup(client.close)
        client.metadata={'capabilities':['completion','tools'],'model_info':{'family.context_length':8192}}
        response=Mock(status_code=200);response.iter_lines.return_value=iter([b'{"message":{"content":"ok"},"done":true}'])
        with patch.object(client.session,'post',return_value=response) as post:self.assertEqual(client.chat([])['content'],'ok')
        payload=post.call_args.kwargs['json'];self.assertEqual(payload['options']['num_ctx'],8192);self.assertEqual(payload['options']['num_predict'],4096);self.assertIn('tools',payload)

    def test_mutation_without_inspection_is_not_marked_completed(self):
        self.store.data['settings']['disabled_tools'] = ['read_file']
        self.agent_run([call('write_file',path='proof.txt',content='actual')]+[{'role':'assistant','content':'Done'}]*3)
        self.assertEqual(self.agent.state,'unverified');self.assertIn('final verification',self.session['history'][-1]['content']);self.assertEqual((self.workspace/'proof.txt').read_text(),'actual')

    def test_ignored_verification_request_recovers_with_actual_read(self):
        self.agent_run([call('write_file',path='proof.txt',content='actual')]+[{'role':'assistant','content':'Done'}]*3)
        self.assertEqual(self.agent.state,'completed')
        reads=[json.loads(m['content']) for m in self.session['history'] if m.get('name')=='read_file']
        self.assertEqual(len(reads),1);self.assertTrue(reads[0]['ok']);self.assertIn('actual',str(reads[0]))
        self.assertEqual(self.session['task']['verification'],[])

    def test_failed_recovery_preserves_unverified_state(self):
        with patch.object(ToolRunner,'tool_read_file',side_effect=RuntimeError('Unavailable target')):
            self.agent_run([call('write_file',path='proof.txt',content='actual')]+[{'role':'assistant','content':'Done'}]*3)
        self.assertEqual(self.agent.state,'unverified')
        self.assertTrue(self.session['task']['verification'])

    def test_mutation_with_actual_read_is_completed(self):
        self.agent_run([call('write_file',path='proof.txt',content='actual'),call('read_file',path='proof.txt'),{'role':'assistant','content':'Verified'}])
        self.assertEqual(self.agent.state,'completed');self.assertIn('actual',self.session['history'][-2]['content'])

    def test_task_updates_keep_stable_prefix_and_latest_verification_status(self):
        import copy
        contexts=[]
        replies=iter([call('write_file',path='proof.txt',content='actual'),call('read_file',path='proof.txt'),{'role':'assistant','content':'Verified'}])
        def chat(messages):
            contexts.append(copy.deepcopy(messages))
            return next(replies)
        with patch.object(ModelClient,'chat',side_effect=chat):
            self.agent.start(self.sid,'Create proof.txt')
            self.agent.worker.join(5)
        self.assertFalse(self.agent.busy)
        self.assertEqual(self.agent.state,'completed')
        self.assertEqual(len({context[0]['content'] for context in contexts}),1)
        statuses=[next(m['content'] for m in context if m['content'].startswith('Application-generated status')) for context in contexts]
        self.assertIn('proof.txt',statuses[1])
        self.assertIn('"kind": "file"',statuses[1])
        self.assertIn('"verification": []',statuses[2])

    def test_successful_repeated_observations_remain_available(self):
        (self.workspace/'proof.txt').write_text('actual')
        self.agent_run([call('read_file',path='proof.txt') for _ in range(4)]+[{'role':'assistant','content':'No changes'}]*3)
        results=[json.loads(m['content']) for m in self.session['history'] if m['role']=='tool']
        self.assertEqual(len(results),4);self.assertTrue(all(r['ok'] for r in results))

    def test_image_engine_limits_and_disabled_default(self):
        with self.assertRaises(RuntimeError):generate(self.store.data['settings'],'','prompt',256,256,self.cancel)
        settings={**self.store.data['settings'],'image_provider':'local_sd'}
        for width in [1,257,2048,True]:
            with self.assertRaises(ValueError):generate(settings,'','prompt',width,256,self.cancel)

    def test_ui_assets_are_embedded_after_dom_with_nonce_and_no_http_fetches(self):
        import re
        from veyq.ui import document
        root=Path(__file__).resolve().parent.parent;html=document(root)
        self.assertNotIn('<script defer',html);self.assertNotIn('<link href=',html)
        self.assertIn('data:image/svg+xml;base64,',html)
        nonce=re.search(r"script-src 'nonce-([^']+)'",html).group(1)
        self.assertEqual(html.count('<script nonce="'+nonce+'">'),5)
        self.assertEqual(html.count('<style nonce="'+nonce+'">'),2)
        self.assertLess(html.index('id="toast"'),html.index('<script nonce='))
        self.assertNotEqual(html,document(root))

    def test_project_expansion_persists_without_browser_storage(self):
        self.api.set_project_group_open('',False)
        self.assertEqual(Store(self.store.root).data['settings']['collapsed_projects'],[''])
        with self.assertRaises(ValueError):self.api.set_project_group_open('unknown',False)
        self.api.set_project_group_open('',True)
        self.assertEqual(Store(self.store.root).data['settings']['collapsed_projects'],[])

    def test_real_image_worker_uses_controlled_local_http_service(self):
        pixels=png();received=[]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def do_POST(server):
                received.append((server.path,json.loads(server.rfile.read(int(server.headers['Content-Length'])))))
                body=json.dumps({'images':[base64.b64encode(pixels).decode()]}).encode()
                server.send_response(200);server.send_header('Content-Length',str(len(body)));server.end_headers();server.wfile.write(body)
            def log_message(*args):pass
        class ExclusiveServer(ThreadingHTTPServer):
            allow_reuse_address=False
            def server_bind(server):
                if os.name=='nt': server.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                super().server_bind()
        server=ExclusiveServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            settings={**self.store.data['settings'],'image_provider':'local_sd','image_url':f'http://127.0.0.1:{server.server_port}'}
            result,width,height=generate(settings,'','Controlled fixture',256,256,self.cancel)
        finally:server.shutdown();server.server_close();thread.join(2)
        self.assertEqual((width,height),(256,256));self.assertEqual(Image.open(io.BytesIO(result)).format,'PNG')
        self.assertEqual(received[0][0],'/sdapi/v1/txt2img');self.assertFalse(received[0][1]['save_images'])

    def test_returned_remote_image_url_never_receives_api_token(self):
        request={'image_provider':'compatible','image_url':'https://example.com/v1','network':True,'image_model':'image-model','token':'private-fixture-token','prompt':'test','width':256,'height':256}
        with patch('veyq.network.public_request',side_effect=[{'text':json.dumps({'data':[{'url':'https://example.net/pixels'}]})},{'data':png()}]) as network:result=worker(request)
        self.assertTrue(result['ok']);self.assertIn('Authorization',network.call_args_list[0].kwargs['headers']);self.assertNotIn('headers',network.call_args_list[1].kwargs)

    def test_image_destination_changed_during_generation_is_preserved(self):
        target=self.workspace/'proof.png';target.write_bytes(b'original')
        def change(*args):target.write_bytes(b'changed externally');return png(),256,256
        with patch('veyq.image_generation.generate',side_effect=change),self.assertRaises(PermissionError):self.runner.tool_generate_image('test','proof.png',256,256)
        self.assertEqual(target.read_bytes(),b'changed externally');self.assertNotIn('artifacts',self.session)

    def test_cancelled_image_does_not_write_host_file(self):
        def cancel(*args):self.cancel.set();return png(),256,256
        with patch('veyq.image_generation.generate',side_effect=cancel),self.assertRaises(RuntimeError):self.runner.tool_generate_image('test','proof.png',256,256)
        self.assertFalse((self.workspace/'proof.png').exists())

    def test_artifact_preview_checks_session_ownership_and_current_hash(self):
        with patch('veyq.image_generation.generate',return_value=(png(),256,256)):
            artifact=self.runner.tool_generate_image('test','proof.png',256,256)
        self.assertIn('data:image/png;base64,',self.api.preview_generated_image(self.sid,artifact['id'])['data_url'])
        other=self.api.create_session()
        with self.assertRaises(ValueError):self.api.preview_generated_image(other,artifact['id'])
        (self.workspace/'proof.png').write_bytes(b'changed')
        with self.assertRaises(ValueError):self.api.preview_generated_image(self.sid,artifact['id'])

if __name__=='__main__':unittest.main()
