from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal


@dataclass
class CommandSpec:
    argv: List[str]
    cwd: Optional[str] = None
    env: Optional[dict] = None


class CommandWorker(QThread):
    line = pyqtSignal(str)
    finished_ok = pyqtSignal(int)  # return code
    finished_err = pyqtSignal(int)  # return code

    def __init__(self, spec: CommandSpec):
        super().__init__()
        self.spec = spec
        self._proc: subprocess.Popen | None = None

    def run(self) -> None:
        try:
            self.line.emit("$ " + " ".join(self.spec.argv))
            self._proc = subprocess.Popen(
                self.spec.argv,
                cwd=self.spec.cwd,
                env=self.spec.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            assert self._proc.stdout is not None
            for ln in self._proc.stdout:
                self.line.emit(ln.rstrip("\n"))
            rc = self._proc.wait()
            if rc == 0:
                self.finished_ok.emit(rc)
            else:
                self.finished_err.emit(rc)
        except Exception as e:
            self.line.emit(f"[worker] ERROR: {e}")
            self.finished_err.emit(1)

    def terminate_process(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
