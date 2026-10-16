"""Persist observations still needed for each actual mutation, across turns."""
from pathlib import Path


class Verification:
    def __init__(self, runner):
        self.runner = runner
        self.pending = list(runner.session.get('task', {}).get('verification', []))

    def path(self, value):
        return str(self.runner.path(value))

    def add(self, kind, target):
        entry = {'kind': kind, 'target': target}
        if entry not in self.pending: self.pending.append(entry)

    def observations(self, enabled):
        """Bounded read-only recovery for known targets; normal tool gates apply."""
        proposed = []
        for entry in self.pending:
            kind, target = entry['kind'], entry['target']
            if kind == 'file': name, args = 'read_file', {'path': target, 'start_line': 1, 'end_line': 200}
            elif kind in {'directory', 'missing'}:
                name, args = 'list_dir', {'path': target if kind == 'directory' else str(Path(target).parent)}
            elif kind == 'project':
                cwd = self.runner.cwd
                name, args = 'list_dir', {'path': str(cwd if cwd.is_relative_to(self.runner.workspace) else self.runner.workspace)}
            elif kind == 'window': name, args = 'computer_inspect', {'window_id': target}
            elif kind == 'desktop': name, args = 'computer_windows', {}
            else: continue  # Remote mutations require the model to select the verification endpoint.
            pair = (name, args)
            if name in enabled and pair not in proposed: proposed.append(pair)
            if len(proposed) == 3: break
        return proposed

    def observe(self, name, args, result):
        if not result.get('ok'): return
        value = result.get('result', {})
        # Inspections discharge only relevant observations, never unrelated work.
        if name in {'read_file', 'read_document', 'view_image', 'inspect_binary'}:
            target = self.path(args['path'])
            self.pending = [p for p in self.pending if not (p['kind'] == 'file' and p['target'] == target)]
        elif name == 'list_dir':
            target = self.path(args.get('path', '.'))
            def confirmed(p):
                if p['kind'] == 'directory': return p['target'] == target
                if p['kind'] == 'missing' and str(Path(p['target']).parent) == target:
                    return not any(row['name'] == Path(p['target']).name for row in value)
                return False
            self.pending = [p for p in self.pending if not confirmed(p)]
        elif name in {'computer_inspect', 'computer_wait'}:
            self.pending = [p for p in self.pending if not (p['kind'] == 'window' and p['target'] == args['window_id'])]
        elif name == 'github' and args.get('method') == 'GET':
            target = (args.get('repository') or self.runner.settings.get('github_repo', '')) + '/' + args['endpoint'].split('?')[0]
            self.pending = [p for p in self.pending if not (p['kind'] == 'github' and p['target'] == target)]
        # Arbitrary commands have unknown side effects: a relevant project read
        # is evidence to assess, not proof of every command's semantic outcome.
        relevant = name in {'git_status', 'sandbox_changes'} or name in {'read_file', 'read_document', 'list_dir', 'search_files'} and Path(self.path(args.get('path', '.'))).is_relative_to(self.runner.workspace)
        if relevant:
            self.pending = [p for p in self.pending if p['kind'] != 'project']
        if name in {'write_file', 'edit_file', 'download_file'}:
            self.add('file', self.path(args['path']))
        elif name == 'make_dir': self.add('directory', self.path(args['path']))
        elif name == 'delete_file': self.add('missing', self.path(args['path']))
        elif name == 'move_file':
            self.add('missing', self.path(args['path']))
            self.add('file', self.path(args['destination']))
        elif name == 'restore_backup': self.add('file', value['restored'])
        elif name in {'computer_action', 'computer_pointer', 'computer_sandbox_type'}:
            snapshot = self.runner.computer.snapshots.get(args['snapshot_id'])
            window = value.get('window_id') if isinstance(value, dict) else None
            if snapshot: window = snapshot['window_id']
            self.add('window' if window is not None else 'desktop', window)
        elif name == 'computer_windows':
            self.pending = [p for p in self.pending if p['kind'] != 'desktop']
        elif name == 'github' and args.get('method') != 'GET':
            self.add('github', (args.get('repository') or self.runner.settings.get('github_repo', '')) + '/' + args['endpoint'].split('?')[0])
        elif name in {'sandbox_changes', 'proposal_changes'} and args.get('action') == 'apply':
            for row in value.get('changed_files', []):
                self.add('missing' if row['deleted'] else 'file', self.path(row['path']))
        elif name in {'exec_cmd', 'clone_repository', 'windows_sandbox'} or name == 'git_worktree' and args.get('action') == 'create':
            self.add('project', str(self.runner.workspace))
        with self.runner.store.lock:
            self.runner.session.setdefault('task', {})['verification'] = self.pending
            self.runner.store.save()
