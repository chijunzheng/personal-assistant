# launchd plists — Mac mini scheduling

This directory holds the five launchd plists that schedule the proactive
loops on the Mac mini host:

| Plist | Cadence | What it runs |
|---|---|---|
| `com.jasonchi.pa.daily-digest.plist`     | 08:00 daily          | `python -m kernel.proactive --task daily-digest` |
| `com.jasonchi.pa.weekly-digest.plist`    | Sunday 18:00         | `python -m kernel.proactive --task weekly-digest` |
| `com.jasonchi.pa.reminder-check.plist`   | Every 5 min (300s)   | `python -m kernel.proactive --task check-reminders` |
| `com.jasonchi.pa.conflict-watcher.plist` | Every 1 min (60s)    | `python -m kernel.conflict_watcher --run-once` |
| `com.jasonchi.pa.bot.plist`              | Long-running daemon  | `python -m kernel.telegram_bridge` (KeepAlive=true) |

Cadences mirror `configs/default.yaml`'s `proactive.*` and
`sync.conflict_watcher_interval_min`. Don't drift them.

## Why `--run-once` for the conflict watcher?

`kernel.conflict_watcher` exposes both `run_once()` and `run_loop()`.
launchd handles cadence itself, so we want each invocation to perform a
single scan and exit. `run_loop()` is reserved for foreground debugging
on a developer laptop where launchd isn't in the picture.

## Install

The plists ship with two placeholder tokens:

- `__PROJECT_ROOT__` — absolute path to your local clone (no trailing slash)
- `__TELEGRAM_BOT_TOKEN__` — the bot token from `@BotFather`

Substitute and copy with this snippet (run from the repo root):

```bash
PROJECT_ROOT="$(pwd)"
TELEGRAM_BOT_TOKEN="<paste-real-token>"

# Render every plist into ~/Library/LaunchAgents
mkdir -p "$HOME/Library/LaunchAgents"
for plist in infra/launchd/com.jasonchi.pa.*.plist; do
  out="$HOME/Library/LaunchAgents/$(basename "$plist")"
  sed \
    -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
    -e "s|__TELEGRAM_BOT_TOKEN__|$TELEGRAM_BOT_TOKEN|g" \
    "$plist" > "$out"
done
```

Then load each plist (`-w` makes the registration persist across reboots):

```bash
for plist in "$HOME/Library/LaunchAgents/com.jasonchi.pa."*.plist; do
  launchctl load -w "$plist"
done
```

## Verify

```bash
launchctl list | grep com.jasonchi.pa
```

You should see all five labels. Schedule-driven jobs report `-` in the
PID column until they next fire; the bot reports a live PID.

Audit log lines confirm the jobs are firing:

```bash
ls -la vault/_audit/
tail -f vault/_audit/$(date +%F).jsonl
```

## Inspect logs

stdout and stderr are routed per-plist into this directory:

```bash
tail -f infra/launchd/bot.log
tail -f infra/launchd/conflict-watcher.log
tail -f infra/launchd/daily-digest.log
tail -f infra/launchd/weekly-digest.log
tail -f infra/launchd/reminder-check.log
```

Each plist also writes a `*.err.log` companion for stderr.

## Unload (stop the jobs)

```bash
for plist in "$HOME/Library/LaunchAgents/com.jasonchi.pa."*.plist; do
  launchctl unload -w "$plist"
done
```

To uninstall completely, delete the rendered plists from
`~/Library/LaunchAgents/` after unloading.

## Reload after editing a plist

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.jasonchi.pa.bot.plist"
# (re-render via the sed snippet above)
launchctl load -w "$HOME/Library/LaunchAgents/com.jasonchi.pa.bot.plist"
```

## Caveats

### macOS Full Disk Access for the vault

The vault lives in `~/Library/CloudStorage/GoogleDrive-.../...`, which
sits behind macOS's app-sandbox protections. Grant the Python interpreter
(or the Terminal you used to install) **Full Disk Access** in
`System Settings → Privacy & Security → Full Disk Access`, otherwise
launchd-spawned Python will see permission errors when reading or writing
vault files.

### `python3.12` discovery

The plists invoke `/usr/bin/env python3.12`. If your Mac mini has Python
under a different name (e.g., Homebrew installs as `python3` only, or
you use `pyenv`), either:

- symlink: `sudo ln -s "$(which python3)" /usr/local/bin/python3.12`
- or edit the plist's `ProgramArguments` to call your interpreter directly.

The `PATH` value inside `EnvironmentVariables` is intentionally minimal
(`/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`) — launchd processes
do **not** inherit your shell's `PATH`. Add `~/.pyenv/shims` here if you
manage Python with pyenv.

### Single-instance lock

`kernel/orchestrator.py` acquires a flock on `/tmp/personal-assistant.lock`.
If the bot plist's KeepAlive restart races a still-cleanly-shutting-down
PID, the new process refuses to start — which is the intended safety
behaviour. launchd will retry after `ThrottleInterval` (10s).

### Time zones

launchd's `StartCalendarInterval` uses the system's local time. Confirm
with `sudo systemsetup -gettimezone`. The default config assumes
`America/Vancouver`; if you relocate the mini, both the system tz and
the plists' wall-clock interpretations will change with it.

### Drive sync vs. immediate writes

Daily/weekly digests and reminder fires write to the vault. Drive Desktop
sync is best-effort; expect a few seconds of replication lag before
edits show up on phone Obsidian.
