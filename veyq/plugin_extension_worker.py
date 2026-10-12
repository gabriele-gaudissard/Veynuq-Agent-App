"""Disposable command-hook/LSP process; parent owns cancellation and limits."""
import json
import os
import subprocess
import sys
from pathlib import Path


def request(value):
    config=value['config'];flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    if value['kind']=='hook':
        command=['powershell','-NoProfile','-NonInteractive','-Command',config['command']] if os.name=='nt' else ['sh','-c',config['command']]
        # File-backed output avoids unbounded communicate() allocations.
        import tempfile
        with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=out,stderr=err,cwd=value['cwd'],**flags)
            try:
                process.stdin.write(json.dumps(value['payload']).encode());process.stdin.close()
                process.wait(timeout=config['timeout'])
            finally:
                if process.poll() is None:
                    if os.name=='nt':subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,**flags)
                    else:process.kill()
                    process.wait(timeout=5)
            out.seek(0);err.seek(0);text=out.read(100001);errors=err.read(100001)
            if len(text)>100000 or len(errors)>100000:raise ValueError('Hook output exceeds its limit.')
            if process.returncode:raise RuntimeError('Hook failed or denied the action: '+errors.decode(errors='replace')[:2000])
            return {'exit_code':0,'text':text.decode(errors='replace'),'untrusted':True,
                'note':'Hook output never grants permissions or authorizes later actions.'}
    methods={'symbols':'textDocument/documentSymbol','definition':'textDocument/definition','references':'textDocument/references',
             'completion':'textDocument/completion','signature':'textDocument/signatureHelp','diagnostics':None}
    if value['method'] not in methods:raise ValueError('Unsupported LSP method.')
    env={k:os.environ[k] for k in ('PATH','SystemRoot','WINDIR','TEMP','TMP','HOME') if k in os.environ};env.update(config.get('env',{}))
    process=subprocess.Popen([config['command'],*config.get('args',[])],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,cwd=value['workspace'],env=env,**flags)
    diagnostics=[]
    def send(method,params,identity=None):
        item={'jsonrpc':'2.0','method':method,'params':params}
        if identity is not None:item['id']=identity
        raw=json.dumps(item).encode();process.stdin.write(f'Content-Length: {len(raw)}\r\n\r\n'.encode()+raw);process.stdin.flush()
    def receive(identity):
        for _ in range(100):
            header={};total=0
            while True:
                line=process.stdout.readline(1025);total+=len(line)
                if not line or total>8000:raise ValueError('Invalid LSP header.')
                if line in {b'\r\n',b'\n'}:break
                key,_,data=line.decode().partition(':');header[key.lower()]=data.strip()
            size=int(header.get('content-length','0'))
            if not 0<size<=1000000:raise ValueError('LSP response exceeds its limit.')
            raw=bytearray()
            while len(raw)<size:
                chunk=process.stdout.read(size-len(raw))
                if not chunk:raise ValueError('LSP closed an incomplete response.')
                raw.extend(chunk)
            item=json.loads(raw)
            if item.get('method')=='textDocument/publishDiagnostics':
                if item.get('params',{}).get('uri')==uri:diagnostics[:]=item['params'].get('diagnostics',[])[:100]
            if item.get('id')==identity and 'method' not in item:
                if 'error' in item:raise RuntimeError(str(item['error'])[:1000])
                return item.get('result')
            if 'id' in item and 'method' in item:
                answer=json.dumps({'jsonrpc':'2.0','id':item['id'],'error':{'code':-32601,'message':'Client method not supported'}}).encode()
                process.stdin.write(f'Content-Length: {len(answer)}\r\n\r\n'.encode()+answer);process.stdin.flush()
        raise ValueError('Too many LSP notifications.')
    uri=Path(value['path']).as_uri()
    try:
        send('initialize',{'processId':None,'rootUri':Path(value['workspace']).as_uri(),'capabilities':{},'initializationOptions':config.get('initializationOptions',{})},1);receive(1)
        send('initialized',{})
        if config.get('settings'):send('workspace/didChangeConfiguration',{'settings':config['settings']})
        send('textDocument/didOpen',{'textDocument':{'uri':uri,'languageId':config['extensionToLanguage'][Path(value['path']).suffix],'version':1,'text':value['text']}})
        params={'textDocument':{'uri':uri}}
        if value['method']!='symbols':params['position']={'line':value['line']-1,'character':len((value['text'].splitlines() or [''])[value['line']-1][:value['column']].encode('utf-16-le'))//2}
        if value['method']=='references':params['context']={'includeDeclaration':True}
        if value['method']=='diagnostics':
            # A synchronization request gives the server a chance to publish push diagnostics.
            send('textDocument/documentSymbol',{'textDocument':{'uri':uri}},2);receive(2);result=diagnostics
        else:send(methods[value['method']],params,2);result=receive(2)
        send('shutdown',{},3);receive(3);send('exit',{})
        return {'result':result,'diagnostics':diagnostics,'untrusted':True,'note':'Results depend on server capabilities; empty push diagnostics are not proof of type safety.'}
    finally:
        process.stdin.close()
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
        process.stdout.close()


if __name__=='__main__':
    try:result={'ok':True,'result':request(json.loads(sys.stdin.buffer.read(700001)))}
    except BaseException as error:result={'ok':False,'error':str(error)[:1500] or type(error).__name__}
    print(json.dumps(result),flush=True)
