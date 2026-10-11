"""Configured image engines; credentials and pixels stay out of tool logs."""
import base64
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path


def generate(settings,token,prompt,width,height,cancel):
    if settings.get('image_provider','disabled') == 'disabled':
        raise RuntimeError('Configure an image engine in Settings to generate images. Local image engines need no account; remote providers may need a vault token.')
    if not prompt.strip() or len(prompt)>10000: raise ValueError('Image prompt must contain 1–10000 characters.')
    if any(type(n) is not int or n<256 or n>1536 or n%64 for n in (width,height)):
        raise ValueError('Image dimensions must be multiples of 64 between 256 and 1536.')
    from .engine import validate_endpoint
    validate_endpoint(settings['image_url'],settings['network'])
    env={k:v for k,v in os.environ.items() if not re.search(r'(?i)(token|secret|password|api_key|credential)',k)}
    env['PYTHONIOENCODING']='utf-8'
    process=subprocess.Popen([sys.executable,'-m','veyq.image_generation'],
        cwd=Path(__file__).resolve().parent.parent,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        text=True,encoding='utf-8',**({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
    result_queue=queue.Queue(maxsize=1)
    def read():
        try:
            raw=process.stdout.readline(24_000_001)
            if len(raw)>24_000_000: raise ValueError('Image response exceeded its limit.')
            result_queue.put(json.loads(raw))
        except Exception: result_queue.put({'ok':False,'error':'Image engine returned an invalid response.'})
    reader=threading.Thread(target=read,daemon=True)
    try:
        request={key:settings.get(key) for key in ('image_provider','image_url','image_model','network')}
        process.stdin.write(json.dumps({**request,'token':token,'prompt':prompt,'width':width,'height':height})+'\n')
        process.stdin.flush(); reader.start()
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            if cancel.is_set(): raise RuntimeError('Image generation stopped. No output file was written; the provider may still finish its request.')
            try: result=result_queue.get(timeout=.2)
            except queue.Empty: continue
            if not result.get('ok'): raise RuntimeError(result.get('error','Image generation failed.'))
            return base64.b64decode(result['png'],validate=True),result['width'],result['height']
        raise TimeoutError('Image engine timed out. No output file was written.')
    finally:
        if process.poll() is None: process.kill()
        process.wait(timeout=10)
        for stream in (process.stdin,process.stdout): stream.close()
        if reader.is_alive(): reader.join(timeout=1)


def worker(request):
    import requests
    from PIL import Image
    from .engine import validate_endpoint
    from .network import public_request
    from urllib.parse import urlsplit
    base=validate_endpoint(request['image_url'],request['network'])
    width,height=request['width'],request['height']
    local_sd=request['image_provider']=='local_sd'
    if request['image_provider'] not in {'local_sd','compatible'}: raise ValueError('Unknown image engine.')
    payload={'prompt':request['prompt'],'width':width,'height':height,'steps':24,'batch_size':1,'n_iter':1,'save_images':False,'send_images':True} if local_sd else {'prompt':request['prompt'],'model':request['image_model'],'n':1,'size':f'{width}x{height}'}
    if not local_sd and not request['image_model']: raise ValueError('Configure the image model name.')
    headers={'Authorization':'Bearer '+request['token']} if request['token'] else {}
    endpoint=base+('/sdapi/v1/txt2img' if local_sd else '/images/generations')
    hostname=urlsplit(base).hostname
    loopback=hostname in {'localhost','127.0.0.1','::1'}
    if local_sd and not loopback: raise ValueError('Local image engines must use a loopback endpoint.')
    if loopback:
        with requests.Session() as client:
            client.trust_env=False
            with client.post(endpoint,json=payload,headers=headers,stream=True,timeout=(10,150),allow_redirects=False) as response:
                if response.status_code!=200: raise RuntimeError('Image engine returned HTTP '+str(response.status_code)+'. Check the configured model and credentials.')
                chunks=[]; size=0
                for chunk in response.iter_content(65536):
                    size+=len(chunk)
                    if size>20_000_000: raise ValueError('Image response exceeds 20 MB.')
                    chunks.append(chunk)
                data=json.loads(b''.join(chunks))
    else:
        data=json.loads(public_request(endpoint,method='POST',body=payload,headers=headers,limit=20_000_000,timeout=150,allow_redirects=False)['text'])
    if local_sd: encoded=data['images'][0].split(',')[-1]
    else:
        first=data['data'][0]; encoded=first.get('b64_json')
        if not encoded:
            # Public returned URLs are fetched without forwarding the API token.
            raw=public_request(first['url'],limit=15_000_000)['data']
            encoded=base64.b64encode(raw).decode('ascii')
    raw=base64.b64decode(encoded,validate=True)
    if len(raw)>15_000_000: raise ValueError('Generated image exceeds 15 MB.')
    with Image.open(io.BytesIO(raw)) as image:
        if image.width*image.height>8_000_000: raise ValueError('Generated image dimensions exceed the limit.')
        image.load(); output=io.BytesIO(); image.convert('RGBA' if 'A' in image.getbands() else 'RGB').save(output,format='PNG')
        if output.tell()>15_000_000: raise ValueError('Generated PNG exceeds 15 MB.')
        return {'ok':True,'png':base64.b64encode(output.getvalue()).decode('ascii'),'width':image.width,'height':image.height}


if __name__=='__main__':
    try:
        line=sys.stdin.readline(50000)
        print(json.dumps(worker(json.loads(line))),flush=True)
    except Exception as error:
        # Provider errors can echo credentials, URLs and prompts: never log them.
        print(json.dumps({'ok':False,'error':'Image generation failed (' + type(error).__name__ + '). Check the image engine, model and vault token in Settings.'}),flush=True)
        sys.exit(1)
