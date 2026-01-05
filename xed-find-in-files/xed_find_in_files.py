# -*- coding: utf-8 -*-
#
# Copyright (c) 2025 Gabriell Araujo
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.

"""
Find in Files plugin for Xed (Linux Mint).

Behavior:
- Side panel tab.
- Folder path entry + Browse button (below the entry).
- Search expression entry (Enter triggers search).
- "Search" and "Stop" buttons (text only).
- Preferences (Preferences -> Plugins -> Find in Files -> Preferences):
    - Use ripgrep (rg) if available
    - Expand results by default
- Ignores hidden files/folders.
- Respects .gitignore when possible (rg or git ls-files).
- Results grouped by file in a TreeView (no header):
    - Root: "/absolute/path/to/file.ext (N)"
    - Children: "line: content"
- Single click / Enter on a match jumps to the file/line (reuses already-open tabs).
- Search runs in a background thread; UI updates are batched (100 matches).
- Limit: 5000 matches; Stop cancels current search.

Debug:
- Set XED_DEBUG_FIND_IN_FILES=1 to print debug logs.
"""

from __future__ import annotations

import os
import time
import threading
import subprocess
import shutil
import configparser
from dataclasses import dataclass
from typing import Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gio", "2.0")
from gi.repository import GObject, Gtk, GLib, Gio
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk

try:
    gi.require_version("Xed", "1.0")
    from gi.repository import Xed
except Exception:  # pragma: no cover
    Xed = None

PeasGtk = None
try:
    gi.require_version("PeasGtk", "1.0")
    from gi.repository import PeasGtk as _PeasGtk  # type: ignore
    PeasGtk = _PeasGtk
except Exception:
    PeasGtk = None


# ---------------------------
# Debug helpers
# ---------------------------

def _env_truthy(name: str) -> bool:
    v = GLib.getenv(name)
    if v is None:
        return False
    v = v.strip().lower()
    return v not in ("", "0", "false", "no", "off")


_DEBUG = _env_truthy("XED_DEBUG_FIND_IN_FILES")


def _debug(msg: str) -> None:
    if _DEBUG:
        print(f"[xed-find-in-files] {msg}")


# ---------------------------
# Config (drop-in friendly)
# ---------------------------

_CFG_DIR = os.path.join(GLib.get_user_config_dir(), "xed-find-in-files")
_CFG_PATH = os.path.join(_CFG_DIR, "config.ini")


def _load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["main"] = {
        "use_rg_if_available": "true",
        "expand_results_by_default": "true",
    }
    try:
        if os.path.exists(_CFG_PATH):
            cfg.read(_CFG_PATH, encoding="utf-8")
    except Exception as e:
        _debug(f"Failed to read config: {e!r}")
    return cfg


def _save_config(cfg: configparser.ConfigParser) -> None:
    try:
        os.makedirs(_CFG_DIR, exist_ok=True)
        with open(_CFG_PATH, "w", encoding="utf-8") as f:
            cfg.write(f)
    except Exception as e:
        _debug(f"Failed to write config: {e!r}")


def _cfg_get_bool(cfg: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    try:
        return cfg.getboolean(section, key, fallback=default)
    except Exception:
        return default


def _path_is_within(base: str, path: str) -> bool:
    try:
        base_n = os.path.normpath(base)
        path_n = os.path.normpath(path)
        return os.path.commonpath([base_n, path_n]) == base_n
    except Exception:
        try:
            return os.path.normpath(path).startswith(os.path.normpath(base) + os.sep)
        except Exception:
            return False


def _canonicalize_path(folder: str, p: str) -> str:
    """
    ripgrep can output relative paths depending on the current working directory.
    We normalize to an absolute path whenever possible and keep it stable for:
      - correct display
      - tab reuse (one tab per file)
    """
    if os.path.isabs(p):
        return os.path.normpath(p)

    cand_a = os.path.normpath(os.path.abspath(p))
    if os.path.exists(cand_a) and _path_is_within(folder, cand_a):
        return cand_a

    cand_b = os.path.normpath(os.path.join(folder, p))
    if os.path.exists(cand_b):
        return cand_b

    return cand_a


# ---------------------------
# Data model
# ---------------------------

@dataclass(frozen=True)
class Match:
    file_path: str
    line_no: int
    line_text: str


# ---------------------------
# App-level preferences (Plugins dialog)
# ---------------------------

if Xed is not None and PeasGtk is not None:

    class XedFindInFilesAppActivatable(GObject.Object, Xed.AppActivatable, PeasGtk.Configurable):
        __gtype_name__ = "XedFindInFilesAppActivatable"

        app = GObject.Property(type=Xed.App)

        def __init__(self):
            super().__init__()
            self._cfg = _load_config()

        def do_activate(self):
            pass

        def do_deactivate(self):
            pass

        def do_create_configure_widget(self) -> Gtk.Widget:
            self._cfg = _load_config()

            use_rg = _cfg_get_bool(self._cfg, "main", "use_rg_if_available", True)
            expand_default = _cfg_get_bool(self._cfg, "main", "expand_results_by_default", True)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            box.set_border_width(10)

            cb_rg = Gtk.CheckButton(label="Use ripgrep (rg) if available")
            cb_rg.set_active(use_rg)
            box.pack_start(cb_rg, False, False, 0)

            cb_expand = Gtk.CheckButton(label="Expand results by default")
            cb_expand.set_active(expand_default)
            box.pack_start(cb_expand, False, False, 0)

            def on_rg_toggled(btn):
                val = bool(btn.get_active())
                self._cfg["main"]["use_rg_if_available"] = "true" if val else "false"
                _save_config(self._cfg)
                _debug(f"Preferences changed: use_rg_if_available={val}")

            def on_expand_toggled(btn):
                val = bool(btn.get_active())
                self._cfg["main"]["expand_results_by_default"] = "true" if val else "false"
                _save_config(self._cfg)
                _debug(f"Preferences changed: expand_results_by_default={val}")

            cb_rg.connect("toggled", on_rg_toggled)
            cb_expand.connect("toggled", on_expand_toggled)

            box.show_all()
            return box


# ---------------------------
# Window UI + search logic
# ---------------------------

class XedFindInFilesWindowActivatable(GObject.Object, Xed.WindowActivatable):
    __gtype_name__ = "XedFindInFilesWindowActivatable"

    window = GObject.Property(type=Xed.Window)

    def __init__(self):
        super().__init__()

        self._panel: Optional[Gtk.Widget] = None
        self._tree_store: Optional[Gtk.TreeStore] = None
        self._tree_view: Optional[Gtk.TreeView] = None

        self._folder_entry: Optional[Gtk.Entry] = None
        self._search_entry: Optional[Gtk.Entry] = None
        self._status_label: Optional[Gtk.Label] = None
        self._search_button: Optional[Gtk.Button] = None
        self._stop_button: Optional[Gtk.Button] = None

        self._search_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._search_id = 0

        self._file_nodes: Dict[str, Gtk.TreeIter] = {}
        self._file_counts: Dict[str, int] = {}
        self._total_matches = 0
        self._start_time = 0.0

        self._expand_by_default = True
        self._opening_files: set[str] = set()

    # ---------- Lifecycle ----------

    def do_activate(self):
        _debug("Activating plugin")
        self._build_ui()

        try:
            panel = self.window.get_side_panel()

            icon_candidates = [
                "system-search",
                "system-search-symbolic",
                "edit-find",
                "edit-find-symbolic",
                "search",
                "find",
                "folder-saved-search",
                "folder-saved-search-symbolic",
                "text-x-generic",
                "text-x-generic-symbolic",
            ]

            last_err = None
            for icon_name in icon_candidates:
                try:
                    panel.add_item(self._panel, "Find in Files", icon_name)
                    last_err = None
                    break
                except TypeError as e:
                    last_err = e
                    try:
                        panel.add_item(self._panel, "find-in-files", "Find in Files")
                        last_err = None
                        break
                    except Exception as e2:
                        last_err = e2
                        continue
                except Exception as e:
                    last_err = e
                    continue

            if last_err is not None:
                raise last_err

        except Exception as e:
            _debug(f"Failed to add side panel item: {e!r}")

    def do_deactivate(self):
        _debug("Deactivating plugin")
        self._request_cancel()

        try:
            if self._panel is not None:
                panel = self.window.get_side_panel()
                try:
                    panel.remove_item(self._panel)
                except Exception:
                    pass
                try:
                    self._panel.destroy()
                except Exception:
                    pass
        except Exception as e:
            _debug(f"Failed to remove side panel item: {e!r}")

        self._panel = None
        self._tree_store = None
        self._tree_view = None

    # ---------- UI ----------

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_border_width(6)

        folder_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        folder_entry = Gtk.Entry()
        folder_entry.set_placeholder_text("Folder path")

        browse_btn = Gtk.Button(label="Browse")
        browse_btn.connect("clicked", self._on_browse_folder)

        browse_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        browse_row.pack_start(browse_btn, False, False, 0)

        folder_box.pack_start(folder_entry, False, False, 0)
        folder_box.pack_start(browse_row, False, False, 0)

        search_entry = Gtk.Entry()
        search_entry.set_placeholder_text("Search expression")
        search_entry.connect("activate", self._on_search_activate)

        buttons_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_btn = Gtk.Button(label="Search")
        stop_btn = Gtk.Button(label="Stop")

        search_btn.connect("clicked", self._on_search_clicked)
        stop_btn.connect("clicked", self._on_stop_clicked)
        stop_btn.set_sensitive(True)

        buttons_row.pack_start(search_btn, False, False, 0)
        buttons_row.pack_start(stop_btn, False, False, 0)

        store = Gtk.TreeStore(str, str, int, bool)  # display, file, line, is_file
        tv = Gtk.TreeView(model=store)
        tv.set_headers_visible(False)

        renderer = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("", renderer, text=0)
        tv.append_column(col)

        tv.connect("row-activated", self._on_row_activated)
        tv.connect("button-release-event", self._on_tree_click_release)
        tv.connect("key-press-event", self._on_tree_key_press)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(tv)

        status = Gtk.Label(label="Ready")
        status.set_xalign(0.0)

        root.pack_start(folder_box, False, False, 0)
        root.pack_start(search_entry, False, False, 0)
        root.pack_start(buttons_row, False, False, 0)
        root.pack_start(scrolled, True, True, 0)
        root.pack_start(status, False, False, 0)

        root.show_all()

        self._panel = root
        self._folder_entry = folder_entry
        self._search_entry = search_entry
        self._search_button = search_btn
        self._stop_button = stop_btn
        self._tree_store = store
        self._tree_view = tv
        self._status_label = status

    # ---------- UI handlers ----------

    def _on_browse_folder(self, *args):
        dialog = Gtk.FileChooserDialog(
            title="Select Folder",
            parent=self.window,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        try:
            if dialog.run() == Gtk.ResponseType.OK and self._folder_entry is not None:
                self._folder_entry.set_text(dialog.get_filename() or "")
        finally:
            dialog.destroy()

    def _on_search_activate(self, *args):
        self._on_search_clicked()

    def _on_search_clicked(self, *args):
        folder = (self._folder_entry.get_text() if self._folder_entry else "").strip()
        expr = (self._search_entry.get_text() if self._search_entry else "").strip()

        if not folder:
            self._status_set("Select a folder.")
            return
        if not os.path.isdir(folder):
            self._status_set("Folder does not exist.")
            return
        if not expr:
            self._status_set("Enter a search expression.")
            return

        cfg = _load_config()
        self._expand_by_default = _cfg_get_bool(cfg, "main", "expand_results_by_default", True)

        self._start_search(folder, expr)

    def _on_stop_clicked(self, *args):
        self._request_cancel()
        #self._status_set("Stopping...")

    def _on_row_activated(self, tree_view: Gtk.TreeView, path: Gtk.TreePath, column: Gtk.TreeViewColumn):
        model = tree_view.get_model()
        it = model.get_iter(path)
        is_file = bool(model.get_value(it, 3))

        if is_file:
            if tree_view.row_expanded(path):
                tree_view.collapse_row(path)
            else:
                tree_view.expand_row(path, False)
            return

        file_path = str(model.get_value(it, 1))
        line_no = int(model.get_value(it, 2))
        self._open_file_at_line(file_path, line_no)

    def _on_tree_click_release(self, tree_view: Gtk.TreeView, event):
        try:
            if getattr(event, "button", 0) != 1:
                return False

            res = tree_view.get_path_at_pos(int(event.x), int(event.y))
            if res is None:
                return False

            path, _col, _cellx, _celly = res
            model = tree_view.get_model()
            it = model.get_iter(path)
            is_file = bool(model.get_value(it, 3))
            if is_file:
                return False

            file_path = str(model.get_value(it, 1))
            line_no = int(model.get_value(it, 2))
            self._open_file_at_line(file_path, line_no)
            return True
        except Exception as e:
            _debug(f"click handler failed: {e!r}")
            return False

    def _on_tree_key_press(self, tree_view: Gtk.TreeView, event):
        # Enter on a selected match opens the file.
        try:
            keyval = getattr(event, "keyval", 0)
            if keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):  # type: ignore[name-defined]
                return False
        except Exception:
            # If Gdk isn't available here for some reason, fall back to "activate" behavior.
            return False

        sel = tree_view.get_selection()
        model, it = sel.get_selected()
        if it is None:
            return False
        is_file = bool(model.get_value(it, 3))
        if is_file:
            return False
        file_path = str(model.get_value(it, 1))
        line_no = int(model.get_value(it, 2))
        self._open_file_at_line(file_path, line_no)
        return True

    # ---------- Search control ----------

    def _request_cancel(self):
        if self._search_thread and self._search_thread.is_alive():
            _debug("Cancel requested")
            self._cancel_event.set()

    def _set_buttons_searching(self, searching: bool) -> None:
        """
        Keep both buttons always clickable.

        This avoids the UI getting stuck due to unexpected worker errors or idle handler issues.
        - "Search" already cancels the previous search (best-effort) and starts a new one.
        - "Stop" is a no-op if nothing is running.
        """
        try:
            if self._search_button is not None:
                self._search_button.set_sensitive(True)
            if self._stop_button is not None:
                self._stop_button.set_sensitive(True)
        except Exception as e:
            _debug(f"Failed to set button state: {e!r}")

    def _start_search(self, folder: str, expr: str):
        if self._search_thread and self._search_thread.is_alive():
            self._request_cancel()

        self._search_id += 1
        sid = self._search_id
        self._cancel_event.clear()

        self._start_time = time.time()
        GLib.idle_add(self._reset_ui_for_new_search, sid)

        self._search_thread = threading.Thread(
            target=self._search_worker,
            args=(sid, folder, expr),
            daemon=True,
        )
        self._search_thread.start()

    def _reset_ui_for_new_search(self, sid: int):
        if sid != self._search_id:
            return False

        self._file_nodes.clear()
        self._file_counts.clear()
        self._total_matches = 0

        if self._tree_store is not None:
            self._tree_store.clear()

        self._set_buttons_searching(True)
        self._status_set("Searching...")
        return False

    # ---------- Worker ----------

    def _search_worker(self, sid: int, folder: str, expr: str):
        try:
            cfg = _load_config()
            use_rg_pref = _cfg_get_bool(cfg, "main", "use_rg_if_available", True)

            rg_path = shutil.which("rg") if use_rg_pref else None
            backend = "rg" if rg_path else "python"

            _debug(
                f"Search start id={sid} folder={folder!r} expr={expr!r} backend={backend!r} "
                f"use_rg_pref={use_rg_pref} expand_by_default={self._expand_by_default}"
            )

            if backend == "rg":
                self._run_rg_search(sid, folder, expr, rg_path)
            else:
                self._run_python_search(sid, folder, expr)

        except Exception as e:
            _debug(f"Worker crashed: {e!r}")
            GLib.idle_add(self._finish_search, sid, str(e), False, False)

    # ---------- Backend: ripgrep ----------

    def _run_rg_search(self, sid: int, folder: str, expr: str, rg_path: str):
        cmd = [
            rg_path,
            "-F",
            "--vimgrep",
            "--color", "never",
            "--no-messages",
            "--",
            expr,
            folder,
        ]
        _debug("rg cmd: " + " ".join(cmd))

        truncated = False
        matches_sent = 0
        batch: List[Match] = []

        proc = None
        try:
            # IMPORTANT: use DEVNULL for stderr to avoid potential deadlocks with a full PIPE.
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert proc.stdout is not None

            for line in proc.stdout:
                if sid != self._search_id:
                    _debug(f"Discarding rg output for old search id={sid}")
                    break
                if self._cancel_event.is_set():
                    _debug("rg search canceled by user")
                    break

                s = line.rstrip("\n")
                parts = s.split(":", 3)
                if len(parts) < 4:
                    continue

                raw_file = parts[0]
                file_path = _canonicalize_path(folder, raw_file)

                try:
                    line_no = int(parts[1])
                except Exception:
                    continue

                text_part = parts[3]
                batch.append(Match(file_path=file_path, line_no=line_no, line_text=text_part))
                matches_sent += 1

                if matches_sent >= 5000:
                    truncated = True
                    _debug("Result limit reached (5000) in rg backend")
                    break

                if len(batch) >= 100:
                    GLib.idle_add(self._apply_batch, sid, folder, batch)
                    batch = []

            if batch:
                GLib.idle_add(self._apply_batch, sid, folder, batch)

        finally:
            stopped = bool(self._cancel_event.is_set())
            if proc is not None:
                try:
                    if stopped or truncated:
                        proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            GLib.idle_add(self._finish_search, sid, None, stopped, truncated)

    # ---------- Backend: Python fallback ----------

    def _run_python_search(self, sid: int, folder: str, expr: str):
        truncated = False
        batch: List[Match] = []
        matches_sent = 0

        file_list = self._enumerate_files_python(folder)
        _debug(f"python backend file count={len(file_list)}")

        for file_path in file_list:
            if sid != self._search_id:
                _debug(f"Stopping python scan (old search id={sid})")
                break
            if self._cancel_event.is_set():
                _debug("python search canceled by user")
                break

            if self._is_hidden_path(folder, file_path):
                continue

            try:
                if not os.path.isfile(file_path):
                    continue
            except Exception:
                continue

            if self._is_binary_file(file_path):
                continue

            try:
                with open(file_path, "rb") as f:
                    line_no = 0
                    for raw in f:
                        line_no += 1
                        if self._cancel_event.is_set():
                            break

                        line = raw.decode("utf-8", errors="replace")
                        if expr in line:
                            batch.append(Match(file_path=file_path, line_no=line_no, line_text=line.rstrip("\n")))
                            matches_sent += 1

                            if matches_sent >= 5000:
                                truncated = True
                                _debug("Result limit reached (5000) in python backend")
                                break

                            if len(batch) >= 100:
                                GLib.idle_add(self._apply_batch, sid, folder, batch)
                                batch = []

                        if truncated:
                            break

                if truncated:
                    break

            except Exception as e:
                _debug(f"Failed to scan file {file_path!r}: {e!r}")
                continue

        if batch:
            GLib.idle_add(self._apply_batch, sid, folder, batch)

        stopped = bool(self._cancel_event.is_set())
        GLib.idle_add(self._finish_search, sid, None, stopped, truncated)

    def _enumerate_files_python(self, folder: str) -> List[str]:
        git_path = shutil.which("git")
        if git_path is None:
            return self._walk_files(folder)

        try:
            p = subprocess.run(
                [git_path, "-C", folder, "rev-parse", "--is-inside-work-tree"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.0,
            )
            if p.returncode != 0 or "true" not in (p.stdout or "").strip().lower():
                return self._walk_files(folder)

            p2 = subprocess.run(
                [git_path, "-C", folder, "rev-parse", "--show-toplevel"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.0,
            )
            if p2.returncode != 0:
                return self._walk_files(folder)

            repo_root = (p2.stdout or "").strip()
            if not repo_root:
                return self._walk_files(folder)

            p3 = subprocess.run(
                [git_path, "-C", folder, "ls-files", "-co", "--exclude-standard", "--", "."],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10.0,
            )
            if p3.returncode != 0:
                return self._walk_files(folder)

            files: List[str] = []
            base = os.path.normpath(folder)
            for rel in (p3.stdout or "").splitlines():
                rel = rel.strip()
                if not rel:
                    continue
                abs_path = os.path.normpath(os.path.join(repo_root, rel))
                if _path_is_within(base, abs_path):
                    files.append(abs_path)

            return files

        except Exception as e:
            _debug(f"git-based enumeration failed: {e!r}")
            return self._walk_files(folder)

    def _walk_files(self, folder: str) -> List[str]:
        files: List[str] = []
        for root, dirs, fnames in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in fnames:
                if fn.startswith("."):
                    continue
                files.append(os.path.join(root, fn))
        return files

    def _is_hidden_path(self, root_folder: str, abs_path: str) -> bool:
        try:
            rel = os.path.relpath(abs_path, root_folder)
        except Exception:
            return False
        for p in rel.split(os.sep):
            if p.startswith(".") and p not in (".", ".."):
                return True
        return False

    def _is_binary_file(self, path: str) -> bool:
        try:
            with open(path, "rb") as f:
                chunk = f.read(8192)
            return b"\0" in chunk
        except Exception:
            return True

    # ---------- Main-thread UI update ----------

    def _apply_batch(self, sid: int, folder: str, batch: List[Match]):
        if sid != self._search_id:
            return False
        if self._tree_store is None:
            return False

        for m in batch:
            if self._cancel_event.is_set():
                break
            try:
                self._add_match_row(folder, m.file_path, m.line_no, m.line_text)
            except Exception as e:
                _debug(f"Failed to add match row: {e!r}")

        self._status_set(f"Searching... {self._total_matches} matches")
        return False

    def _add_match_row(self, folder: str, file_path: str, line_no: int, line_text: str):
        if self._tree_store is None:
            return

        file_path = _canonicalize_path(folder, file_path)

        pit = self._file_nodes.get(file_path)
        new_parent = False
        if pit is None:
            pit = self._tree_store.append(None, ["", file_path, 0, True])
            self._file_nodes[file_path] = pit
            self._file_counts[file_path] = 0
            new_parent = True

        display_line = self._format_match_line(line_no, line_text)
        self._tree_store.append(pit, [display_line, file_path, int(line_no), False])

        self._file_counts[file_path] = int(self._file_counts.get(file_path, 0)) + 1
        self._total_matches += 1

        parent_label = self._format_file_label(folder, file_path, self._file_counts[file_path])
        try:
            self._tree_store.set_value(pit, 0, parent_label)
        except Exception:
            pass

        if new_parent and self._expand_by_default and self._tree_view is not None:
            try:
                path = self._tree_store.get_path(pit)
                self._tree_view.expand_row(path, False)
            except Exception:
                pass

    def _format_file_label(self, folder: str, file_path: str, count: int) -> str:
        """
        UI format:
          - If file is directly under the chosen folder:
              main.c (12)
          - Otherwise:
              main.c (src/utils) (12)

        The directory is relative to the chosen folder to save space.
        """
        try:
            rel = os.path.relpath(file_path, folder)
        except Exception:
            rel = os.path.basename(file_path)

        rel = rel.replace(os.sep, "/").lstrip("./")
        fn = os.path.basename(rel)
        rel_dir = os.path.dirname(rel).replace(os.sep, "/")

        if rel_dir in ("", "."):
            return f"{fn} ({count})"
        return f"{fn} ({rel_dir}) ({count})"

    def _format_match_line(self, line_no: int, line_text: str) -> str:
        s = (line_text or "").rstrip("\n")
        if len(s) > 400:
            s = s[:397] + "..."
        return f"{int(line_no)}: {s}"

    def _finish_search(self, sid: int, error: Optional[str], stopped: bool, truncated: bool):
        if sid != self._search_id:
            return False

        elapsed_ms = int((time.time() - self._start_time) * 1000.0) if self._start_time else 0
        file_count = len(self._file_nodes)
        matches = self._total_matches

        # Re-enable Search no matter what.
        self._set_buttons_searching(False)

        if error:
            self._status_set(f"Error: {error}")
            _debug(f"Search finished with error: {error!r}")
            return False

        if stopped:
            msg = f"Stopped: {matches} matches in {file_count} files ({elapsed_ms} ms)"
        else:
            msg = f"Done: {matches} matches in {file_count} files ({elapsed_ms} ms)"

        if truncated:
            msg += " (limit 5000 reached)"

        if matches == 0 and not stopped:
            msg = f"No matches ({elapsed_ms} ms)"

        self._status_set(msg)
        _debug(msg)
        return False

    def _status_set(self, text: str):
        if self._status_label is None:
            return
        GLib.idle_add(self._status_label.set_text, text)

    # ---------- Open file at line (reuse open tab) ----------

    def _find_open_tab_for_file(self, gfile: Gio.File):
        if hasattr(self.window, "get_tab_from_location"):
            try:
                tab = self.window.get_tab_from_location(gfile)
                if tab is not None:
                    return tab
            except Exception:
                pass

        uri = ""
        try:
            uri = gfile.get_uri()
        except Exception:
            uri = ""

        if uri and hasattr(self.window, "get_documents") and hasattr(self.window, "get_tab_from_document"):
            try:
                for doc in self.window.get_documents():
                    try:
                        loc = doc.get_location()
                    except Exception:
                        loc = None
                    if loc is None:
                        continue
                    try:
                        if loc.equal(gfile):
                            return self.window.get_tab_from_document(doc)
                    except Exception:
                        try:
                            if loc.get_uri() == uri:
                                return self.window.get_tab_from_document(doc)
                        except Exception:
                            pass
            except Exception:
                pass

        return None

    def _open_file_at_line(self, file_path: str, line_no: int):
        try:
            folder = (self._folder_entry.get_text() if self._folder_entry else "").strip()
        except Exception:
            folder = ""
        if folder and os.path.isdir(folder):
            file_path = _canonicalize_path(folder, file_path)

        _debug(f"Open request: {file_path!r} at line {line_no}")

        try:
            gfile = Gio.File.new_for_path(file_path)
        except Exception as e:
            _debug(f"Gio.File.new_for_path failed: {e!r}")
            return

        tab = self._find_open_tab_for_file(gfile)
        if tab is not None:
            _debug("Reusing existing tab")
            try:
                self.window.set_active_tab(tab)
            except Exception:
                pass
            GLib.idle_add(self._place_cursor_and_scroll_retry, tab, int(line_no), 0, file_path)
            return

        if file_path in self._opening_files:
            return GLib.timeout_add(120, self._open_file_at_line, file_path, line_no)

        self._opening_files.add(file_path)

        created = False
        for args in (
            (gfile,),
            (gfile, None),
            (gfile, None, 0),
            (gfile, None, 0, False),
            (gfile, None, 0, False, True),
        ):
            try:
                self.window.create_tab_from_location(*args)
                created = True
                break
            except TypeError:
                continue
            except Exception as e:
                _debug(f"create_tab_from_location{args} failed: {e!r}")
                continue

        if not created:
            uri = ""
            try:
                uri = gfile.get_uri()
            except Exception:
                uri = ""
            if uri:
                for args in (
                    (uri,),
                    (uri, None),
                    (uri, None, 0),
                    (uri, None, 0, False),
                    (uri, None, 0, False, True),
                ):
                    try:
                        self.window.create_tab_from_uri(*args)
                        created = True
                        break
                    except TypeError:
                        continue
                    except Exception as e:
                        _debug(f"create_tab_from_uri{args} failed: {e!r}")
                        continue

        tab2 = None
        if hasattr(self.window, "get_active_tab"):
            try:
                tab2 = self.window.get_active_tab()
            except Exception:
                tab2 = None

        if tab2 is None:
            tab2 = self._find_open_tab_for_file(gfile)

        if tab2 is None or not hasattr(tab2, "get_view"):
            _debug(f"Failed to obtain a valid tab object (created={created}, tab_type={type(tab2)!r})")
            self._opening_files.discard(file_path)
            return

        try:
            self.window.set_active_tab(tab2)
        except Exception:
            pass

        GLib.idle_add(self._place_cursor_and_scroll_retry, tab2, int(line_no), 0, file_path)

    def _place_cursor_and_scroll_retry(self, tab, line_no: int, attempt: int, file_path: str):
        try:
            view = tab.get_view()
            if view is None:
                raise RuntimeError("tab.get_view() returned None")

            buf = view.get_buffer()
            if buf is None:
                raise RuntimeError("view.get_buffer() returned None")

            try:
                if hasattr(buf, "set_readonly"):
                    buf.set_readonly(False)
            except Exception:
                pass

            target = max(int(line_no) - 1, 0)
            it = buf.get_iter_at_line(target)

            buf.place_cursor(it)
            view.grab_focus()
            view.scroll_to_iter(it, 0.25, False, 0.0, 0.0)

            self._opening_files.discard(file_path)
            return False

        except Exception as e:
            if attempt < 10:
                return GLib.timeout_add(100, self._place_cursor_and_scroll_retry, tab, line_no, attempt + 1, file_path)

            _debug(f"Failed to place cursor/scroll after retries: {e!r}")
            self._opening_files.discard(file_path)
            return False
