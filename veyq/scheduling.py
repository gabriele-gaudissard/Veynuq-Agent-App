"""Read-only schedules in the app or its explicitly enabled Windows worker."""
import threading
import time
import uuid


class Scheduler:
    def __init__(self, api):
        self.api = api
        self.stop_event = threading.Event()
        self.thread = None
        self.active = None

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stop_event.wait(10):
            try: self.tick()
            except Exception: pass  # A failed background check must never crash the UI.

    def tick(self, now=None, finish_only=False):
        now = now or time.time()
        store, agent = self.api._store, self.api._agent
        with agent.lock:
            if agent.busy or self.api._maintenance.locked() or self.api._voice.busy(): return
            with store.lock:
                if self.active:
                    job = next((j for j in store.data['automations'] if j['id'] == self.active), None)
                    if job:
                        job['last_state'] = agent.state
                        if agent.state != 'completed': job['enabled'] = False
                        store.save()
                    self.active = None
                if finish_only or self.api._runs.busy(): return
                for job in store.data['automations']:
                    if not job['enabled'] or job['next_run'] > now: continue
                    session = next((s for s in store.data['sessions'] if s['id'] == job['session_id']), None)
                    if not session:
                        job['enabled'] = False
                        job['last_state'] = 'missing_chat'
                        store.save()
                        continue
                    job['next_run'] = now + job['interval_hours'] * 3600
                    job['last_state'] = 'running'
                    store.save()
                    try:
                        agent.start(job['session_id'], 'Scheduled read-only task: ' + job['prompt'], policy='read_only')
                        self.active = job['id']
                    except Exception:
                        job['enabled'] = False
                        job['last_state'] = 'failed'
                        store.save()
                    return
