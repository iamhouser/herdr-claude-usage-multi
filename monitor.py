#!/usr/bin/env python3
"""herdr-claude-usage-multi - Claude plan usage gauges in the Herdr spaces sidebar.

Every workspace row shows how much of the Claude subscription its panes are
burning, as a bottom gauge row:

    ■■■■■□□□□□ 51/17

(gauge = 5-hour session window, numbers = session/week utilization %). Row
color escalates as usage grows (yellow at 70%, red at 90%), and an exhausted
window turns the row into a countdown until usage unblocks:

    ⏳ back in 47m · 20:50

Multi-account aware: with several Claude Code profiles (one CLAUDE_CONFIG_DIR
per folder), each workspace shows the account its panes actually run, mapped
by pane cwd. Zero config needed for a single default account.

The percentages are the ones Claude Code itself shows in /status, read from
Anthropic's OAuth usage endpoint with the credentials Claude Code already
stores on the machine. Nothing leaves the machine except that one HTTPS call
to api.anthropic.com. Inspired by alejodelosrios/herdr-claude-usage.

Commands:
  ensure  start the background monitor if not already running (event hooks use this)
  daemon  the monitor loop itself (spawned by ensure)
  stop    stop the monitor and clear the sidebar tokens
  popup   detail view for `herdr plugin pane open`
"""
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PLUGIN_ID = "houser.claude-usage"
SOURCE = f"plugin:{PLUGIN_ID}"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
DEFAULT_KEYCHAIN_SERVICE = "Claude Code-credentials"
DEFAULT_CREDENTIALS_FILE = "~/.claude/.credentials.json"

POLL_S = 300  # API poll; usage barely moves minute-to-minute
TICK_S = 30  # local re-render for the countdown; costs no API calls
TTL_MS = POLL_S * 4 * 1000  # tokens outlive a few failed fetches, then expire on their own
REFRESH_S = 240  # re-report unchanged tokens well before the TTL expires
GAUGE_CELLS = 10
WARN_PCT, HOT_PCT = 70, 90
VARIANTS = ("cu", "cu_warn", "cu_hot", "cu_out")  # each styled by its own sidebar row

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
SOCKET_PATH = os.environ.get("HERDR_SOCKET_PATH", "")
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
    f"~/.local/state/{PLUGIN_ID}")
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.expanduser(
    f"~/.config/herdr/plugins/config/{PLUGIN_ID}")
PIDFILE = os.path.join(STATE_DIR, "monitor.pid")


# ---------------------------------------------------------------- accounts --

def load_accounts():
    """Account roster from <config dir>/accounts.json, or a single default.

    Each entry: {"name": str, "config_dir": str|null, "prefixes": [str, ...]}.
    A pane whose cwd is under any prefix belongs to that account; the first
    account with no prefixes is the fallback for everything else. config_dir
    null means the default Claude Code install (no CLAUDE_CONFIG_DIR).
    """
    try:
        with open(os.path.join(CONFIG_DIR, "accounts.json"), encoding="utf-8") as f:
            entries = json.load(f)["accounts"]
    except Exception:
        entries = []
    accounts = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name"):
            accounts.append({
                "name": str(entry["name"])[:12],
                "config_dir": entry.get("config_dir"),
                "prefixes": [str(p) for p in entry.get("prefixes") or []],
            })
    return accounts or [{"name": "claude", "config_dir": None, "prefixes": []}]


ACCOUNTS = load_accounts()


def credential_locations(account):
    """(keychain service, credentials file path) for one account."""
    config_dir = account.get("config_dir")
    if not config_dir:
        return DEFAULT_KEYCHAIN_SERVICE, os.path.expanduser(DEFAULT_CREDENTIALS_FILE)
    config_dir = os.path.expanduser(config_dir)
    # Claude Code suffixes its Keychain entry with a hash of CLAUDE_CONFIG_DIR
    suffix = hashlib.sha256(config_dir.encode()).hexdigest()[:8]
    return (f"{DEFAULT_KEYCHAIN_SERVICE}-{suffix}",
            os.path.join(config_dir, ".credentials.json"))


def oauth_token(account):
    service, credentials_file = credential_locations(account)
    raw = None
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=5)
            if proc.returncode == 0 and proc.stdout.strip():
                raw = proc.stdout
        except Exception:
            pass
    if raw is None:
        try:
            with open(credentials_file, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            return None
    try:
        return json.loads(raw).get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def fetch_usage(account):
    """{"session": {...}, "week": {...}} | "ratelimited" | None."""
    token = oauth_token(account)
    if not token:
        return None
    request = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as err:
        return "ratelimited" if err.code == 429 else None
    except Exception:
        return None
    windows = {}
    for label, key in (("session", "five_hour"), ("week", "seven_day")):
        block = payload.get(key) or {}
        pct = block.get("utilization")
        windows[label] = {
            "pct": None if pct is None else round(pct),
            "resets_at": block.get("resets_at"),
        }
    return windows


# ------------------------------------------------------------------- herdr --

def herdr_cli(*args):
    return subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=10)


def herdr_json(*args, key):
    proc = herdr_cli(*args)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"herdr {' '.join(args)} failed")
    return json.loads(proc.stdout)["result"][key]


def account_for_cwd(cwd):
    for account in ACCOUNTS:
        for prefix in account["prefixes"]:
            root = os.path.expanduser(prefix)
            if cwd == root or cwd.startswith(root + os.sep):
                return account["name"]
    return next((a["name"] for a in ACCOUNTS if not a["prefixes"]), ACCOUNTS[0]["name"])


def map_workspaces(panes):
    """workspace_id -> account name, by majority vote over pane cwds."""
    tallies = {}
    for pane in panes:
        cwd = pane.get("cwd") or pane.get("foreground_cwd") or ""
        name = account_for_cwd(cwd)
        tally = tallies.setdefault(pane["workspace_id"], {})
        tally[name] = tally.get(name, 0) + 1
    order = [a["name"] for a in ACCOUNTS]
    return {
        ws: max(tally, key=lambda n: (tally[n], -order.index(n)))
        for ws, tally in tallies.items()
    }


def report_variant(workspace_id, variant, text):
    """Publish one variant and retract the other three, in a single CLI call."""
    args = ["workspace", "report-metadata", workspace_id, "--source", SOURCE,
            "--ttl-ms", str(TTL_MS), "--token", f"{variant}={text}"]
    for name in VARIANTS:
        if name != variant:
            args += ["--clear-token", name]
    herdr_cli(*args)


def clear_variants(workspace_id):
    args = ["workspace", "report-metadata", workspace_id, "--source", SOURCE]
    for name in VARIANTS:
        args += ["--clear-token", name]
    herdr_cli(*args)


# --------------------------------------------------------------- rendering --

def parse_iso(iso):
    try:
        parsed = datetime.fromisoformat(iso)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def humanize(seconds):
    minutes = max(1, math.ceil(seconds / 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def render(usage):
    """(variant, text) for one account's usage.

    Exhausted windows (100%) take over the row with a countdown until usage
    unblocks - the latest reset among exhausted windows, because a fresh
    session does not help while the week window is still spent.
    """
    session, week = usage["session"], usage["week"]
    spent = [w for w in (session, week) if (w["pct"] or 0) >= 100]
    if spent:
        resets = [dt for w in spent if (dt := parse_iso(w["resets_at"]))]
        if resets:
            until = max(resets)
            left = (until - datetime.now(timezone.utc)).total_seconds()
            if left > 0:
                stamp = until.astimezone().strftime("%H:%M" if left < 86400 else "%a %H:%M")
                return "cu_out", f"⏳ back in {humanize(left)} · {stamp}"
        return "cu_out", "⏳ limit reached"
    worst = max(session["pct"] or 0, week["pct"] or 0)
    variant = "cu_hot" if worst >= HOT_PCT else "cu_warn" if worst >= WARN_PCT else "cu"
    filled = min(GAUGE_CELLS, round(GAUGE_CELLS * (session["pct"] or 0) / 100))
    gauge = "■" * filled + "□" * (GAUGE_CELLS - filled)
    week_pct = "?" if week["pct"] is None else week["pct"]
    return variant, f"{gauge} {session['pct']}/{week_pct}"


# ------------------------------------------------------------------ daemon --

def running_pid():
    try:
        pid = int(open(PIDFILE).read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def cmd_ensure():
    if running_pid():
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    # check-then-spawn has a tiny race; a duplicate daemon exits on its first tick
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "daemon"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def cmd_daemon():
    os.makedirs(STATE_DIR, exist_ok=True)
    other = running_pid()
    if other and other != os.getpid():
        return
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    consecutive_failures = 0
    last_good = {}  # account name -> last successful usage
    published = {}  # workspace_id -> (variant, text, monotonic_ts)
    next_poll = 0.0
    while True:
        if SOCKET_PATH and not os.path.exists(SOCKET_PATH):
            break  # herdr server is gone
        try:
            panes = herdr_json("pane", "list", key="panes")
            workspaces = herdr_json("workspace", "list", key="workspaces")
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                break
            time.sleep(10)
            continue
        ws_account = map_workspaces(panes)
        now = time.monotonic()
        if now >= next_poll:
            rate_limited = False
            for account in ACCOUNTS:
                if account["name"] not in ws_account.values():
                    continue
                usage = fetch_usage(account)
                if usage == "ratelimited":
                    rate_limited = True
                elif isinstance(usage, dict) and usage["session"]["pct"] is not None:
                    # keep the last good value so a failed fetch never blanks the row
                    last_good[account["name"]] = usage
            # back off hard while rate-limited so we don't extend the penalty
            # window or compete with the user's own /status calls
            next_poll = now + (POLL_S * 3 if rate_limited else POLL_S)
        for ws in workspaces:
            ws_id = ws["workspace_id"]
            usage = last_good.get(ws_account.get(ws_id))
            if usage:
                variant, text = render(usage)
                prev = published.get(ws_id)
                if not prev or (prev[0], prev[1]) != (variant, text) or now - prev[2] > REFRESH_S:
                    report_variant(ws_id, variant, text)
                    published[ws_id] = (variant, text, now)
            elif ws_id in published:
                clear_variants(ws_id)
                del published[ws_id]
        time.sleep(TICK_S)
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


def cmd_stop():
    pid = running_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        for ws in herdr_json("workspace", "list", key="workspaces"):
            clear_variants(ws["workspace_id"])
    except Exception:
        pass
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


# ------------------------------------------------------------------- popup --

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"


def meter(pct, width=32):
    filled = min(width, round(width * pct / 100))
    tint = "\033[31m" if pct >= HOT_PCT else "\033[33m" if pct >= WARN_PCT else "\033[32m"
    return f"{tint}{'█' * filled}{DIM}{'·' * (width - filled)}{RESET}"


def stamp(iso):
    parsed = parse_iso(iso)
    return parsed.astimezone().strftime("%a %H:%M") if parsed else "-"


def cmd_popup():
    while True:
        sys.stdout.write("\033[2J\033[H")
        print(f"{BOLD} Claude plan usage{RESET}")
        for account in ACCOUNTS:
            usage = fetch_usage(account)
            where = account.get("config_dir") or "default profile"
            print()
            print(f"  {BOLD}{account['name']}{RESET}  {DIM}({where}){RESET}")
            if not isinstance(usage, dict) or usage["session"]["pct"] is None:
                print("    No usage data. Is this profile logged in?")
                continue
            for label, window in (("Session", usage["session"]), ("Week", usage["week"])):
                print(f"    {label:8} {meter(window['pct'])}  {window['pct']:>3}%"
                      f"   resets {stamp(window['resets_at'])}")
            variant, text = render(usage)
            if variant == "cu_out":
                print(f"    \033[31m{BOLD}{text}{RESET}")
        print()
        print(f"{DIM} refreshes every 30s - close the popup to exit{RESET}")
        sys.stdout.flush()
        time.sleep(30)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "ensure"
    {"ensure": cmd_ensure, "daemon": cmd_daemon,
     "stop": cmd_stop, "popup": cmd_popup}.get(command, cmd_ensure)()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
