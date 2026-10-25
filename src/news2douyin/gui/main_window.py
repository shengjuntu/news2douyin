from __future__ import annotations

import json
import os
import shutil
import subprocess
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSettings, QTimer, Qt, QUrl, QSize, QPoint, QTime
from PyQt6.QtGui import QAction
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
    QFileDialog,
    QCheckBox,
    QProgressBar,
)

from .worker import CommandSpec, CommandWorker
from ..tts.synth import available_backend, synthesize, TTSError


DEFAULT_CONFIG = "configs/pipeline.yaml"
DEFAULT_OUT = "assets"  # legacy; assets now live under run_dir/assets
DEFAULT_ZH_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_EN_VOICE = "en-US-JennyNeural"


def _project_root() -> Path:
    # repo layout: src/news2douyin/gui/main_window.py -> repo_root is parents[3]
    try:
        return Path(__file__).resolve().parents[3]
    except Exception:
        return Path.cwd()


def _run_python_argv() -> list[str]:
    # Use current interpreter, -m for module runs.
    return [os.fspath(Path(os.sys.executable).resolve())]


def _load_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


@dataclass
class EventItem:
    path: Path
    name: str

    def script_path(self, lang: str) -> Path:
        return self.path / f"script_{lang}.txt"

    def audio_path(self, lang: str) -> Path:
        # mp3 by default; if pyttsx3 used, we may generate wav
        mp3 = self.path / f"voice_{lang}.mp3"
        wav = self.path / f"voice_{lang}.wav"
        return mp3 if mp3.exists() else wav


class MainWindow(QMainWindow):
    def __init__(self, *, settings: QSettings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle("news2douyin · Daily News → Script → Voice Player")

        self.worker: Optional[CommandWorker] = None
        self.current_run_dir: Optional[Path] = None

        # audio
        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)

        # queue playback (Library + Player)
        self.queue_items: list[EventItem] = []
        self.queue_index: int = -1
        self.queue_lang: str = "zh"
        self.player.mediaStatusChanged.connect(self._on_player_media_status)

        # daily scheduler
        self.scheduler_timer = QTimer(self)
        self.scheduler_timer.setInterval(20_000)  # check every 20s
        self.scheduler_timer.timeout.connect(self._scheduler_tick)

        self._build_ui()
        # Restore *after* the widget tree is fully constructed and parented.
        # This avoids "wrapped C/C++ object ... has been deleted" on some platforms.
        self._restore_settings()
        self.refresh_events()
        self.refresh_tts_events()

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        # IMPORTANT: build a stable widget tree up-front.
        # If widgets are created without a parent and later re-parented via layouts,
        # PyQt can occasionally GC the wrappers and you may hit
        #   RuntimeError: wrapped C/C++ object ... has been deleted
        # on startup when restoring settings.
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.tabs = QTabWidget(central)

        # Tab 1: Pipeline
        tab_pipeline = QWidget(self.tabs)
        self.tabs.addTab(tab_pipeline, "Pipeline")

        self.le_config = QLineEdit(DEFAULT_CONFIG, tab_pipeline)
        btn_pick_cfg = QPushButton("Browse…", tab_pipeline)
        btn_pick_cfg.clicked.connect(self._pick_config)

        self.btn_run = QPushButton("Run now (download+select)", tab_pipeline)
        self.btn_run.clicked.connect(self.run_pipeline)

        self.lbl_run_dir = QLabel("Run dir: —", tab_pipeline)

        pipeline_box = QGroupBox("Daily run (news2douyin run)")
        form = QFormLayout()
        cfg_row = QHBoxLayout()
        cfg_row.addWidget(self.le_config, 1)
        cfg_row.addWidget(btn_pick_cfg, 0)
        form.addRow("Config", cfg_row)
        form.addRow("", self.btn_run)
        form.addRow("Last run", self.lbl_run_dir)
        pipeline_box.setLayout(form)

        # scheduler group
        sched = QGroupBox("Schedule (optional)", tab_pipeline)
        sched_form = QFormLayout()
        self.cb_schedule = QCheckBox("Enable daily run", tab_pipeline)
        self.time_daily = QTimeEdit(tab_pipeline)
        self.time_daily.setDisplayFormat("HH:mm")
        self.time_daily.setTime(self.time_daily.time().fromString("09:00", "HH:mm"))
        self.lbl_next = QLabel("Next: —", tab_pipeline)
        sched_form.addRow(self.cb_schedule)
        sched_form.addRow("Daily time", self.time_daily)
        sched_form.addRow("Next run", self.lbl_next)
        sched.setLayout(sched_form)

        pipeline_layout = QVBoxLayout(tab_pipeline)
        pipeline_layout.addWidget(pipeline_box)
        pipeline_layout.addWidget(sched)
        pipeline_layout.addStretch(1)

        # Tab 2: Assets & TTS
        tab_assets = QWidget(self.tabs)
        self.tabs.addTab(tab_assets, "Assets + TTS")

        # Left: event list with status
        self.list_tts_events = QListWidget(tab_assets)
        self.list_tts_events.setSelectionMode(self.list_tts_events.SelectionMode.ExtendedSelection)

        self.lbl_tts_stats = QLabel("Events: 0 | script: 0 | zh: 0 | en: 0 | failed: 0", tab_assets)
        btn_tts_refresh = QPushButton("Refresh", tab_assets)
        btn_tts_refresh.clicked.connect(self.refresh_tts_events)

        tts_header = QHBoxLayout()
        tts_header.addWidget(self.lbl_tts_stats, 1)
        tts_header.addWidget(btn_tts_refresh, 0)

        left = QWidget(tab_assets)
        left_l = QVBoxLayout(left)
        left_l.addLayout(tts_header)
        left_l.addWidget(self.list_tts_events, 1)

        # Right: controls (prep + tts)
        right = QWidget(tab_assets)
        right_l = QVBoxLayout(right)

        self.le_out = QLineEdit(DEFAULT_OUT, tab_assets)
        btn_pick_out = QPushButton("Browse…", tab_assets)
        btn_pick_out.clicked.connect(self._pick_out)

        self.btn_prep = QPushButton("Prep assets (tools/prep_assets.py)", tab_assets)
        self.btn_prep.clicked.connect(self.prep_assets)

        self.cmb_llm = QComboBox(tab_assets)
        self.cmb_llm.addItems(["none", "openai", "vllm"])

        assets_box = QGroupBox("Convert run → event folders (+ scripts)", tab_assets)
        assets_form = QFormLayout(assets_box)
        out_row = QHBoxLayout()
        out_row.addWidget(self.le_out, 1)
        out_row.addWidget(btn_pick_out)
        assets_form.addRow("Output root", out_row)
        assets_form.addRow("Script LLM", self.cmb_llm)
        assets_form.addRow("", self.btn_prep)

        tts_box = QGroupBox("TTS synthesis (Chinese + English)", tab_assets)
        tts_form = QFormLayout(tts_box)
        self.lbl_tts_backend = QLabel(f"Backend: {available_backend()}", tab_assets)
        self.cmb_zh_voice = QLineEdit(DEFAULT_ZH_VOICE, tab_assets)
        self.cmb_en_voice = QLineEdit(DEFAULT_EN_VOICE, tab_assets)

        self.cb_tts_zh = QCheckBox("Chinese (zh)", tab_assets)
        self.cb_tts_zh.setChecked(True)
        self.cb_tts_en = QCheckBox("English (en)", tab_assets)
        self.cb_tts_en.setChecked(True)

        self.btn_tts_selected = QPushButton("Synthesize selected", tab_assets)
        self.btn_tts_selected.clicked.connect(self.synthesize_selected_tts_list)

        self.btn_tts_all = QPushButton("Synthesize ALL", tab_assets)
        self.btn_tts_all.clicked.connect(self.synthesize_all_tts_list)

        tts_form.addRow(self.lbl_tts_backend)
        tts_form.addRow("ZH voice", self.cmb_zh_voice)
        tts_form.addRow("EN voice", self.cmb_en_voice)
        tts_form.addRow("", self.cb_tts_zh)
        tts_form.addRow("", self.cb_tts_en)
        tts_form.addRow("", self.btn_tts_selected)
        tts_form.addRow("", self.btn_tts_all)

        self.pb_tts = QProgressBar(tab_assets)
        self.pb_tts.setRange(0, 100)
        self.pb_tts.setValue(0)

        right_l.addWidget(assets_box)
        right_l.addWidget(tts_box)
        right_l.addWidget(self.pb_tts)

        splitter_assets = QSplitter(Qt.Orientation.Horizontal, tab_assets)
        splitter_assets.addWidget(left)
        splitter_assets.addWidget(right)
        splitter_assets.setStretchFactor(0, 2)
        splitter_assets.setStretchFactor(1, 3)

        assets_layout = QVBoxLayout(tab_assets)
        assets_layout.addWidget(splitter_assets)

        # Tab 3: Library + Player (events + queue + download)

        tab_player = QWidget(self.tabs)
        self.tabs.addTab(tab_player, "Library + Player")

        # Left: events list + queue list
        left = QWidget(tab_player)
        left_layout = QVBoxLayout(left)

        left_layout.addWidget(QLabel("Events"), 0)
        self.list_events = QListWidget(left)
        self.list_events.setSelectionMode(self.list_events.SelectionMode.ExtendedSelection)
        self.list_events.currentItemChanged.connect(self._on_event_selected)
        left_layout.addWidget(self.list_events, 2)

        q_hdr = QHBoxLayout()
        q_hdr.addWidget(QLabel("Queue"))
        q_hdr.addStretch(1)
        self.btn_queue_add = QPushButton("Add selected →", left)
        self.btn_queue_add.clicked.connect(self.add_selected_to_queue)
        self.btn_queue_clear = QPushButton("Clear", left)
        self.btn_queue_clear.clicked.connect(self.clear_queue)
        q_hdr.addWidget(self.btn_queue_add)
        q_hdr.addWidget(self.btn_queue_clear)
        left_layout.addLayout(q_hdr)

        self.list_queue = QListWidget(left)
        self.list_queue.setSelectionMode(self.list_queue.SelectionMode.ExtendedSelection)
        left_layout.addWidget(self.list_queue, 1)

        q_controls = QHBoxLayout()
        self.btn_queue_play = QPushButton("Play queue", left)
        self.btn_queue_play.clicked.connect(self.play_queue)
        self.btn_prev = QPushButton("Prev", left)
        self.btn_prev.clicked.connect(self.play_prev_in_queue)
        self.btn_next = QPushButton("Next", left)
        self.btn_next.clicked.connect(self.play_next_in_queue)
        q_controls.addWidget(self.btn_queue_play)
        q_controls.addWidget(self.btn_prev)
        q_controls.addWidget(self.btn_next)
        left_layout.addLayout(q_controls)

        # Right: script view + playback controls
        right = QWidget(tab_player)
        right_layout = QVBoxLayout(right)

        self.txt_script = QPlainTextEdit(right)
        self.txt_script.setReadOnly(True)

        self.cmb_lang = QComboBox(right)
        self.cmb_lang.addItems(["zh", "en"])
        self.cmb_lang.currentTextChanged.connect(lambda _: self._refresh_script_view())

        btn_refresh = QPushButton("Refresh", right)
        btn_refresh.clicked.connect(self.refresh_events)

        self.btn_download = QPushButton("Download audio…", right)
        self.btn_download.clicked.connect(self.download_selected_audio)

        self.btn_play = QPushButton("Play", right)
        self.btn_play.clicked.connect(self.play_selected_audio)
        self.btn_stop = QPushButton("Stop", right)
        self.btn_stop.clicked.connect(self.stop_audio)

        self.vol = QSpinBox(right)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.valueChanged.connect(lambda v: self.audio_out.setVolume(v / 100.0))

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Lang"))
        controls.addWidget(self.cmb_lang)
        controls.addStretch(1)
        controls.addWidget(btn_refresh)
        controls.addWidget(self.btn_download)
        controls.addWidget(QLabel("Vol"))
        controls.addWidget(self.vol)
        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_stop)

        right_layout.addLayout(controls)
        right_layout.addWidget(self.txt_script, 1)

        splitter = QSplitter(tab_player)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        player_layout = QVBoxLayout(tab_player)
        player_layout.addWidget(splitter, 1)
        tab_player.setLayout(player_layout)

        # logs dock-ish (simple)
        self.txt_log = QPlainTextEdit(central)
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(4000)

        layout.addWidget(self.tabs, 3)
        layout.addWidget(QLabel("Logs"))
        layout.addWidget(self.txt_log, 2)
        # layout is already set on 'central' via QVBoxLayout(central)

        # menu actions
        act_open_out = QAction("Open output folder", self)
        act_open_out.triggered.connect(self._reveal_out_dir)
        self.menuBar().addAction(act_open_out)

        self.cb_schedule.toggled.connect(self._on_schedule_toggled)
        self.time_daily.timeChanged.connect(self._update_next_run_label)

    # ---------------- Settings ----------------
    def _restore_settings(self) -> None:
        # QSettings types can differ across platforms; request concrete types.
        try:
            self.resize(self.settings.value("win/size", self.size(), type=QSize))
            self.move(self.settings.value("win/pos", self.pos(), type=QPoint))
        except Exception:
            pass

        # Guard against rare cases where widgets were not fully constructed.
        try:
            self.le_config.setText(self.settings.value("cfg/path", DEFAULT_CONFIG, type=str))
            self.le_out.setText(self.settings.value("out/path", DEFAULT_OUT, type=str))
            self.cmb_llm.setCurrentText(self.settings.value("prep/llm", "none", type=str))
            self.cmb_zh_voice.setText(self.settings.value("tts/zh_voice", DEFAULT_ZH_VOICE, type=str))
            self.cmb_en_voice.setText(self.settings.value("tts/en_voice", DEFAULT_EN_VOICE, type=str))
            self.cb_schedule.setChecked(self.settings.value("sched/enabled", False, type=bool))
            self.time_daily.setTime(self.settings.value("sched/time", self.time_daily.time(), type=QTime))
        except RuntimeError:
            # wrapped C/C++ object ... has been deleted
            return
        self._update_next_run_label()
        if self.cb_schedule.isChecked():
            self.scheduler_timer.start()

    def closeEvent(self, event):  # type: ignore[override]
        self.settings.setValue("win/size", self.size())
        self.settings.setValue("win/pos", self.pos())
        self.settings.setValue("cfg/path", self.le_config.text().strip())
        self.settings.setValue("out/path", self.le_out.text().strip())
        self.settings.setValue("prep/llm", self.cmb_llm.currentText())
        self.settings.setValue("tts/zh_voice", self.cmb_zh_voice.text().strip())
        self.settings.setValue("tts/en_voice", self.cmb_en_voice.text().strip())
        self.settings.setValue("sched/enabled", self.cb_schedule.isChecked())
        self.settings.setValue("sched/time", self.time_daily.time())
        super().closeEvent(event)

    # ---------------- Helpers ----------------
    def log(self, s: str) -> None:
        self.txt_log.appendPlainText(s)
        self.txt_log.verticalScrollBar().setValue(self.txt_log.verticalScrollBar().maximum())

    def _pick_config(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Select config", str(_project_root()), "YAML (*.yaml *.yml)")
        if p:
            self.le_config.setText(os.path.relpath(p, str(_project_root())))

    def _pick_out(self) -> None:
        p = QFileDialog.getExistingDirectory(self, "Select output folder", str(_project_root()))
        if p:
            self.le_out.setText(os.path.relpath(p, str(_project_root())))
            self.refresh_events()
        self.refresh_tts_events()

    def _reveal_out_dir(self) -> None:
        out_dir = (_project_root() / self.le_out.text().strip()).resolve()
        if not out_dir.exists():
            QMessageBox.information(self, "Output folder", f"Folder does not exist: {out_dir}")
            return
        QUrl.fromLocalFile(str(out_dir))
        # best-effort open in file manager
        try:
            import subprocess, platform
            if platform.system() == "Windows":
                subprocess.run(["explorer", str(out_dir)])
            elif platform.system() == "Darwin":
                subprocess.run(["open", str(out_dir)])
            else:
                subprocess.run(["xdg-open", str(out_dir)])
        except Exception as e:
            QMessageBox.warning(self, "Open folder", f"Failed to open folder: {e}")

    def _set_busy(self, busy: bool) -> None:
        # Disable UI actions while running subprocess commands.
        for w in [
            getattr(self, "btn_run", None),
            getattr(self, "btn_prep", None),
            getattr(self, "btn_tts_selected", None),
            getattr(self, "btn_tts_all", None),
        ]:
            if w is not None:
                w.setEnabled(not busy)

    def _run_cmd(self, argv: list[str], cwd: Optional[Path] = None) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "Busy", "A command is already running.")
            return
        self._set_busy(True)
        spec = CommandSpec(argv=argv, cwd=str(cwd) if cwd else None, env=os.environ.copy())
        self.worker = CommandWorker(spec)
        self.worker.line.connect(self.log)
        self.worker.finished_ok.connect(self._on_worker_ok)
        self.worker.finished_err.connect(self._on_worker_err)
        self.worker.start()

    def _on_worker_ok(self, rc: int) -> None:
        self.log(f"[done] rc={rc}")
        self._set_busy(False)
        self.refresh_events()
        self.refresh_tts_events()

    def _on_worker_err(self, rc: int) -> None:
        self.log(f"[failed] rc={rc}")
        self._set_busy(False)
        QMessageBox.warning(self, "Command failed", f"Return code: {rc}\nSee logs below.")

    # ---------------- Actions ----------------
    def run_pipeline(self) -> None:
        cfg = self.le_config.text().strip()
        if not cfg:
            QMessageBox.warning(self, "Config", "Please select a pipeline config yaml.")
            return
        argv = _run_python_argv() + ["-m", "news2douyin.cli", "run", "--config", cfg]
        self._run_cmd(argv, cwd=_project_root())

        # heuristically compute expected run dir date folder
        # actual run dir is printed by CLI; user can read in logs.
        self.lbl_run_dir.setText("Run dir: (see logs)")

    def prep_assets(self) -> None:
        # Find latest run dir if not set: choose newest by mtime under runs/YYYY-MM-DD/run_*
        run_dir = self._guess_latest_run_dir()
        if not run_dir:
            QMessageBox.warning(self, "Run dir", "No run dir found under ./runs. Run pipeline first.")
            return

        out_root = self.le_out.text().strip() or DEFAULT_OUT
        llm = self.cmb_llm.currentText()

        argv = _run_python_argv() + [
            str((_project_root() / "tools" / "prep_assets.py").resolve()),
            "--run-dir",
            os.fspath(run_dir),
            # "--out",  # removed

            out_root,
            "--llm",
            llm,
        ]
        self._run_cmd(argv, cwd=_project_root())

    def synthesize_selected(self) -> None:
        ev = self._current_event()
        if not ev:
            QMessageBox.information(self, "Select", "Select an event first (Library + Player).")
            return
        try:
            self._synthesize_event(ev)
            QMessageBox.information(self, "TTS", "Synthesis done.")
            self.refresh_events()
            self.refresh_tts_events()
        except TTSError as e:
            QMessageBox.warning(self, "TTS", str(e))

    def _synthesize_event(self, ev: EventItem, lang: str | None = None) -> None:
        """Synthesize TTS audio for an event.

        If lang is None, synthesize both zh and en when scripts exist.
        If lang is 'zh' or 'en', synthesize only that language.
        """
        backend = "auto"
        zh_voice = self.cmb_zh_voice.text().strip() or DEFAULT_ZH_VOICE
        en_voice = self.cmb_en_voice.text().strip() or DEFAULT_EN_VOICE

        zh_text = _load_text(ev.script_path("zh")).strip()
        en_text = _load_text(ev.script_path("en")).strip()

        if not zh_text and not en_text:
            raise TTSError("No script_zh.txt / script_en.txt found in this event folder.")

        def _do_one(which: str, text: str, voice: str) -> None:
            # Prefer mp3 for edge-tts; wav for pyttsx3
            out = ev.path / f"voice_{which}.mp3"
            try:
                used, outp = synthesize(text, out, backend=backend, voice=voice, lang=which, preprocess_markdown=True)
            except TTSError:
                out = ev.path / f"voice_{which}.wav"
                used, outp = synthesize(text, out, backend=backend, voice=voice, lang=which, preprocess_markdown=True)
            self.log(f"[tts] {which} -> {outp} ({used})")

        if lang in (None, "zh") and zh_text:
            _do_one("zh", zh_text, zh_voice)

        if lang in (None, "en") and en_text:
            _do_one("en", en_text, en_voice)

    def play_selected_audio(self) -> None:
        ev = self._current_event()
        if not ev:
            return
        lang = self.cmb_lang.currentText()
        ap = ev.audio_path(lang)
        if not ap.exists():
            QMessageBox.information(self, "Audio", f"No audio found for {lang}. Click 'Synthesize' first.")
            return

        self.player.setSource(QUrl.fromLocalFile(str(ap.resolve())))
        self.player.play()
        self.log(f"[play] {ap}")

    def stop_audio(self) -> None:
        self.player.stop()

    # ---------------- Queue + Download (Library + Player) ----------------
    def _selected_event_items(self) -> list[EventItem]:
        items: list[EventItem] = []
        # use selected multi-items if any
        for it in self.list_events.selectedItems():
            p = it.data(Qt.ItemDataRole.UserRole)
            if not p:
                continue
            pp = Path(str(p))
            items.append(EventItem(path=pp, name=it.text()))
        # fallback to current
        if not items:
            ev = self._current_event()
            if ev:
                items.append(ev)
        return items

    def add_selected_to_queue(self) -> None:
        added = 0
        for ev in self._selected_event_items():
            self.queue_items.append(ev)
            qi = QListWidgetItem(ev.name)
            qi.setData(Qt.ItemDataRole.UserRole, str(ev.path))
            self.list_queue.addItem(qi)
            added += 1
        if added:
            self.log(f"[queue] added {added} item(s)")

    def clear_queue(self) -> None:
        self.queue_items = []
        self.queue_index = -1
        self.list_queue.clear()
        self.log("[queue] cleared")

    def play_queue(self) -> None:
        if not self.queue_items:
            # Build queue from current selection
            self.clear_queue()
            self.add_selected_to_queue()
        if not self.queue_items:
            return
        self.queue_lang = self.cmb_lang.currentText()
        self.queue_index = 0
        self._play_queue_index(self.queue_index)

    def play_next_in_queue(self) -> None:
        if not self.queue_items:
            return
        if self.queue_index < 0:
            self.queue_index = 0
        else:
            self.queue_index += 1
        if self.queue_index >= len(self.queue_items):
            self.queue_index = len(self.queue_items) - 1
            self.log("[queue] end")
            return
        self._play_queue_index(self.queue_index)

    def play_prev_in_queue(self) -> None:
        if not self.queue_items:
            return
        self.queue_index = max(0, self.queue_index - 1)
        self._play_queue_index(self.queue_index)

    def _play_queue_index(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.queue_items):
            return
        ev = self.queue_items[idx]
        lang = self.queue_lang or self.cmb_lang.currentText()
        ap = ev.audio_path(lang)
        if not ap.exists():
            self.log(f"[queue][skip] {ev.name} missing audio for {lang}: {ap.name}")
            # auto-advance
            if idx + 1 < len(self.queue_items):
                self.queue_index = idx + 1
                self._play_queue_index(self.queue_index)
            return

        # highlight queue row
        self.list_queue.setCurrentRow(idx)
        # highlight event row if present
        self._select_event_in_list(ev)

        self.player.setSource(QUrl.fromLocalFile(str(ap.resolve())))
        self.player.play()
        self.log(f"[queue][play] ({idx+1}/{len(self.queue_items)}) {ev.name} · {ap.name}")

    def _select_event_in_list(self, ev: EventItem) -> None:
        for i in range(self.list_events.count()):
            it = self.list_events.item(i)
            p = it.data(Qt.ItemDataRole.UserRole)
            if p and Path(str(p)) == ev.path:
                self.list_events.setCurrentRow(i)
                break

    def _on_player_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        # auto-advance when queue playback hits end-of-media
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.queue_items and 0 <= self.queue_index < len(self.queue_items) - 1:
                self.queue_index += 1
                self._play_queue_index(self.queue_index)

    def download_selected_audio(self) -> None:
        ev = self._current_event()
        if not ev:
            return
        lang = self.cmb_lang.currentText()
        ap = ev.audio_path(lang)
        if not ap.exists():
            QMessageBox.information(self, "Download", f"No audio found for {lang}.")
            return
        default_name = f"{ev.name}_{lang}{ap.suffix}"
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save audio as",
            str((_project_root() / default_name).resolve()),
            f"Audio (*{ap.suffix});;All files (*)",
        )
        if not out_path:
            return
        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(ap), str(out_path))
            self.log(f"[download] {ap} -> {out_path}")
            QMessageBox.information(self, "Download", "Saved.")
        except Exception as e:
            QMessageBox.warning(self, "Download", f"Failed to save: {e!r}")

    # ---------------- Event library ----------------
    
    # ---------------- TTS event list (Assets + TTS) ----------------
    def _out_root(self) -> Path:
        return (_project_root() / self.le_out.text().strip()).resolve()

    def _tts_event_dirs(self) -> list[Path]:
        out_root = self._out_root()
        if not out_root.exists():
            return []
        dirs = [p for p in out_root.iterdir() if p.is_dir()]
        dirs.sort(key=lambda p: p.name)
        return dirs

    def _tts_status(self, ev_dir: Path) -> dict:
        script_zh = ev_dir / "script_zh.txt"
        script_en = ev_dir / "script_en.txt"
        zh_mp3 = ev_dir / "voice_zh.mp3"
        en_mp3 = ev_dir / "voice_en.mp3"
        zh_wav = ev_dir / "voice_zh.wav"
        en_wav = ev_dir / "voice_en.wav"
        fail_mark = ev_dir / "_tts_failed.txt"
        return {
            "has_script_zh": script_zh.exists() and script_zh.stat().st_size > 0,
            "has_script_en": script_en.exists() and script_en.stat().st_size > 0,
            "has_zh_audio": zh_mp3.exists() or zh_wav.exists(),
            "has_en_audio": en_mp3.exists() or en_wav.exists(),
            "failed": fail_mark.exists(),
        }

    def refresh_tts_events(self) -> None:
        """Refresh the event list shown in Assets + TTS."""
        if not hasattr(self, "list_tts_events"):
            return
        out_root = self._out_root()
        self.list_tts_events.clear()

        if not out_root.exists():
            self.lbl_tts_stats.setText(f"Out dir not found: {out_root}")
            return

        dirs = self._tts_event_dirs()
        n_total = len(dirs)
        n_script_ok = n_zh = n_en = n_failed = 0

        for ev in dirs:
            st = self._tts_status(ev)
            if st["has_script_zh"] or st["has_script_en"]:
                n_script_ok += 1
            if st["has_zh_audio"]:
                n_zh += 1
            if st["has_en_audio"]:
                n_en += 1
            if st["failed"]:
                n_failed += 1

            s_zh = "✓" if st["has_script_zh"] else "✗"
            s_en = "✓" if st["has_script_en"] else "✗"
            a_zh = "✓" if st["has_zh_audio"] else "✗"
            a_en = "✓" if st["has_en_audio"] else "✗"
            fail = " !" if st["failed"] else ""
            text = f"[S zh{s_zh} en{s_en} | A zh{a_zh} en{a_en}]{fail}  {ev.name}"

            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, os.fspath(ev))
            self.list_tts_events.addItem(item)

        self.lbl_tts_stats.setText(
            f"Events: {n_total} | script: {n_script_ok} | zh: {n_zh} | en: {n_en} | failed: {n_failed}"
        )

    def _selected_tts_event_dirs(self) -> list[Path]:
        items = self.list_tts_events.selectedItems()
        out: list[Path] = []
        for it in items:
            p = it.data(Qt.ItemDataRole.UserRole)
            if p:
                out.append(Path(p))
        return out

    def synthesize_selected_tts_list(self) -> None:
        dirs = self._selected_tts_event_dirs()
        if not dirs:
            QMessageBox.information(self, "TTS", "Select one or more events in the left list.")
            return
        self._synthesize_dirs(dirs)

    def synthesize_all_tts_list(self) -> None:
        dirs = self._tts_event_dirs()
        if not dirs:
            QMessageBox.information(self, "TTS", "No events found in output directory.")
            return
        self._synthesize_dirs(dirs)

    def _synthesize_dirs(self, dirs: list[Path]) -> None:
        do_zh = bool(getattr(self, "cb_tts_zh", None) and self.cb_tts_zh.isChecked())
        do_en = bool(getattr(self, "cb_tts_en", None) and self.cb_tts_en.isChecked())
        if not (do_zh or do_en):
            QMessageBox.information(self, "TTS", "Choose Chinese and/or English.")
            return

        total = len(dirs)
        self.pb_tts.setRange(0, total)
        self.pb_tts.setValue(0)

        done = 0
        for ev_dir in dirs:
            ev = EventItem(path=ev_dir, name=ev_dir.name)
            try:
                # backend selection handled inside synthesize()
                if do_zh:
                    self._synthesize_event(ev, lang="zh")
                if do_en:
                    self._synthesize_event(ev, lang="en")
                # clear fail mark on success
                fm = ev_dir / "_tts_failed.txt"
                if fm.exists():
                    fm.unlink()
                self.log(f"[tts] OK: {ev_dir.name}")
            except Exception as e:
                (ev_dir / "_tts_failed.txt").write_text(str(e), encoding="utf-8")
                self.log(f"[tts] FAIL: {ev_dir.name} :: {e!r}")
            finally:
                done += 1
                self.pb_tts.setValue(done)

        self.refresh_events()
        self.refresh_tts_events()


    def refresh_events(self) -> None:
        out_root = (_project_root() / self.le_out.text().strip()).resolve()
        self.list_events.clear()
        if not out_root.exists():
            self.log(f"[library] output folder does not exist: {out_root}")
            return

        # event folder = contains images.json or script_*.txt
        items = []
        for p in sorted(out_root.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
            if not p.is_dir():
                continue
            if (p / "images.json").exists() or (p / "script_zh.txt").exists() or (p / "script_en.txt").exists():
                items.append(EventItem(path=p, name=p.name))

        for it in items:
            lw = QListWidgetItem(it.name)
            lw.setData(Qt.ItemDataRole.UserRole, str(it.path))
            self.list_events.addItem(lw)

        if self.list_events.count() > 0 and self.list_events.currentRow() < 0:
            self.list_events.setCurrentRow(0)

        # keep Assets+TTS list in sync
        try:
            self.refresh_tts_events()
        except Exception:
            pass

    def _current_event(self) -> Optional[EventItem]:
        item = self.list_events.currentItem()
        if not item:
            return None
        p = Path(item.data(Qt.ItemDataRole.UserRole))
        return EventItem(path=p, name=item.text())

    def _on_event_selected(self, current: QListWidgetItem, previous: QListWidgetItem):  # type: ignore[override]
        self._refresh_script_view()

    def _refresh_script_view(self) -> None:
        ev = self._current_event()
        if not ev:
            self.txt_script.setPlainText("")
            return
        lang = self.cmb_lang.currentText()
        self.txt_script.setPlainText(_load_text(ev.script_path(lang)))

    # ---------------- Run dir helpers ----------------
    def _guess_latest_run_dir(self) -> Optional[Path]:
        runs = (_project_root() / "runs")
        if not runs.exists():
            return None

        candidates = []
        for day_dir in runs.iterdir():
            if not day_dir.is_dir():
                continue
            for run_dir in day_dir.glob("run_*"):
                if run_dir.is_dir():
                    candidates.append(run_dir)
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]

    # ---------------- Scheduler ----------------
    def _on_schedule_toggled(self, enabled: bool) -> None:
        if enabled:
            self.scheduler_timer.start()
        else:
            self.scheduler_timer.stop()
        self._update_next_run_label()

    def _update_next_run_label(self) -> None:
        # very lightweight: show today's target time; actual trigger checks each tick
        t = self.time_daily.time()
        self.lbl_next.setText(f"Next: daily at {t.toString('HH:mm')}")

    def _scheduler_tick(self) -> None:
        if not self.cb_schedule.isChecked():
            return
        if self.worker and self.worker.isRunning():
            return

        now = QApplication.instance().applicationState()  # keep Qt active

        # trigger if local time matches HH:MM and we haven't run in the last 23h
        from PyQt6.QtCore import QDateTime

        target = self.time_daily.time()
        cur = QDateTime.currentDateTime()
        if cur.time().hour() == target.hour() and cur.time().minute() == target.minute():
            last_run = self.settings.value("sched/last_run_iso", "", type=str)
            if last_run:
                try:
                    last = QDateTime.fromString(last_run, Qt.DateFormat.ISODate)
                    if last.isValid() and last.secsTo(cur) < 23 * 3600:
                        return
                except Exception:
                    pass
            self.settings.setValue("sched/last_run_iso", cur.toString(Qt.DateFormat.ISODate))
            self.log("[schedule] triggering daily run…")
            self.run_pipeline()
