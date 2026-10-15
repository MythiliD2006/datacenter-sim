"""Child processes exit by themselves if the control panel that started them dies."""
from __future__ import annotations

import os
import threading
import time

import psutil


def watch_parent(pid: int | None, interval: float = 2.0) -> None:
    if not pid:
        return

    def loop():
        while True:
            time.sleep(interval)
            if not psutil.pid_exists(pid):
                os._exit(0)
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    os._exit(0)
            except psutil.Error:
                os._exit(0)

    threading.Thread(target=loop, daemon=True, name="dcsim-lifeline").start()
