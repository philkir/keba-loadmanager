#!/usr/bin/env python3
"""Run the private controller and public API as one container workload."""
import signal
import subprocess
import sys
import time


commands = [
    [sys.executable, '-m', 'uvicorn', 'loadmanager.controller:app', '--host', '127.0.0.1', '--port', '8091', '--no-access-log', '--no-server-header'],
    [sys.executable, '-m', 'uvicorn', 'loadmanager.api:app', '--host', '0.0.0.0', '--port', '8000', '--no-access-log', '--no-server-header'],
    [sys.executable, '-m', 'uvicorn', 'loadmanager.web:app', '--host', '0.0.0.0', '--port', '8080', '--no-access-log', '--no-server-header'],
]
processes: list[subprocess.Popen] = []
stopping = False


def stop(signum=None, _frame=None):
    global stopping
    if stopping:
        return
    stopping = True
    for process in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

try:
    processes = [subprocess.Popen(command) for command in commands]
    while not stopping and all(process.poll() is None for process in processes):
        time.sleep(0.25)
finally:
    stop()
    deadline = time.monotonic() + 8
    for process in processes:
        remaining = max(0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
    failed = next((process.returncode for process in processes if process.returncode not in (0, -signal.SIGTERM)), 0)
    raise SystemExit(failed or 0)
