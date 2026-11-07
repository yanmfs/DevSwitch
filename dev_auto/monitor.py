"""One read-only quota request at a time, separate from the agent/UI loop."""
import queue
import threading
import time


class QuotaMonitor:
    def __init__(self, reader):
        self.reader = reader
        self.cancel = threading.Event()
        self.thread = None
        self.results = queue.Queue(maxsize=1)

    def request(self):
        if self.thread is not None or self.cancel.is_set():
            return
        started = time.time()

        def read():
            try:
                result = (started, self.reader(), False)
            except Exception:
                # Provider errors can contain account data; never keep their text.
                result = (started, None, True)
            self.results.put(result)

        self.thread = threading.Thread(target=read, name="devswitch-quota", daemon=True)
        self.thread.start()

    def take(self):
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            return None
        self.thread.join()
        self.thread = None
        return result

    def close(self):
        self.cancel.set()
        if self.thread is not None:
            self.thread.join(timeout=6)
