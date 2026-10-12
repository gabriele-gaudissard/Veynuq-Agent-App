"""Reviewed command hooks and bounded stdio language-server requests."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def components(root, manifest):
    from .plugins import safe_path,IDENTIFIER
    hooks={};lsp={}
    def read(value):return json.loads(safe_path(root,value).read_text(encoding='utf-8-sig')) if isinstance(value,str) else value
    configs=[]
    if (root/'hooks/hooks.json').exists():configs.append('hooks/hooks.json')
    if manifest.get('hooks'):configs.extend(manifest['hooks'] if isinstance(manifest['hooks'],list) else [manifest['hooks']])
    for config in configs:
        config=read(config)
        config=config.get('hooks',config)
        if not isinstance(config,dict) or len(config)>20:raise ValueError('Invalid plugin hooks.')
        for event,groups in config.items():
            if not IDENTIFIER.fullmatch(event) or not isinstance(groups,list) or len(groups)>20:raise ValueError('Invalid plugin hook event.')
            rows=[]
            for group in groups:
                if not isinstance(group,dict) or not isinstance(group.get('hooks'),list):raise ValueError('Invalid plugin hook group.')
                for hook in group['hooks']:
                    if not isinstance(hook,dict) or hook.get('type')!='command':raise ValueError('Only command hooks are supported.')
                    command=hook.get('command','')
                    if not isinstance(command,str) or not command.strip() or len(command)>4000 or re.search(r'\$\{(?!CLAUDE_PLUGIN_ROOT\})',command):raise ValueError('Invalid hook command or variable.')
                    rows.append({'command':command,'matcher':str(group.get('matcher',''))[:200],'timeout':min(60,max(1,int(hook.get('timeout',30))))})
            if len(rows)>30:raise ValueError('Too many plugin hooks.')
            hooks[event]=rows
    configs=[]
    if (root/'.lsp.json').exists():configs.append('.lsp.json')
    if manifest.get('lspServers'):configs.extend(manifest['lspServers'] if isinstance(manifest['lspServers'],list) else [manifest['lspServers']])
    for config in configs:
        config=read(config)
        if not isinstance(config,dict) or len(config)>10:raise ValueError('Invalid plugin LSP servers.')
        for name,value in config.items():
            if not IDENTIFIER.fullmatch(name) or not isinstance(value,dict):raise ValueError('Invalid LSP server.')
            if set(value)-{'command','args','env','extensionToLanguage','initializationOptions','settings','startupTimeout','shutdownTimeout','restartOnCrash','maxRestarts','transport'}:raise ValueError('Unsupported LSP configuration field.')
            if value.get('transport','stdio')!='stdio':raise ValueError('Only stdio LSP is supported.')
            from .plugins import validate_server
            server=validate_server({k:value[k] for k in ('command','args','env') if k in value})
            extensions=value.get('extensionToLanguage',{})
            if not isinstance(extensions,dict) or len(extensions)>50 or any(not re.fullmatch(r'\.[a-zA-Z0-9]{1,15}',k) or not isinstance(v,str) or len(v)>80 for k,v in extensions.items()):raise ValueError('Invalid LSP language map.')
            lsp[name]={**server,'extensionToLanguage':extensions,'initializationOptions':value.get('initializationOptions',{}),'settings':value.get('settings',{})}
    return hooks,lsp


def configuration(manager, plugin_id, kind, name, index=0):
    row=manager.record(plugin_id);manager.verify(row)
    if kind=='hook':
        entries=row.get('hooks',{}).get(name,[])
        if not 0<=index<len(entries):raise ValueError('Unknown hook index.')
        value=entries[index]
    elif kind=='lsp':
        value=row.get('lsp',{}).get(name)
        if not value:raise ValueError('Unknown LSP server.')
    else:raise ValueError('Choose hook or lsp.')
    return row,value


def invoke(runner, plugin_id, kind, name, index=0, payload=None, path='', method='symbols', line=1,column=0):
    from .plugins import Plugins
    manager=Plugins(runner.store);row,config=configuration(manager,plugin_id,kind,name,index)
    folder=manager.folder(row)
    def expand(value):return value.replace('${CLAUDE_PLUGIN_ROOT}',str(folder))
    request={'kind':kind,'config':config,'cwd':str(folder),'payload':payload or {}}
    if len(json.dumps(request['payload']))>20000:raise ValueError('Hook payload exceeds 20,000 characters.')
    if kind=='hook':request['config']={**config,'command':expand(config['command'])}
    else:
        target=runner.path(path)
        if not target.is_file() or target.stat().st_size>500000:raise ValueError('LSP requires a text file under 500 KB.')
        text=target.read_text(encoding='utf-8-sig');lines=text.splitlines() or ['']
        if not 1<=line<=len(lines) or not 0<=column<=len(lines[line-1]):raise ValueError('Invalid LSP position.')
        if target.suffix not in config['extensionToLanguage']:raise ValueError('This LSP does not declare this file extension.')
        request.update(path=str(target),workspace=str(runner.workspace),text=text,method=method,line=line,column=column)
        request['config']={**config,'command':expand(config['command']),'args':[expand(v) for v in config.get('args',[])],
            'env':{k:expand(v) for k,v in config.get('env',{}).items()}}
    return run_worker(runner,request)


def run_worker(runner,request):
    import tempfile,time
    from .storage import redact
    env={k:v for k,v in os.environ.items() if not re.search(r'(?i)(token|secret|password|api_key|credential)',k)}
    env['PYTHONPATH']=str(Path(__file__).resolve().parent.parent);env['PYTHONDONTWRITEBYTECODE']='1'
    flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
    with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
        runner.process=subprocess.Popen([sys.executable,'-m','veyq.plugin_extension_worker'],stdin=subprocess.PIPE,stdout=output,stderr=errors,env=env,**flags)
        started=time.monotonic()
        try:
            runner.process.stdin.write(json.dumps(request).encode());runner.process.stdin.close()
            while runner.process.poll() is None:
                if runner.cancel.wait(.1):raise RuntimeError('Plugin operation interrupted; inspect the outcome before retrying.')
                if time.monotonic()-started>70:raise TimeoutError('Plugin operation timed out; inspect the outcome before retrying.')
                if os.fstat(output.fileno()).st_size+os.fstat(errors.fileno()).st_size>2000000:raise ValueError('Plugin output exceeds 2 MB.')
            output.seek(0);result=json.loads(output.read(2000001))
            from .plugins import Plugins
            result=json.loads(redact(json.dumps(result),Plugins(runner.store).credentials()))
            if not result['ok']:raise RuntimeError(result['error'])
            return result['result']
        finally:
            runner.stop_process()
            try:runner.process.wait(timeout=5)
            except subprocess.TimeoutExpired:runner.process.kill();runner.process.wait(timeout=5)
            runner.process=None
