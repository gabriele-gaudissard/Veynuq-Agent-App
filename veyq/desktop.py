"""Narrow JavaScript bridge: tools are reachable only through the agent gate."""
import json
import os
import re
import shutil
import sys
import threading
import uuid
from pathlib import Path

import requests
from .engine import Agent, validate_endpoint
from .storage import Store, DEFAULTS, redact, activity_title

ROOT = Path(__file__).resolve().parent.parent


class DesktopAPI:
    def __init__(self, store=None):
        self._store = store or Store(legacy=ROOT / "codex_data.json")
        self._agent = Agent(self._store, ROOT)
        self._window = None
        self._maintenance = threading.Lock()
        self._model_cancel = threading.Event()
        self._model_response = None
        self._image_cancel = threading.Event()
        self._image_status_lock = threading.RLock()
        self._image_status = {'state':'idle'}
        self._model_check_cancel = threading.Event()
        self._model_check_client = None
        self._model_check_status = {'state':'idle'}
        self._plugin_account_cancel=threading.Event()
        self._plugin_account_runner=None
        self._plugin_account_status={'state':'idle'}
        from .voice import VoiceInput
        self._voice = VoiceInput(self._store.root)
        from .scheduling import Scheduler
        self._scheduler = Scheduler(self)
        with self._store.lock:
            if not self._store.data["settings"]["workspace"]:
                workspace = self._store.root.parent / "Veynuq Workspace"
                workspace.mkdir(exist_ok=True)
                self._store.data["settings"]["workspace"] = str(workspace)
                self._store.save()

    def _idle(self):
        if self._voice.busy(): raise RuntimeError('Stop dictation before changing configuration.')
        if self._agent.busy or self._maintenance.locked():
            raise RuntimeError("Attendi la fine dell'attivita' prima di cambiare configurazione.")

    def _begin_setup(self):
        with self._agent.lock:
            self._idle()
            if not self._maintenance.acquire(blocking=False):raise RuntimeError('Another setup is running.')

    def get_settings(self):
        s = self._store.snapshot()["settings"]
        s["has_provider_token"] = bool(self._store.vault.get("provider"))
        s["has_github_token"] = bool(self._store.vault.get("github"))
        s["data_dir"] = str(self._store.root)
        s["version"] = "4.5.0"
        s['has_image_token'] = bool(self._store.vault.get('image'))
        s["recovery_notice"] = self._store.recovery_notice
        return s

    def get_tool_catalog(self):
        from .tools import TOOLS
        names = {tool['function']['name'] for tool in TOOLS}
        groups = [
            ('Files and coding', 'Read, search, edit, run commands and tests, manage Git, clone repositories and restore backups.', {'list_dir','read_file','search_files','write_file','edit_file','make_dir','move_file','delete_file','exec_cmd','git_status','clone_repository','restore_backup','git_worktree','sandbox_changes','delegate_coding','proposal_changes'}),
            ('Mouse and keyboard', 'Inspect Windows applications, click, double-click, right-click, drag, scroll, type and change keyboard layouts.', {'computer_windows','computer_inspect','computer_action','computer_pointer','computer_wait','windows_sandbox','computer_sandbox_type','keyboard_layout'}),
            ('Web and browser', 'Search, read websites, download files and operate an isolated browser with observed page elements.', {'web_search','read_url','download_file','browser_open','browser_state','browser_action'}),
            ('GitHub', 'Read and update repositories, issues, pull requests, branches and releases.', {'github'}),
            ('Images and documents', 'Inspect image files, generate PNGs with a configured image engine and read PDF/DOCX documents. Create documents, spreadsheets and charts with project code.', {'view_image','read_document','generate_image'}),
            ('Memory and task control', 'Maintain local memory, plan work, ask essential questions and use context from related project chats.', {'save_memory','update_plan','ask_user','read_project_context','checkpoint_task','load_procedure','delegate_tasks'}),
            ('Code intelligence', 'Navigate Python symbols, definitions, references and syntax diagnostics without executing project code.', {'code_intelligence'}),
            ('Plugins', 'Load installed skills and connect to reviewed MCP tools when needed.', {'plugin_catalog','plugin_tools','plugin_call','plugin_extension'}),
        ]
        disabled=set(self._store.snapshot()['settings'].get('disabled_tools', []))
        return {'total': len(names), 'enabled':len(names-disabled), 'groups': [{'name': title,'description': description,'count': len(tools & names),
            'tools':[{'name':tool['function']['name'],'description':tool['function']['description'],'enabled':tool['function']['name'] not in disabled}
                     for tool in TOOLS if tool['function']['name'] in tools]} for title, description, tools in groups]}

    def set_tool_enabled(self, name, enabled):
        from .tools import SPECS
        if name not in SPECS or type(enabled) is not bool: raise ValueError('Invalid tool toggle.')
        with self._agent.lock:
            self._idle()
            with self._store.lock:
                disabled=set(self._store.data['settings'].get('disabled_tools',[]))
                if enabled: disabled.discard(name)
                else: disabled.add(name)
                self._store.data['settings']['disabled_tools']=sorted(disabled)
                self._store.save()
        return {'ok':True}

    def enable_all_tools(self):
        with self._agent.lock:
            self._idle()
            with self._store.lock:
                self._store.data['settings']['disabled_tools']=[]
                self._store.save()
        return {'ok':True}

    def get_plugins(self):
        from .plugins import Plugins
        from .plugin_oauth import key_for,AccountStorage
        manager=Plugins(self._store)
        return {'plugins':[{**row,'has_token':bool(self._store.vault.get('plugin:'+row['id'])),
            'accounts':{name:bool(AccountStorage(self._store.root,key_for(row['id'],name)).read().get('tokens')) for name,cfg in row['servers'].items() if 'url' in cfg}} for row in manager.catalog()]}

    def install_plugin_folder(self, confirmed=False):
        with self._agent.lock:
            self._idle()
            if confirmed is not True: raise ValueError('Review and confirm external plugin installation.')
            if not self._window: raise RuntimeError('Plugin folder selection requires a visible window.')
            import webview
            selected=self._window.create_file_dialog(webview.FOLDER_DIALOG)
            if not selected: return {'cancelled':True}
            from .plugins import Plugins
            return Plugins(self._store).install(selected[0])

    def install_mcp_config(self, name, configuration, confirmed=False):
        with self._agent.lock:
            self._idle()
            if confirmed is not True: raise ValueError('Review and confirm external plugin installation.')
            if not isinstance(configuration,str) or len(configuration)>40000: raise ValueError('MCP configuration too large.')
            import tempfile
            from .plugins import Plugins,IDENTIFIER
            if not isinstance(name,str) or not IDENTIFIER.fullmatch(name): raise ValueError('Use a short plugin name with letters, numbers or hyphens.')
            parsed=json.loads(configuration)
            with tempfile.TemporaryDirectory(prefix='veynuq-plugin-') as folder:
                root=Path(folder)
                (root/'plugin.json').write_text(json.dumps({'name':name}),encoding='utf-8')
                (root/'.mcp.json').write_text(json.dumps(parsed),encoding='utf-8')
                return Plugins(self._store).install(root)

    def get_plugin_marketplace(self, repository):
        self._idle()
        if not self.get_settings()['network']:raise PermissionError('Enable online access to read a marketplace.')
        from .plugin_marketplace import catalog
        with self._maintenance:return catalog(repository)

    def review_marketplace_plugin(self, repository, revision, name):
        self._idle()
        if not self.get_settings()['network']:raise PermissionError('Enable online access to read a marketplace.')
        from .plugin_marketplace import selection
        with self._maintenance:return selection(repository,revision,name)

    def install_marketplace_plugin(self, repository, revision, name, reviewed_source, confirmed=False):
        self._idle()
        if confirmed is not True:raise ValueError('Review and confirm marketplace plugin installation.')
        if not self.get_settings()['network']:raise PermissionError('Enable online access to install a plugin.')
        from .plugin_marketplace import install
        with self._maintenance:return install(self._store,repository,revision,name,reviewed_source)

    def connect_plugin_account(self, plugin_id, server, confirmed=False):
        self._idle()
        if confirmed is not True:raise ValueError('Confirm opening the account authorization browser.')
        if not self.get_settings()['network']:raise PermissionError('Enable online access to connect an account.')
        from .plugins import Plugins,invoke
        row,config=Plugins(self._store).server(plugin_id,server)
        if 'url' not in config:raise ValueError('Browser account sign-in is available for HTTP MCP servers only.')
        self._begin_setup()
        self._plugin_account_cancel.clear();self._plugin_account_status={'state':'connecting'}
        def run():
            from .tools import ToolRunner
            runner=ToolRunner(self._store,self.get_settings(),'plugin-account',self._plugin_account_cancel,lambda *a:None,lambda *a:False,ROOT)
            self._plugin_account_runner=runner
            try:
                result=invoke(runner,plugin_id,server,interactive=True)
                self._plugin_account_status={'state':'connected','tools':len(result.get('tools',[]))}
            except Exception as error:self._plugin_account_status={'state':'stopped' if self._plugin_account_cancel.is_set() else 'error','error':redact(str(error),Plugins(self._store).credentials())[:1500]}
            finally:
                runner.close();self._plugin_account_runner=None;self._maintenance.release()
        threading.Thread(target=run,daemon=True).start()
        return {'state':'connecting'}

    def plugin_account_status(self):return dict(self._plugin_account_status)

    def cancel_plugin_account(self):
        self._plugin_account_cancel.set()
        if self._plugin_account_runner:self._plugin_account_runner.stop_process()
        return {'ok':True}

    def disconnect_plugin_account(self, plugin_id, server):
        self._idle()
        from .plugins import Plugins
        from .plugin_oauth import key_for
        row=Plugins(self._store).record(plugin_id,False)
        if server not in row['servers']:raise ValueError('Unknown MCP server.')
        self._store.vault.set(key_for(plugin_id,server),'')
        return {'ok':True}

    def set_plugin_enabled(self, plugin_id, enabled):
        with self._agent.lock:
            self._idle()
            from .plugins import Plugins
            Plugins(self._store).set_enabled(plugin_id,enabled)
        return {'ok':True}

    def save_plugin_token(self, plugin_id, token, clear=False):
        with self._agent.lock:
            self._idle()
            from .plugins import Plugins
            Plugins(self._store).record(plugin_id,False)
            if not isinstance(token,str) or len(token)>8000 or type(clear) is not bool: raise ValueError('Invalid plugin credential.')
            if clear or token: self._store.vault.set('plugin:'+plugin_id,'' if clear else token)
        return {'ok':True}

    def remove_plugin(self, plugin_id):
        with self._agent.lock:
            self._idle()
            from .plugins import Plugins
            Plugins(self._store).remove(plugin_id)
        return {'ok':True}

    def save_settings(self, values):
        with self._agent.lock:
            self._idle()
            if not isinstance(values, dict):
                raise ValueError("Impostazioni non valide.")
            s = self._store.snapshot()["settings"]
            for key in ("lang", "provider", "url", "model", "permission", "network", "max_steps", "command_timeout", "github_repo", "auto_update", "vision", "execution_environment", "desktop_scope", "background_schedules", "image_provider", "image_url", "image_model", "context_tokens", "response_tokens"):
                if key in values:
                    s[key] = values[key]
            if s["provider"] not in {"local", "compatible"} or s["permission"] not in {"always", "auto", "full"}:
                raise ValueError("Modalita' non valida.")
            if s["lang"] not in {"en", "it", "es", "fr"}:
                raise ValueError("Supported languages: English, Italian, Spanish, French.")
            if s['execution_environment'] not in {'host', 'sandbox'}:
                raise ValueError('Unknown execution environment.')
            if s['desktop_scope'] not in {'all','sandbox'}: raise ValueError('Unknown desktop scope.')
            if type(s['background_schedules']) is not bool: raise ValueError('Invalid background setting.')
            if type(s['context_tokens']) is not int or not 8192<=s['context_tokens']<=65536: raise ValueError('Context tokens must be between 8192 and 65536.')
            if type(s['response_tokens']) is not int or not 512<=s['response_tokens']<=8192: raise ValueError('Response tokens must be between 512 and 8192.')
            if s['image_provider'] not in {'disabled','managed','local_sd','compatible'}: raise ValueError('Unknown image engine.')
            if not isinstance(s['image_model'],str) or len(s['image_model'])>200: raise ValueError('Invalid image model name.')
            if s['image_provider'] not in {'disabled','managed'}:
                validate_endpoint(s['image_url'],s['network'])
                if s['image_provider']=='local_sd':
                    from urllib.parse import urlsplit
                    if urlsplit(s['image_url']).hostname not in {'localhost','127.0.0.1','::1'}: raise ValueError('Local image engines must use a loopback endpoint.')
                elif not s['image_model'].strip(): raise ValueError('Configure the image model name.')
            if s["permission"] == "full" and values.get("confirm_full") is not True:
                raise ValueError("Conferma esplicita richiesta per accesso completo.")
            if not isinstance(s["network"], bool) or not isinstance(s.get("auto_update", False), bool):
                raise ValueError("Valore rete/aggiornamenti non valido.")
            if not isinstance(s["vision"], bool):
                raise ValueError("Vision must be enabled explicitly for remote vision models.")
            if type(s["max_steps"]) is not int or not 0 <= s["max_steps"] <= 100:
                raise ValueError("Step limit: 0 for unlimited, or 1–100.")
            if type(s["command_timeout"]) is not int or not 0 <= s["command_timeout"] <= 600:
                raise ValueError("Command timeout: 0 to disable, or 1–600 seconds.")
            if not isinstance(s["model"], str) or not s["model"].strip() or len(s["model"]) > 200:
                raise ValueError("Nome modello non valido.")
            validate_endpoint(s["url"], s["network"])
            for field in ('provider_token','github_token','image_token'):
                if field in values and (not isinstance(values[field],str) or len(values[field])>8000): raise ValueError('Invalid credential value.')
                if 'clear_'+field in values and type(values['clear_'+field]) is not bool: raise ValueError('Invalid credential removal setting.')
            # Register only after validation, and only when the user changes this option.
            if s['background_schedules'] != self._store.data['settings'].get('background_schedules',False):
                from .background import configure
                configure(ROOT,self._store,s['background_schedules'])
            for name, field in (("provider", "provider_token"), ("github", "github_token"), ('image','image_token')):
                if values.get("clear_" + field):
                    self._store.vault.set(name, "")
                elif values.get(field):
                    self._store.vault.set(name, values[field])
            with self._store.lock:
                self._store.data["settings"] = s
                self._store.save()
            return {"ok": True}

    def get_sessions(self, query=""):
        query = str(query).lower()[:500]
        rows = []
        for s in self._store.snapshot()["sessions"]:
            history = s.get("history", [])
            if query and query not in (s["title"] + " " + " ".join(str(m.get("content", "")) for m in history)).lower():
                continue
            title = s["title"]
            first = next((m.get("content", "").strip() for m in history if m.get("role") == "user"), "")
            if s.get("auto_title", True) and first and title in {first[:25],first.splitlines()[0][:100]}:
                title = activity_title(first)
            rows.append({**{k: s.get(k) for k in ("id", "workspace", "project_id")}, "title": title,
                         "untitled": s.get("title") == "New activity" and not history})
        return rows

    def get_projects(self):
        return self._store.snapshot()["projects"]

    def set_project_group_open(self,project_id,opened):
        if not isinstance(project_id,str) or type(opened) is not bool:
            raise ValueError('Invalid project group state.')
        with self._store.lock:
            valid={p['id'] for p in self._store.data['projects']}|{''}
            if project_id not in valid: raise ValueError('Project not found.')
            collapsed=set(self._store.data['settings'].get('collapsed_projects',[]))&valid
            if opened: collapsed.discard(project_id)
            else: collapsed.add(project_id)
            self._store.data['settings']['collapsed_projects']=sorted(collapsed)
            self._store.save()
        return {'ok':True}

    def set_language(self, lang):
        if lang not in {"en", "it", "es", "fr"}:
            raise ValueError("Unsupported language.")
        with self._store.lock:
            self._store.data["settings"]["lang"] = lang
            self._store.save()
        return {"ok": True}

    def create_project(self, name, path=""):
        with self._agent.lock:
            self._idle()
            if not isinstance(name, str) or not name.strip() or len(name) > 100:
                raise ValueError("Project name must contain 1–100 characters.")
            project_id = uuid.uuid4().hex
            target = Path(path).resolve() if path else Path(self._store.data["settings"]["workspace"]) / (re.sub(r"[^A-Za-z0-9_-]", "-", name).strip("-")[:50] or "project")
            from .tools import sensitive
            if sensitive(target) or target.is_relative_to(self._store.root.resolve()) or target == Path(target.anchor):
                raise ValueError("Choose a project folder outside protected data.")
            if not path and target.exists():
                target = target.with_name(target.name + "-" + project_id[:6])
            target.mkdir(parents=True, exist_ok=True)
            project = {"id": project_id, "name": name.strip(), "path": str(target)}
            with self._store.lock:
                self._store.data["projects"].append(project)
                self._store.save()
            return project

    def assign_project(self, session_id, project_id):
        with self._agent.lock:
            self._idle()
            project = next((p for p in self._store.data["projects"] if p["id"] == project_id), None)
            if project_id and not project:
                raise ValueError("Project not found.")
            with self._store.lock:
                s = next(s for s in self._store.data["sessions"] if s["id"] == session_id)
                s["project_id"] = project_id
                if project:
                    s["workspace"] = project["path"]
                    s["cwd"] = project["path"]
                self._store.save()
            return {"ok": True}

    def get_session(self, session_id):
        return next((s for s in self._store.snapshot()["sessions"] if s["id"] == session_id), None)

    def resume_task(self, session_id):
        session = self.get_session(session_id)
        if not session or session.get('task', {}).get('state') not in {'interrupted','failed','cancelled','limit','unverified','blocked'}:
            raise ValueError('No interrupted task to resume.')
        return self._agent.start(session_id, 'Resume the previous task: ' + session['task'].get('goal', '')[:15000] + '\nInspect the actual state and uncertain tool outcomes before retrying. Preserve completed work. Use the saved progress and next steps.', policy=session['task'].get('policy'))

    def get_task_overview(self, session_id):
        from .workflows import PROCEDURES
        from .windows_sandbox import executable
        session=self.get_session(session_id) or {}
        return {'task': (self.get_session(session_id) or {}).get('task', {}),
                'procedures': [{'name': n, 'purpose': p[0]} for n, p in PROCEDURES.items()],
                'automations': [j for j in self._store.snapshot()['automations'] if j['session_id'] == session_id],
                'sandbox_available': bool(shutil.which('docker')), 'windows_sandbox_available':bool(executable()),
                'background_enabled':self._store.data['settings'].get('background_schedules',False),
                'artifacts':[{'id':a['id'],'name':Path(a['path']).name} for a in session.get('artifacts',[])][-30:],
                'proposals':[{'id':p['id'],'goal':p['goal'],'state':p['state']} for p in session.get('proposals', [])]}

    def review_proposal(self, session_id, proposal_id):
        self._idle()
        session = self.get_session(session_id)
        if not session: raise ValueError('Choose a chat first.')
        from .tools import ToolRunner
        settings = {**self._store.snapshot()['settings'], 'workspace':session.get('workspace') or self._store.data['settings']['workspace']}
        runner = ToolRunner(self._store, settings, 'proposal-review', threading.Event(), lambda *a:None, lambda *a:False, ROOT, session)
        try:
            changes = runner.tool_proposal_changes('review', proposal_id)
            return {'diff':redact('\n\n'.join(c['diff'] for c in changes)[:60000], [self._store.vault.get(n) for n in ('provider','github','image')])}
        finally: runner.close()

    def preview_generated_image(self,session_id,artifact_id):
        self._idle()
        session=self.get_session(session_id) or {}
        artifact=next((a for a in session.get('artifacts',[]) if a['id']==artifact_id),None)
        if not artifact: raise ValueError('Generated image not found in this chat.')
        from .tools import ToolRunner
        runner=ToolRunner(self._store,{**self._store.data['settings'],'workspace':session.get('workspace') or self._store.data['settings']['workspace']},'preview',threading.Event(),lambda *args:None,lambda *args:False,ROOT,session)
        try:
            import hashlib,base64,io
            from PIL import Image
            path=runner.path(artifact['path'])
            with path.open('rb') as source: pixels=source.read(15_000_001)
            if len(pixels)>15_000_000 or hashlib.sha256(pixels).hexdigest()!=artifact['sha256']:
                raise ValueError('Generated image changed. Inspect the current file before previewing it.')
            with Image.open(io.BytesIO(pixels)) as image:
                if image.width*image.height>8_000_000: raise ValueError('Image dimensions exceed the limit.')
                image.thumbnail((1600,1200)); buffer=io.BytesIO(); image.convert('RGB').save(buffer,format='PNG')
            return {'path':str(path),'data_url':'data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode('ascii')}
        finally: runner.close()

    def prepare_windows_sandbox(self,session_id):
        self._idle()
        session=self.get_session(session_id)
        if not session: raise ValueError('Choose a chat first.')
        # A button is an explicit user request; this method only prepares a file.
        # Opening/operating the VM is an agent tool governed by the permission gate.
        from .windows_sandbox import prepare
        workspace=session.get('workspace') or self._store.data['settings']['workspace']
        return prepare(workspace,self._store.root,threading.Event())

    def review_changes(self, session_id):
        import difflib
        session = self.get_session(session_id)
        if not session: raise ValueError('Choose a chat first.')
        from .tools import ToolRunner
        settings = self._store.snapshot()['settings']
        settings['workspace'] = session.get('workspace') or settings['workspace']
        runner = ToolRunner(self._store, settings, 'review', threading.Event(), lambda *args: None, lambda *args: False, ROOT, session)
        try:
            result = runner.tool_git_status('diff')
            if not result['exit_code']:
                # Include staged and new files, which ordinary git diff omits.
                argv = ['git','--no-optional-locks','-c','core.fsmonitor=false','-c','core.pager=cat']
                staged = runner.run_process([*argv,'diff','--cached','--no-ext-diff','--no-textconv','--','.',':(exclude)*.env*',':(exclude)*.pem',':(exclude)*.key',':(exclude)*state.json',':(exclude)*codex_data.json'],runner.workspace,20)
                untracked = runner.run_process([*argv,'ls-files','--others','--exclude-standard','-z'],runner.workspace,20)
                parts = [result['output'], staged['output']]
                for name in untracked['output'].split('\0')[:30]:
                    if not name: continue
                    try:
                        target = runner.path(name)
                        if not target.is_relative_to(runner.workspace) or not target.is_file() or target.stat().st_size>100000: continue
                        parts.append('\n'.join(difflib.unified_diff([],target.read_text(encoding='utf-8').splitlines(),fromfile='/dev/null',tofile=name)))
                    except (OSError, ValueError, PermissionError, UnicodeError): continue
                if runner.sandbox.root:
                    parts.extend(row['diff'] for row in runner.sandbox.changes())
                return {'source': 'Git and sandbox', 'diff': redact('\n'.join(parts)[:60000], [self._store.vault.get('provider'), self._store.vault.get('github'), self._store.vault.get('image')])}
            rows = []
            for item in self.get_backups()[:30]:
                try:
                    target = runner.path(item['path'])
                    if not target.is_relative_to(runner.workspace) or target.stat().st_size > 500000: continue
                    before = (self._store.root/'backups'/item['id']).read_text(encoding='utf-8').splitlines()
                    after = target.read_text(encoding='utf-8').splitlines()
                    rows.extend(difflib.unified_diff(before, after, fromfile=item['path']+' (backup)', tofile=item['path']))
                except (OSError, ValueError, UnicodeError): continue
            if runner.sandbox.root: rows.extend(row['diff'] for row in runner.sandbox.changes())
            return {'source':'Backups and sandbox', 'diff':redact('\n'.join(rows)[:30000], [self._store.vault.get('provider'), self._store.vault.get('github'), self._store.vault.get('image')])}
        finally: runner.close()

    def save_automation(self, session_id, prompt, interval_hours=24):
        import time
        self._idle()
        if not self.get_session(session_id) or not isinstance(prompt,str) or not prompt.strip() or len(prompt)>3000:
            raise ValueError('Choose a chat and provide a concise task.')
        if type(interval_hours) is not int or not 1 <= interval_hours <= 168:
            raise ValueError('Choose an interval between 1 and 168 hours.')
        with self._store.lock:
            if len(self._store.data['automations']) >= 20: raise ValueError('Maximum 20 local schedules.')
            job = {'id':uuid.uuid4().hex,'session_id':session_id,'prompt':redact(prompt, [self._store.vault.get('provider'), self._store.vault.get('github'), self._store.vault.get('image')]), 'interval_hours':interval_hours,
                   'next_run':time.time()+interval_hours*3600,'enabled':True,'last_state':'pending'}
            self._store.data['automations'].append(job)
            self._store.save()
        return job

    def toggle_automation(self, job_id):
        import time
        with self._store.lock:
            job = next((j for j in self._store.data['automations'] if j['id']==job_id),None)
            if not job: raise ValueError('Schedule not found.')
            job['enabled'] = not job['enabled']
            job['next_run'] = time.time()+job['interval_hours']*3600
            self._store.save()
        return {'ok':True}

    def remove_automation(self, job_id):
        with self._store.lock:
            self._store.data['automations'] = [j for j in self._store.data['automations'] if j['id'] != job_id]
            self._store.save()
        return {'ok':True}

    def create_session(self):
        with self._agent.lock:
            self._idle()
            with self._store.lock:
                session = {"id": uuid.uuid4().hex, "title": "New activity", "history": [], "plan": [],
                           "workspace": self._store.data["settings"]["workspace"], "project_id": "", "auto_title": True}
                self._store.data["sessions"].insert(0, session)
                self._store.save()
            return session["id"]

    def rename_session(self, session_id, title):
        with self._agent.lock:
            self._idle()
            with self._store.lock:
                s = next(s for s in self._store.data["sessions"] if s["id"] == session_id)
                s["title"] = str(title).strip()[:100] or "Attivita'"
                s["auto_title"] = False
                self._store.save()
            return {"ok": True}

    def delete_session(self, session_id):
        with self._agent.lock:
            self._idle()
            with self._store.lock:
                self._store.data["sessions"] = [s for s in self._store.data["sessions"] if s["id"] != session_id]
                self._store.data['automations'] = [j for j in self._store.data['automations'] if j['session_id'] != session_id]
                self._store.save()
            if self._agent.current_session == session_id:
                self._agent.current_session = ""
                self._agent.events = type(self._agent.events)((e for e in self._agent.events if e.get("session_id") != session_id), maxlen=2000)
            return {"ok": True}

    def choose_workspace(self, session_id):
        self._idle()
        if not self._window:
            return None
        import webview
        selected = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return self.set_workspace(session_id, selected[0]) if selected else None

    def set_workspace(self, session_id, path):
        with self._agent.lock:
            self._idle()
            workspace = Path(path).resolve()
            if not workspace.is_dir() or workspace == Path(workspace.anchor):
                raise ValueError("Scegli una cartella di progetto, non la radice del disco.")
            if workspace == self._store.root.resolve():
                raise ValueError("La cartella dati privata non puo' essere il progetto.")
            with self._store.lock:
                s = next(s for s in self._store.data["sessions"] if s["id"] == session_id)
                s["workspace"] = str(workspace)
                s["cwd"] = str(workspace)
                self._store.data["settings"]["workspace"] = str(workspace)
                self._store.save()
            return str(workspace)

    def attach_file(self, session_id):
        if not self._window:
            return None
        import webview
        files = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
        if not files:
            return None
        from .tools import sensitive
        session = self.get_session(session_id)
        if not session or len(files) > 10:
            raise ValueError("Choose a chat and at most ten files.")
        workspace = Path(session["workspace"]).resolve()
        folder = workspace / ".veyq-attachments"
        if sensitive(folder.resolve()) or folder.is_symlink() or not folder.resolve().is_relative_to(workspace):
            raise ValueError("Attachment folder is protected or points outside the project.")
        result = []
        for value in files:
            source = Path(value).resolve()
            if sensitive(source) or source.is_relative_to(self._store.root.resolve()) or not source.is_file():
                raise ValueError("Protected attachment: use an example without secrets.")
            if source.stat().st_size > 20_000_000:
                raise ValueError("Attachment limit is 20 MB per file.")
            folder.mkdir(exist_ok=True)
            destination = folder / (uuid.uuid4().hex[:8] + "-" + source.name)
            shutil.copy2(source, destination)
            record = {"name": source.name, "path": str(destination), "bytes": destination.stat().st_size}
            if source.stat().st_size < 1_000_000:
                try:
                    text = source.read_text(encoding="utf-8")
                    if "\x00" not in text:
                        record["text"] = text[:8000]
                        record["truncated"] = len(text) > 8000
                except (UnicodeError, OSError):
                    pass
            result.append(record)
        return result

    def link_folder(self):
        if not self._window:
            return None
        import webview
        selected = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not selected:
            return None
        target = Path(selected[0]).resolve()
        from .tools import sensitive
        if sensitive(target) or target.is_relative_to(self._store.root.resolve()) or target == Path(target.anchor):
            raise ValueError("Choose a folder outside protected data.")
        return str(target)

    def start_run(self, session_id, text):
        with self._agent.lock:
            self._idle()
            return self._agent.start(session_id, text)

    def follow_up(self, session_id, text):
        return self._agent.follow_up(session_id, text)

    def answer_question(self, question_id, answer):
        return self._agent.resolve_question(question_id, answer)

    def regenerate(self, session_id, history_index, confirmed=False):
        with self._agent.lock:
            self._idle()
            if not confirmed or type(history_index) is not int:
                raise ValueError("Confirm regeneration; later chat messages will be removed, executed actions stay applied.")
            session = next(s for s in self._store.data["sessions"] if s["id"] == session_id)
            if not 0 <= history_index < len(session["history"]) or session["history"][history_index]["role"] != "assistant":
                raise ValueError("Choose an assistant response.")
            with self._store.lock:
                session["history"] = session["history"][:history_index]
                self._store.save()
            return self._agent.start(session_id, "Regenerate the response using existing tool evidence. Completed side effects remain applied; inspect before repeating any action.")

    def browse_project(self, session_id, path="."):
        self._idle()
        return self._agent.manual(session_id, "list_dir", {"path": path})

    def preview_project_file(self, session_id, path, start_line=1):
        self._idle()
        return self._agent.manual(session_id, "read_file", {"path": path, "start_line": start_line, "end_line": start_line + 299})

    def get_events(self, after=0):
        return {**self._agent.poll(after), "model_busy": self._maintenance.locked()}

    def voice_status(self):
        result=self._voice.snapshot()
        if result.get('text'):
            result['text']=redact(result['text'],[self._store.vault.get('provider'),self._store.vault.get('github'),self._store.vault.get('image')])
        return result

    def prepare_voice(self, confirmed=False):
        if confirmed is not True: raise ValueError('Confirm the speech-model download first.')
        self._idle()
        return self._voice.start('prepare')

    def start_voice(self):
        if self._maintenance.locked():raise RuntimeError('Wait for the download to finish.')
        return self._voice.start('record')

    def stop_voice(self):
        return self._voice.stop()

    def cancel_voice(self):
        return self._voice.cancel()

    def stop_run(self):
        return self._agent.stop()

    def resolve_approval(self, approval_id, allow):
        return self._agent.resolve(approval_id, allow)

    def get_memory(self):
        return self._store.snapshot().get("memory", "")

    def clear_memory(self):
        self._idle()
        with self._store.lock:
            self._store.data["memory"] = ""
            self._store.save()
        return {"ok": True}

    def save_memory(self, text):
        self._idle()
        if not isinstance(text, str) or len(text) > 10000:
            raise ValueError("Memory limit: 10000 characters.")
        if redact(text, [self._store.vault.get("provider"), self._store.vault.get("github"), self._store.vault.get("image")]) != text:
            raise ValueError("Do not store credentials in memory.")
        with self._store.lock:
            self._store.data["memory"] = text
            self._store.save()
        return {"ok": True}

    def open_external(self, url):
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or len(url) > 4000:
            raise ValueError("Only public HTTPS links can be opened.")
        from .network import public_target
        public_target(url)
        import webbrowser
        webbrowser.open(url)
        return {"ok": True}

    def open_provider_account(self):
        from urllib.parse import urlsplit
        s = self.get_settings()
        if s["provider"] == "local":
            return self.open_external("https://ollama.com")
        p = urlsplit(s["url"])
        return self.open_external("https://" + p.netloc)

    def setup_local_engine(self, confirmed=False):
        self._idle()
        if confirmed is not True:
            raise ValueError("Confirm installation of the optional local engine.")
        from .setup import setup_engine
        with self._maintenance:
            return setup_engine(self._store.root)

    def image_engine_status(self):
        from .image_setup import ready
        with self._image_status_lock:
            return {**self._image_status,'ready':ready(self._store.root)}

    def model_check_status(self):
        with self._image_status_lock: return dict(self._model_check_status)

    def cancel_model_check(self):
        self._model_check_cancel.set()
        if self._model_check_client: self._model_check_client.close()
        return {'ok':True}

    def test_model_actions(self, model, confirmed=False):
        self._idle()
        if confirmed is not True: raise ValueError('Confirm the controlled model test.')
        if not isinstance(model,str) or not model.strip() or len(model)>200: raise ValueError('Choose a model.')
        self._begin_setup()
        self._model_check_cancel.clear()
        settings={**self._store.snapshot()['settings'],'model':model}
        with self._image_status_lock: self._model_check_status={'state':'running','model':model}
        def active(client): self._model_check_client=client
        def work():
            try:
                from .model_check import check
                result=check(settings,self._store.vault.get('provider'),self._model_check_cancel,active)
                if self._model_check_cancel.is_set(): raise RuntimeError('Model test stopped.')
                import time
                result.update(model=model,checked_at=time.time(),provider=settings['provider'])
                with self._store.lock:
                    key=settings['provider']+'|'+settings['url']+'|'+model
                    self._store.data.setdefault('model_checks',{})[key]=result
                    self._store.save()
                with self._image_status_lock: self._model_check_status=result
            except Exception as error:
                with self._image_status_lock: self._model_check_status={'state':'stopped' if self._model_check_cancel.is_set() else 'error','error':redact(str(error),[self._store.vault.get('provider')])[:500],'model':model}
            finally: self._maintenance.release()
        threading.Thread(target=work,daemon=True).start()
        return self.model_check_status()

    def cancel_image_setup(self):
        self._image_cancel.set()
        return {'ok':True}

    def prepare_image_engine(self, confirmed=False):
        self._idle()
        if confirmed is not True: raise ValueError('Confirm downloading the optional local image engine and model (approximately 4.3 GB).')
        if not self._store.snapshot()['settings']['network']: raise PermissionError('Enable online access to download the image engine.')
        self._begin_setup()
        self._image_cancel.clear()
        with self._image_status_lock: self._image_status={'state':'downloading'}
        def progress(value):
            with self._image_status_lock: self._image_status={'state':'downloading',**value}
        def work():
            try:
                from .image_setup import prepare
                result=prepare(self._store.root,self._image_cancel,progress)
                if self._image_cancel.is_set():raise RuntimeError('Image setup stopped.')
                with self._image_status_lock: self._image_status={'state':'completed',**result}
            except Exception as error:
                with self._image_status_lock: self._image_status={'state':'stopped' if self._image_cancel.is_set() else 'error','error':str(error)[:500]}
            finally: self._maintenance.release()
        threading.Thread(target=work,daemon=True).start()
        return self.image_engine_status()

    def test_image_engine(self, confirmed=False):
        self._idle()
        if confirmed is not True: raise ValueError('Confirm generating one test image with the configured provider.')
        settings=self._store.snapshot()['settings']
        if settings['image_provider']=='disabled': raise ValueError('Select and save an image engine first.')
        self._begin_setup()
        self._image_cancel.clear()
        with self._image_status_lock: self._image_status={'state':'testing'}
        def work():
            try:
                from .image_generation import generate
                import base64
                size=256 if settings['image_provider']=='managed' else 1024
                pixels,width,height=generate(settings,self._store.vault.get('image'),'A small red robot standing in a green garden, watercolor illustration',size,size,self._image_cancel,data_root=self._store.root)
                if self._image_cancel.is_set(): raise RuntimeError('Image generation stopped.')
                with self._image_status_lock: self._image_status={'state':'tested','width':width,'height':height,'data_url':'data:image/png;base64,'+base64.b64encode(pixels).decode('ascii')}
            except Exception as error:
                with self._image_status_lock: self._image_status={'state':'stopped' if self._image_cancel.is_set() else 'error','error':redact(str(error),[self._store.vault.get('image')])[:500]}
            finally: self._maintenance.release()
        threading.Thread(target=work,daemon=True).start()
        return self.image_engine_status()

    def finish_setup(self):
        with self._store.lock:
            self._store.data["settings"]["setup_completed"] = True
            self._store.save()
        return {"ok": True}

    def export_session(self, session_id):
        session = self.get_session(session_id)
        if not self._window or not session:
            return None
        import webview
        files = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename="veyq-chat.json")
        if files:
            Path(files[0]).write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(files[0])
        return None

    def get_backups(self):
        folder = self._store.root / "backups"
        return sorted([{**json.loads(p.read_text(encoding="utf-8")), "id": p.stem} for p in folder.glob("*.json")], key=lambda b: b["time"], reverse=True)[:100]

    def restore_backup(self, backup_id, confirmed=False):
        self._idle()
        if not confirmed or not isinstance(backup_id, str) or not re.fullmatch(r"[a-f0-9]{32}", backup_id):
            raise ValueError("Conferma di ripristino richiesta.")
        folder = self._store.root / "backups"
        record = json.loads((folder / (backup_id + ".json")).read_text(encoding="utf-8"))
        target = Path(record["path"]).resolve()
        from .tools import ToolRunner, sensitive
        if target.is_relative_to(self._store.root.resolve()) or sensitive(target):
            raise ValueError("Ripristino in dati privati bloccato.")
        if target.exists():
            runner = ToolRunner(self._store, {**DEFAULTS, "workspace": str(target.parent)}, "restore", threading.Event(), lambda *a: None, lambda *a: False, ROOT, {})
            runner.backup(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(folder / backup_id, target)
        self._store.audit("restore", "restore_backup", "completed", backup_id)
        return {"ok": True, "path": str(target)}

    def get_models(self):
        s = self.get_settings()
        base = validate_endpoint(s["url"], s["network"])
        token = self._store.vault.get("provider")
        client = requests.Session()
        client.trust_env = False
        try:
            r = client.get(base + ("/api/tags" if s["provider"] == "local" else "/models"),
                           headers={"Authorization": "Bearer " + token} if token else {}, timeout=(5, 10), allow_redirects=False)
            r.raise_for_status()
            key = "models" if s["provider"] == "local" else "data"
            records = r.json().get(key, [])
            return {"ok": True, "models": [m.get("name", m.get("id")) for m in records], "details": records}
        except Exception as e:
            return {"ok": False, "error": redact(str(e), [token])}
        finally:
            client.close()

    def get_model_catalog(self):
        from .models import catalog, hardware
        return {"models": catalog(), "hardware": hardware(self._store.data["settings"]["workspace"])}

    def get_model_info(self, name):
        from .models import estimate, hardware
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", name):
            raise ValueError("Choose a model first.")
        info = estimate(name)
        s = self.get_settings()
        info["hardware"] = hardware(s["workspace"])
        key=s['provider']+'|'+s['url']+'|'+name
        info['autonomy_check']=self._store.snapshot().get('model_checks',{}).get(key)
        if s["provider"] != "local":
            info = {**estimate("unknown"), "name": name, "hardware": info["hardware"], 'autonomy_check':info['autonomy_check'], "metadata_origin": "Remote provider: local requirements do not apply"}
            return info
        client = requests.Session()
        client.trust_env = False
        try:
            token = self._store.vault.get("provider")
            response = client.post(validate_endpoint(s["url"], s["network"]) + "/api/show", json={"model": name}, headers={"Authorization": "Bearer " + token} if token else {}, timeout=(5, 10), allow_redirects=False)
            if response.status_code == 200:
                data = response.json()
                info["installed"] = True
                info["metadata_origin"] = "Installed engine metadata"
                if isinstance(data.get("capabilities"), list):
                    info["installed_capabilities"] = data["capabilities"]
                    info["tools"] = "tools" in data["capabilities"]
                    info["vision"] = "vision" in data["capabilities"]
                details = data.get("details", {})
                info["parameters"] = details.get("parameter_size") or info.get("parameters")
                info["quantization"] = details.get("quantization_level") or info.get("quantization")
                info["family"] = details.get("family")
                limits = [v for k, v in data.get("model_info", {}).items() if k.endswith(".context_length") and isinstance(v, int)]
                if limits: info["context_tokens"] = max(limits)
                # A tag can be customized locally; its capabilities override catalog expectations.
                tags = self.get_models()
                record = next((m for m in tags.get("details", []) if m.get("name") == name), {})
                if record.get("size"):
                    info["installed_size_gb"] = round(record["size"] / 1e9, 2)
                    if not info.get("source"):
                        actual = estimate(name, record["size"])
                        for k in ["download_gb", "ram_min_gb", "ram_recommended_gb", "vram_min_gb", "vram_recommended_gb"]: info[k] = actual[k]
        except Exception:
            pass
        finally:
            client.close()
        return info

    def open_model_source(self, name):
        from .models import catalog
        model = next((m for m in catalog() if m["name"] == name), None)
        if not model:
            raise ValueError("Choose a catalog model.")
        import webbrowser
        webbrowser.open(model["source"])
        return {"ok": True}

    def model_action(self, model, action, confirmed=False):
        self._idle()
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
            raise ValueError("Enter a valid model name.")
        if not confirmed or action not in {"pull", "delete"}:
            raise ValueError("Conferma richiesta per la gestione dei modelli.")
        if not self._maintenance.acquire(blocking=False):
            raise RuntimeError("Manutenzione gia' in corso.")
        client = requests.Session()
        client.trust_env = False
        self._model_cancel.clear()
        try:
            s = self.get_settings()
            if s["provider"] != "local":
                raise ValueError("Gestione download disponibile solo per il motore locale.")
            # The user's explicit model-download confirmation is independent of
            # the agent's network permission, like the app updater and installer.
            base = validate_endpoint(s["url"], s["network"])
            if action == "pull" and base in {"http://localhost:11434", "http://127.0.0.1:11434"} and os.name == "nt":
                from .setup import engine_available, setup_engine
                if not engine_available():
                    self._agent.emit("model", {"status": "Setting up the local engine…"})
                    setup_engine(self._store.root)
            token = self._store.vault.get("provider")
            headers = {"Authorization": "Bearer " + token} if token else {}
            if action == "delete":
                with client.delete(base + "/api/delete", headers=headers, json={"model": model}, timeout=(5, 30), allow_redirects=False) as r:
                    r.raise_for_status()
            else:
                with client.post(base + "/api/pull", headers=headers, json={"model": model, "stream": True}, stream=True, timeout=(10, 90), allow_redirects=False) as r:
                    self._model_response = r
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if self._model_cancel.is_set():
                            return {"ok": False, "cancelled": True}
                        if line:
                            chunk = json.loads(line)
                            if "error" in chunk:
                                raise RuntimeError(chunk["error"])
                            self._agent.emit("model", {"status": chunk.get("status"), "completed": chunk.get("completed"), "total": chunk.get("total")})
            return {"ok": True}
        except Exception:
            if self._model_cancel.is_set():
                return {"ok": False, "cancelled": True}
            raise
        finally:
            self._model_response = None
            client.close()
            self._maintenance.release()

    def cancel_model_action(self):
        self._model_cancel.set()
        if self._model_response is not None:
            from .engine import interrupt_response
            interrupt_response(self._model_response)
        return {"ok": True}

    def check_updates(self):
        self._idle()
        from .updater import Updater
        with self._maintenance:
            return Updater(ROOT, self._store).check_and_stage()

    def apply_update(self):
        self._idle()
        from .updater import Updater
        result = Updater(ROOT, self._store).launch_apply()
        if result.get("ok") and self._window:
            self._window.destroy()
        return result


def main():
    from .runtime import ProfileLease,profile_root
    if '--background' in sys.argv:
        from .background import run
        data_root=sys.argv[sys.argv.index('--background-profile')+1] if '--background-profile' in sys.argv else None
        return run(ROOT,data_root)
    # Acquire before Store loads, recovers or saves any shared state.
    with ProfileLease(profile_root(ROOT)).acquire(foreground=True,timeout=30) as lease:
        return foreground_main(lease.root)


def foreground_main(data_dir):
    store = Store(root=data_dir, legacy=ROOT / "codex_data.json")
    if store.data['settings'].get('background_schedules') and os.name=='nt' and '--self-check' not in sys.argv:
        try:
            marker=store.root/'background-task.json'
            record=json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
            if record.get('python')!=str(Path(sys.executable).with_name('pythonw.exe')):
                from .background import configure
                configure(ROOT,store,True)
        except Exception:
            from .storage import atomic_json
            atomic_json(store.root/'background-status.json',{'state':'setup_failed'})
    if "--self-check" in sys.argv:
        api = DesktopAPI(store)
        print(json.dumps({"version": "4.5.0", "models": api.get_models(), "data_dir": str(store.root)}))
        return
    if "--install" in sys.argv:
        from .updater import enable_updates
        enable_updates(ROOT, store)
        from .storage import atomic_json
        atomic_json(ROOT / "profile.json", {"data_dir": str(store.root)})
        return
    import webview
    from .ui import document
    api = DesktopAPI(store)
    window = webview.create_window("Veynuq", html=document(ROOT), js_api=api,
                                  width=1440, height=940, min_size=(900, 650), resizable=True, text_select=True)
    api._window = window
    import faulthandler
    window.events.loaded += faulthandler.cancel_dump_traceback_later
    window.events.loaded += api._scheduler.start
    def shutdown():
        # pywebview puts callback returns in a set: API dictionaries cannot
        # be returned from an event handler. Closing must also stop downloads.
        api._agent.stop()
        api.cancel_model_action()
        api._scheduler.stop_event.set()
        api._voice.close()
        api.cancel_image_setup()
        api.cancel_model_check()
        api.cancel_plugin_account()
    window.events.closing += shutdown
    window.events.closed += shutdown
    webview.start(debug=False, icon=str(ROOT / "assets" / "brand" / "veynuq-dark.ico"))
