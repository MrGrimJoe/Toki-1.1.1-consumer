"""
executor.py — runs PowerShell commands with a real, killable process handle.

The whole point of the Stop button is that it has to be able to interrupt
something that's ALREADY RUNNING, not just stop the UI from waiting on it.
So this uses subprocess.Popen directly (not subprocess.run), keeps the
Popen object around, and exposes .terminate() for the UI to call.
"""

import subprocess
import threading
from typing import Callable, Optional


class RunningCommand:
    """One in-flight PowerShell invocation. Call .stop() to kill it."""

    def __init__(self, command: str, on_output: Callable[[str], None], on_done: Callable[[int], None]):
        self.command  = command
        self.on_output = on_output
        self.on_done   = on_done
        self._proc: Optional[subprocess.Popen] = None
        self._stopped = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", self.command],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            for line in self._proc.stdout:
                if self._stopped:
                    break
                self.on_output(line.rstrip("\n"))
            self._proc.wait(timeout=5)
            exit_code = self._proc.returncode if not self._stopped else -1
        except Exception as e:
            self.on_output(f"[executor error] {e}")
            exit_code = -1
        self.on_done(exit_code)

    def stop(self):
        """Called from the Stop button. Kills the process tree immediately."""
        self._stopped = True
        if self._proc and self._proc.poll() is None:
            try:
                # taskkill /T kills the whole process tree, not just the
                # powershell.exe wrapper — important for anything that
                # spawned a child process.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                    capture_output=True, timeout=5,
                )
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
