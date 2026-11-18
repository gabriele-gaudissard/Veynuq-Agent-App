"""Parallel coding on filtered copies; apply through conflict-checked backups."""
import concurrent.futures
import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

ALLOWED = {'read_file', 'list_dir', 'search_files', 'write_file', 'edit_file', 'make_dir',
           'move_file', 'delete_file', 'exec_cmd', 'sandbox_changes', 'update_plan', 'checkpoint_task'}


def prepare(runner, task):
    from .tools import SKIP, sensitive, linklike
    from .storage import atomic_json, redact
    key = uuid.uuid4().hex
    folder = runner.store.root / 'proposals' / key
    if folder.parent.exists() and linklike(folder.parent): raise PermissionError('Linked proposal directory is refused.')
    source = folder / 'source'
    source.mkdir(parents=True)
    baseline = {}
    total = 0
    try:
        for directory, dirs, files in os.walk(runner.workspace, followlinks=False):
            if runner.cancel.is_set(): raise RuntimeError('Coding work stopped.')
            dirs[:] = [n for n in dirs if n not in SKIP and not sensitive(Path(directory, n)) and not linklike(Path(directory, n)) and not Path(directory, n).resolve().is_relative_to(runner.store.root)]
            for name in files:
                path = Path(directory, name)
                if sensitive(path) or linklike(path) or path.resolve().is_relative_to(runner.store.root): continue
                total += path.stat().st_size
                if total > 20_000_000 or len(baseline) >= 2000 or path.stat().st_size > 2_000_000:
                    raise ValueError('Choose a source project below 20 MB / 2000 files / 2 MB per file.')
                rel = path.relative_to(runner.workspace).as_posix()
                target = source / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                pixels = path.read_bytes()
                if runner.path(str(path)) != path.resolve(): raise PermissionError('Source path changed during copying.')
                target.write_bytes(pixels)
                baseline[rel] = hashlib.sha256(pixels).hexdigest()
        atomic_json(folder / 'baseline.json', baseline)
        safe = redact(task, [runner.store.vault.get(n) for n in ('provider', 'github', 'image')])
        record = {'id':key, 'goal':safe, 'workspace':str(runner.workspace), 'state':'running'}
        with runner.store.lock:
            runner.session.setdefault('proposals', []).append(record)
            runner.store.save()
        return record
    except Exception:
        # Only this generated directory under the verified profile is removed.
        if folder.resolve().parent != (runner.store.root / 'proposals').resolve(): raise
        shutil.rmtree(folder)
        raise


def sandbox(runner, key):
    from .sandbox import Sandbox
    from .tools import linklike
    if not re.fullmatch(r'[0-9a-f]{32}', key): raise ValueError('Invalid coding proposal.')
    record = next((r for r in runner.session.get('proposals', []) if r['id'] == key), None)
    if not record or record['workspace'] != str(runner.workspace): raise PermissionError('Proposal belongs to another chat or project.')
    if record['state'] == 'running': raise RuntimeError('Wait for the coding worker to finish.')
    folder = runner.store.root / 'proposals' / key
    if linklike(folder.parent) or linklike(folder) or linklike(folder / 'source'): raise PermissionError('Linked proposal is refused.')
    data = folder / 'baseline.json'
    if data.stat().st_size > 500000: raise ValueError('Proposal baseline is oversized.')
    baseline = json.loads(data.read_text(encoding='utf-8'))
    if not isinstance(baseline, dict) or any(not isinstance(p, str) or not p or Path(p).is_absolute() or '..' in Path(p).parts or not isinstance(h, str) or not re.fullmatch(r'[0-9a-f]{64}', h) for p, h in baseline.items()):
        raise ValueError('Invalid proposal baseline.')
    result = Sandbox(runner)
    result.root, result.baseline = folder / 'source', baseline
    result.save = lambda: None  # Applying a proposal must not replace the command sandbox.
    return result, record


def changes(runner, key, apply=False):
    from .storage import atomic_json
    proposal, record = sandbox(runner, key)
    if not apply: return proposal.changes()
    result = proposal.apply()
    atomic_json(proposal.root.parent / 'baseline.json', proposal.baseline)
    with runner.store.lock:
        record['state'] = 'applied'
        runner.store.save()
    return result


def delegate(runner, tasks):
    from .engine import Agent
    from .storage import Store, redact
    from .parallel import worker_slot
    limit = runner.settings.get("max_subagents", 3)
    if type(limit) is not int or not 0 <= limit <= 8:
        raise ValueError("Invalid subagent limit.")
    if not limit:
        raise PermissionError("Subagents are disabled in Settings.")
    if not 1 <= len(tasks) <= 8 or any(not isinstance(t, str) or not t.strip() or len(t) > 4000 for t in tasks):
        raise ValueError('Provide 1–8 independent coding tasks, each below 4000 characters.')
    if len(runner.session.get('proposals', [])) + len(tasks) > 30:
        raise ValueError('This chat already has 30 coding proposals. Review the existing work first.')
    records = []
    try:
        for task in tasks: records.append(prepare(runner, task))
    except Exception:
        with runner.store.lock:
            for record in records: record['state'] = 'interrupted'
            runner.store.save()
        raise
    secrets = [runner.store.vault.get(n) for n in ('provider', 'github', 'image')]

    def worker(record):
        folder = runner.store.root / 'proposals' / record['id']
        store = Store(folder / 'profile')
        store.vault.get = lambda name: secrets[0] if name == 'provider' else ''
        store.data['settings'] = {**copy.deepcopy(runner.settings), 'workspace':str(folder / 'source'),
            'permission':'full', 'execution_environment':'sandbox', 'auto_update':False,
            'max_steps':24, 'command_timeout':30, '_worker_tools':sorted(ALLOWED)}
        session = {'id':uuid.uuid4().hex, 'history':[], 'workspace':str(folder / 'source')}
        store.data['sessions'] = [session]
        store.save()
        agent = Agent(store, runner.app_root)
        runner.child_agents.append(agent)
        deadline = time.monotonic() + 300
        try:
            agent.start(session['id'], record['goal'])
            while agent.busy:
                if runner.cancel.is_set() or time.monotonic() >= deadline:
                    agent.stop()
                agent.worker.join(.2)
            summary = next((m.get('content', '') for m in reversed(session['history']) if m.get('role') == 'assistant' and not m.get('tool_calls')), '')
            record.update(state=agent.state, answer=redact(summary[:4000], secrets))
        except Exception as error:
            record.update(state='failed', answer=redact(str(error)[:1000], secrets))
        finally:
            agent.stop()
            runner.child_agents.remove(agent)
            with runner.store.lock: runner.store.save()
        return {**record, 'changes':changes(runner, record['id']), 'host_project_unchanged':True,
            'command_environment':'offline Docker only; unavailable engines fail closed'}

    runner.emit('notice', {'text':'Parallel coding started on separate project copies.'})
    def guarded(task):
        runner.emit("subagent", {"id": task["id"], "kind": "coding", "state": "queued"})
        try:
            with worker_slot(runner):
                runner.emit("subagent", {"id": task["id"], "kind": "coding", "state": "running"})
                result = worker(task)
        except Exception as error:
            with runner.store.lock:
                task.update(state="cancelled" if runner.cancel.is_set() else "failed",
                            answer=redact(str(error)[:1000], secrets))
                runner.store.save()
            result = {**task, "host_project_unchanged": True}
        runner.emit("subagent", {"id": task["id"], "kind": "coding", "state": task["state"]})
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(limit, len(tasks))) as executor:
        return list(executor.map(guarded, records))
