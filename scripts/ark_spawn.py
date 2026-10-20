"""Spawn PDEATHSIG-bound children from one persistent parent thread.

Linux parent-death signals follow the CREATING THREAD, not just getppid().
Spawning from a short-lived IPC/async worker would kill a successfully started
game when that request finishes. Never use threaded preexec_fn instead.
"""
import queue
import subprocess
import threading


class PersistentSpawner:
    def __init__(self, popen=subprocess.Popen):
        self.popen = popen
        self.queue = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self.run, name='ark-lifetime-spawner', daemon=True)
        self.thread.start()

    def run(self):
        while True:
            request = self.queue.get()
            if request is None:
                return
            args, kwargs, done, result = request
            try:
                result.append(self.popen(*args, **kwargs))
            except Exception as error:
                result.append(error)
            finally:
                done.set()

    def __call__(self, *args, **kwargs):
        if not self.thread.is_alive():
            raise RuntimeError('Lifetime spawner was lost.')
        done, result = threading.Event(), []
        self.queue.put((args, kwargs, done, result), timeout=30)
        if not done.wait(30):
            raise RuntimeError('Spawn outcome is unknown; do not replay.')
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    def close(self):
        self.queue.put(None)
        self.thread.join(timeout=30)
