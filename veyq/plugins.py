"""Explicit plugin installation; declarative skills and gated MCP connections."""
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .storage import atomic_json, redact

IDENTIFIER = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}')
IGNORED = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.ssh', '.aws', '.azure', '.codex'}


def safe_path(root, value):
    if not isinstance(value, str) or Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValueError('Plugin paths must stay inside the installed plugin.')
    target = root / value
    for part in (target, *target.parents):
        if part == root.parent: break
        if part.exists() and (part.is_symlink() or getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Plugin links and junctions are not supported.')
    if not target.resolve().is_relative_to(root.resolve()): raise ValueError('Plugin path escaped its root.')
    return target


def validate_server(config):
    if not isinstance(config, dict): raise ValueError('Invalid MCP server configuration.')
    allowed = {'command', 'args', 'env', 'url', 'type', 'headers'}
    if set(config) - allowed: raise ValueError('Unsupported MCP server fields: ' + ', '.join(sorted(set(config)-allowed)))
    if 'url' in config:
        if any(k in config for k in ('command', 'args', 'env')): raise ValueError('Choose HTTP or stdio, not both.')
        url = urlsplit(config['url'])
        if url.username or url.password or url.fragment or url.query or not url.hostname:
            raise ValueError('Use an MCP URL without embedded credentials, query or fragment.')
        if url.hostname in {'localhost', '127.0.0.1', '::1'}:
            if url.scheme != 'http' or not url.port or url.port < 1024: raise ValueError('Local MCP needs an explicit HTTP port >=1024.')
        elif url.scheme != 'https': raise ValueError('Remote MCP needs HTTPS.')
        if config.get('type', 'http') not in {'http', 'streamable-http'}: raise ValueError('Only Streamable HTTP and stdio MCP are supported.')
    else:
        if config.get('type', 'stdio') != 'stdio' or not isinstance(config.get('command'), str) or not config['command'].strip():
            raise ValueError('Stdio MCP requires an executable command.')
        if any(c in config['command'] for c in '\r\n'): raise ValueError('Invalid executable.')
        if not isinstance(config.get('args', []), list) or len(config.get('args', [])) > 40 or any(not isinstance(v, str) or len(v)>2000 for v in config.get('args', [])):
            raise ValueError('MCP args must be a bounded list of literal strings.')
    if any('${VEYNUQ_PLUGIN_TOKEN}' in v for v in [config.get('command',''),config.get('url',''),*config.get('args',[])]):
        raise ValueError('Token placeholders are allowed only in headers or environment values, never URLs or process arguments.')
    for kind in ('env', 'headers'):
        values = config.get(kind, {})
        if not isinstance(values, dict) or len(values)>20: raise ValueError('Invalid MCP ' + kind)
        for key, value in values.items():
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', key) or not isinstance(value, str) or len(value)>2000 or '\r' in value or '\n' in value:
                raise ValueError('Invalid MCP configuration value.')
            if re.search(r'(?i)(token|secret|password|key|authorization|cookie)', key) and value and '${VEYNUQ_PLUGIN_TOKEN}' not in value:
                raise ValueError('Use ${VEYNUQ_PLUGIN_TOKEN} for credentials; enter the token only in the plugin vault field.')
            if re.search(r'\$\{(?!CLAUDE_PLUGIN_ROOT\}|VEYNUQ_PLUGIN_TOKEN\})', value):
                raise ValueError('Unsupported environment variable; only CLAUDE_PLUGIN_ROOT and VEYNUQ_PLUGIN_TOKEN are expanded.')
    raw=json.dumps(config)
    if redact(raw)!=raw: raise ValueError('Credentials must be stored in the plugin vault, not in the manifest.')
    if len(raw)>16000: raise ValueError('MCP configuration too large.')
    return copy.deepcopy(config)


class Plugins:
    def __init__(self, store):
        self.store = store
        self.root = store.root / 'plugins'

    def records(self):
        return self.store.snapshot().get('plugins', [])

    def record(self, plugin_id, enabled=True):
        if not isinstance(plugin_id, str) or not IDENTIFIER.fullmatch(plugin_id): raise ValueError('Invalid plugin ID.')
        row = next((p for p in self.records() if p['id'] == plugin_id), None)
        if not row: raise ValueError('Plugin not installed.')
        if enabled and not row['enabled']: raise PermissionError('Plugin disattivato. Enable it in Tools → Plugins.')
        return row

    def folder(self, row):
        return safe_path(self.root, row['id'])

    def verify(self, row):
        root = self.folder(row)
        for name, digest in row['files'].items():
            path = safe_path(root, name)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise PermissionError('Installed plugin changed; reinstall it before use.')
        actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
        if actual != set(row['files']): raise PermissionError('Installed plugin has unexpected files; reinstall before use.')

    def install(self, source):
        source = Path(source).resolve()
        if not source.is_dir() or source == Path(source.anchor) or source.is_relative_to(self.store.root):
            raise ValueError('Choose a separate plugin folder.')
        self.root.mkdir(exist_ok=True)
        plugin_id = uuid.uuid4().hex
        target = self.root / plugin_id
        target.mkdir()
        try:
            size = 0
            files = {}
            from .tools import sensitive, linklike
            for directory, folders, names in os.walk(source, followlinks=False):
                folders[:] = [n for n in folders if n not in IGNORED and not sensitive(Path(directory)/n) and not linklike(Path(directory)/n)]
                for name in names:
                    path = Path(directory)/name
                    if sensitive(path) or linklike(path) or path.suffix.lower() in {'.pyc','.pyo'}: continue
                    if path.stat().st_size>2_000_000: raise ValueError('Plugin file exceeds 2 MB.')
                    size += path.stat().st_size
                    if size>20_000_000 or len(files)>=2000: raise ValueError('Plugin exceeds installation limits.')
                    relative = path.relative_to(source).as_posix()
                    dest = safe_path(target, relative)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    content = path.read_bytes()
                    dest.write_bytes(content)
                    files[relative] = hashlib.sha256(content).hexdigest()
            manifest_path = target/'.claude-plugin/plugin.json'
            if not manifest_path.exists(): manifest_path = target/'plugin.json'
            manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig')) if manifest_path.exists() else {}
            if not isinstance(manifest, dict): raise ValueError('Invalid plugin manifest.')
            name = manifest.get('name', source.name)
            if not isinstance(name, str) or not IDENTIFIER.fullmatch(name): raise ValueError('Invalid plugin name.')
            skills = {}
            def add_skill(path, label):
                text = path.read_text(encoding='utf-8-sig')
                if len(text)>40000: raise ValueError('Plugin instruction exceeds 40,000 characters.')
                skills[label] = path.relative_to(target).as_posix()
            locations = ['skills', *([manifest['skills']] if isinstance(manifest.get('skills'), str) else manifest.get('skills', []))]
            for location in locations:
                place = safe_path(target, location)
                if (place/'SKILL.md').exists(): add_skill(place/'SKILL.md', place.name)
                elif place.is_dir():
                    for file in sorted(place.glob('*/SKILL.md')): add_skill(file, file.parent.name)
            if (target/'SKILL.md').exists(): add_skill(target/'SKILL.md', name)
            commands = manifest.get('commands', 'commands')
            if isinstance(commands, dict):
                for label, item in commands.items():
                    if not isinstance(item, dict) or 'source' not in item: raise ValueError('Inline commands are not supported; use a Markdown source file.')
                    add_skill(safe_path(target, item['source']), label)
            else:
                for location in ([commands] if isinstance(commands, str) else commands):
                    place = safe_path(target, location)
                    for file in ([place] if place.is_file() else sorted(place.glob('*.md'))): add_skill(file, file.stem)
            if len(skills)>60 or any(not IDENTIFIER.fullmatch(k) for k in skills): raise ValueError('Invalid or excessive plugin skills.')
            servers = {}
            def merge_servers(value):
                if isinstance(value, str): value = json.loads(safe_path(target,value).read_text(encoding='utf-8-sig'))
                if not isinstance(value, dict): raise ValueError('Invalid MCP manifest.')
                value = value.get('mcpServers', value)
                if not isinstance(value, dict): raise ValueError('Invalid MCP server list.')
                for key, config in value.items():
                    if not IDENTIFIER.fullmatch(key): raise ValueError('Invalid MCP server name.')
                    servers[key] = validate_server(config)
            if (target/'.mcp.json').exists(): merge_servers('.mcp.json')
            if manifest.get('mcpServers'): merge_servers(manifest['mcpServers'])
            if len(servers)>10 or not (skills or servers): raise ValueError('No supported skills, commands or MCP servers were found.')
            unsupported = [key for key in ('hooks','agents','lspServers','outputStyles','userConfig','dependencies','experimental') if key in manifest]
            for folder, key in [('hooks','hooks'),('agents','agents'),('output-styles','outputStyles')]:
                if (target/folder).exists() and key not in unsupported: unsupported.append(key)
            row = {'id':plugin_id, 'name':name, 'description':str(manifest.get('description',''))[:500], 'version':str(manifest.get('version',''))[:40],
                   'enabled':True, 'skills':skills, 'servers':servers, 'files':files, 'unsupported':unsupported}
            with self.store.lock:
                self.store.data.setdefault('plugins', []).append(row)
                self.store.save()
            return self.public(row)
        except Exception:
            shutil.rmtree(target)
            raise

    def public(self, row):
        return {k: copy.deepcopy(row[k]) for k in ('id','name','description','version','enabled','skills','servers','unsupported')}

    def catalog(self):
        return [self.public(row) for row in self.records()]

    def set_enabled(self, plugin_id, enabled):
        self.record(plugin_id, False)
        if type(enabled) is not bool: raise ValueError('Invalid enabled state.')
        with self.store.lock:
            next(p for p in self.store.data['plugins'] if p['id']==plugin_id)['enabled'] = enabled
            self.store.save()

    def remove(self, plugin_id):
        row = self.record(plugin_id, False)
        target = self.folder(row).resolve()
        if not target.is_relative_to(self.root.resolve()) or target == self.root.resolve(): raise ValueError('Invalid plugin folder.')
        if target.exists(): shutil.rmtree(target)
        self.store.vault.set('plugin:'+plugin_id, '')
        with self.store.lock:
            self.store.data['plugins'] = [p for p in self.store.data['plugins'] if p['id']!=plugin_id]
            self.store.save()

    def skill(self, plugin_id, skill):
        row = self.record(plugin_id)
        self.verify(row)
        if skill not in row['skills']: raise ValueError('Unknown plugin skill.')
        return {'plugin':row['name'], 'skill':skill, 'content':safe_path(self.folder(row),row['skills'][skill]).read_text(encoding='utf-8-sig'),
                'untrusted':True, 'note':'Reference instructions only. Permissions, user intent and tool restrictions still apply. Shell substitutions and hooks never execute on load.'}

    def server(self, plugin_id, server):
        row = self.record(plugin_id)
        self.verify(row)
        if server not in row['servers']: raise ValueError('Unknown MCP server.')
        return row, validate_server(row['servers'][server])

    def credentials(self):
        return [self.store.vault.get('plugin:'+p['id']) for p in self.records()]


def invoke(runner, plugin_id, server, tool=None, arguments=None):
    """A dedicated SDK process owns transport cleanup; Stop kills its tree."""
    manager = Plugins(runner.store)
    row, config = manager.server(plugin_id, server)
    token = runner.store.vault.get('plugin:'+plugin_id)
    root = str(manager.folder(row))
    def expand(value):
        value = value.replace('${CLAUDE_PLUGIN_ROOT}', root)
        if '${VEYNUQ_PLUGIN_TOKEN}' in value and not token: raise ValueError('This service needs a token. Enter it personally in Tools → Plugins; never in chat.')
        return value.replace('${VEYNUQ_PLUGIN_TOKEN}', token)
    config = json.loads(json.dumps(config))
    for key in ('command', 'url'):
        if key in config: config[key] = expand(config[key])
    config['args'] = [expand(v) for v in config.get('args', [])]
    for kind in ('env','headers'): config[kind] = {k:expand(v) for k,v in config.get(kind,{}).items()}
    config['env']['PYTHONDONTWRITEBYTECODE']='1'
    if config.get('url') and token and 'Authorization' not in config['headers']: config['headers']['Authorization'] = 'Bearer '+token
    if 'url' in config and urlsplit(config['url']).hostname not in {'localhost','127.0.0.1','::1'}:
        from .network import public_target
        public_target(config['url'])
    request = {'config':config, 'cwd':root, 'tool':tool, 'arguments':arguments or {}}
    env = {k:v for k,v in os.environ.items() if not re.search(r'(?i)(token|secret|password|api_key|credential)', k)}
    env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    flags = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        runner.process = subprocess.Popen([sys.executable,'-m','veyq.plugin_worker'], stdin=subprocess.PIPE, stdout=output, stderr=errors, env=env, **flags)
        started = time.monotonic()
        try:
            runner.process.stdin.write(json.dumps(request).encode()); runner.process.stdin.close()
            while runner.process.poll() is None:
                if runner.cancel.wait(.1): raise RuntimeError('Plugin operation interrupted; outcome may be unknown. Inspect the service before retrying.')
                if time.monotonic()-started>90: raise TimeoutError('Plugin operation timed out; outcome may be unknown. Do not repeat a mutation blindly.')
                if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size>2_000_000: raise ValueError('Plugin output exceeds 2 MB.')
            output.seek(0)
            value = json.loads(output.read(2_000_001))
            value = json.loads(redact(json.dumps(value), [token, *manager.credentials()]))
            if not value.get('ok'): raise RuntimeError(value.get('error','Plugin connection failed.'))
            return value['result']
        finally:
            runner.stop_process()
            try: runner.process.wait(timeout=5)
            except subprocess.TimeoutExpired: runner.process.kill(); runner.process.wait(timeout=5)
            runner.process = None
