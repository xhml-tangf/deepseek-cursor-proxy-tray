# deepseek-cursor-proxy-tray

Windows system-tray supervisor for [`deepseek-cursor-proxy`](https://github.com/yxlao/deepseek-cursor-proxy).

Adds **zero-console-flicker autostart**, a **state-machine watchdog with labeled error states**, and **GUI windows** for editing `config.yaml`, configuring the ngrok authtoken, and managing the reasoning cache.

```
┌─ DeepSeek Cursor Proxy (running) ─────────────┐
│ Status: running (pid=18764)                    │
│ Public: https://your-domain.ngrok-free.dev/v1  │
│ ──────────────────────────────────────────────│
│ Copy Public URL                                │
│ Restart Proxy                                  │
│ [x] Watchdog (auto-restart on health failure)  │
│ ──────────────────────────────────────────────│
│ Proxy Settings...                              │
│ Configure ngrok authtoken...                   │
│ Reasoning Cache...                             │
│ ──────────────────────────────────────────────│
│ Open Latest Proxy Log                          │
│ Open Tray Log                                  │
│ Open Log Folder                                │
│ Open status.json                               │
│ ──────────────────────────────────────────────│
│ Quit (stops proxy)                             │
└────────────────────────────────────────────────┘
```

## Why this exists

The [upstream proxy](https://github.com/yxlao/deepseek-cursor-proxy) is a great CLI. But on Windows, running it as a long-lived service has friction:

- `powershell.exe ... -WindowStyle Hidden` from Task Scheduler **still flashes a console window** at every run/restart.
- `Stop-Process -Force` to restart the proxy hard-kills ngrok before its tunnel session can be released, occasionally causing `provider error` on the next start.
- When the proxy hangs or crashes, you find out from Cursor failing, not from the OS.
- `ngrok authtoken`, `config.yaml`, and the `--clear-reasoning-cache` CLI flag all require hand-editing or terminal incantations.

This repo solves all of the above by wrapping the proxy in a single tray app that:

- Launches via `pythonw.exe` (no console allocation — **truly silent autostart**)
- Owns the proxy subprocess and `taskkill /T /F` the whole tree (proxy + ngrok) on stop, so ngrok cloud sessions release cleanly
- Probes `/healthz` every 60 s, requires 3 consecutive failures before restarting, and waits 8 s between stop and start to avoid session collisions
- Distinguishes 5 lifecycle states by icon color and surfaces 7 distinct error labels
- Exposes config, authtoken, and cache management as native tkinter dialogs

## Requirements

- Windows 10 / 11
- Python ≥ 3.10 (the venv created by `uv sync` will have it)
- [`uv`](https://docs.astral.sh/uv/) (or `pip` if you prefer)
- `ngrok` ≥ 3 — install via `winget install Ngrok.Ngrok`
- An ngrok authtoken (the tray will prompt you on first run if missing)

The upstream proxy itself is fetched automatically from GitHub as a Python dependency; you don't need to clone or `pip install` it separately.

## Installation

### Recommended: download the installer

Grab `dscp-tray-setup-<version>.exe` from the [latest release](https://github.com/xhml-tangf/deepseek-cursor-proxy-tray/releases/latest) and double-click it.

- Per-user install, **no admin required**
- Bundles a self-contained Python 3.12 (with tkinter); you don't need Python
- Bundles both wheels (this project + the upstream proxy), so install is fully offline once downloaded
- Optionally `winget install`s ngrok for you if missing
- Registers Task Scheduler logon autostart via standard `Register-ScheduledTask`
- Clean uninstall via Add/Remove Programs

On first launch, if you haven't configured the ngrok authtoken yet, the tray will land in `STATE_ERROR` with label `authtoken-missing` and **auto-open the config window** so you can paste your token.

Once running, right-click the tray icon → **Copy Public URL**, paste into Cursor's custom model settings as the Base URL.

### From source (for development)

```powershell
git clone https://github.com/xhml-tangf/deepseek-cursor-proxy-tray.git
cd deepseek-cursor-proxy-tray

# Create venv and install (uv pulls the upstream proxy from GitHub)
uv sync

# Manually launch the tray (it owns the proxy subprocess and registers
# itself nowhere - useful for trying changes without touching Task Scheduler)
.\scripts\start-tray.ps1
```

### Building the installer yourself

Requires Inno Setup 6 (`winget install JRSoftware.InnoSetup`) and a normal
Python 3.12.x install on PATH (used to harvest tkinter for the embeddable).

```powershell
.\installer\build-installer.ps1
# Result: installer\out\dscp-tray-setup-0.1.0.exe (~20 MB)
```

## State machine

```
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
```

| State | Icon | Meaning |
|---|---|---|
| `stopped`  | **gray**     | Clean idle (initial, after user Quit, after Dismiss Error) |
| `starting` | **amber**    | Launching proxy + waiting for ngrok public URL |
| `running`  | **green**    | `/healthz` OK. Accumulated health failures tint it **orange** as a sub-flag |
| `stopping` | **blue-gray**| Tearing down via `taskkill /T /F` |
| `error`    | **red**      | Last attempt failed or proxy crashed; carries a labeled cause |

"Restart" is not a state — it's `stop_proxy()` then `start_proxy()`. "Unhealthy" is not a state either — it's a sub-flag of `running`.

## Error labels

| Label | When |
|---|---|
| `exe-missing`        | `deepseek-cursor-proxy.exe` not found in venv or on PATH |
| `launch-failed`      | `subprocess.Popen` raised `OSError` |
| `startup-exit`       | Proxy exited before reporting a public URL (ngrok auth failure, port conflict, …) |
| `crashed`            | Proxy process disappeared during `running` (3 consecutive missed heartbeats) |
| `unresponsive`       | Proxy alive but `/healthz` timed out 3 times in a row |
| `ngrok-missing`      | Preflight: `ngrok.exe` not findable; install via `winget install Ngrok.Ngrok` |
| `authtoken-missing`  | Preflight: `ngrok.yml` has no authtoken; the config window auto-opens |

`status.json` also exports `lastError` and `lastErrorLabel` for external monitoring.

## Tray menu reference

| Item | Visible when | Action |
|---|---|---|
| Status: …             | always           | non-interactive; shows `state (pid=…)` and, in `running`, `health failures N/3` |
| Error: …              | `error` state    | non-interactive; shows the labeled cause |
| Public: …             | always           | non-interactive; ngrok public URL or `(waiting…)` |
| Copy Public URL       | always           | copies the public URL to clipboard |
| Restart Proxy         | always           | `stop_proxy()` + 8 s grace + `start_proxy()` |
| Watchdog (☐)          | always           | toggle the in-process auto-restart-on-failure loop |
| Dismiss Error         | `error` state    | clear the error label and transition to `stopped` (gray) |
| Proxy Settings…       | always           | open the `config.yaml` editor (see below) |
| Configure ngrok authtoken… | always      | open the authtoken editor (see below) |
| Reasoning Cache…      | always           | open the cache stats + clear window (see below) |
| Open Latest Proxy Log | always           | open the most recent `proxy-{ts}.log` in the system editor |
| Open Tray Log         | always           | open `tray.log` (rotated, 1 MB × 3) |
| Open Log Folder       | always           | open `~/.deepseek-cursor-proxy/logs/` |
| Open status.json      | always           | open the runtime status file |
| Quit (stops proxy)    | always           | clean shutdown of proxy then tray |

## GUI windows

### Proxy Settings…

Structured editor over `~/.deepseek-cursor-proxy/config.yaml`. Exposes the 10 most-tuned keys (model, thinking, reasoning effort, display/collapsible reasoning, missing-reasoning strategy, request timeout, ngrok reserved domain, verbose, CORS). On save, re-reads the YAML, merges form values in, and writes back — **preserving any keys not exposed in the UI** (host, port, cache limits, etc.). Optional auto-restart after save. "Open raw YAML" button drops to the system editor for advanced fields.

The `ngrok` on/off switch is intentionally not exposed — disabling it makes the proxy useless to Cursor; if you really need local-only mode, hand-edit the YAML.

### Configure ngrok authtoken…

Reads `%LOCALAPPDATA%\ngrok\ngrok.yml` to show whether a token is configured (without ever logging or displaying the cleartext — only length + last 4 chars). Save runs `ngrok config add-authtoken <token>` via subprocess. Optional auto-restart after save.

### Reasoning Cache…

Reads `~/.deepseek-cursor-proxy/reasoning_content.sqlite3` in **SQLite read-only URI mode** (no locking even when the proxy is hammering it). Shows file size, row count, oldest/newest entry timestamps. "Clear All…" runs the upstream `ReasoningStore.clear()` directly (no CLI subprocess) after a confirmation dialog.

## Health check parameters

| Parameter | Default | Why |
|---|---|---|
| Probe interval | 60 s | Avoid false positives during long SSE streams |
| Per-probe timeout | 10 s | Slack for occasional GC pauses / SQLite checkpoints |
| Failure threshold | 3 | One bad blip ignored; three = real problem |
| Stop grace before next start | 8 s | Let ngrok's cloud-side session release |
| ngrok URL wait | 35 s | Cold-start tolerance for the tunnel |
| `tray.log` rotation | 1 MB × 3 | Hard cap ≈ 4 MB |

Adjust by editing the constants at the top of `src/dscp_tray/tray.py` and restarting the tray. Adjusting these via the GUI is on the wish list but not implemented.

## Data layout

| Path | Owner | Content |
|---|---|---|
| `~/.deepseek-cursor-proxy/config.yaml`               | upstream | model / thinking / ngrok / cache settings |
| `~/.deepseek-cursor-proxy/reasoning_content.sqlite3` | upstream | `reasoning_content` cache |
| `~/.deepseek-cursor-proxy/status.json`               | tray     | `state`, `lastError`, `pid`, `publicUrl`, `startedAt`, … |
| `~/.deepseek-cursor-proxy/logs/proxy-{ts}.log`       | upstream | proxy stdout/stderr per launch |
| `~/.deepseek-cursor-proxy/logs/tray.log`             | tray     | rotating tray supervisor log |
| `%LOCALAPPDATA%\ngrok\ngrok.yml`                     | ngrok    | authtoken + ngrok agent config |

## Architecture

```
[Task Scheduler @ Logon (15s delay)]
         │
         ▼
  pythonw.exe -m dscp_tray        ← single autostart entry, no console window
         │
         ├─ pystray icon + right-click menu
         ├─ supervisor thread: starts proxy, runs /healthz every 60s
         ├─ tkinter dialogs (authtoken, settings, cache) — daemon threads
         │
         └─► subprocess: deepseek-cursor-proxy.exe   ← upstream proxy
                ├─ stdout / stderr → logs/proxy-{ts}.log
                └─► subprocess: ngrok.exe → public URL
```

The tray and proxy share `~/.deepseek-cursor-proxy/` so the CLI and the tray see consistent state.

## Common operations

```powershell
# Manual tray start (when autostart is disabled or you need to test)
.\scripts\start-tray.ps1
.\scripts\start-tray.ps1 -KillExistingTray   # replace running instance

# Inspect runtime state
Get-Content $env:USERPROFILE\.deepseek-cursor-proxy\status.json | ConvertFrom-Json

# Tail the tray log
Get-Content $env:USERPROFILE\.deepseek-cursor-proxy\logs\tray.log -Wait -Tail 30

# Probe locally
Invoke-WebRequest http://127.0.0.1:9000/healthz

# Stop and uninstall everything
.\scripts\uninstall-autostart.ps1 -AlsoStopTray
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tray icon never appears        | Read `~/.deepseek-cursor-proxy/logs/tray.log`. Most often `pythonw` failed to start (missing venv → `uv sync`) or `pystray`/`Pillow` aren't installed. |
| Red icon, label `ngrok-missing`     | `winget install Ngrok.Ngrok`, then right-click → Restart Proxy. |
| Red icon, label `authtoken-missing` | Config window opens automatically; paste your token from <https://dashboard.ngrok.com/get-started/your-authtoken> and save. |
| Red icon, label `startup-exit` | Inspect `logs/proxy-{ts}.err.log`. Usually ngrok auth, domain conflict, or port 9000 still occupied. |
| Red icon, label `crashed`      | The proxy died unexpectedly. Look at the most recent `proxy-{ts}.log`; click Restart Proxy to retry. |
| Red icon, label `unresponsive` | Proxy alive but `/healthz` timing out. Upstream DeepSeek API is likely throttling or the proxy is blocked on a long SSE stream. Wait or click Restart. |
| Orange icon (running but warning) | Single missed health probe. Auto-recovers after the next success; no action needed. |
| `Cursor` reports `provider error` after a restart | Should not happen anymore (we wait 8 s between stop and start). If it does, file an issue with `tray.log`. |

## Credits

This is a downstream wrapper for [`yxlao/deepseek-cursor-proxy`](https://github.com/yxlao/deepseek-cursor-proxy). All of the actual proxy magic — reasoning_content scope hashing, multi-turn cache lookup, streaming SSE rewriter, ngrok integration — lives there.

## License

MIT. See [LICENSE](LICENSE). The upstream proxy is also MIT, by Yixing Lao.
