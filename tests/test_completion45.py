import asyncio
import base64
import hashlib
import http.server
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from veyq.storage import Store
from veyq.tools import ToolRunner
from veyq.plugins import Plugins
from veyq import image_setup
from veyq.plugin_oauth import AccountStorage, Callback, provider, key_for


LSP='''import json,sys
def recv():
 header={}
 while True:
  line=sys.stdin.buffer.readline()
  if not line:sys.exit()
  if line==b"\\r\\n":break
  k,v=line.decode().split(":",1);header[k.lower()]=v.strip()
 return json.loads(sys.stdin.buffer.read(int(header["content-length"])))
while True:
 m=recv()
 if m["method"]=="exit":break
 if "id" not in m:continue
 result={"capabilities":{"documentSymbolProvider":True}} if m["method"]=="initialize" else [{"name":"fixture","kind":12,"range":{"start":{"line":0,"character":0},"end":{"line":1,"character":0}},"selectionRange":{"start":{"line":0,"character":4},"end":{"line":0,"character":11}}}] if m["method"]=="textDocument/documentSymbol" else None
 raw=json.dumps({"jsonrpc":"2.0","id":m["id"],"result":result}).encode()
 sys.stdout.buffer.write(f"Content-Length: {len(raw)}\\r\\n\\r\\n".encode()+raw);sys.stdout.buffer.flush()
'''


class Completion45(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.workspace=self.root/'project';self.workspace.mkdir();self.store=Store(self.root/'profile');self.tokens={}
  self.store.vault.get=lambda k:self.tokens.get(k,'');self.store.vault.set=lambda k,v:self.tokens.__setitem__(k,v)
  self.store.data['settings'].update(workspace=str(self.workspace),network=True,permission='full',auto_update=False)
  self.runner=ToolRunner(self.store,self.store.snapshot()['settings'],'controlled',threading.Event(),lambda *a:None,lambda *a:True,self.root/'app',{})
  self.addCleanup(self.runner.close)

 def image_fixture(self):
  folder=self.store.root/'image-engine';folder.mkdir();binary=folder/'bin';binary.mkdir()
  with zipfile.ZipFile(folder/'engine.zip','w') as z:
   z.writestr('sd-cli.exe',b'fixture never executed');z.writestr('lib.dll',b'fixture dll');z.writestr('sd-server.exe',b'not extracted')
  (binary/'sd-cli.exe').write_bytes(b'fixture never executed');(binary/'lib.dll').write_bytes(b'fixture dll');(folder/'sd15.safetensors').write_bytes(b'fixture weights')
  for name,file in [('ENGINE','engine.zip'),('MODEL','sd15.safetensors')]:
   self.enterContext(patch.object(image_setup,name+'_SIZE',(folder/file).stat().st_size));self.enterContext(patch.object(image_setup,name+'_SHA',hashlib.sha256((folder/file).read_bytes()).hexdigest()))
  return folder,binary

 def test_managed_integrity_verifies_actual_executable_model_and_archive(self):
  folder,binary=self.image_fixture();executable,model=image_setup.verify(self.store.root);self.assertEqual(executable,binary/'sd-cli.exe');self.assertTrue(image_setup.ready(self.store.root))
  (binary/'lib.dll').write_bytes(b'tampered')
  with self.assertRaises(PermissionError):image_setup.verify(self.store.root)

 def test_managed_extra_dll_is_refused(self):
  folder,binary=self.image_fixture();(binary/'injected.dll').write_bytes(b'bad')
  with self.assertRaises(PermissionError):image_setup.verify(self.store.root)

 def test_managed_corrupted_weights_are_refused(self):
  folder,binary=self.image_fixture();(folder/'sd15.safetensors').write_bytes(b'corrupt')
  with self.assertRaises(PermissionError):image_setup.verify(self.store.root)

 def test_managed_archive_paths_and_duplicate_files_are_refused(self):
  for name in ('../sd-cli.exe','nested/sd-cli.exe','C:sd-cli.exe'):
   with io.BytesIO() as stream:
    with zipfile.ZipFile(stream,'w') as z:z.writestr(name,b'bad')
    with zipfile.ZipFile(stream) as z:
     with self.assertRaises(ValueError):image_setup.entries(z)

 def test_managed_oversize_never_starts_inference(self):
  from veyq.image_generation import generate
  with patch('veyq.image_generation.subprocess.Popen') as launch:
   with self.assertRaises(ValueError):generate({'image_provider':'managed'},'','prompt',1024,512,threading.Event(),self.store.root)
   launch.assert_not_called()

 def test_managed_stop_preserves_cancellation_when_windows_cleanup_is_delayed(self):
  from veyq.image_generation import generate
  directory=tempfile.TemporaryDirectory(dir=self.root);attempts=[];cleanup=directory.cleanup
  (Path(directory.name)/'prompt.txt').write_text('synthetic test prompt')
  def delayed():
   attempts.append(1)
   if len(attempts)==1:raise PermissionError('Synthetic Windows cwd handle still closing')
   cleanup()
  directory.cleanup=delayed
  class Process:
   def __init__(self):self.stdin=io.StringIO();self.stdout=io.StringIO('{}\n')
   def poll(self):return 0
   def wait(self,**kw):return 0
  cancel=threading.Event();cancel.set()
  with patch('tempfile.TemporaryDirectory',return_value=directory),patch('veyq.image_generation.subprocess.Popen',return_value=Process()),patch('veyq.image_generation.time.sleep'):
   with self.assertRaisesRegex(RuntimeError,'stopped'):generate({'image_provider':'managed'},'','synthetic prompt',512,512,cancel,self.store.root)
  self.assertEqual(len(attempts),2);self.assertFalse(Path(directory.name).exists())

 def test_static_python_analysis_does_not_execute_source(self):
  file=self.workspace/'sample.py';file.write_text('raise RuntimeError("must not execute")\ndef greet(name):\n    return name\ngreet("world")\n')
  symbols=self.runner.execute('code_intelligence',{'path':'sample.py','action':'symbols'});self.assertTrue(symbols['ok'],symbols);self.assertIn('greet',[i['name'] for i in symbols['result']['items']])
  result=self.runner.execute('code_intelligence',{'path':'sample.py','action':'definition','line':4,'column':3});self.assertTrue(result['ok'],result);self.assertEqual(result['result']['items'][0]['line'],2)

 def test_static_diagnostics_report_real_syntax_error(self):
  (self.workspace/'invalid.py').write_text('def broken(:\n')
  result=self.runner.execute('code_intelligence',{'path':'invalid.py','action':'diagnostics'});self.assertTrue(result['ok']);self.assertEqual(result['result']['diagnostics'][0]['line'],1)

 def install_extensions(self):
  folder=self.root/'plugin';folder.mkdir();(folder/'hooks').mkdir();(folder/'lsp.py').write_text(LSP)
  (folder/'plugin.json').write_text(json.dumps({'name':'controlled','hooks':'hooks/hooks.json','lspServers':{'fixture':{'command':sys.executable,'args':['${CLAUDE_PLUGIN_ROOT}/lsp.py'],'extensionToLanguage':{'.py':'python'}}}}))
  command='Write-Output "controlled-hook"' if os.name=='nt' else 'printf controlled-hook'
  (folder/'hooks/hooks.json').write_text(json.dumps({'hooks':{'PreToolUse':[{'matcher':'*','hooks':[{'type':'command','command':command}]}]}}))
  return Plugins(self.store).install(folder)

 def test_hooks_import_without_running_then_execute_only_via_tool(self):
  with patch('veyq.plugin_extensions.run_worker') as run:row=self.install_extensions();run.assert_not_called()
  result=self.runner.execute('plugin_extension',{'plugin_id':row['id'],'kind':'hook','name':'PreToolUse','payload':{'tool_name':'read_file'}})
  self.assertTrue(result['ok'],result);self.assertIn('controlled-hook',result['result']['text']);self.assertIsNone(self.runner.process)

 def test_actual_lsp_protocol_symbol_result(self):
  row=self.install_extensions();(self.workspace/'sample.py').write_text('def fixture():\n    return 1\n')
  result=self.runner.execute('plugin_extension',{'plugin_id':row['id'],'kind':'lsp','name':'fixture','path':'sample.py','method':'symbols'})
  self.assertTrue(result['ok'],result);self.assertEqual(result['result']['result'][0]['name'],'fixture');self.assertIsNone(self.runner.process)

 def test_extension_denied_offline_disabled_readonly_and_tampered(self):
  row=self.install_extensions();args={'plugin_id':row['id'],'kind':'hook','name':'PreToolUse'}
  with patch('veyq.plugin_extensions.run_worker') as run:
   self.runner.settings.update(permission='auto');self.runner.approve=lambda p:False;self.assertFalse(self.runner.execute('plugin_extension',args)['ok'])
   self.runner.settings.update(permission='full',network=False);self.assertFalse(self.runner.execute('plugin_extension',args)['ok'])
   self.runner.settings.update(network=True,_read_only=True);self.assertFalse(self.runner.execute('plugin_extension',args)['ok'])
   self.runner.settings.pop('_read_only');self.runner.settings['disabled_tools']=['plugin_extension'];self.assertFalse(self.runner.execute('plugin_extension',args)['ok'])
   self.runner.settings['disabled_tools']=[];(Plugins(self.store).folder(row)/'lsp.py').write_text('changed');self.assertFalse(self.runner.execute('plugin_extension',args)['ok']);run.assert_not_called()

 def test_marketplace_installs_exact_reviewed_commit_and_rejects_escape(self):
  from veyq import plugin_marketplace as market
  sha='a'*40;manifest={'repository':'owner/repo','revision':sha,'plugins':[{'name':'example','source':'./plugins/example','supported':True}]}
  def archive(name):
   stream=io.BytesIO()
   with zipfile.ZipFile(stream,'w') as z:z.writestr('repo-'+sha+'/plugins/example/'+name,'# Controlled skill')
   return stream.getvalue()
  reviewed={'repository':'owner/repo','commit':sha,'path':'plugins/example'}
  with patch.object(market,'catalog',return_value=manifest),patch.object(market,'public_request',return_value={'data':archive('SKILL.md')}) as read:
   row=market.install(self.store,'owner/repo',sha,'example',reviewed);self.assertTrue(row['enabled']);self.assertEqual(row['source']['commit'],sha);self.assertIn(sha,read.call_args.args[0])
  with patch.object(market,'catalog',return_value=manifest),patch.object(market,'public_request',return_value={'data':archive('../escape.md')}):
   with self.assertRaises(ValueError):market.install(self.store,'owner/repo',sha,'example',reviewed)
  with patch.object(market,'catalog',return_value=manifest),patch.object(market,'public_request') as read:
   with self.assertRaises(ValueError):market.install(self.store,'owner/repo',sha,'example',{**reviewed,'commit':'b'*40})
   read.assert_not_called()

 def test_model_action_check_requires_actual_write_read(self):
  from veyq.model_check import check
  class Client:
   def __init__(self,*a):pass
   def close(self):pass
   def chat(self,messages):
    nonce=re.search(r'Veynuq-[a-f0-9]{32}',messages[1]['content'])[0]
    return {'role':'assistant','content':'','tool_calls':[{'id':'w','function':{'name':'write_file','arguments':{'path':'proof.txt','content':nonce}}},{'id':'r','function':{'name':'read_file','arguments':{'path':'proof.txt'}}}]}
  with patch('veyq.model_check.ModelClient',Client):self.assertEqual(check(self.runner.settings,'',threading.Event())['state'],'passed')
  Client.chat=lambda *a:{'role':'assistant','content':'Here are instructions: create a file.'}
  with patch('veyq.model_check.ModelClient',Client):self.assertEqual(check(self.runner.settings,'',threading.Event())['state'],'failed')

 def test_marketplace_supports_public_github_subdirectories_only(self):
  from veyq.plugin_marketplace import github_source,selection
  source={'source':'git-subdir','url':'https://github.com/owner/repo.git','path':'plugins/sample','sha':'b'*40}
  self.assertEqual(github_source(source)['repo'],'owner/repo');self.assertIsNone(github_source({**source,'url':'https://private.example/repo.git'}))
  catalog={'repository':'owner/catalog','revision':'a'*40,'plugins':[{'name':'sample','source':source,'supported':True}]}
  with patch('veyq.plugin_marketplace.catalog',return_value=catalog),patch('veyq.plugin_marketplace.public_request') as read:
   result=selection('owner/catalog','a'*40,'sample');self.assertEqual(result['commit'],'b'*40);self.assertEqual(result['path'],'plugins/sample');read.assert_not_called()

 def test_model_check_cannot_write_outside_literal_test_file(self):
  from veyq.model_check import check
  outside=self.root/'outside.txt'
  class Client:
   def __init__(self,*a):pass
   def close(self):pass
   def chat(self,messages):return {'role':'assistant','tool_calls':[{'function':{'name':'write_file','arguments':{'path':str(outside),'content':'bad'}}}]}
  with patch('veyq.model_check.ModelClient',Client):self.assertEqual(check(self.runner.settings,'',threading.Event())['state'],'failed')
  self.assertFalse(outside.exists())

 def account(self):
  owner=self
  class Vault:
   def __init__(self,*a):pass
   def get(self,k):return owner.tokens.get(k,'')
   def set(self,k,v):owner.tokens[k]=v
  self.enterContext(patch('veyq.plugin_oauth.Vault',Vault));return AccountStorage(self.store.root,key_for('plugin','remote'))

 def test_oauth_callback_rejects_wrong_state_accepts_once_and_closes(self):
  import requests
  storage=self.account()
  with Callback(storage,True) as callback:
   callback.state='controlled-state';url=callback.url
   self.assertEqual(requests.get(url+'?code=x&state=wrong',timeout=5).status_code,400);self.assertTrue(callback.result.empty())
   self.assertEqual(requests.get(url+'?code=valid&state=controlled-state',timeout=5).status_code,200);self.assertEqual(asyncio.run(callback.receive()),('valid','controlled-state'))
  with self.assertRaises(requests.exceptions.ConnectionError):requests.get(url,timeout=1)

 def test_oauth_noninteractive_never_opens_browser(self):
  storage=self.account()
  with Callback(storage,False) as callback,patch('webbrowser.open') as browser:
   with self.assertRaises(PermissionError):asyncio.run(callback.redirect('https://service.example/authorize'))
   browser.assert_not_called()

 def test_oauth_actual_sdk_pkce_token_storage_and_refresh(self):
  import httpx
  storage=self.account();seen=[];challenge={}
  async def authorize(url):
   query=parse_qs(urlsplit(url).query);challenge.update(query)
  async def receive():return 'controlled-code',challenge['state'][0]
  def handler(request):
   seen.append(str(request.url));url=str(request.url)
   if url=='https://service.example/mcp':
    return httpx.Response(200,text='verified') if request.headers.get('authorization')=='Bearer controlled-access' else httpx.Response(401,headers={'WWW-Authenticate':'Bearer resource_metadata="https://service.example/.well-known/oauth-protected-resource"'})
   if 'oauth-protected-resource' in url:return httpx.Response(200,json={'resource':'https://service.example/mcp','authorization_servers':['https://auth.example']})
   if 'well-known' in url:return httpx.Response(200,json={'issuer':'https://auth.example','authorization_endpoint':'https://auth.example/authorize','token_endpoint':'https://auth.example/token','registration_endpoint':'https://auth.example/register','response_types_supported':['code'],'code_challenge_methods_supported':['S256'],'token_endpoint_auth_methods_supported':['none']})
   if url.endswith('/register'):
    value=json.loads(request.content);return httpx.Response(201,json={**value,'client_id':'controlled-client'})
   if url.endswith('/token'):
    form=parse_qs(request.content.decode())
    if form['grant_type']==['authorization_code']:
     self.assertEqual(form['code'],['controlled-code']);computed=base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'][0].encode()).digest()).rstrip(b'=').decode();self.assertEqual(computed,challenge['code_challenge'][0])
    else:self.assertEqual(form['grant_type'],['refresh_token']);self.assertEqual(form['refresh_token'],['controlled-refresh'])
    return httpx.Response(200,json={'access_token':'controlled-access','token_type':'Bearer','expires_in':3600,'refresh_token':'controlled-refresh'})
   raise AssertionError(url)
  async def run():
   with Callback(storage,True) as callback:
    callback.redirect=authorize;callback.receive=receive
    auth=provider(storage,callback,'https://service.example/mcp')
    async with httpx.AsyncClient(auth=auth,transport=httpx.MockTransport(handler)) as client:self.assertEqual((await client.get('https://service.example/mcp')).text,'verified')
   self.assertEqual(storage.read()['tokens']['refresh_token'],'controlled-refresh');storage.save({'expires_at':time.time()-10})
   with Callback(storage,False) as callback:
    auth=provider(storage,callback,'https://service.example/mcp')
    async with httpx.AsyncClient(auth=auth,transport=httpx.MockTransport(handler)) as client:self.assertEqual((await client.get('https://service.example/mcp')).text,'verified')
  asyncio.run(run());self.assertEqual(seen.count('https://auth.example/token'),2);self.assertEqual(seen.count('https://auth.example/register'),1)


if __name__=='__main__':unittest.main()
