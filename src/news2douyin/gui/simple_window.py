from __future__ import annotations

import sys
import subprocess
from pathlib import Path
from typing import Optional, List

from PyQt6.QtCore import Qt, QSettings, QTimer, QUrl
from PyQt6.QtGui import QAction, QDesktopServices
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QFileDialog, QListWidget, QListWidgetItem, QTextEdit, QMessageBox, QComboBox,
    QProgressBar, QCheckBox, QGroupBox, QFormLayout, QSplitter
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

def _safe_int(v, default=0):
    try: return int(v)
    except Exception: return default

class SimpleWindow(QMainWindow):
    """Single-page UI:
    - Daily auto run (run -> prep-assets -> tts)
    - Manual event selection
    - Playback + queue
    """

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("news2douyin - Collector & Player")

        # Player
        self.audio_out = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_out)
        self.player.mediaStatusChanged.connect(self._on_media_status)

        self._queue: List[Path] = []
        self._queue_idx: int = -1
        self.current_run_dir: Optional[Path] = None

        # Central
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Top controls (config, server, run)
        top = QGroupBox("Daily collector")
        form = QFormLayout(top)

        self.le_config = QLineEdit(top)
        self.le_out = QLineEdit(top)
        self.le_server = QLineEdit(top)
        self.le_time = QLineEdit(top)  # HH:MM local
        self.cb_lang = QComboBox(top)
        self.cb_lang.addItems(["both","zh","en"])

        self.btn_browse_cfg = QPushButton("Browse…", top)
        self.btn_browse_cfg.clicked.connect(self._browse_cfg)
        cfg_row = QWidget(top)
        cfg_l = QHBoxLayout(cfg_row); cfg_l.setContentsMargins(0,0,0,0)
        cfg_l.addWidget(self.le_config, 1); cfg_l.addWidget(self.btn_browse_cfg, 0)

        self.btn_browse_out = QPushButton("Browse…", top)
        self.btn_browse_out.clicked.connect(self._browse_out)
        out_row = QWidget(top)
        out_l = QHBoxLayout(out_row); out_l.setContentsMargins(0,0,0,0)
        out_l.addWidget(self.le_out, 1); out_l.addWidget(self.btn_browse_out, 0)

        self.btn_run_now = QPushButton("Run now (collect → assets → TTS)", top)
        self.btn_run_now.clicked.connect(self._run_now)

        self.cb_auto_upload = QCheckBox("Auto upload after run", top)
        self.btn_upload = QPushButton("Upload latest run", top)
        self.btn_upload.clicked.connect(self._upload_latest_run)

        form.addRow("Config", cfg_row)
        form.addRow("Assets out", out_row)
        form.addRow("TTS lang", self.cb_lang)
        form.addRow("Daily time (HH:MM)", self.le_time)
        form.addRow("Server URL (optional)", self.le_server)
        form.addRow(self.btn_run_now)
        form.addRow(self.cb_auto_upload)
        form.addRow(self.btn_upload)

        layout.addWidget(top)

        # Split: events list + player panel
        splitter = QSplitter(Qt.Orientation.Horizontal, central)
        layout.addWidget(splitter, 1)

        left = QWidget(splitter)
        left_l = QVBoxLayout(left)
        self.lbl_stats = QLabel("Events: 0", left)
        left_l.addWidget(self.lbl_stats)
        self.list_events = QListWidget(left)
        self.list_events.setSelectionMode(self.list_events.SelectionMode.ExtendedSelection)
        self.list_events.itemSelectionChanged.connect(self._on_selection)
        left_l.addWidget(self.list_events, 1)

        btns = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh", left)
        self.btn_refresh.clicked.connect(self.refresh_events)
        self.btn_add_queue = QPushButton("Add to queue →", left)
        self.btn_add_queue.clicked.connect(self._add_selected_to_queue)
        btns.addWidget(self.btn_refresh); btns.addWidget(self.btn_add_queue)
        left_l.addLayout(btns)

        right = QWidget(splitter)
        right_l = QVBoxLayout(right)

        player_box = QGroupBox("Player")
        pb = QVBoxLayout(player_box)
        ctl = QHBoxLayout()
        self.btn_play = QPushButton("Play")
        self.btn_pause = QPushButton("Pause")
        self.btn_prev = QPushButton("Prev")
        self.btn_next = QPushButton("Next")
        self.btn_download = QPushButton("Download audio…")
        self.btn_play.clicked.connect(self._play_selected_or_queue)
        self.btn_pause.clicked.connect(self.player.pause)
        self.btn_prev.clicked.connect(self._play_prev)
        self.btn_next.clicked.connect(self._play_next)
        self.btn_download.clicked.connect(self._download_audio)
        for b in [self.btn_prev,self.btn_play,self.btn_pause,self.btn_next,self.btn_download]:
            ctl.addWidget(b)
        pb.addLayout(ctl)

        self.pb_progress = QProgressBar(player_box)
        self.pb_progress.setRange(0,100)
        pb.addWidget(self.pb_progress)

        self.log = QTextEdit(right); self.log.setReadOnly(True)
        right_l.addWidget(player_box)
        right_l.addWidget(QLabel("Queue"))
        self.list_queue = QListWidget(right)
        right_l.addWidget(self.list_queue, 1)

        qbtns = QHBoxLayout()
        self.btn_play_queue = QPushButton("Play queue", right); self.btn_play_queue.clicked.connect(self._play_queue)
        self.btn_clear_queue = QPushButton("Clear", right); self.btn_clear_queue.clicked.connect(self._clear_queue)
        qbtns.addWidget(self.btn_play_queue); qbtns.addWidget(self.btn_clear_queue)
        right_l.addLayout(qbtns)

        right_l.addWidget(QLabel("Logs"))
        right_l.addWidget(self.log, 1)

        splitter.addWidget(left); splitter.addWidget(right)
        splitter.setStretchFactor(0, 2); splitter.setStretchFactor(1, 3)

        # timer for daily run
        self.timer = QTimer(self)
        self.timer.setInterval(20_000)
        self.timer.timeout.connect(self._tick_daily)
        self.timer.start()

        self._restore_settings()
        self.refresh_events()

    def _restore_settings(self):
        self.le_config.setText(self.settings.value("cfg/path", "configs/default.yaml", type=str))
        self.le_out.setText("(auto: run_dir/assets)")
        self.le_out.setEnabled(False)
        self.btn_browse_out.setEnabled(False)
        self.le_server.setText(self.settings.value("server/url", "", type=str))
        self.le_time.setText(self.settings.value("daily/time", "09:00", type=str))
        self.cb_lang.setCurrentText(self.settings.value("tts/lang", "both", type=str))

    def _save_settings(self):
        self.settings.setValue("cfg/path", self.le_config.text().strip())
        self.settings.setValue("assets/out", self.le_out.text().strip())
        self.settings.setValue("server/url", self.le_server.text().strip())
        self.settings.setValue("daily/time", self.le_time.text().strip())
        self.settings.setValue("tts/lang", self.cb_lang.currentText())

    def closeEvent(self, e):
        self._save_settings()
        super().closeEvent(e)

    def _browse_cfg(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select config", "", "YAML (*.yml *.yaml);;All (*.*)")
        if p: self.le_config.setText(p)

    def _browse_out(self):
        QMessageBox.information(self, "Assets dir", "Assets are now stored under run_dir/assets automatically.")
        return

    def _log(self, msg: str):
        self.log.append(msg)

    def _tick_daily(self):
        # simple daily trigger by local HH:MM; prevents re-run within 23h
        target = self.le_time.text().strip()
        if len(target) != 5 or target[2] != ":":
            return
        from datetime import datetime, timedelta
        now = datetime.now()
        hh = _safe_int(target[:2], 9); mm = _safe_int(target[3:], 0)
        if now.hour != hh or now.minute != mm:
            return
        last = self.settings.value("daily/last_ts", "", type=str)
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if now - last_dt < timedelta(hours=23):
                    return
            except Exception:
                pass
        self.settings.setValue("daily/last_ts", now.isoformat())
        self._run_now()

def _run_now(self):
    cfg = self.le_config.text().strip()
    lang = self.cb_lang.currentText()
    self._log(f"[RUN] config={cfg} lang={lang}")
    try:
        self._run_cli(["run", "--config", cfg])

        run_dir = self._find_latest_run_dir()
        if run_dir is None:
            raise RuntimeError("No run directory found under ./runs after running pipeline")
        self.current_run_dir = run_dir

        self._run_cli(["prep-assets", "--run-dir", str(run_dir)])
        self._run_cli(["tts", "--run-dir", str(run_dir), "--lang", lang])

        self._log(f"[RUN] done: {run_dir}")

        if self.cb_auto_upload.isChecked() and self.le_server.text().strip():
            self._upload_run_dir(run_dir)

    except Exception as e:
        QMessageBox.critical(self, "Run failed", str(e))
    finally:
        self.refresh_events()

    def _run_cli(self, args: list[str]):
        cmd = [sys.executable, "-m", "news2douyin.cli"] + args
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self._log(p.stdout.strip())
        if p.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(cmd)}")

def _find_latest_run_dir(self) -> Optional[Path]:
    runs_root = Path("runs")
    if not runs_root.exists():
        return None
    candidates = [p for p in runs_root.glob("*/*") if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)

def _upload_latest_run(self):
    run_dir = self._find_latest_run_dir()
    if run_dir is None:
        QMessageBox.information(self, "Upload", "No run found under ./runs")
        return
    self._upload_run_dir(run_dir)

def _upload_run_dir(self, run_dir: Path):
    server = self.le_server.text().strip().rstrip("/")
    if not server:
        QMessageBox.information(self, "Upload", "Server URL is empty")
        return

    import tempfile, zipfile
    tmp = Path(tempfile.gettempdir()) / f"{run_dir.parent.name}_{run_dir.name}.zip"
    self._log(f"[UPLOAD] zipping {run_dir} -> {tmp}")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in run_dir.rglob("*"):
            if p.is_file():
                arc = f"{run_dir.parent.name}/{run_dir.name}/" + str(p.relative_to(run_dir)).replace('\\', '/')
                z.write(p, arcname=arc)

    url = f"{server}/api/upload_run"
    run_key = f"{run_dir.parent.name}/{run_dir.name}"
    self._log(f"[UPLOAD] POST {url} run_key={run_key}")

    try:
        import requests
        with open(tmp, "rb") as f:
            files = {"zip_file": (tmp.name, f, "application/zip")}
            data = {"run_key": run_key, "title": "Daily run", "note": "client"}
            r = requests.post(url, files=files, data=data, timeout=180)
        if r.status_code >= 300:
            raise RuntimeError(f"Upload failed: {r.status_code} {r.text[:200]}")
        self._log(f"[UPLOAD] OK: {r.text.strip()}")
        try:
            j = r.json()
            run_url = j.get("run_url")
            if run_url:
                full = server + run_url
                self._log(f"[UPLOAD] Run page: {full}")
                # copy to clipboard and offer open
                from PyQt6.QtWidgets import QMessageBox
                from PyQt6.QtGui import QGuiApplication
                QGuiApplication.clipboard().setText(full)
                if QMessageBox.question(self, "Upload", f"Uploaded.\nRun page copied to clipboard:\n{full}\n\nOpen in browser?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                    QDesktopServices.openUrl(QUrl(full))
        except Exception:
            pass
    except Exception as e:
        QMessageBox.critical(self, "Upload failed", str(e))
        self._log(f"[UPLOAD] FAIL: {e!r}")

def refresh_events(self):
    run_dir = self.current_run_dir or self._find_latest_run_dir()
    if run_dir is None:
        self.lbl_stats.setText("Events: 0 (no runs yet)")
        self.list_events.clear()
        return
    self.current_run_dir = run_dir
    assets_dir = (run_dir / "assets").resolve()

    self.list_events.clear()
    if not assets_dir.exists():
        self.lbl_stats.setText(f"Assets dir not found: {assets_dir}")
        return

    ev_dirs = sorted([p for p in assets_dir.iterdir() if p.is_dir()], key=lambda x: x.name)
    self.lbl_stats.setText(f"Run: {run_dir.parent.name}/{run_dir.name} | Events: {len(ev_dirs)}")
    for ev in ev_dirs:
        zh = (ev / "voice_zh.mp3").exists() or (ev / "voice_zh.wav").exists()
        en = (ev / "voice_en.mp3").exists() or (ev / "voice_en.wav").exists()
        item = QListWidgetItem(f"[{('zh' if zh else '  ')}|{('en' if en else '  ')}] {ev.name}")
        item.setData(Qt.ItemDataRole.UserRole, str(ev))
        self.list_events.addItem(item)