import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from veyq.desktop import DesktopAPI
from veyq.engine import ModelClient
from veyq.plugins import Plugins, validate_server
from veyq.storage import Store
from veyq.tools import TOOLS, ToolRunner


SERVER = '''from mcp.server.fastmcp import FastMCP
import os,time
mcp=FastMCP("controlled-test")
@mcp.tool()
def add(a:int,b:int)->int:
    return a+b
@mcp.tool()
def token_echo()->str:
    return os.environ.get('TEST_TOKEN','missing')
@mcp.tool()
def failure()->str:
    raise ValueError('controlled failure')
@mcp.tool()
def wait()->str:
    time.sleep(60)
    return 'done'
mcp.run()
'''


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.workspace=self.root/'project';self.workspace.mkdir()
        self.store=Store(self.root/'profile');self.tokens={}
        self.store.vault.get=lambda k:self.tokens.get(k,'')
        self.store.vault.set=lambda k,v:self.tokens.__setitem__(k,v)
        self.store.data['settings'].update(workspace=str(self.workspace),permission='full',network=True,auto_update=False)
        self.manager=Plugins(self.store);self.api=DesktopAPI(self.store)
        self.runner=ToolRunner(self.store,self.store.snapshot()['settings'],'plugin-test',threading.Event(),lambda *a:None,lambda *a:True,self.root/'app',{})
        self.addCleanup(self.runner.close)

    def install(self, config=None, manifest=None):
        source=self.root/'source';source.mkdir(exist_ok=True)
        (source/'.claude-plugin').mkdir(exist_ok=True)
        (source/'.claude-plugin/plugin.json').write_text(json.dumps(manifest or {'name':'controlled-plugin','version':'1.0','hooks':'./hooks/hooks.json'}))
        (source/'skills/check').mkdir(parents=True,exist_ok=True)
        (source/'skills/check/SKILL.md').write_text('Read and verify the requested project. Never send secrets. !`no-shell-on-load`')
        (source/'commands').mkdir(exist_ok=True);(source/'commands/review.md').write_text('Review the actual diff.')
        (source/'.env').write_text('EXCLUDED_SECRET=private')
        (source/'server.py').write_text(SERVER)
        (source/'.mcp.json').write_text(json.dumps({'mcpServers':{'local':config or {'command':sys.executable,'args':['${CLAUDE_PLUGIN_ROOT}/server.py']}}}))
        return self.manager.install(source)

    def test_claude_skills_commands_import_enabled_no_hooks(self):
        row=self.install();self.assertTrue(row['enabled']);self.assertEqual(set(row['skills']),{'check','review'})
        self.assertIn('hooks',row['unsupported']);self.assertFalse((self.manager.folder(row)/'.env').exists())
        self.assertTrue(self.manager.skill(row['id'],'check')['untrusted'])
        self.assertIn('no-shell-on-load',self.manager.skill(row['id'],'check')['content'])
        self.assertEqual(len(Plugins(Store(self.store.root)).catalog()),1)

    def test_mcp_stdio_discovery_call_actual_sdk(self):
        row=self.install();args={'plugin_id':row['id'],'server':'local'}
        result=self.runner.execute('plugin_tools',args);self.assertTrue(result['ok'],result)
        self.assertIn('add',[t['name'] for t in result['result']['tools']])
        result=self.runner.execute('plugin_call',{**args,'tool':'add','arguments':{'a':2,'b':5}})
        self.assertTrue(result['ok'],result);self.assertEqual(result['result']['structuredContent'],{'result':7})
        self.assertFalse(self.runner.process)

    def test_mcp_tool_error_is_failure_not_success(self):
        row=self.install();args={'plugin_id':row['id'],'server':'local'}
        self.assertTrue(self.runner.execute('plugin_tools',args)['ok'])
        result=self.runner.execute('plugin_call',{**args,'tool':'failure','arguments':{}})
        self.assertFalse(result['ok']);self.assertTrue(result['result']['isError'])

    def test_requires_discovery_and_valid_arguments(self):
        row=self.install();args={'plugin_id':row['id'],'server':'local','tool':'add','arguments':{'a':2}}
        with patch('veyq.plugins.invoke') as invoke:
            self.assertFalse(self.runner.execute('plugin_call',args)['ok']);invoke.assert_not_called()
        self.assertTrue(self.runner.execute('plugin_tools',{'plugin_id':row['id'],'server':'local'})['ok'])
        result=self.runner.execute('plugin_call',args);self.assertFalse(result['ok'],result)

    def test_disabled_plugin_and_tool_block_actual_execution(self):
        row=self.install();self.manager.set_enabled(row['id'],False)
        result=self.runner.execute('plugin_tools',{'plugin_id':row['id'],'server':'local'});self.assertFalse(result['ok'])
        self.assertEqual(self.runner.tool_plugin_catalog(),[])
        self.manager.set_enabled(row['id'],True)
        self.runner.settings['disabled_tools']=['plugin_tools','write_file']
        with patch('veyq.plugins.invoke') as invoke:
            self.assertFalse(self.runner.execute('plugin_tools',{'plugin_id':row['id'],'server':'local'})['ok']);invoke.assert_not_called()
        self.assertFalse(self.runner.execute('write_file',{'path':'blocked.txt','content':'blocked'})['ok'])
        self.assertFalse((self.workspace/'blocked.txt').exists())

    def test_toggle_persistence_and_default_catalog(self):
        catalog=self.api.get_tool_catalog();self.assertEqual(catalog['total'],len(TOOLS));self.assertEqual(catalog['enabled'],len(TOOLS))
        self.api.set_tool_enabled('write_file',False)
        restored=Store(self.store.root);self.assertIn('write_file',restored.data['settings']['disabled_tools'])
        self.api.enable_all_tools();self.assertEqual(self.api.get_tool_catalog()['enabled'],len(TOOLS))
        self.api._agent.busy=True
        with self.assertRaises(RuntimeError):self.api.set_tool_enabled('read_file',False)
        self.api._agent.busy=False

    def test_model_catalog_omits_disabled_tools(self):
        self.api.set_tool_enabled('write_file',False);sid=self.api.create_session();seen=[]
        def chat(client,messages):
            seen.extend(t['function']['name'] for t in client.tools)
            return {'role':'assistant','content':'Read-only explanation.'}
        with patch.object(ModelClient,'chat',chat):
            self.api.start_run(sid,'Explain the project tools.');deadline=time.monotonic()+10
            while self.api._agent.busy and time.monotonic()<deadline:time.sleep(.05)
        self.assertNotIn('write_file',seen);self.assertIn('read_file',seen)

    def test_denial_offline_and_readonly_do_not_start_server(self):
        row=self.install();args={'plugin_id':row['id'],'server':'local'}
        with patch('veyq.plugins.invoke') as invoke:
            self.runner.settings['permission']='auto';self.runner.approve=lambda preview:False
            self.assertFalse(self.runner.execute('plugin_tools',args)['ok'])
            self.runner.settings.update(permission='full',network=False)
            self.assertFalse(self.runner.execute('plugin_tools',args)['ok'])
            self.runner.settings.update(network=True,_read_only=True)
            self.assertFalse(self.runner.execute('plugin_tools',args)['ok']);invoke.assert_not_called()

    def test_changed_plugin_or_approval_refused(self):
        row=self.install();folder=self.manager.folder(row)
        (folder/'server.py').write_text('tampered')
        self.assertFalse(self.runner.execute('plugin_tools',{'plugin_id':row['id'],'server':'local'})['ok'])
        row=self.install();folder=self.manager.folder(row)
        self.runner.settings['permission']='auto'
        def approve(preview):
            (folder/'server.py').write_text('changed during approval');return True
        self.runner.approve=approve
        self.assertFalse(self.runner.execute('plugin_tools',{'plugin_id':row['id'],'server':'local'})['ok'])

    def test_config_credentials_and_unsupported_transport_refused(self):
        for config in [{'url':'https://example.com/mcp','headers':{'Authorization':'Bearer actual-secret'}},
                       {'url':'https://user:secret@example.com/mcp'}, {'url':'http://example.com/mcp'},
                       {'url':'https://example.com/mcp','type':'sse'}, {'command':'python','env':{'API_TOKEN':'${OTHER_TOKEN}'}},
                       {'command':'python','args':'shell string'}]:
            with self.assertRaises(ValueError):validate_server(config)
        validate_server({'command':'python','env':{'API_TOKEN':'${VEYNUQ_PLUGIN_TOKEN}'}})
        with self.assertRaises(ValueError):validate_server({'command':'python','args':['${VEYNUQ_PLUGIN_TOKEN}']})

    def test_skill_path_escape_refused_no_install_left(self):
        with self.assertRaises(ValueError):self.install(manifest={'name':'bad','skills':'../outside'})
        self.assertEqual(self.manager.records(),[]);self.assertEqual(list(self.manager.root.iterdir()),[])

    def test_plugin_token_not_echoed_or_returned_by_ui(self):
        row=self.install(config={'command':sys.executable,'args':['${CLAUDE_PLUGIN_ROOT}/server.py'],'env':{'TEST_TOKEN':'${VEYNUQ_PLUGIN_TOKEN}'}})
        self.api.save_plugin_token(row['id'],'controlled-private-value')
        self.assertNotIn('controlled-private-value',json.dumps(self.api.get_plugins()))
        args={'plugin_id':row['id'],'server':'local'};self.assertTrue(self.runner.execute('plugin_tools',args)['ok'])
        result=self.runner.execute('plugin_call',{**args,'tool':'token_echo','arguments':{}})
        self.assertTrue(result['ok'],result);self.assertNotIn('controlled-private-value',json.dumps(result));self.assertIn('[REDACTED]',json.dumps(result))

    def test_cancel_stops_server_process_tree(self):
        row=self.install();args={'plugin_id':row['id'],'server':'local'}
        self.assertTrue(self.runner.execute('plugin_tools',args)['ok']);result=[]
        task=threading.Thread(target=lambda:result.append(self.runner.execute('plugin_call',{**args,'tool':'wait','arguments':{}})))
        task.start();deadline=time.monotonic()+8
        while self.runner.process is None and time.monotonic()<deadline:time.sleep(.02)
        time.sleep(.7);self.runner.cancel.set();task.join(8)
        self.assertFalse(task.is_alive());self.assertFalse(result[0]['ok']);self.assertIsNone(self.runner.process)

    def test_remove_plugin_without_touching_source_or_workspace(self):
        row=self.install();folder=self.manager.folder(row);self.api.save_plugin_token(row['id'],'private')
        self.api.remove_plugin(row['id']);self.assertFalse(folder.exists());self.assertEqual(self.manager.records(),[])
        self.assertTrue((self.root/'source/server.py').exists());self.assertEqual(self.tokens['plugin:'+row['id']],'')

    def test_actual_streamable_http_json_and_sse(self):
        messages=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def handle(self):
                try:super().handle()
                except ConnectionResetError:pass
            def log_message(self,*args):pass
            def do_DELETE(self):self.send_response(200);self.end_headers()
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));messages.append(data)
                if 'id' not in data:self.send_response(202);self.send_header('Content-Length','0');self.end_headers();return
                method=data['method']
                if method=='initialize':result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
                elif method=='tools/list':result={'tools':[{'name':'add','inputSchema':{'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},'required':['a','b']}}]}
                else:result={'content':[{'type':'text','text':str(sum(data['params']['arguments'].values()))}],'isError':False}
                envelope={'jsonrpc':'2.0','id':data['id'],'result':result}
                body=json.dumps(envelope).encode()
                kind='application/json'
                if method=='tools/call':body=b'event: message\ndata: '+body+b'\n\n';kind='text/event-stream'
                self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        fixture=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=fixture.serve_forever,daemon=True);thread.start()
        try:
            row=self.install(config={'type':'http','url':f'http://127.0.0.1:{fixture.server_port}/mcp'})
            args={'plugin_id':row['id'],'server':'local'};discovery=self.runner.execute('plugin_tools',args);self.assertTrue(discovery['ok'],{'discovery':discovery,'messages':messages})
            result=self.runner.execute('plugin_call',{**args,'tool':'add','arguments':{'a':3,'b':4}})
            self.assertTrue(result['ok'],result);self.assertEqual(result['result']['content'][0]['text'],'7')
            self.assertEqual(sum(m.get('method')=='tools/call' for m in messages),1)
        finally:fixture.shutdown();fixture.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
