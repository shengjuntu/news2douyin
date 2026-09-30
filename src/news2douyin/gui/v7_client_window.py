from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
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
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..client_sdk.api import Client
from .api_worker import ApiCallWorker


class V7ClientWindow(QMainWindow):
    def __init__(self, *, settings: QSettings):
        super().__init__()
        self.settings = settings
        self.client = Client(self.settings.value('server/base_url', 'http://127.0.0.1:18080', type=str))
        self._workers: list[ApiCallWorker] = []
        self._last_package: dict[str, Any] | None = None
        self._build_ui()
        self._restore_settings()
        self.refresh_server_status()
        self.refresh_profiles()
        self.refresh_jobs()
        self.refresh_runs()

    def _build_ui(self) -> None:
        self.setWindowTitle('news2douyin V7 Client')
        self.resize(1500, 900)
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.tabs = QTabWidget(central)
        layout.addWidget(self.tabs)

        self._build_server_tab()
        self._build_profiles_tab()
        self._build_jobs_tab()
        self._build_collect_tab()
        self._build_search_tab()

    def _build_server_tab(self) -> None:
        tab = QWidget(self.tabs)
        self.tabs.addTab(tab, 'Server')
        root = QVBoxLayout(tab)

        box = QGroupBox('Server connection', tab)
        form = QFormLayout(box)
        self.le_base_url = QLineEdit(tab)
        self.btn_ping = QPushButton('Test connection', tab)
        self.btn_ping.clicked.connect(self.refresh_server_status)
        row = QHBoxLayout()
        row.addWidget(self.le_base_url, 1)
        row.addWidget(self.btn_ping, 0)
        form.addRow('Base URL', row)
        self.lbl_health = QLabel('Health: —', tab)
        self.lbl_scheduler = QLabel('Scheduler: —', tab)
        form.addRow(self.lbl_health)
        form.addRow(self.lbl_scheduler)
        root.addWidget(box)

        self.server_status_text = QPlainTextEdit(tab)
        self.server_status_text.setReadOnly(True)
        root.addWidget(self.server_status_text, 1)

    def _build_profiles_tab(self) -> None:
        tab = QWidget(self.tabs)
        self.tabs.addTab(tab, 'Profiles')
        split = QSplitter(Qt.Orientation.Horizontal, tab)
        root = QVBoxLayout(tab)
        root.addWidget(split)

        left = QWidget(split)
        lv = QVBoxLayout(left)
        hdr = QHBoxLayout()
        self.btn_profiles_refresh = QPushButton('Refresh', left)
        self.btn_profiles_refresh.clicked.connect(self.refresh_profiles)
        hdr.addWidget(QLabel('Profiles', left))
        hdr.addStretch(1)
        hdr.addWidget(self.btn_profiles_refresh)
        lv.addLayout(hdr)
        self.list_profiles = QListWidget(left)
        self.list_profiles.itemSelectionChanged.connect(self._on_profile_selected)
        lv.addWidget(self.list_profiles, 1)

        right = QWidget(split)
        form = QFormLayout(right)
        self.p_name = QLineEdit(right)
        self.p_provider = QComboBox(right)
        self.p_provider.addItems(['worldnewsapi', 'mock'])
        self.p_country = QLineEdit(right)
        self.p_language = QLineEdit(right)
        self.p_categories = QLineEdit(right)
        self.p_keywords_include = QLineEdit(right)
        self.p_keywords_exclude = QLineEdit(right)
        self.p_source_whitelist = QLineEdit(right)
        self.p_source_blacklist = QLineEdit(right)
        self.p_max_items = QLineEdit(right)
        self.p_market_scope = QLineEdit(right)
        self.p_market_tags = QLineEdit(right)
        self.p_extra = QPlainTextEdit(right)
        self.p_extra.setPlaceholderText('{"api_key_env": "WORLDNEWS_API_KEY"}')
        form.addRow('Name', self.p_name)
        form.addRow('Provider', self.p_provider)
        form.addRow('Country', self.p_country)
        form.addRow('Language', self.p_language)
        form.addRow('Categories', self.p_categories)
        form.addRow('Include keywords', self.p_keywords_include)
        form.addRow('Exclude keywords', self.p_keywords_exclude)
        form.addRow('Whitelist domains', self.p_source_whitelist)
        form.addRow('Blacklist domains', self.p_source_blacklist)
        form.addRow('Max items', self.p_max_items)
        form.addRow('Market scope', self.p_market_scope)
        form.addRow('Market tags', self.p_market_tags)
        form.addRow('Extra JSON', self.p_extra)
        btns = QHBoxLayout()
        self.btn_profile_new = QPushButton('New', right)
        self.btn_profile_new.clicked.connect(self._clear_profile_form)
        self.btn_profile_save = QPushButton('Save profile', right)
        self.btn_profile_save.clicked.connect(self.save_profile)
        btns.addWidget(self.btn_profile_new)
        btns.addWidget(self.btn_profile_save)
        form.addRow(btns)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)

    def _build_jobs_tab(self) -> None:
        tab = QWidget(self.tabs)
        self.tabs.addTab(tab, 'Jobs')
        split = QSplitter(Qt.Orientation.Horizontal, tab)
        root = QVBoxLayout(tab)
        root.addWidget(split)

        left = QWidget(split)
        lv = QVBoxLayout(left)
        hdr = QHBoxLayout()
        self.btn_jobs_refresh = QPushButton('Refresh', left)
        self.btn_jobs_refresh.clicked.connect(self.refresh_jobs)
        hdr.addWidget(QLabel('Jobs', left))
        hdr.addStretch(1)
        hdr.addWidget(self.btn_jobs_refresh)
        lv.addLayout(hdr)
        self.list_jobs = QListWidget(left)
        self.list_jobs.itemSelectionChanged.connect(self._on_job_selected)
        lv.addWidget(self.list_jobs, 1)
        self.btn_job_enable = QPushButton('Enable selected', left)
        self.btn_job_enable.clicked.connect(lambda: self._toggle_selected_job(True))
        self.btn_job_disable = QPushButton('Disable selected', left)
        self.btn_job_disable.clicked.connect(lambda: self._toggle_selected_job(False))
        lv.addWidget(self.btn_job_enable)
        lv.addWidget(self.btn_job_disable)

        right = QWidget(split)
        form = QFormLayout(right)
        self.j_name = QLineEdit(right)
        self.j_enabled = QCheckBox('Enabled', right)
        self.j_timezone = QLineEdit(right)
        self.j_cron = QLineEdit(right)
        self.j_profile = QComboBox(right)
        self.j_auto_editorial = QCheckBox('auto editorial', right)
        self.j_auto_video = QCheckBox('auto video', right)
        self.j_auto_tts = QCheckBox('auto tts', right)
        form.addRow('Name', self.j_name)
        form.addRow('', self.j_enabled)
        form.addRow('Timezone', self.j_timezone)
        form.addRow('Cron expr', self.j_cron)
        form.addRow('Profile', self.j_profile)
        flags = QWidget(right)
        fl = QHBoxLayout(flags); fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(self.j_auto_editorial)
        fl.addWidget(self.j_auto_video)
        fl.addWidget(self.j_auto_tts)
        form.addRow('Auto steps', flags)
        btns = QHBoxLayout()
        self.btn_job_new = QPushButton('New', right)
        self.btn_job_new.clicked.connect(self._clear_job_form)
        self.btn_job_save = QPushButton('Save job', right)
        self.btn_job_save.clicked.connect(self.save_job)
        btns.addWidget(self.btn_job_new)
        btns.addWidget(self.btn_job_save)
        form.addRow(btns)

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)

    def _build_collect_tab(self) -> None:
        tab = QWidget(self.tabs)
        self.tabs.addTab(tab, 'Collect + Runs')
        split = QSplitter(Qt.Orientation.Horizontal, tab)
        root = QVBoxLayout(tab)
        root.addWidget(split)

        left = QWidget(split)
        form = QFormLayout(left)
        self.c_profile = QComboBox(left)
        self.c_country = QLineEdit(left)
        self.c_language = QLineEdit(left)
        self.c_categories = QLineEdit(left)
        self.c_keywords_include = QLineEdit(left)
        self.c_keywords_exclude = QLineEdit(left)
        self.c_max_items = QLineEdit(left)
        self.btn_run_now = QPushButton('Run now', left)
        self.btn_run_now.clicked.connect(self.run_now)
        form.addRow('Profile', self.c_profile)
        form.addRow('Override country', self.c_country)
        form.addRow('Override language', self.c_language)
        form.addRow('Override categories', self.c_categories)
        form.addRow('Override include', self.c_keywords_include)
        form.addRow('Override exclude', self.c_keywords_exclude)
        form.addRow('Override max items', self.c_max_items)
        form.addRow(self.btn_run_now)
        self.collect_result = QPlainTextEdit(left)
        self.collect_result.setReadOnly(True)
        form.addRow('Result', self.collect_result)

        right = QWidget(split)
        rv = QVBoxLayout(right)
        rtop = QHBoxLayout()
        self.btn_runs_refresh = QPushButton('Refresh runs', right)
        self.btn_runs_refresh.clicked.connect(self.refresh_runs)
        rtop.addWidget(QLabel('Recent runs', right))
        rtop.addStretch(1)
        rtop.addWidget(self.btn_runs_refresh)
        rv.addLayout(rtop)
        self.list_runs = QListWidget(right)
        self.list_runs.itemSelectionChanged.connect(self._on_run_selected)
        rv.addWidget(self.list_runs, 1)
        self.run_detail = QPlainTextEdit(right)
        self.run_detail.setReadOnly(True)
        rv.addWidget(self.run_detail, 1)

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)

    def _build_search_tab(self) -> None:
        tab = QWidget(self.tabs)
        self.tabs.addTab(tab, 'Search + Script')
        layout = QVBoxLayout(tab)
        outer = QSplitter(Qt.Orientation.Vertical, tab)
        layout.addWidget(outer)

        top = QWidget(outer)
        top_grid = QGridLayout(top)
        self.e_query = QLineEdit(top)
        self.e_country = QLineEdit(top)
        self.e_topic = QLineEdit(top)
        self.btn_events_search = QPushButton('Search events', top)
        self.btn_events_search.clicked.connect(self.search_events)
        top_grid.addWidget(QLabel('Event query'), 0, 0)
        top_grid.addWidget(self.e_query, 0, 1)
        top_grid.addWidget(QLabel('Country'), 0, 2)
        top_grid.addWidget(self.e_country, 0, 3)
        top_grid.addWidget(QLabel('Topic'), 0, 4)
        top_grid.addWidget(self.e_topic, 0, 5)
        top_grid.addWidget(self.btn_events_search, 0, 6)
        self.list_events = QListWidget(top)
        self.list_events.itemSelectionChanged.connect(self._on_event_selected)
        top_grid.addWidget(self.list_events, 1, 0, 1, 7)
        ev_btns = QHBoxLayout()
        self.btn_build_editorial = QPushButton('Build editorial', top)
        self.btn_build_editorial.clicked.connect(self.build_editorial)
        self.script_profile = QComboBox(top)
        self.script_profile.setEditable(True)
        self.script_profile.addItems(['douyin_market_60s', 'close_review_90s'])
        self.btn_build_script = QPushButton('Build script package', top)
        self.btn_build_script.clicked.connect(self.build_script)
        self.btn_open_package = QPushButton('Open package dir', top)
        self.btn_open_package.clicked.connect(self.open_last_package)
        ev_btns.addWidget(self.btn_build_editorial)
        ev_btns.addWidget(QLabel('Script profile', top))
        ev_btns.addWidget(self.script_profile)
        ev_btns.addWidget(self.btn_build_script)
        ev_btns.addWidget(self.btn_open_package)
        top_grid.addLayout(ev_btns, 2, 0, 1, 7)
        self.event_detail = QPlainTextEdit(top)
        self.event_detail.setReadOnly(True)
        top_grid.addWidget(self.event_detail, 3, 0, 1, 7)

        bottom = QWidget(outer)
        bot_grid = QGridLayout(bottom)
        self.a_query = QLineEdit(bottom)
        self.a_country = QLineEdit(bottom)
        self.a_category = QLineEdit(bottom)
        self.a_duplicates = QComboBox(bottom)
        self.a_duplicates.addItems(['any', 'yes', 'no'])
        self.btn_articles_search = QPushButton('Search articles', bottom)
        self.btn_articles_search.clicked.connect(self.search_articles)
        bot_grid.addWidget(QLabel('Article query'), 0, 0)
        bot_grid.addWidget(self.a_query, 0, 1)
        bot_grid.addWidget(QLabel('Country'), 0, 2)
        bot_grid.addWidget(self.a_country, 0, 3)
        bot_grid.addWidget(QLabel('Category'), 0, 4)
        bot_grid.addWidget(self.a_category, 0, 5)
        bot_grid.addWidget(QLabel('Duplicates'), 0, 6)
        bot_grid.addWidget(self.a_duplicates, 0, 7)
        bot_grid.addWidget(self.btn_articles_search, 0, 8)
        self.list_articles = QListWidget(bottom)
        self.list_articles.itemSelectionChanged.connect(self._on_article_selected)
        bot_grid.addWidget(self.list_articles, 1, 0, 1, 9)
        self.article_detail = QPlainTextEdit(bottom)
        self.article_detail.setReadOnly(True)
        bot_grid.addWidget(self.article_detail, 2, 0, 1, 9)

        outer.setStretchFactor(0, 3)
        outer.setStretchFactor(1, 2)

    def _restore_settings(self) -> None:
        self.le_base_url.setText(self.settings.value('server/base_url', 'http://127.0.0.1:18080', type=str))
        self.e_country.setText(self.settings.value('search/events/country', '', type=str))
        self.a_country.setText(self.settings.value('search/articles/country', '', type=str))
        self.script_profile.setCurrentText(self.settings.value('script/profile', 'douyin_market_60s', type=str))

    def _save_settings(self) -> None:
        self.settings.setValue('server/base_url', self.le_base_url.text().strip())
        self.settings.setValue('search/events/country', self.e_country.text().strip())
        self.settings.setValue('search/articles/country', self.a_country.text().strip())
        self.settings.setValue('script/profile', self.script_profile.currentText().strip())

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_settings()
        super().closeEvent(event)

    def _set_client_from_ui(self) -> None:
        self.client = Client(self.le_base_url.text().strip() or 'http://127.0.0.1:18080')

    def _run_async(self, fn, on_ok, *, on_err=None) -> None:
        self._set_client_from_ui()
        worker = ApiCallWorker(fn)
        worker.finished_ok.connect(on_ok)
        worker.finished_err.connect(on_err or self._show_error)
        worker.finished.connect(lambda: self._cleanup_worker(worker))
        self._workers.append(worker)
        worker.start()

    def _cleanup_worker(self, worker: ApiCallWorker) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        worker.deleteLater()

    def _show_error(self, msg: str) -> None:
        QMessageBox.critical(self, 'Error', msg)

    def _parse_csv(self, text: str) -> list[str]:
        return [x.strip() for x in text.split(',') if x.strip()]

    def _parse_json(self, text: str, default):
        text = text.strip()
        if not text:
            return default
        return json.loads(text)

    def refresh_server_status(self) -> None:
        def task():
            self._set_client_from_ui()
            return {
                'health': self.client.health(),
                'system': self.client.system_status(),
                'scheduler': self.client.scheduler_status(),
            }
        self._run_async(task, self._on_server_status)

    def _on_server_status(self, payload: dict[str, Any]) -> None:
        self.lbl_health.setText(f"Health: {'ok' if payload['health'].get('ok') else 'down'}")
        sched = payload['scheduler']
        self.lbl_scheduler.setText(f"Scheduler: {'running' if sched.get('running') else 'stopped'} | interval={sched.get('interval_sec')}s")
        self.server_status_text.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    def refresh_profiles(self) -> None:
        self._run_async(self.client.list_profiles, self._on_profiles_loaded)

    def _on_profiles_loaded(self, rows: list[dict[str, Any]]) -> None:
        self.list_profiles.clear()
        self.c_profile.clear()
        self.j_profile.clear()
        for row in rows:
            item = QListWidgetItem(f"{row['name']} [{row.get('country','')}/{row.get('language','')}] {row.get('provider','')}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.list_profiles.addItem(item)
            self.c_profile.addItem(row['name'])
            self.j_profile.addItem(row['name'])
        if rows:
            self.script_profile.setCurrentText(self.script_profile.currentText() or 'douyin_market_60s')

    def _on_profile_selected(self) -> None:
        item = self.list_profiles.currentItem()
        if not item:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        self._fill_profile_form(row)

    def _fill_profile_form(self, row: dict[str, Any]) -> None:
        self.p_name.setText(row.get('name', ''))
        idx = max(0, self.p_provider.findText(row.get('provider', 'worldnewsapi')))
        self.p_provider.setCurrentIndex(idx)
        self.p_country.setText(row.get('country', ''))
        self.p_language.setText(row.get('language', ''))
        self.p_categories.setText(', '.join(row.get('categories', [])))
        self.p_keywords_include.setText(', '.join(row.get('keywords_include', [])))
        self.p_keywords_exclude.setText(', '.join(row.get('keywords_exclude', [])))
        self.p_source_whitelist.setText(', '.join(row.get('source_whitelist', [])))
        self.p_source_blacklist.setText(', '.join(row.get('source_blacklist', [])))
        self.p_max_items.setText(str(row.get('max_items', 100)))
        self.p_market_scope.setText(row.get('market_scope', ''))
        self.p_market_tags.setText(', '.join(row.get('market_tags', [])))
        self.p_extra.setPlainText(json.dumps(row.get('extra', {}), ensure_ascii=False, indent=2))

    def _clear_profile_form(self) -> None:
        self._fill_profile_form({'provider': 'worldnewsapi', 'country': 'us', 'language': 'en', 'max_items': 100, 'extra': {}})
        self.p_name.setFocus()

    def save_profile(self) -> None:
        try:
            payload = {
                'name': self.p_name.text().strip(),
                'provider': self.p_provider.currentText().strip() or 'worldnewsapi',
                'country': self.p_country.text().strip() or 'us',
                'language': self.p_language.text().strip() or 'en',
                'categories': self._parse_csv(self.p_categories.text()),
                'keywords_include': self._parse_csv(self.p_keywords_include.text()),
                'keywords_exclude': self._parse_csv(self.p_keywords_exclude.text()),
                'source_whitelist': self._parse_csv(self.p_source_whitelist.text()),
                'source_blacklist': self._parse_csv(self.p_source_blacklist.text()),
                'max_items': int(self.p_max_items.text().strip() or '100'),
                'market_scope': self.p_market_scope.text().strip() or self.p_country.text().strip() or 'global',
                'market_tags': self._parse_csv(self.p_market_tags.text()),
                'extra': self._parse_json(self.p_extra.toPlainText(), {}),
            }
            if not payload['name']:
                raise ValueError('profile name is required')
        except Exception as e:
            self._show_error(str(e))
            return
        self._run_async(lambda: self.client.save_profile(payload), self._on_profile_saved)

    def _on_profile_saved(self, row: dict[str, Any]) -> None:
        self._fill_profile_form(row)
        self.refresh_profiles()

    def refresh_jobs(self) -> None:
        self._run_async(self.client.list_jobs, self._on_jobs_loaded)

    def _on_jobs_loaded(self, rows: list[dict[str, Any]]) -> None:
        self.list_jobs.clear()
        for row in rows:
            status = 'ON' if row.get('enabled') else 'OFF'
            item = QListWidgetItem(f"#{row.get('id')} {row.get('name')} [{status}] {row.get('cron_expr')} -> {row.get('profile_name')}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.list_jobs.addItem(item)

    def _on_job_selected(self) -> None:
        item = self.list_jobs.currentItem()
        if not item:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        self.j_name.setText(row.get('name', ''))
        self.j_enabled.setChecked(bool(row.get('enabled', True)))
        self.j_timezone.setText(row.get('timezone', 'UTC'))
        self.j_cron.setText(row.get('cron_expr', '0 9 * * 1-5'))
        idx = max(0, self.j_profile.findText(row.get('profile_name', '')))
        self.j_profile.setCurrentIndex(idx)
        self.j_auto_editorial.setChecked(bool(row.get('auto_editorial')))
        self.j_auto_video.setChecked(bool(row.get('auto_video')))
        self.j_auto_tts.setChecked(bool(row.get('auto_tts')))

    def _clear_job_form(self) -> None:
        self.j_name.clear()
        self.j_enabled.setChecked(True)
        self.j_timezone.setText('UTC')
        self.j_cron.setText('0 9 * * 1-5')
        self.j_auto_editorial.setChecked(False)
        self.j_auto_video.setChecked(False)
        self.j_auto_tts.setChecked(False)

    def save_job(self) -> None:
        payload = {
            'name': self.j_name.text().strip(),
            'enabled': self.j_enabled.isChecked(),
            'timezone': self.j_timezone.text().strip() or 'UTC',
            'cron_expr': self.j_cron.text().strip() or '0 9 * * 1-5',
            'profile_name': self.j_profile.currentText().strip(),
            'auto_editorial': self.j_auto_editorial.isChecked(),
            'auto_video': self.j_auto_video.isChecked(),
            'auto_tts': self.j_auto_tts.isChecked(),
        }
        if not payload['name'] or not payload['profile_name']:
            self._show_error('job name and profile are required')
            return
        self._run_async(lambda: self.client.save_job(payload), lambda _: self.refresh_jobs())

    def _toggle_selected_job(self, enabled: bool) -> None:
        item = self.list_jobs.currentItem()
        if not item:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        job_id = row.get('id')
        if enabled:
            self._run_async(lambda: self.client.enable_job(job_id), lambda _: self.refresh_jobs())
        else:
            self._run_async(lambda: self.client.disable_job(job_id), lambda _: self.refresh_jobs())

    def run_now(self) -> None:
        profile_name = self.c_profile.currentText().strip()
        if not profile_name:
            self._show_error('profile is required')
            return
        override = {}
        if self.c_country.text().strip():
            override['country'] = self.c_country.text().strip()
        if self.c_language.text().strip():
            override['language'] = self.c_language.text().strip()
        if self.c_categories.text().strip():
            override['categories'] = self._parse_csv(self.c_categories.text())
        if self.c_keywords_include.text().strip():
            override['keywords_include'] = self._parse_csv(self.c_keywords_include.text())
        if self.c_keywords_exclude.text().strip():
            override['keywords_exclude'] = self._parse_csv(self.c_keywords_exclude.text())
        if self.c_max_items.text().strip():
            override['max_items'] = int(self.c_max_items.text().strip())
        self.collect_result.setPlainText('Running...')
        self._run_async(lambda: self.client.run_now(profile_name, override), self._on_run_now_done)

    def _on_run_now_done(self, payload: dict[str, Any]) -> None:
        self.collect_result.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
        self.refresh_runs()
        self.search_events()

    def refresh_runs(self) -> None:
        self._run_async(lambda: self.client.list_runs(50), self._on_runs_loaded)

    def _on_runs_loaded(self, rows: list[dict[str, Any]]) -> None:
        self.list_runs.clear()
        for row in rows:
            item = QListWidgetItem(f"#{row.get('id')} {row.get('run_key')} [{row.get('status')}] {row.get('profile_name')}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.list_runs.addItem(item)

    def _on_run_selected(self) -> None:
        item = self.list_runs.currentItem()
        if not item:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        self.run_detail.setPlainText(json.dumps(row, ensure_ascii=False, indent=2))

    def search_events(self) -> None:
        q = self.e_query.text().strip()
        country = self.e_country.text().strip()
        topic = self.e_topic.text().strip()
        self._run_async(lambda: self.client.search_events(query=q, country=country, topic=topic, limit=100), self._on_events_loaded)

    def _on_events_loaded(self, rows: list[dict[str, Any]]) -> None:
        self.list_events.clear()
        for row in rows:
            item = QListWidgetItem(f"{row.get('event_key')} | {row.get('event_title')} | {row.get('topic')} | n={row.get('article_count')}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.list_events.addItem(item)

    def _current_event_key(self) -> str:
        item = self.list_events.currentItem()
        if not item:
            return ''
        row = item.data(Qt.ItemDataRole.UserRole)
        return row.get('event_key', '')

    def _on_event_selected(self) -> None:
        event_key = self._current_event_key()
        if not event_key:
            return
        self._run_async(lambda: self.client.get_event(event_key), lambda row: self.event_detail.setPlainText(json.dumps(row, ensure_ascii=False, indent=2)))

    def build_editorial(self) -> None:
        event_key = self._current_event_key()
        if not event_key:
            self._show_error('select an event first')
            return
        self._run_async(lambda: self.client.build_editorial(event_key), lambda payload: self.event_detail.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2)))

    def build_script(self) -> None:
        event_key = self._current_event_key()
        if not event_key:
            self._show_error('select an event first')
            return
        profile_name = self.script_profile.currentText().strip() or 'douyin_market_60s'
        self._run_async(lambda: self.client.build_script(event_key, profile_name), self._on_script_built)

    def _on_script_built(self, payload: dict[str, Any]) -> None:
        self._last_package = payload
        package_key = payload.get('package_key', '')
        if package_key:
            self._run_async(lambda: self.client.get_script(package_key), lambda row: self.event_detail.setPlainText(json.dumps(row, ensure_ascii=False, indent=2)))
        else:
            self.event_detail.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    def open_last_package(self) -> None:
        if not self._last_package or not self._last_package.get('output_dir'):
            self._show_error('no package built yet')
            return
        self._open_path(Path(self._last_package['output_dir']))

    def search_articles(self) -> None:
        q = self.a_query.text().strip()
        country = self.a_country.text().strip()
        category = self.a_category.text().strip()
        duplicates = self.a_duplicates.currentText()
        self._run_async(lambda: self.client.search_articles(query=q, country=country, category=category, duplicates=duplicates, limit=100), self._on_articles_loaded)

    def _on_articles_loaded(self, rows: list[dict[str, Any]]) -> None:
        self.list_articles.clear()
        for row in rows:
            dup = 'dup' if row.get('is_duplicate') else 'uniq'
            item = QListWidgetItem(f"{dup} | {row.get('source_domain')} | {row.get('title')}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.list_articles.addItem(item)

    def _on_article_selected(self) -> None:
        item = self.list_articles.currentItem()
        if not item:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        self.article_detail.setPlainText(json.dumps(row, ensure_ascii=False, indent=2))

    def _open_path(self, path: Path) -> None:
        path = path.resolve()
        if not path.exists():
            self._show_error(f'path does not exist: {path}')
            return
        try:
            if platform.system() == 'Windows':
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif platform.system() == 'Darwin':
                subprocess.Popen(['open', str(path)])
            else:
                subprocess.Popen(['xdg-open', str(path)])
        except Exception:
            QDesktopServices.openUrl(path.as_uri())
