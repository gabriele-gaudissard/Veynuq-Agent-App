"""Explicit browser sign-in using SDK PKCE; tokens never enter model/UI output."""
import asyncio
import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs

import httpx
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata, OAuthClientInformationFull, OAuthToken, OAuthMetadata, ProtectedResourceMetadata

from .storage import Vault
from .network import public_target


def key_for(plugin_id, server):
    from .plugins import IDENTIFIER
    if not IDENTIFIER.fullmatch(plugin_id) or not IDENTIFIER.fullmatch(server): raise ValueError('Invalid plugin account identifier.')
    return 'plugin-oauth:'+plugin_id+':'+server


class AccountStorage:
    def __init__(self, root, key):
        self.vault=Vault(root);self.key=key;self.provider=None

    def read(self):
        raw=self.vault.get(self.key)
        if len(raw)>40000:raise ValueError('Stored plugin account exceeds its limit.')
        return json.loads(raw) if raw else {}

    def save(self, change):
        value={**self.read(),**change}
        raw=json.dumps(value)
        if len(raw)>40000:raise ValueError('Plugin account exceeds its storage limit.')
        self.vault.set(self.key,raw)

    async def get_tokens(self):
        value=self.read()
        if self.provider:
            context=self.provider.context
            context.token_expiry_time=value.get('expires_at')
            if value.get('metadata'):context.oauth_metadata=OAuthMetadata.model_validate(value['metadata'])
            if value.get('protected_resource'):context.protected_resource_metadata=ProtectedResourceMetadata.model_validate(value['protected_resource'])
            if value.get('auth_server_url'):context.auth_server_url=value['auth_server_url']
        return OAuthToken.model_validate(value['tokens']) if value.get('tokens') else None

    async def set_tokens(self, tokens):
        context=self.provider.context
        self.save({'tokens':tokens.model_dump(mode='json',exclude_none=True),
                   'expires_at':time.time()+tokens.expires_in if tokens.expires_in else None,
                   'metadata':context.oauth_metadata.model_dump(mode='json',exclude_none=True) if context.oauth_metadata else None,
                   'protected_resource':context.protected_resource_metadata.model_dump(mode='json',exclude_none=True) if context.protected_resource_metadata else None,
                   'auth_server_url':context.auth_server_url})

    async def get_client_info(self):
        value=self.read().get('client')
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client):
        self.save({'client':client.model_dump(mode='json',exclude_none=True)})


class CheckedTransport(httpx.AsyncBaseTransport):
    def __init__(self, server_url):
        self.server=urlsplit(server_url);self.inner=httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        parsed=urlsplit(str(request.url))
        local=(parsed.scheme,parsed.hostname,parsed.port)==(self.server.scheme,self.server.hostname,self.server.port) and parsed.hostname in {'localhost','127.0.0.1','::1'}
        if not local:
            if parsed.scheme!='https':raise PermissionError('OAuth services require public HTTPS.')
            await asyncio.to_thread(public_target,str(request.url))
        response=await self.inner.handle_async_request(request)
        original=response.stream
        class Bounded(httpx.AsyncByteStream):
            async def __aiter__(self):
                total=0
                async for chunk in original:
                    total+=len(chunk)
                    if total>2_000_000:raise ValueError('Plugin HTTP response exceeds 2 MB.')
                    yield chunk
            async def aclose(self):await original.aclose()
        response.stream=Bounded()
        return response

    async def aclose(self):await self.inner.aclose()


class Callback:
    def __init__(self, storage, interactive):
        self.interactive=interactive;self.result=queue.Queue(maxsize=1);self.state=''
        saved=storage.read().get('client',{}).get('redirect_uris') or []
        self.port=0;self.path='/veynuq/'+uuid.uuid4().hex
        if saved:
            parsed=urlsplit(saved[0])
            if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.port<1024 or not parsed.path.startswith('/veynuq/'):
                raise ValueError('Invalid stored OAuth callback. Disconnect and reconnect this account.')
            self.port,self.path=parsed.port,parsed.path
        self.http=None;self.thread=None

    def __enter__(self):
        if not self.interactive:return self
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed=urlsplit(self.path);values=parse_qs(parsed.query)
                state=values.get('state',[''])[0];code=values.get('code',[''])[0]
                import secrets
                valid=parsed.path==owner.path and owner.state and secrets.compare_digest(state,owner.state) and 0<len(code)<=8000
                if not valid:
                    self.send_response(400);self.end_headers();return
                if not owner.result.full():owner.result.put((code,state))
                body=b'Authorization received. Return to Veynuq to check the connection.'
                self.send_response(200);self.send_header('Content-Type','text/plain; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        self.http=HTTPServer(('127.0.0.1',self.port),Handler)
        self.port=self.http.server_port
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        return self

    @property
    def url(self):return f'http://127.0.0.1:{self.port}{self.path}'

    async def redirect(self, url):
        if not self.interactive:raise PermissionError('This service needs sign-in. Use Connect account in Tools → Plugins; never paste credentials into chat.')
        parsed=urlsplit(url)
        if parsed.scheme!='https':raise PermissionError('The account authorization page must use public HTTPS.')
        await asyncio.to_thread(public_target,url)
        self.state=parse_qs(parsed.query).get('state',[''])[0]
        if not self.state:raise ValueError('The OAuth authorization request has no state.')
        import webbrowser
        if not await asyncio.to_thread(webbrowser.open,url):raise RuntimeError('Cannot open the account browser.')

    async def receive(self):
        if not self.interactive:raise PermissionError('Connect this account personally in Tools → Plugins.')
        try:return await asyncio.to_thread(self.result.get,True,300)
        except queue.Empty:raise TimeoutError('Account sign-in timed out. Retry Connect account.')

    def __exit__(self,*args):
        if self.http:self.http.shutdown();self.http.server_close()
        if self.thread:self.thread.join(timeout=2)


def provider(storage, callback, server_url):
    meta=OAuthClientMetadata(redirect_uris=[callback.url],client_name='Veynuq',token_endpoint_auth_method='none')
    class ReviewedProvider(OAuthClientProvider):
        async def async_auth_flow(self, request):
            flow=super().async_auth_flow(request)
            try:
                outgoing=await flow.__anext__()
                while True:
                    response=yield outgoing
                    # Refresh is allowed; registering a new OAuth client requires the account button.
                    if not callback.interactive and outgoing is request and response.status_code in {401,403}:
                        raise PermissionError('This service needs sign-in. Use Connect account in Tools → Plugins; never paste credentials into chat.')
                    outgoing=await flow.asend(response)
            except StopAsyncIteration:return
            finally:await flow.aclose()
    result=ReviewedProvider(server_url,meta,storage,redirect_handler=callback.redirect,callback_handler=callback.receive)
    storage.provider=result
    return result


def secrets_for(raw):
    value=json.loads(raw) if raw else {}
    return [value.get('tokens',{}).get(name,'') for name in ('access_token','refresh_token')] + [value.get('client',{}).get('client_secret','')]
