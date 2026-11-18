"""Bounded chat activities and shared execution resources."""
import copy
import threading
from collections import deque
from contextlib import contextmanager


@contextmanager
def worker_slot(runner):
    limit = runner.settings.get("max_subagents", 3)
    if type(limit) is not int or not 0 <= limit <= 8:
        raise ValueError("Invalid subagent limit.")
    if not limit:
        raise PermissionError("Subagents are disabled in Settings.")
    with runner.store.lock:
        pool = getattr(runner.store, "_subagent_pool", None)
        if pool is None:
            pool = runner.store._subagent_pool = threading.BoundedSemaphore(limit)
    while not pool.acquire(timeout=.2):
        if runner.cancel.is_set():
            raise RuntimeError("Subagent stopped before starting.")
    try:
        if runner.cancel.is_set():
            raise RuntimeError("Subagent stopped before starting.")
        yield
    finally:
        pool.release()


@contextmanager
def host_action(runner, name):
    from .tools import WRITE
    protected = WRITE | {"exec_cmd", "clone_repository", "restore_backup", "git_worktree",
                         "sandbox_changes", "proposal_changes", "keyboard_layout",
                         "windows_sandbox", "tool_setup", "plugin_marketplace", "plugin_call", "plugin_extension"}
    if name not in protected and not name.startswith("computer_"):
        yield
        return
    with runner.store.lock:
        lock = getattr(runner.store, "_host_action_lock", None)
        if lock is None:
            lock = runner.store._host_action_lock = threading.RLock()
    while not lock.acquire(timeout=.2):
        if runner.cancel.is_set():
            raise RuntimeError("Action stopped before execution.")
    try:
        if runner.cancel.is_set():
            raise RuntimeError("Action stopped before execution.")
        yield
    finally:
        lock.release()


class Activities:
    def __init__(self, store, app_root, primary):
        self.store, self.app_root, self.primary = store, app_root, primary
        self.lock = primary.lock
        self.agents = [primary]
        self.poll_lock = threading.Lock()
        self.positions = {}
        self.events = deque(maxlen=4000)
        self.sequence = 0

    def snapshot(self):
        with self.lock:
            return list(self.agents)

    def busy(self, session_id=None):
        return any(a.busy and (session_id is None or a.current_session == session_id)
                   for a in self.snapshot())

    def find(self, session_id):
        return next((a for a in self.snapshot() if a.current_session == session_id), None)

    def start(self, session_id, text, policy=None):
        from .engine import Agent
        with self.lock:
            owner = self.find(session_id)
            if owner and owner.busy:
                raise RuntimeError("This chat is already running. Send a follow-up or use a new parallel chat.")
            limit = self.store.data["settings"].get("max_parallel_runs", 2)
            if type(limit) is not int or not 1 <= limit <= 8:
                raise ValueError("Invalid parallel request limit.")
            if sum(a.busy for a in self.agents) >= limit:
                raise RuntimeError("Parallel request limit reached. Wait for a request to finish or increase the limit in Settings.")
            owner = owner or next((a for a in self.agents if not a.busy), None)
            if owner is None:
                owner = Agent(self.store, self.app_root)
                self.agents.append(owner)
            return owner.start(session_id, text, policy=policy)

    def poll(self, after=0, session_id=None):
        with self.poll_lock:
            snapshots = []
            for agent in self.snapshot():
                value = agent.poll(self.positions.get(agent, 0))
                self.positions[agent] = value["sequence"]
                snapshots.append(value)
                for event in value["events"]:
                    self.sequence += 1
                    self.events.append({**event, "seq": self.sequence})
            selected = next((r for r in snapshots if r["session_id"] == session_id), None) if session_id else None
            if selected is None and session_id:
                session = next((s for s in self.store.snapshot()["sessions"] if s["id"] == session_id), {})
                selected = {"session_id": session_id, "state": session.get("task", {}).get("state", "idle"),
                            "busy": False, "approval": None, "question": None}
            selected = selected or next((r for r in snapshots if r["busy"]), snapshots[0])
            return {**{key: selected[key] for key in ("session_id", "busy", "state", "approval", "question")},
                    "any_busy": any(r["busy"] for r in snapshots),
                    "runs": [{key: r[key] for key in ("session_id", "busy", "state")} for r in snapshots if r["session_id"]],
                    "events": [copy.deepcopy(e) for e in self.events if e["seq"] > after], "sequence": self.sequence}

    def follow_up(self, session_id, text):
        owner = self.find(session_id)
        if owner is None:
            raise ValueError("This chat has no running activity.")
        return owner.follow_up(session_id, text)

    def stop(self, session_id=None):
        owners = self.snapshot() if session_id is None else [self.find(session_id)]
        for owner in owners:
            if owner:
                owner.stop()
        return {"ok": True}

    def resolve(self, identity, value, question=False):
        for owner in self.snapshot():
            with owner.lock:
                pending = owner.pending_question if question else owner.pending
                if pending and pending["id"] == identity:
                    return owner.resolve_question(identity, value) if question else owner.resolve(identity, value)
        raise ValueError("Question or approval expired or already resolved.")

    def forget(self, session_id):
        with self.poll_lock:
            self.events = deque((e for e in self.events if e["session_id"] != session_id), maxlen=4000)
        owner = self.find(session_id)
        if owner and not owner.busy:
            with owner.lock:
                owner.current_session = ""
                owner.events = deque((e for e in owner.events if e["session_id"] != session_id), maxlen=2000)
