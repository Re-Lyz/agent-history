"""A small daemon pool for replaceable reads; mutations keep independent runners."""
import queue
import threading


class ReadWorkers:
    def __init__(self, workers=4):
        self.queue = queue.Queue()
        self.closed = False
        self.workers = workers
        self.lock = threading.Lock()
        for _ in range(workers):
            threading.Thread(target=self.run, daemon=True).start()

    def submit(self, work):
        if not self.closed:
            self.queue.put(work)

    def run(self):
        while True:
            work = self.queue.get()
            try:
                if work is None:
                    return
                if not self.closed:
                    work()
            finally:
                self.queue.task_done()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for _ in range(self.workers):
                self.queue.put(None)
