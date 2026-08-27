# herdr-claude-usage-multi

Claude plan usage gauges in the [Herdr](https://herdr.dev) spaces sidebar - multi-account aware.

Every workspace row shows how much of the Claude subscription its panes are burning:

```
● my-project
  main  ✚2
  ■■■■■□□□□□ 51/17
```

- **Gauge** = 5-hour session window, **numbers** = session/week utilization in percent.
- The exact percentages Claude Code shows in `/status` - no estimation, no extra login, zero plan tokens spent.
- Row color escalates as usage grows: default green, yellow at 70%, red at 90% (worse of the two windows).
- An exhausted window (100%) turns the row into a countdown until usage unblocks:

```
⏳ back in 47m · 20:50
```

The countdown targets the latest reset among exhausted windows (a fresh session does not help while the week window is still spent) and ticks with minute precision - computed locally, no extra API calls.

- `prefix+u` (optional keybinding below) opens a detail popup: big meters, both windows, reset times, all accounts.

## Multi-account

If you run several Claude Code profiles (one `CLAUDE_CONFIG_DIR` per folder - work and personal, for example), each workspace shows the account its panes actually run, mapped by pane cwd.
Workspaces mixing both accounts get the majority account.
With a single default account there is nothing to configure.

## Install

```bash
herdr plugin install iamhouser/herdr-claude-usage-multi
```

Add the gauge rows to `~/.config/herdr/config.toml` (the plugin reports exactly one of the four token variants per space; rows without a token are skipped, so regular spaces stay compact):

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["branch", "git_status"],
  [{ token = "$cu", fg = "#a6e3a1" }],
  [{ token = "$cu_warn", fg = "#f9e2af" }],
  [{ token = "$cu_hot", fg = "#f38ba8", bold = true }],
  [{ token = "$cu_out", fg = "#f38ba8", bold = true }],
]
```

Optional popup keybinding:

```toml
[[keys.command]]
key = "prefix+u"
type = "shell"
command = '"$HERDR_BIN_PATH" plugin pane open --plugin houser.claude-usage --entrypoint usage'
```

Reload and kick off the monitor once:

```bash
herdr server reload-config
herdr plugin action invoke start --plugin houser.claude-usage
```

After that it auto-starts on session activity (workspace/pane creation events) and stops itself when the Herdr server goes away.

## Multi-account configuration

Create `accounts.json` in the plugin config dir (`herdr plugin list` prints it, typically `~/.config/herdr/plugins/config/houser.claude-usage/`):

```json
{
  "accounts": [
    {
      "name": "work",
      "config_dir": "~/.config/claude/accounts/work",
      "prefixes": ["~/Documents/workspace"]
    },
    {
      "name": "personal",
      "config_dir": "~/.config/claude/accounts/personal",
      "prefixes": []
    }
  ]
}
```

- `config_dir` - the profile's `CLAUDE_CONFIG_DIR` (`null` for the default `~/.claude` install).
- `prefixes` - folders whose panes belong to this account; the first account with no prefixes is the fallback for everything else.
- Restart the monitor after editing (`stop` + `start` actions).

## How it works

- A single-file, stdlib-only Python daemon polls Anthropic's OAuth usage endpoint (`api.anthropic.com/api/oauth/usage`) every 5 minutes, once per account in use.
- Credentials are the ones Claude Code already stores on the machine: the macOS Keychain entry (`Claude Code-credentials`, suffixed with `sha256(CLAUDE_CONFIG_DIR)[:8]` for non-default profiles) or `<config_dir>/.credentials.json` on Linux.
- Pane cwds come from `herdr pane list`; usage rows are published per workspace with `herdr workspace report-metadata` and a 20-minute TTL, so stale data disappears on its own if the monitor dies.
- If the monitor keeps running but its usage fetches fail, the cached value picks up a trailing `?` after 20 minutes and stops being published after an hour, so the gauges never freeze at plausible-looking numbers.
- Tokens re-render every 30 seconds (change-detected) so the countdown ticks without extra API traffic.
- Sidebar tokens cannot carry ANSI colors, so the plugin reports one of four token variants (`cu`, `cu_warn`, `cu_hot`, `cu_out`) and your config styles each row - that is what makes the color dynamic.

## Privacy

Nothing leaves your machine except the HTTPS call to `api.anthropic.com` - the same endpoint and credentials Claude Code itself uses.
No telemetry, no third parties, no state beyond a pidfile.
The endpoint is undocumented and may change; if it does, the rows simply expire and disappear.

## Requirements

- Herdr ≥ 0.7.0, macOS or Linux, `python3` in `PATH`
- Claude Code logged in on the same machine (that's where the credentials come from)

## Credits

Inspired by [alejodelosrios/herdr-claude-usage](https://github.com/alejodelosrios/herdr-claude-usage), which pioneered the sidebar-token approach for a single account.

## License

MIT
