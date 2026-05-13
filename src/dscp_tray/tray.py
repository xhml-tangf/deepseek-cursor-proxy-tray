"""DeepSeek Cursor Proxy - System Tray Manager.

A long-running tray app that owns the proxy subprocess, performs
restarts as a stop+start (no transient "restarting" state), and
exposes a right-click menu (status, public URL, copy / restart /
watchdog toggle, logs, quit).

Launched headlessly via pythonw.exe so no console window is created.

State machine (five states):

      ┌─────────┐  start_proxy()   ┌──────────┐  health OK    ┌─────────┐
      │ stopped │ ───────────────► │ starting │ ────────────► │ running │
      └─────────┘                  └──────────┘               └─────────┘
           ▲                            │                          │
           │                            │ fail                     │ crash /
           │                            ▼                          │ probe-fail x3
           │                       ┌─────────┐                     │
           │                       │  error  │ ◄───────────────────┘
           │                       └─────────┘
           │       stop_proxy()         │
           │      ┌──────────┐          │ restart (user / watchdog)
           └──────┤ stopping │ ◄────────┘
                  └──────────┘

- stopped: clean idle (initial, or after user Quit). Gray.
- starting: launching proxy + waiting for ngrok URL. Amber.
- running:  /healthz OK. Green.
            (sub-flag: accumulated health failures → orange tint)
- stopping: tearing down (taskkill /T /F). Blueish gray.
- error:    last attempt failed or proxy crashed. Red. Carries an
            error label (one of ERROR_*) describing the failure.

Restart = stop_proxy() then start_proxy(); no `restarting` state.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw
import pystray


# ----------------------------- Configuration -----------------------------


def _find_proxy_exe() -> Path | None:
    """Locate the upstream `deepseek-cursor-proxy.exe`.

    Order:
      1. Same Scripts/ dir as the running Python (covers `pythonw -m dscp_tray`
         and Task Scheduler launches from the project venv).
      2. Anywhere on PATH (covers globally installed proxy).
    Returns None when nothing is found.
    """
    here = Path(sys.executable).parent
    candidate = here / "deepseek-cursor-proxy.exe"
    if candidate.exists():
        return candidate
    found = shutil.which("deepseek-cursor-proxy")
    if found:
        return Path(found)
    return None


# Upstream proxy's own data dir; we share it so tray + proxy + CLI agree on
# config.yaml / reasoning_content.sqlite3 locations.
DATA_DIR = Path(os.environ["USERPROFILE"]) / ".deepseek-cursor-proxy"
PROXY_CONFIG_FILE = DATA_DIR / "config.yaml"
REASONING_DB_FILE = DATA_DIR / "reasoning_content.sqlite3"
LOG_DIR = DATA_DIR / "logs"
STATUS_FILE = DATA_DIR / "status.json"
TRAY_LOG_FILE = LOG_DIR / "tray.log"

NGROK_CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "ngrok"
NGROK_CONFIG_FILE = NGROK_CONFIG_DIR / "ngrok.yml"
NGROK_DASHBOARD_URL = "https://dashboard.ngrok.com/get-started/your-authtoken"
NGROK_INSTALL_HINT = "winget install Ngrok.Ngrok"

LOCAL_URL = "http://127.0.0.1:9000/v1"
HEALTHZ_URL = "http://127.0.0.1:9000/healthz"
NGROK_API = "http://127.0.0.1:4040/api/tunnels"

HEALTH_CHECK_INTERVAL = 60            # seconds between probes
HEALTH_CHECK_TIMEOUT = 10             # per-probe timeout
HEALTH_FAILURES_BEFORE_RESTART = 3    # consecutive misses before watchdog acts
NGROK_URL_WAIT = 35                   # seconds to wait for public URL
RESTART_GRACE_SECONDS = 8             # wait after stop before next start
STOP_WAIT_SECONDS = 8                 # max wait for process tree to exit
SUPERVISOR_LOOP_SLEEP = 1.0           # tick granularity (so cancel is snappy)

CREATE_NO_WINDOW = 0x08000000


# ----------------------------- States ------------------------------------

STATE_STOPPED  = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING  = "running"
STATE_STOPPING = "stopping"
STATE_ERROR    = "error"

VALID_STATES = {STATE_STOPPED, STATE_STARTING, STATE_RUNNING, STATE_STOPPING, STATE_ERROR}

BASE_COLORS = {
    STATE_STOPPED:  (160, 160, 160),  # neutral gray (clean stopped)
    STATE_STARTING: (220, 180,  40),  # amber
    STATE_RUNNING:  ( 60, 170,  80),  # green
    STATE_STOPPING: (140, 140, 200),  # blueish gray
    STATE_ERROR:    (200,  50,  50),  # red
}
ICON_COLOR_WARNING = (220, 130,  40)  # orange — running but accumulating health failures


# ----------------------------- Error labels ------------------------------

ERROR_EXE_MISSING       = "exe-missing"
ERROR_LAUNCH_FAILED     = "launch-failed"
ERROR_STARTUP_EXIT      = "startup-exit"
ERROR_CRASHED           = "crashed"
ERROR_UNRESPONSIVE      = "unresponsive"
ERROR_NGROK_MISSING     = "ngrok-missing"
ERROR_AUTHTOKEN_MISSING = "authtoken-missing"

ERROR_LABELS = {
    ERROR_EXE_MISSING:       "proxy executable not found",
    ERROR_LAUNCH_FAILED:     "failed to launch process",
    ERROR_STARTUP_EXIT:      "proxy exited during startup",
    ERROR_CRASHED:           "proxy crashed unexpectedly",
    ERROR_UNRESPONSIVE:      "proxy unresponsive (health timeout)",
    ERROR_NGROK_MISSING:     "ngrok.exe not installed",
    ERROR_AUTHTOKEN_MISSING: "ngrok authtoken not configured",
}

FAILURE_REASON_PROCESS_GONE = "process-gone"
FAILURE_REASON_PROBE_FAILED = "probe-failed"


# ----------------------------- Logging setup -----------------------------

TRAY_LOG_MAX_BYTES = 1 * 1024 * 1024   # 1 MB per file
TRAY_LOG_BACKUP_COUNT = 3              # keep tray.log + 3 rotated copies (~4 MB total)

LOG_DIR.mkdir(parents=True, exist_ok=True)
_rotating_handler = logging.handlers.RotatingFileHandler(
    filename=str(TRAY_LOG_FILE),
    maxBytes=TRAY_LOG_MAX_BYTES,
    backupCount=TRAY_LOG_BACKUP_COUNT,
    encoding="utf-8",
)
_rotating_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
logging.basicConfig(level=logging.INFO, handlers=[_rotating_handler])
log = logging.getLogger("tray")


# ----------------------------- Utilities ---------------------------------


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _taskkill_tree(pid: int) -> None:
    """Forcefully terminate a process and all its descendants."""
    if pid <= 4:
        return
    log.info("taskkill /T /F /PID %s", pid)
    subprocess.run(
        ["taskkill", "/T", "/F", "/PID", str(pid)],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )


def _kill_port_9000_owners() -> None:
    """Kill any process listening on :9000 (orphan from previous tray run)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("netstat failed: %s", exc)
        return
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        local_addr = parts[1]
        if not local_addr.endswith(":9000"):
            continue
        try:
            pids.add(int(parts[4]))
        except ValueError:
            continue
    for pid in pids:
        _taskkill_tree(pid)


def _kill_ngrok_orphans() -> None:
    """Kill any ngrok.exe processes still floating around."""
    subprocess.run(
        ["taskkill", "/F", "/IM", "ngrok.exe"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )


def _copy_to_clipboard(text: str) -> bool:
    try:
        subprocess.run(
            ["clip"],
            input=text.encode("utf-16le"),
            check=True,
            timeout=3,
            creationflags=CREATE_NO_WINDOW,
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("clipboard copy failed: %s", exc)
        return False


def _find_ngrok_exe() -> str | None:
    """Locate ngrok.exe even if PATH doesn't have it (Task Scheduler context)."""
    from shutil import which
    found = which("ngrok")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "")
    candidates = [
        Path(local) / "Microsoft" / "WinGet" / "Packages"
            / "Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe" / "ngrok.exe",
        Path(local) / "ngrok" / "ngrok.exe",
        Path(program_files) / "ngrok" / "ngrok.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _load_proxy_config() -> tuple[dict, str | None]:
    """Load ~/.deepseek-cursor-proxy/config.yaml as a plain dict.

    Returns (config_dict, error_message). On any error returns ({}, message).
    Preserves unknown keys for round-trip.
    """
    if not PROXY_CONFIG_FILE.exists():
        return {}, f"config file does not exist: {PROXY_CONFIG_FILE}"
    try:
        import yaml
        with open(PROXY_CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:
        return {}, f"failed to parse config.yaml: {exc}"
    if not isinstance(data, dict):
        return {}, "config.yaml is not a YAML mapping"
    return data, None


def _save_proxy_config(data: dict) -> tuple[bool, str]:
    """Write the proxy config.yaml. Returns (ok, message)."""
    try:
        import yaml
        PROXY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(
            data, default_flow_style=False, sort_keys=False, allow_unicode=True
        )
        PROXY_CONFIG_FILE.write_text(text, encoding="utf-8")
        return True, f"saved {PROXY_CONFIG_FILE}"
    except Exception as exc:
        log.exception("failed to save proxy config")
        return False, f"save failed: {exc}"


def _read_cache_stats() -> dict:
    """Read read-only stats from the reasoning_content.sqlite3 file.

    Returns dict with: exists, size_bytes, row_count, oldest_ts, newest_ts.
    Missing fields are set to None if the DB or table doesn't exist."""
    out = {
        "exists": REASONING_DB_FILE.exists(),
        "size_bytes": None,
        "row_count": None,
        "oldest_ts": None,
        "newest_ts": None,
        "path": str(REASONING_DB_FILE),
        "error": None,
    }
    if not out["exists"]:
        return out
    try:
        out["size_bytes"] = REASONING_DB_FILE.stat().st_size
    except OSError as exc:
        out["error"] = f"stat failed: {exc}"
        return out
    try:
        import sqlite3
        # Open in read-only URI mode so we never lock or alter the file.
        uri = f"file:{REASONING_DB_FILE.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT COUNT(*), MIN(created_at), MAX(created_at) "
                "FROM reasoning_cache"
            ).fetchone()
            if row is not None:
                out["row_count"] = int(row[0] or 0)
                out["oldest_ts"] = row[1]
                out["newest_ts"] = row[2]
        finally:
            conn.close()
    except Exception as exc:
        out["error"] = f"sqlite read failed: {exc}"
    return out


def _clear_cache_via_store() -> tuple[bool, str, int]:
    """Use the upstream ReasoningStore to clear the cache.

    Relies on `deepseek-cursor-proxy` being installed as a real dependency
    in the active environment (see pyproject.toml). Returns (ok, message,
    rows_deleted).
    """
    try:
        from deepseek_cursor_proxy.reasoning_store import ReasoningStore
    except Exception as exc:
        log.exception("failed to import ReasoningStore")
        return False, f"import failed: {exc}", 0
    try:
        store = ReasoningStore(REASONING_DB_FILE)
        deleted = store.clear()
        store.close()
        return True, f"deleted {deleted} row(s)", deleted
    except Exception as exc:
        log.exception("clear via store failed")
        return False, f"clear failed: {exc}", 0


def _format_bytes(num: int | None) -> str:
    if num is None:
        return "—"
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    if num < 1024 * 1024 * 1024:
        return f"{num / (1024 * 1024):.2f} MB"
    return f"{num / (1024 * 1024 * 1024):.2f} GB"


def _format_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return f"{ts}"


def _ngrok_authtoken_status() -> tuple[bool, str]:
    """Return (configured, detail) by reading ngrok.yml without exposing the token.

    detail is a short human-readable string (token length / config file path /
    parse error / not configured)."""
    if not NGROK_CONFIG_FILE.exists():
        return False, f"no config file at {NGROK_CONFIG_FILE}"
    try:
        import yaml  # PyYAML, already a project dependency
        with open(NGROK_CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"failed to parse ngrok.yml: {exc}"
    if not isinstance(data, dict):
        return False, "ngrok.yml has unexpected structure"
    token = data.get("authtoken")
    if not token:
        agent = data.get("agent")
        if isinstance(agent, dict):
            token = agent.get("authtoken")
    if isinstance(token, str) and token.strip():
        masked = f"{len(token)}-char token (***{token[-4:]})" if len(token) >= 8 else "(short)"
        return True, masked
    return False, "no authtoken field set"


def _make_icon(color: tuple[int, int, int]) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, size - 4, size - 4], fill=(*color, 255))
    draw.ellipse(
        [4, 4, size - 4, size - 4],
        outline=(20, 20, 20, 200),
        width=2,
    )
    return img


# ----------------------------- Tray manager ------------------------------


class ProxyTray:
    def __init__(self) -> None:
        self._state: str = STATE_STOPPED
        self._public_url: str | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_file: Path | None = None
        self._err_file: Path | None = None
        self._started_at: str | None = None
        self._stopping = threading.Event()
        self._watchdog_enabled = True
        self._health_failures = 0
        self._last_error: str | None = None         # error label, see ERROR_*
        self._last_failure_reason: str | None = None  # detail for next watchdog trip
        self._lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._config_window_open = False      # authtoken window
        self._settings_window_open = False    # proxy settings window
        self._cache_window_open = False       # reasoning cache window

        self.icon = pystray.Icon(
            "deepseek-cursor-proxy",
            _make_icon(BASE_COLORS[STATE_STOPPED]),
            title="DeepSeek Cursor Proxy",
            menu=self._build_menu(),
        )

    # --- public entry point ---

    def run(self) -> None:
        threading.Thread(target=self._supervisor_loop, daemon=True).start()
        self.icon.run()

    # ============================================================
    # Menu
    # ============================================================

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(self._menu_status_label, None, enabled=False),
            pystray.MenuItem(
                self._menu_error_label, None, enabled=False,
                visible=lambda item: self._state == STATE_ERROR,
            ),
            pystray.MenuItem(self._menu_url_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Copy Public URL", self._on_copy_url),
            pystray.MenuItem("Restart Proxy", self._on_restart),
            pystray.MenuItem(
                "Watchdog (auto-restart on health failure)",
                self._on_toggle_watchdog,
                checked=lambda item: self._watchdog_enabled,
            ),
            pystray.MenuItem(
                "Dismiss Error (error -> stopped)", self._on_clear_error,
                visible=lambda item: self._state == STATE_ERROR,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Proxy Settings...", self._on_proxy_settings,
            ),
            pystray.MenuItem(
                "Configure ngrok authtoken...", self._on_configure_authtoken,
            ),
            pystray.MenuItem(
                "Reasoning Cache...", self._on_reasoning_cache,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Latest Proxy Log", self._on_open_log),
            pystray.MenuItem("Open Tray Log", self._on_open_tray_log),
            pystray.MenuItem("Open Log Folder", self._on_open_log_folder),
            pystray.MenuItem("Open status.json", self._on_open_status),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit (stops proxy)", self._on_quit),
        )

    def _menu_status_label(self, _item: pystray.MenuItem) -> str:
        with self._lock:
            state = self._state
            proc = self._proc
            failures = self._health_failures
        suffix = ""
        if proc is not None and proc.poll() is None:
            suffix = f" (pid={proc.pid})"
        if state == STATE_RUNNING and failures > 0:
            return (
                f"Status: {state}{suffix} — "
                f"health failures {failures}/{HEALTH_FAILURES_BEFORE_RESTART}"
            )
        return f"Status: {state}{suffix}"

    def _menu_error_label(self, _item: pystray.MenuItem) -> str:
        with self._lock:
            err = self._last_error
        if err is None:
            return ""
        return f"Error: {ERROR_LABELS.get(err, err)} [{err}]"

    def _menu_url_label(self, _item: pystray.MenuItem) -> str:
        with self._lock:
            return f"Public: {self._public_url or '(waiting for ngrok...)'}"

    def _on_copy_url(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        with self._lock:
            url = self._public_url
        if not url:
            self._notify("No public URL yet")
            return
        if _copy_to_clipboard(url):
            self._notify(f"Copied: {url}")
        else:
            self._notify("Copy failed (see tray.log)")

    def _on_restart(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        # User-initiated restart: clear any stale error so the brief stopped
        # gap is gray, not red. If the new start_proxy itself fails, that
        # error path will set a fresh ERROR_* anyway.
        with self._lock:
            self._last_error = None
            self._last_failure_reason = None
        self._refresh_icon()
        self._refresh_menu()
        threading.Thread(
            target=self._restart_proxy, args=("user-requested",), daemon=True
        ).start()

    def _on_toggle_watchdog(
        self, _icon: pystray.Icon, _item: pystray.MenuItem
    ) -> None:
        self._watchdog_enabled = not self._watchdog_enabled
        log.info("watchdog enabled=%s", self._watchdog_enabled)
        # toggling on resets the counter to avoid an immediate stale-trip
        if self._watchdog_enabled:
            with self._lock:
                self._health_failures = 0
        self._refresh_icon()

    def _on_clear_error(
        self, _icon: pystray.Icon, _item: pystray.MenuItem
    ) -> None:
        with self._lock:
            had = self._last_error
            self._last_error = None
            self._last_failure_reason = None
        if had:
            log.info("user dismissed error %s -> stopped", had)
        # error -> stopped (gray). User acknowledges; no auto-recovery.
        self._set_state(STATE_STOPPED)

    def _on_configure_authtoken(
        self, _icon: pystray.Icon, _item: pystray.MenuItem
    ) -> None:
        with self._lock:
            if self._config_window_open:
                log.info("config window already open, focusing skipped")
                self._notify("ngrok config window already open")
                return
            self._config_window_open = True
        threading.Thread(
            target=self._run_authtoken_window, daemon=True
        ).start()

    def _run_authtoken_window(self) -> None:
        """Open a small tkinter window to set ngrok authtoken.

        Runs in its own daemon thread; tkinter requires its mainloop to be in
        the same thread as the Tk root, which is why we create root here.
        """
        try:
            import tkinter as tk
            from tkinter import messagebox, ttk
        except ImportError:
            log.exception("tkinter unavailable")
            with self._lock:
                self._config_window_open = False
            return

        try:
            root = tk.Tk()
            root.title("ngrok Authtoken Configuration")
            root.geometry("560x340")
            root.resizable(False, False)
            # Bring to front briefly so user notices it appearing.
            try:
                root.attributes("-topmost", True)
                root.after(150, lambda: root.attributes("-topmost", False))
            except tk.TclError:
                pass

            container = ttk.Frame(root, padding=14)
            container.pack(fill="both", expand=True)

            ttk.Label(
                container,
                text="Configure the ngrok authtoken used by this proxy.",
                font=("Segoe UI", 10, "bold"),
            ).pack(anchor="w")

            ttk.Label(
                container,
                text=(
                    "Get a free token from your ngrok dashboard:\n"
                    f"  {NGROK_DASHBOARD_URL}"
                ),
                foreground="#444",
            ).pack(anchor="w", pady=(4, 8))

            # --- Status row ---
            configured, detail = _ngrok_authtoken_status()
            status_var = tk.StringVar(
                value=f"Currently: {'configured' if configured else 'NOT configured'} — {detail}"
            )
            status_lbl = ttk.Label(
                container,
                textvariable=status_var,
                foreground="#2a7a2a" if configured else "#a23a3a",
            )
            status_lbl.pack(anchor="w")
            ttk.Label(
                container,
                text=f"Config file: {NGROK_CONFIG_FILE}",
                foreground="#666",
                font=("Segoe UI", 8),
            ).pack(anchor="w", pady=(0, 12))

            # --- Token entry ---
            ttk.Label(
                container, text="New authtoken:"
            ).pack(anchor="w")
            token_var = tk.StringVar()
            entry = ttk.Entry(
                container, textvariable=token_var, show="*", width=64,
            )
            entry.pack(fill="x", pady=(2, 4))
            entry.focus_set()

            show_var = tk.BooleanVar(value=False)

            def _toggle_show() -> None:
                entry.config(show="" if show_var.get() else "*")

            ttk.Checkbutton(
                container, text="Show token",
                variable=show_var, command=_toggle_show,
            ).pack(anchor="w")

            restart_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(
                container,
                text="Restart proxy after saving (recommended)",
                variable=restart_var,
            ).pack(anchor="w", pady=(8, 0))

            # --- Buttons ---
            btn_frame = ttk.Frame(container)
            btn_frame.pack(fill="x", pady=(14, 0))

            def _save() -> None:
                token = token_var.get().strip()
                if not token:
                    messagebox.showerror(
                        "Empty token",
                        "Please paste an authtoken before saving.",
                        parent=root,
                    )
                    return
                if len(token) < 20:
                    if not messagebox.askyesno(
                        "Suspicious token",
                        (
                            f"The value you entered is only {len(token)} chars long, "
                            "which is unusually short for an ngrok authtoken. "
                            "Save anyway?"
                        ),
                        parent=root,
                    ):
                        return

                ok, message = self._apply_ngrok_authtoken(token)
                if not ok:
                    messagebox.showerror(
                        "Save failed", message, parent=root,
                    )
                    return

                new_configured, new_detail = _ngrok_authtoken_status()
                status_var.set(
                    f"Currently: "
                    f"{'configured' if new_configured else 'NOT configured'} — {new_detail}"
                )
                status_lbl.config(
                    foreground="#2a7a2a" if new_configured else "#a23a3a"
                )

                should_restart = restart_var.get()
                token_var.set("")
                root.destroy()
                if should_restart:
                    log.info("config window: restarting proxy to pick up new authtoken")
                    threading.Thread(
                        target=self._restart_proxy,
                        args=("authtoken-update",),
                        daemon=True,
                    ).start()
                self._notify(
                    "ngrok authtoken saved"
                    + (" — restarting proxy" if should_restart else "")
                )

            def _cancel() -> None:
                root.destroy()

            ttk.Button(btn_frame, text="Cancel", command=_cancel).pack(
                side="right", padx=(6, 0)
            )
            ttk.Button(btn_frame, text="Save", command=_save).pack(
                side="right"
            )

            root.bind("<Return>", lambda _e: _save())
            root.bind("<Escape>", lambda _e: _cancel())

            root.mainloop()
        except Exception:
            log.exception("config window crashed")
        finally:
            with self._lock:
                self._config_window_open = False

    def _apply_ngrok_authtoken(self, token: str) -> tuple[bool, str]:
        """Run `ngrok config add-authtoken <token>`. Returns (ok, message)."""
        ngrok_exe = _find_ngrok_exe()
        if ngrok_exe is None:
            return False, (
                "ngrok.exe not found on PATH or in known install locations.\n"
                "Install ngrok first (e.g. winget install Ngrok.Ngrok)."
            )
        log.info("running: ngrok config add-authtoken <hidden> (exe=%s)", ngrok_exe)
        try:
            result = subprocess.run(
                [ngrok_exe, "config", "add-authtoken", token],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.exception("ngrok config command failed")
            return False, f"Failed to run ngrok: {exc}"

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            log.warning(
                "ngrok config exited %s: %s", result.returncode, err
            )
            return False, f"ngrok exited with code {result.returncode}:\n{err}"

        log.info("ngrok authtoken saved successfully")
        return True, (result.stdout or "saved").strip()

    # -----------------------------------------------------------
    # Proxy Settings window (Task 3)
    # -----------------------------------------------------------

    def _on_proxy_settings(
        self, _icon: pystray.Icon, _item: pystray.MenuItem
    ) -> None:
        with self._lock:
            if self._settings_window_open:
                self._notify("Proxy Settings window already open")
                return
            self._settings_window_open = True
        threading.Thread(
            target=self._run_proxy_settings_window, daemon=True
        ).start()

    def _run_proxy_settings_window(self) -> None:
        try:
            import tkinter as tk
            from tkinter import messagebox, ttk
        except ImportError:
            log.exception("tkinter unavailable")
            with self._lock:
                self._settings_window_open = False
            return

        cfg, load_err = _load_proxy_config()
        if load_err is not None:
            log.warning("proxy settings load: %s", load_err)

        try:
            root = tk.Tk()
            root.title("Proxy Settings")
            root.geometry("620x560")
            root.resizable(False, False)
            try:
                root.attributes("-topmost", True)
                root.after(150, lambda: root.attributes("-topmost", False))
            except tk.TclError:
                pass

            container = ttk.Frame(root, padding=14)
            container.pack(fill="both", expand=True)

            ttk.Label(
                container, text="Edit proxy config.yaml",
                font=("Segoe UI", 10, "bold"),
            ).pack(anchor="w")
            ttk.Label(
                container,
                text=f"File: {PROXY_CONFIG_FILE}",
                foreground="#666", font=("Segoe UI", 8),
            ).pack(anchor="w", pady=(0, 4))
            if load_err:
                ttk.Label(
                    container, text=f"⚠ {load_err} — defaults will be used",
                    foreground="#a23a3a",
                ).pack(anchor="w", pady=(0, 6))

            form = ttk.Frame(container)
            form.pack(fill="both", expand=True, pady=(8, 0))
            form.columnconfigure(1, weight=1)

            def _row(grid_row: int, label: str, widget: tk.Widget,
                     hint: str = "") -> None:
                ttk.Label(form, text=label).grid(
                    row=grid_row, column=0, sticky="w", padx=(0, 8), pady=3
                )
                widget.grid(row=grid_row, column=1, sticky="ew", pady=3)
                if hint:
                    ttk.Label(form, text=hint, foreground="#666",
                              font=("Segoe UI", 8)).grid(
                        row=grid_row, column=2, sticky="w", padx=(8, 0)
                    )

            # --- Form variables initialized from cfg with defaults ---
            v_model = tk.StringVar(value=str(cfg.get("model", "deepseek-v4-pro")))
            v_thinking = tk.StringVar(value=str(cfg.get("thinking", "enabled")))
            v_effort = tk.StringVar(value=str(cfg.get("reasoning_effort", "max")))
            v_display = tk.BooleanVar(value=bool(cfg.get("display_reasoning", True)))
            v_collapsible = tk.BooleanVar(
                value=bool(cfg.get("collasible_reasoning",
                                   cfg.get("collapsible_reasoning", True)))
            )
            v_missing = tk.StringVar(
                value=str(cfg.get("missing_reasoning_strategy", "recover"))
            )
            v_timeout = tk.StringVar(value=str(cfg.get("request_timeout", 300)))
            v_ngrok_url = tk.StringVar(value=str(cfg.get("ngrok_url") or ""))
            v_verbose = tk.BooleanVar(value=bool(cfg.get("verbose", False)))
            v_cors = tk.BooleanVar(value=bool(cfg.get("cors", False)))

            # --- Widgets ---
            _row(0, "Model:",
                 ttk.Combobox(form, textvariable=v_model,
                              values=["deepseek-v4-pro", "deepseek-v4-flash"],
                              state="readonly", width=24),
                 "DeepSeek model to forward to")
            _row(1, "Thinking:",
                 ttk.Combobox(form, textvariable=v_thinking,
                              values=["enabled", "disabled"],
                              state="readonly", width=24),
                 "enable reasoning mode")
            _row(2, "Reasoning effort:",
                 ttk.Combobox(form, textvariable=v_effort,
                              values=["low", "medium", "high", "max", "xhigh"],
                              state="readonly", width=24),
                 "more = slower, deeper thinking")
            _row(3, "Display reasoning:",
                 ttk.Checkbutton(form, variable=v_display,
                                 text="mirror reasoning to Cursor as <details>"),
                 "")
            _row(4, "Collapsible reasoning:",
                 ttk.Checkbutton(form, variable=v_collapsible,
                                 text="use <details> folding (vs inline)"),
                 "")
            _row(5, "Missing reasoning:",
                 ttk.Combobox(form, textvariable=v_missing,
                              values=["recover", "reject"],
                              state="readonly", width=24),
                 "recover = auto-fix; reject = strict")
            _row(6, "Request timeout (s):",
                 ttk.Entry(form, textvariable=v_timeout, width=26),
                 "upstream DeepSeek API timeout")
            _row(7, "Reserved domain:",
                 ttk.Entry(form, textvariable=v_ngrok_url, width=42),
                 "blank = random ngrok-free.dev URL")
            _row(8, "Verbose logging:",
                 ttk.Checkbutton(form, variable=v_verbose,
                                 text="write full payloads to proxy logs"),
                 "")
            _row(9, "CORS headers:",
                 ttk.Checkbutton(form, variable=v_cors,
                                 text="send permissive CORS"),
                 "for browser clients")

            ttk.Separator(container, orient="horizontal").pack(
                fill="x", pady=(12, 8)
            )
            restart_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(
                container,
                text="Restart proxy after saving (recommended)",
                variable=restart_var,
            ).pack(anchor="w")

            btn_frame = ttk.Frame(container)
            btn_frame.pack(fill="x", pady=(12, 0))

            def _open_yaml_in_editor() -> None:
                if PROXY_CONFIG_FILE.exists():
                    os.startfile(str(PROXY_CONFIG_FILE))  # noqa: S606

            def _save() -> None:
                # Validate request_timeout
                try:
                    timeout = float(v_timeout.get())
                    if timeout <= 0:
                        raise ValueError("must be > 0")
                except ValueError as exc:
                    messagebox.showerror(
                        "Invalid timeout",
                        f"request_timeout must be a positive number: {exc}",
                        parent=root,
                    )
                    return

                # Reload current config to preserve any keys we don't expose.
                fresh, _ = _load_proxy_config()
                merged = dict(fresh) if fresh else {}
                merged["model"] = v_model.get().strip() or "deepseek-v4-pro"
                merged["thinking"] = v_thinking.get()
                merged["reasoning_effort"] = v_effort.get()
                merged["display_reasoning"] = bool(v_display.get())
                # Preserve the upstream's typo ("collasible") if that's what's
                # in the file, but always write a value.
                key = (
                    "collasible_reasoning"
                    if "collasible_reasoning" in merged or "collasible_reasoning" not in merged
                    else "collapsible_reasoning"
                )
                merged[key] = bool(v_collapsible.get())
                merged["missing_reasoning_strategy"] = v_missing.get()
                merged["request_timeout"] = timeout
                # ngrok itself is not exposed in the UI (it must stay on for
                # this proxy to be useful to Cursor). Preserve whatever value
                # the raw YAML has; default to True if absent.
                if "ngrok" not in merged:
                    merged["ngrok"] = True
                ngrok_url_value = v_ngrok_url.get().strip()
                if ngrok_url_value:
                    merged["ngrok_url"] = ngrok_url_value
                else:
                    merged.pop("ngrok_url", None)
                merged["verbose"] = bool(v_verbose.get())
                merged["cors"] = bool(v_cors.get())

                ok, message = _save_proxy_config(merged)
                if not ok:
                    messagebox.showerror("Save failed", message, parent=root)
                    return

                log.info("proxy settings saved")
                should_restart = restart_var.get()
                root.destroy()
                if should_restart:
                    log.info("settings: restarting proxy to pick up new config")
                    threading.Thread(
                        target=self._restart_proxy,
                        args=("settings-update",),
                        daemon=True,
                    ).start()
                self._notify(
                    "Proxy settings saved"
                    + (" — restarting" if should_restart else "")
                )

            ttk.Button(btn_frame, text="Open raw YAML",
                       command=_open_yaml_in_editor).pack(side="left")
            ttk.Button(btn_frame, text="Cancel",
                       command=root.destroy).pack(side="right", padx=(6, 0))
            ttk.Button(btn_frame, text="Save", command=_save).pack(side="right")

            root.bind("<Escape>", lambda _e: root.destroy())

            root.mainloop()
        except Exception:
            log.exception("settings window crashed")
        finally:
            with self._lock:
                self._settings_window_open = False

    # -----------------------------------------------------------
    # Reasoning Cache window (Task 4)
    # -----------------------------------------------------------

    def _on_reasoning_cache(
        self, _icon: pystray.Icon, _item: pystray.MenuItem
    ) -> None:
        with self._lock:
            if self._cache_window_open:
                self._notify("Reasoning Cache window already open")
                return
            self._cache_window_open = True
        threading.Thread(
            target=self._run_cache_window, daemon=True
        ).start()

    def _run_cache_window(self) -> None:
        try:
            import tkinter as tk
            from tkinter import messagebox, ttk
        except ImportError:
            log.exception("tkinter unavailable")
            with self._lock:
                self._cache_window_open = False
            return

        try:
            root = tk.Tk()
            root.title("Reasoning Cache")
            root.geometry("520x340")
            root.resizable(False, False)
            try:
                root.attributes("-topmost", True)
                root.after(150, lambda: root.attributes("-topmost", False))
            except tk.TclError:
                pass

            container = ttk.Frame(root, padding=14)
            container.pack(fill="both", expand=True)

            ttk.Label(
                container, text="DeepSeek reasoning_content cache",
                font=("Segoe UI", 10, "bold"),
            ).pack(anchor="w")
            ttk.Label(
                container,
                text=(
                    "SQLite cache that the proxy uses to restore the\n"
                    "reasoning_content field Cursor drops from history."
                ),
                foreground="#444",
            ).pack(anchor="w", pady=(2, 8))

            stats_frame = ttk.LabelFrame(container, text="Current stats", padding=8)
            stats_frame.pack(fill="x")
            stats_frame.columnconfigure(1, weight=1)

            var_path = tk.StringVar()
            var_size = tk.StringVar()
            var_rows = tk.StringVar()
            var_oldest = tk.StringVar()
            var_newest = tk.StringVar()
            var_status = tk.StringVar()

            def _row(grid_row: int, label: str, var: tk.StringVar) -> None:
                ttk.Label(stats_frame, text=label, foreground="#444").grid(
                    row=grid_row, column=0, sticky="w", padx=(0, 8), pady=1
                )
                ttk.Label(stats_frame, textvariable=var,
                          font=("Segoe UI", 9, "bold")).grid(
                    row=grid_row, column=1, sticky="w", pady=1
                )

            _row(0, "Database file:", var_path)
            _row(1, "File size:", var_size)
            _row(2, "Row count:", var_rows)
            _row(3, "Oldest entry:", var_oldest)
            _row(4, "Newest entry:", var_newest)

            status_lbl = ttk.Label(container, textvariable=var_status,
                                   foreground="#666",
                                   font=("Segoe UI", 8))
            status_lbl.pack(anchor="w", pady=(8, 0))

            def _refresh() -> None:
                stats = _read_cache_stats()
                var_path.set(stats["path"])
                if not stats["exists"]:
                    var_size.set("(no file yet)")
                    var_rows.set("—")
                    var_oldest.set("—")
                    var_newest.set("—")
                    var_status.set("Cache file does not exist yet.")
                    return
                if stats["error"]:
                    var_status.set(f"⚠ {stats['error']}")
                else:
                    var_status.set("OK")
                var_size.set(_format_bytes(stats["size_bytes"]))
                var_rows.set(
                    "—" if stats["row_count"] is None
                    else f"{stats['row_count']:,}"
                )
                var_oldest.set(_format_ts(stats["oldest_ts"]))
                var_newest.set(_format_ts(stats["newest_ts"]))

            _refresh()

            btn_frame = ttk.Frame(container)
            btn_frame.pack(fill="x", pady=(14, 0))

            def _open_folder() -> None:
                os.startfile(str(REASONING_DB_FILE.parent))  # noqa: S606

            def _clear() -> None:
                if not messagebox.askyesno(
                    "Clear reasoning cache?",
                    (
                        "This will delete ALL cached reasoning_content rows.\n\n"
                        "Effect:\n"
                        "  - In-progress conversations may lose the cached\n"
                        "    thinking trace for past tool calls.\n"
                        "  - The proxy will recover automatically thanks to\n"
                        "    missing_reasoning_strategy=recover, but DeepSeek\n"
                        "    will likely re-think from scratch on the next turn.\n\n"
                        "Proceed?"
                    ),
                    parent=root,
                ):
                    return
                ok, msg, deleted = _clear_cache_via_store()
                if ok:
                    messagebox.showinfo(
                        "Cache cleared",
                        f"{msg}.\nReasoning cache reset.",
                        parent=root,
                    )
                    log.info("user cleared reasoning cache: %s", msg)
                    _refresh()
                else:
                    messagebox.showerror(
                        "Clear failed", msg, parent=root,
                    )

            ttk.Button(btn_frame, text="Refresh", command=_refresh).pack(side="left")
            ttk.Button(btn_frame, text="Open folder",
                       command=_open_folder).pack(side="left", padx=(6, 0))
            ttk.Button(btn_frame, text="Close",
                       command=root.destroy).pack(side="right", padx=(6, 0))
            ttk.Button(btn_frame, text="Clear All...",
                       command=_clear).pack(side="right")

            root.bind("<Escape>", lambda _e: root.destroy())

            root.mainloop()
        except Exception:
            log.exception("cache window crashed")
        finally:
            with self._lock:
                self._cache_window_open = False

    def _on_open_log(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        with self._lock:
            target = self._log_file
        if target and target.exists():
            os.startfile(str(target))  # noqa: S606
        else:
            self._on_open_log_folder(_icon, _item)

    def _on_open_tray_log(
        self, _icon: pystray.Icon, _item: pystray.MenuItem
    ) -> None:
        if TRAY_LOG_FILE.exists():
            os.startfile(str(TRAY_LOG_FILE))  # noqa: S606

    def _on_open_log_folder(
        self, _icon: pystray.Icon, _item: pystray.MenuItem
    ) -> None:
        os.startfile(str(LOG_DIR))  # noqa: S606

    def _on_open_status(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        if STATUS_FILE.exists():
            os.startfile(str(STATUS_FILE))  # noqa: S606

    def _on_quit(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        log.info("quit requested")
        # User quit -> normal stopped (gray), drop any stale error.
        with self._lock:
            self._last_error = None
            self._last_failure_reason = None
        self._stopping.set()
        self._stop_proxy(reason="user-quit")
        self.icon.stop()

    # ============================================================
    # Supervisor loop
    # ============================================================

    def _supervisor_loop(self) -> None:
        try:
            log.info("=== tray supervisor starting ===")
            _kill_port_9000_owners()
            _kill_ngrok_orphans()
            time.sleep(1.5)
            # _start_proxy itself runs the dependency preflight; if it fails
            # we'll land in ERROR with the right label automatically.
            self._start_proxy(reason="supervisor-boot")
        except Exception:
            log.exception("initial start crashed")
            # If something inside _start_proxy raised before it could set a
            # terminal state, force ERROR so the icon never hangs on amber
            # forever. _enter_error is idempotent in that respect.
            with self._lock:
                state = self._state
            if state not in (STATE_RUNNING, STATE_ERROR):
                self._enter_error(ERROR_LAUNCH_FAILED)

        next_health_check = time.monotonic() + HEALTH_CHECK_INTERVAL
        while not self._stopping.is_set():
            self._stopping.wait(SUPERVISOR_LOOP_SLEEP)
            if self._stopping.is_set():
                break
            now = time.monotonic()
            if now >= next_health_check:
                try:
                    self._tick_health()
                except Exception:
                    log.exception("health tick crashed")
                next_health_check = now + HEALTH_CHECK_INTERVAL

    def _preflight_dependencies(self) -> bool:
        """Verify ngrok.exe + authtoken before we try to start the proxy.

        Returns True if it's safe to proceed. On failure, transitions to
        STATE_ERROR with an appropriate label and (in the authtoken case)
        auto-opens the config window so the user can fix it on the spot.

        Skips the check if config.yaml has ngrok=false.
        """
        cfg, _err = _load_proxy_config()
        ngrok_enabled = bool(cfg.get("ngrok", True)) if cfg else True
        if not ngrok_enabled:
            log.info("preflight: ngrok disabled in config, skipping ngrok checks")
            return True

        # 1) ngrok.exe must be locatable.
        if _find_ngrok_exe() is None:
            log.error("preflight: ngrok.exe not found on PATH or known locations")
            self._enter_error(ERROR_NGROK_MISSING)
            self._notify(
                f"ngrok is required but not installed.\nRun:  {NGROK_INSTALL_HINT}"
            )
            return False

        # 2) authtoken must be configured in ngrok.yml.
        configured, detail = _ngrok_authtoken_status()
        if not configured:
            log.error("preflight: ngrok authtoken not configured (%s)", detail)
            self._enter_error(ERROR_AUTHTOKEN_MISSING)
            self._notify("ngrok authtoken not configured — opening config window")
            # Auto-open the authtoken window (non-blocking; runs in its own thread).
            self._on_configure_authtoken(self.icon, None)  # type: ignore[arg-type]
            return False

        log.info("preflight ok: ngrok exe found, authtoken configured")
        return True

    def _tick_health(self) -> None:
        with self._lock:
            state = self._state
            proc = self._proc

        # Only meaningful while we believe we're running.
        # Skip ticks during transitions; let the action complete first.
        if state != STATE_RUNNING:
            return

        if proc is None or proc.poll() is not None:
            with self._lock:
                self._health_failures += 1
                failures = self._health_failures
                self._last_failure_reason = FAILURE_REASON_PROCESS_GONE
            log.warning("proxy process gone (failures=%s)", failures)
        elif self._probe_health():
            with self._lock:
                had_failures = self._health_failures > 0
                self._health_failures = 0
                self._last_failure_reason = None
            if had_failures:
                log.info("health recovered")
                self._refresh_icon()
            return
        else:
            with self._lock:
                self._health_failures += 1
                failures = self._health_failures
                self._last_failure_reason = FAILURE_REASON_PROBE_FAILED
            log.warning("health probe failed (failures=%s)", failures)

        # If we got here, this tick was a miss.
        self._refresh_icon()
        self._refresh_menu()
        with self._lock:
            failures = self._health_failures
            reason = self._last_failure_reason
        if failures < HEALTH_FAILURES_BEFORE_RESTART:
            return
        if not self._watchdog_enabled:
            log.info(
                "watchdog disabled; staying in running state with failures=%s",
                failures,
            )
            return

        log.info(
            "watchdog tripped after %s consecutive failures (reason=%s)",
            failures, reason,
        )
        # Move to ERROR with the matching label first, so the user sees the
        # red icon and reason during the restart attempt. _restart_proxy will
        # then drive the recovery (stopping -> starting -> running on success;
        # back to error with a fresh label if the new start_proxy fails).
        if reason == FAILURE_REASON_PROCESS_GONE:
            self._enter_error(ERROR_CRASHED)
        elif reason == FAILURE_REASON_PROBE_FAILED:
            self._enter_error(ERROR_UNRESPONSIVE)
        self._restart_proxy(reason="watchdog")

    def _probe_health(self) -> bool:
        try:
            with urllib.request.urlopen(
                HEALTHZ_URL, timeout=HEALTH_CHECK_TIMEOUT
            ) as resp:
                return getattr(resp, "status", 200) == 200
        except (urllib.error.URLError, OSError):
            return False

    # ============================================================
    # Proxy lifecycle (the only two real verbs)
    # ============================================================

    def _start_proxy(self, reason: str = "") -> None:
        """{STOPPED, ERROR} -> STARTING -> RUNNING (or back to ERROR on failure)."""
        if not self._action_lock.acquire(blocking=False):
            log.info("start: action in progress, skipping (reason=%s)", reason)
            return
        try:
            with self._lock:
                if self._state in (STATE_STARTING, STATE_RUNNING):
                    log.info(
                        "start: already in state=%s, skipping", self._state
                    )
                    return
                self._health_failures = 0
            self._set_state(STATE_STARTING)
            log.info("start_proxy reason=%s", reason)

            proxy_exe = _find_proxy_exe()
            if proxy_exe is None:
                log.error(
                    "proxy exe missing: not in %s and not on PATH",
                    Path(sys.executable).parent,
                )
                self._enter_error(ERROR_EXE_MISSING)
                return

            # Dependency preflight (ngrok exe + authtoken), only if config
            # actually uses ngrok. Bail early into ERROR so the user gets a
            # specific label instead of a generic startup-exit later.
            if not self._preflight_dependencies():
                return

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            log_file = LOG_DIR / f"proxy-{stamp}.log"
            err_file = LOG_DIR / f"proxy-{stamp}.err.log"

            try:
                out_fh = open(log_file, "wb")
                err_fh = open(err_file, "wb")
                proc = subprocess.Popen(
                    [str(proxy_exe)],
                    cwd=str(DATA_DIR),
                    stdout=out_fh,
                    stderr=err_fh,
                    stdin=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
            except OSError:
                log.exception("Popen failed")
                self._enter_error(ERROR_LAUNCH_FAILED)
                return

            with self._lock:
                self._proc = proc
                self._log_file = log_file
                self._err_file = err_file
                self._started_at = _now_iso()
                self._public_url = None

            public_url = self._wait_for_public_url(proc, NGROK_URL_WAIT)
            with self._lock:
                self._public_url = public_url

            if proc.poll() is not None:
                log.error(
                    "proxy exited during startup exit=%s log=%s err=%s",
                    proc.returncode,
                    log_file,
                    err_file,
                )
                with self._lock:
                    self._proc = None
                self._enter_error(ERROR_STARTUP_EXIT)
                return

            # _set_state writes status.json on every transition, so we no
            # longer call _write_status explicitly here.
            self._set_state(STATE_RUNNING)
            log.info(
                "proxy started pid=%s public=%s",
                proc.pid,
                public_url or "(none)",
            )
            if public_url:
                _copy_to_clipboard(public_url)
        finally:
            self._action_lock.release()

    def _stop_proxy(self, reason: str = "") -> None:
        """Any state -> STOPPING -> STOPPED. Preserves _last_error."""
        if not self._action_lock.acquire(blocking=False):
            log.info("stop: action in progress, skipping (reason=%s)", reason)
            return
        try:
            with self._lock:
                proc = self._proc
                state = self._state
            # Fast path: nothing alive to stop.
            if proc is None or proc.poll() is not None:
                if state not in (STATE_STOPPED, STATE_ERROR):
                    log.info("stop: process already gone, normalising state")
                with self._lock:
                    self._proc = None
                if state != STATE_ERROR:
                    self._set_state(STATE_STOPPED)
                # If we were in ERROR, leave the state alone — caller wants
                # the error label preserved across the restart attempt.
                return

            self._set_state(STATE_STOPPING)
            log.info(
                "stop_proxy pid=%s reason=%s (taskkill /T /F)",
                proc.pid,
                reason,
            )

            # Kill the whole tree (proxy + spawned ngrok).
            _taskkill_tree(proc.pid)
            try:
                proc.wait(timeout=STOP_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                log.warning("proc.wait timed out after taskkill")

            _kill_ngrok_orphans()
            _kill_port_9000_owners()

            with self._lock:
                self._proc = None
                self._public_url = None
            self._set_state(STATE_STOPPED)
        finally:
            self._action_lock.release()

    def _restart_proxy(self, reason: str = "") -> None:
        """Composite verb: stop_proxy() then start_proxy(). No 'restarting' state."""
        log.info("restart_proxy reason=%s", reason)
        self._stop_proxy(reason=reason)
        # ngrok cloud session sometimes needs a beat to release before we
        # spawn a new one; otherwise the next ngrok process may fail or
        # collide with the dying session.
        time.sleep(RESTART_GRACE_SECONDS)
        self._start_proxy(reason=reason)

    # ============================================================
    # Helpers
    # ============================================================

    def _wait_for_public_url(
        self, proc: subprocess.Popen[bytes], timeout: float
    ) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return None
            try:
                with urllib.request.urlopen(NGROK_API, timeout=2) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                time.sleep(0.8)
                continue
            tunnels = payload.get("tunnels") or payload.get("endpoints") or []
            for tunnel in tunnels:
                url = tunnel.get("public_url") or tunnel.get("url")
                if isinstance(url, str) and url.startswith("https://"):
                    return f"{url.rstrip('/')}/v1"
            time.sleep(0.8)
        return None

    def _write_status(self) -> None:
        with self._lock:
            payload = {
                "pid": self._proc.pid if self._proc is not None else None,
                "localUrl": LOCAL_URL,
                "publicUrl": self._public_url,
                "startedAt": self._started_at,
                "logFile": str(self._log_file) if self._log_file else None,
                "errFile": str(self._err_file) if self._err_file else None,
                "venvDir": str(Path(sys.executable).parent.parent),
                "state": self._state,
                "lastError": self._last_error,
                "lastErrorLabel": (
                    ERROR_LABELS.get(self._last_error)
                    if self._last_error else None
                ),
                "managedBy": "tray_app",
                "trayPid": os.getpid(),
            }
        try:
            STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATUS_FILE.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            log.exception("write status failed")

    def _set_state(self, new_state: str) -> None:
        if new_state not in VALID_STATES:
            raise ValueError(f"invalid state {new_state}")
        with self._lock:
            if self._state == new_state:
                return
            old = self._state
            self._state = new_state
            # Entering RUNNING means we recovered from any prior error.
            if new_state == STATE_RUNNING:
                self._last_error = None
                self._last_failure_reason = None
            # Entering ERROR requires a label to be set (caller's responsibility)
            # but stay robust: synthesise a generic label if missing.
            if new_state == STATE_ERROR and self._last_error is None:
                self._last_error = ERROR_CRASHED
        log.info("state %s -> %s", old, new_state)
        self._refresh_icon()
        self._refresh_menu()
        self._write_status()

    def _enter_error(self, error: str) -> None:
        """Atomically set last_error and transition to STATE_ERROR."""
        with self._lock:
            self._last_error = error
        log.warning(
            "entering error state: %s (%s)",
            error, ERROR_LABELS.get(error, "?"),
        )
        self._set_state(STATE_ERROR)

    def _refresh_icon(self) -> None:
        with self._lock:
            state = self._state
            failures = self._health_failures
            last_error = self._last_error
        if state == STATE_RUNNING and failures > 0:
            color = ICON_COLOR_WARNING
        else:
            color = BASE_COLORS[state]
        if state == STATE_ERROR and last_error is not None:
            title_suffix = (
                f"{state}: {ERROR_LABELS.get(last_error, last_error)}"
            )
        elif state == STATE_RUNNING and failures > 0:
            title_suffix = (
                f"{state}: {failures}/{HEALTH_FAILURES_BEFORE_RESTART} failed"
            )
        else:
            title_suffix = state
        try:
            self.icon.icon = _make_icon(color)
            self.icon.title = f"DeepSeek Cursor Proxy ({title_suffix})"
        except Exception:
            log.exception("icon update failed")

    def _refresh_menu(self) -> None:
        try:
            self.icon.update_menu()
        except Exception:
            log.exception("menu refresh failed")

    def _notify(self, message: str) -> None:
        try:
            self.icon.notify(message, "DeepSeek Cursor Proxy")
        except Exception:
            log.info("notify (fallback): %s", message)


# ----------------------------- Entry point -------------------------------


def main() -> int:
    log.info("=== tray_app launched (pid=%s) ===", os.getpid())
    tray = ProxyTray()
    try:
        tray.run()
    except Exception:
        log.exception("tray crashed")
        return 1
    finally:
        log.info("=== tray_app exiting ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
