# Claude Code Monitor

A TUI dashboard for monitoring all running Claude Code instances on your machine.

## Features

- **Live monitoring** — auto-refreshes every 5 seconds
- **State detection** — detects when an instance needs your input (permission prompts)
- **Context usage** — shows how full each instance's context window is
- **Task preview** — shows the first user message for each session
- **Workspace grouping** — groups instances by Hyprland workspace (or directory on other systems)
- **Focus switching** — press Enter to jump to an instance's window (Hyprland only)
- **Waybar integration** — status icon that pulses red when any instance needs input

## Requirements

- Python 3.6+
- Linux (`/proc` filesystem for process introspection)
- Claude Code CLI installed

### Optional

- **Hyprland** — for workspace grouping, window focus, and waybar integration
- **Waybar** — for the status bar indicator

## Installation

```bash
# Clone the repo
git clone <repo-url> ~/Documents/ClaudeCodeMonitor
cd ~/Documents/ClaudeCodeMonitor

# Symlink the TUI to your PATH
mkdir -p ~/.claude/bin
ln -sf "$(pwd)/claude-ps.py" ~/.claude/bin/claude-ps
export PATH="$HOME/.claude/bin:$PATH"

# Or add an alias
echo 'alias claude-ps="~/Documents/ClaudeCodeMonitor/claude-ps.py"' >> ~/.zshrc
```

### Waybar integration (Hyprland only)

```bash
# Symlink the waybar script
ln -sf "$(pwd)/claude-waybar.sh" ~/.config/waybar/modules/claude.sh
```

Add to your waybar config (`~/.config/waybar/config`):

```json
"custom/claude": {
    "format": "{}",
    "exec": "~/.config/waybar/modules/claude.sh",
    "return-type": "json",
    "interval": 5,
    "on-click": "~/.claude/bin/claude-ps",
    "tooltip": true
}
```

Add to your waybar CSS (`~/.config/waybar/style.css`):

```css
#custom-claude.ok {
    color: #a6da95;
}

#custom-claude.attention {
    color: #ed8796;
    animation: pulse 1s ease-in-out infinite;
}

#custom-claude.none {
    padding: 0;
    margin: 0;
}

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
}
```

## Configuration

Edit `config.json` in the project directory:

```json
{
  "refresh_interval": 5,
  "hyprland": {
    "enabled": true,
    "workspace_labels": {
      "1": "Main",
      "2": "Code",
      "3": "Term"
    }
  },
  "group_by": "workspace"
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `refresh_interval` | `5` | Seconds between data refreshes |
| `hyprland.enabled` | `true` | Set to `false` on non-Hyprland systems (Windows, macOS, other WMs) |
| `hyprland.workspace_labels` | `{}` | Human-readable names for workspace numbers |
| `group_by` | `"workspace"` | `"workspace"` or `"directory"` — how to group instances. Falls back to `"directory"` when Hyprland is disabled |

### Windows / macOS / non-Hyprland users

Set `hyprland.enabled` to `false` in `config.json`:

```json
{
  "hyprland": {
    "enabled": false
  },
  "group_by": "directory"
}
```

This disables workspace detection, window focus, and waybar features. The TUI still works for monitoring instances, state detection, context usage, and task preview.

**Note:** Currently requires Linux (`/proc` filesystem). macOS/Windows support would need alternative process introspection.

## Keybindings

| Key | Action |
|-----|--------|
| `j` / `↓` | Move cursor down |
| `k` / `↑` | Move cursor up |
| `Enter` | Focus the selected instance's window (Hyprland only) |
| `r` | Force refresh |
| `q` / `Esc` | Quit |

## Instance States

| Indicator | Color | Meaning |
|-----------|-------|---------|
| `! NEEDS INPUT` | Red (blinking) | Waiting for permission approval |
| `● working` | Green | Actively generating or executing tools |
| `○ idle` | Yellow | Turn ended, waiting for user's next message |

## Context Usage Colors

| Range | Color |
|-------|-------|
| 0–70% | Green |
| 70–90% | Yellow |
| 90–100% | Red |

## How State Detection Works

The monitor reads the tail of each Claude session's `.jsonl` file to determine state:

- If the last message is `assistant` with `tool_use` and the file hasn't been modified for >5 seconds, the instance is likely **waiting for permission**
- If the last message is `assistant` with text or a `turn_duration` system event, the instance is **idle**
- Otherwise, it's **working**

## Files

| File | Purpose |
|------|---------|
| `claude-ps.py` | Main TUI application |
| `claude-waybar.sh` | Waybar status indicator script |
| `config.json` | User configuration |
