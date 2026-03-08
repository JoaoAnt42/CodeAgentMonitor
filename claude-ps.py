#!/usr/bin/env python3
"""claude-ps — TUI monitor for running Claude Code instances."""

import curses
import json
import os
import subprocess
import sqlite3
import time

HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
OPENCODE_DB = os.path.join(HOME, ".local", "share", "opencode", "opencode.db")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Load config
_config_path = os.path.join(SCRIPT_DIR, "config.json")
try:
    with open(_config_path) as _f:
        CONFIG = json.load(_f)
except (OSError, json.JSONDecodeError):
    CONFIG = {}

REFRESH_INTERVAL = CONFIG.get("refresh_interval", 5)
HYPRLAND_ENABLED = CONFIG.get("hyprland", {}).get("enabled", True)
WORKSPACE_LABELS = CONFIG.get("hyprland", {}).get("workspace_labels", {})
GROUP_BY = CONFIG.get("group_by", "workspace" if HYPRLAND_ENABLED else "directory")
TOOLS_CONFIG = CONFIG.get("tools", {"claude": {"enabled": True}, "opencode": {"enabled": True}})


def shorten_path(path):
    if path.startswith(HOME):
        return "~" + path[len(HOME):]
    return path


def format_mem(kb):
    if kb >= 1048576:
        return f"{kb / 1048576:.1f}G"
    elif kb >= 1024:
        return f"{kb // 1024}M"
    return f"{kb}K"


def format_elapsed(raw):
    raw = raw.strip()
    if "-" in raw:
        parts = raw.split("-")
        days = parts[0]
        rest = parts[1].split(":")
        return f"{days}d{rest[0]}h"
    parts = raw.split(":")
    if len(parts) == 3:
        h, m = int(parts[0]), int(parts[1])
        if h > 0:
            return f"{h}h{m:02d}m"
        return f"{m}m"
    elif len(parts) == 2:
        return f"{int(parts[0])}m"
    return raw


def get_ppid(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().split()[3])
    except (OSError, ValueError, IndexError):
        return 1


def get_hyprland_windows():
    """Get all Hyprland windows as a dict keyed by PID and a list."""
    try:
        result = subprocess.run(
            ["hyprctl", "clients", "-j"],
            capture_output=True, text=True, timeout=3
        )
        clients = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return {}, []

    by_pid = {}
    for c in clients:
        pid = c.get("pid", 0)
        if pid not in by_pid:
            by_pid[pid] = []
        by_pid[pid].append(c)

    return by_pid, clients


def match_window(instance, windows_by_pid, all_windows):
    """Match a Claude instance to its Hyprland window.

    Strategy:
    1. Walk up the PID tree to find a window PID match
    2. For VSCode: if multiple windows share the PID, match by directory basename in title
    3. Return (workspace, address) or (None, None)
    """
    pid = instance["pid"]
    cwd = instance["cwd"]
    dir_basename = os.path.basename(cwd)

    # Walk up process tree
    p = pid
    visited = set()
    while p > 1 and p not in visited:
        visited.add(p)
        if p in windows_by_pid:
            candidates = windows_by_pid[p]
            # If only one window for this PID, use it
            if len(candidates) == 1:
                w = candidates[0]
                ws = w.get("workspace", {}).get("name", "?")
                return ws, w.get("address", "")

            # Multiple windows (e.g. VSCode with multiple windows) — match by title
            for w in candidates:
                title = w.get("title", "")
                if dir_basename and dir_basename in title:
                    ws = w.get("workspace", {}).get("name", "?")
                    return ws, w.get("address", "")

            # Fallback: return first candidate
            w = candidates[0]
            ws = w.get("workspace", {}).get("name", "?")
            return ws, w.get("address", "")

        p = get_ppid(p)

    # Fallback: try matching by directory basename in any window title
    for w in all_windows:
        title = w.get("title", "")
        cls = w.get("class", "")
        if dir_basename and dir_basename in title:
            if instance["source"] == "vscode" and "code" in cls.lower():
                ws = w.get("workspace", {}).get("name", "?")
                return ws, w.get("address", "")
            elif instance["source"] == "terminal" and "code" not in cls.lower():
                ws = w.get("workspace", {}).get("name", "?")
                return ws, w.get("address", "")

    return None, None


def cwd_to_project_dir(cwd):
    """Convert a CWD path to the Claude projects directory name."""
    # ~/.claude/projects/ replaces both / and _ with -, leading slash becomes single -
    return cwd.replace("/", "-").replace("_", "-")


def get_active_sessions(cwd):
    """Get active sessions for a project directory, sorted by most recent.

    Returns list of (session_id, first_prompt, mtime) tuples.
    """
    proj_name = cwd_to_project_dir(cwd)
    proj_dir = os.path.join(CLAUDE_PROJECTS, proj_name)
    if not os.path.isdir(proj_dir):
        return []

    import glob as g
    jsonls = g.glob(os.path.join(proj_dir, "*.jsonl"))
    if not jsonls:
        return []

    now = time.time()
    recent = []
    for j in jsonls:
        mtime = os.path.getmtime(j)
        if now - mtime > 300:  # only sessions active in last 5 min
            continue
        recent.append((j, mtime))

    recent.sort(key=lambda x: x[1], reverse=True)

    sessions = []
    for jpath, mtime in recent:
        sid = os.path.basename(jpath).replace(".jsonl", "")
        first_prompt = _extract_first_prompt(jpath)
        ctx_pct = get_session_context_usage(jpath)
        sessions.append((sid, first_prompt, mtime, ctx_pct))

    return sessions


def _extract_first_prompt(jsonl_path):
    """Extract the first user message from a session jsonl."""
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    if msg.get("message", {}).get("role") != "user":
                        continue
                    content = msg["message"].get("content", "")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text = part["text"].strip()
                                if text and not text.startswith("Base directory"):
                                    return text
                    elif isinstance(content, str) and content.strip():
                        return content.strip()
                except (json.JSONDecodeError, KeyError):
                    continue
    except OSError:
        pass
    return ""


def get_session_context_usage(jsonl_path):
    """Read the last message.usage from a session jsonl to get context fill %."""
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.strip().split("\n")):
            try:
                msg = json.loads(line)
                usage = msg.get("message", {}).get("usage")
                if usage:
                    total = (usage.get("input_tokens", 0)
                             + usage.get("cache_creation_input_tokens", 0)
                             + usage.get("cache_read_input_tokens", 0))
                    # Claude context window is 200k tokens
                    pct = min(100, int(total / 200000 * 100))
                    return pct
            except (json.JSONDecodeError, KeyError):
                continue
    except OSError:
        pass
    return None


def detect_session_state(cwd):
    """Detect the state of sessions in a project directory.

    Returns list of (session_id, state, mtime) where state is one of:
      "working"    — actively generating/executing
      "permission" — waiting for user to approve a tool call
      "input"      — waiting for user's next message
    """
    proj_name = cwd_to_project_dir(cwd)
    proj_dir = os.path.join(CLAUDE_PROJECTS, proj_name)
    if not os.path.isdir(proj_dir):
        return []

    import glob as g
    jsonls = g.glob(os.path.join(proj_dir, "*.jsonl"))
    if not jsonls:
        return []

    now = time.time()
    results = []
    for j in jsonls:
        mtime = os.path.getmtime(j)
        if now - mtime > 300:
            continue

        sid = os.path.basename(j).replace(".jsonl", "")
        staleness = now - mtime

        # Read last few lines to determine state
        last_role = ""
        last_content_types = []
        last_subtype = ""
        try:
            with open(j, "rb") as f:
                # Seek to end and read last ~8KB for the tail
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 8192))
                tail = f.read().decode("utf-8", errors="replace")

            for line in reversed(tail.strip().split("\n")):
                try:
                    msg = json.loads(line)
                    role = msg.get("message", {}).get("role", "")
                    if role:
                        last_role = role
                        content = msg.get("message", {}).get("content", [])
                        if isinstance(content, list):
                            last_content_types = [
                                c.get("type", "") for c in content if isinstance(c, dict)
                            ]
                        break
                    t = msg.get("type", "")
                    subtype = msg.get("subtype", "")
                    if t == "system" and subtype == "turn_duration":
                        last_subtype = "turn_duration"
                        break
                except (json.JSONDecodeError, KeyError):
                    continue
        except OSError:
            continue

        if last_subtype == "turn_duration":
            state = "input"
        elif last_role == "assistant" and "tool_use" in last_content_types:
            state = "permission" if staleness > 5 else "working"
        elif last_role == "assistant":
            state = "input" if staleness > 3 else "working"
        elif last_role == "user":
            state = "working"
        else:
            state = "input"

        results.append((sid, state, mtime))

    results.sort(key=lambda x: x[2], reverse=True)
    return results

def _claude_is_subagent(ppid):
    """Check if a Claude process is a sub-agent by inspecting parent cmdline."""
    try:
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            parent_cmd = f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace")
        return "claude" in parent_cmd and "claude-ps" not in parent_cmd
    except OSError:
        return False


CLAUDE_ADAPTER = {
    "name": "claude",
    "process_name": "claude",
    "match_process": lambda args, cmd: cmd == "claude" or cmd.endswith("/claude"),
    "detect_source": lambda args: "vscode" if "stream-json" in args else "terminal",
    "is_subagent": lambda args, pid, ppid, all_pids: _claude_is_subagent(ppid),
    "get_sessions": get_active_sessions,
    "detect_state": detect_session_state,
}



def _opencode_get_sessions(cwd):
    """Get active OpenCode sessions for a directory from SQLite."""
    if not os.path.isfile(OPENCODE_DB):
        return []
    try:
        conn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=2)
        conn.execute("PRAGMA journal_mode=WAL")
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - 300_000  # 5 minutes
        rows = conn.execute(
            """SELECT s.id, s.title, s.time_updated
               FROM session s
               WHERE s.directory = ? AND s.time_updated > ?
               ORDER BY s.time_updated DESC""",
            (cwd, cutoff)
        ).fetchall()
        sessions = []
        for sid, title, mtime in rows:
            task = ""
            part_row = conn.execute(
                """SELECT json_extract(p.data, '$.text')
                   FROM part p
                   JOIN message m ON p.message_id = m.id
                   WHERE p.session_id = ?
                     AND json_extract(m.data, '$.role') = 'user'
                     AND json_extract(p.data, '$.type') = 'text'
                   ORDER BY p.time_created ASC LIMIT 1""",
                (sid,)
            ).fetchone()
            if part_row and part_row[0]:
                task = part_row[0]
            ctx_pct = _opencode_context_usage(conn, sid)
            sessions.append((sid, task, mtime / 1000.0, ctx_pct))
        conn.close()
        return sessions
    except (sqlite3.Error, OSError):
        return []


def _opencode_context_usage(conn, session_id):
    """Get context window usage % for an OpenCode session."""
    try:
        row = conn.execute(
            """SELECT data FROM message
               WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant'
               ORDER BY time_created DESC LIMIT 1""",
            (session_id,)
        ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        tokens = data.get("tokens", {})
        total = (tokens.get("input", 0)
                 + tokens.get("output", 0)
                 + tokens.get("cache", {}).get("read", 0)
                 + tokens.get("cache", {}).get("write", 0))
        if total <= 0:
            return None
        return min(100, int(total / 200000 * 100))
    except (sqlite3.Error, json.JSONDecodeError, KeyError):
        return None


def _opencode_detect_state(cwd):
    """Detect state of OpenCode sessions from SQLite."""
    if not os.path.isfile(OPENCODE_DB):
        return []
    try:
        conn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=2)
        conn.execute("PRAGMA journal_mode=WAL")
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - 300_000
        rows = conn.execute(
            """SELECT s.id, s.time_updated
               FROM session s
               WHERE s.directory = ? AND s.time_updated > ?
               ORDER BY s.time_updated DESC""",
            (cwd, cutoff)
        ).fetchall()
        results = []
        now = time.time()
        for sid, mtime_ms in rows:
            mtime = mtime_ms / 1000.0
            staleness = now - mtime
            part_row = conn.execute(
                """SELECT json_extract(data, '$.type'),
                          json_extract(data, '$.state.status')
                   FROM part
                   WHERE session_id = ?
                   ORDER BY time_created DESC LIMIT 1""",
                (sid,)
            ).fetchone()
            if part_row:
                ptype, pstatus = part_row
                if ptype == "tool" and pstatus == "running":
                    state = "working"
                elif ptype == "tool" and pstatus == "pending":
                    state = "permission"
                elif ptype == "tool" and pstatus == "completed" and staleness < 3:
                    state = "working"
                elif staleness < 3:
                    state = "working"
                else:
                    state = "input"
            else:
                state = "input"
            results.append((sid, state, mtime))
        conn.close()
        return results
    except (sqlite3.Error, OSError):
        return []


def _opencode_is_subagent(args, pid, ppid, all_pids):
    """Check if an OpenCode process is a sub-agent (launched with -s flag)."""
    return "-s " in args or "-s=" in args


OPENCODE_ADAPTER = {
    "name": "opencode",
    "process_name": "opencode",
    "match_process": lambda args, cmd: cmd == "opencode" or cmd.endswith("/opencode"),
    "detect_source": lambda args: "terminal",
    "is_subagent": _opencode_is_subagent,
    "get_sessions": _opencode_get_sessions,
    "detect_state": _opencode_detect_state,
}


TOOL_ADAPTERS = []
if TOOLS_CONFIG.get("claude", {}).get("enabled", True):
    TOOL_ADAPTERS.append(CLAUDE_ADAPTER)
if TOOLS_CONFIG.get("opencode", {}).get("enabled", True):
    TOOL_ADAPTERS.append(OPENCODE_ADAPTER)

def focus_window(address):
    """Focus a Hyprland window by address."""
    if not address:
        return
    subprocess.run(
        ["hyprctl", "dispatch", "focuswindow", f"address:{address}"],
        capture_output=True, timeout=3
    )


def collect_instances():
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,pcpu,rss,etime,args", "--no-headers"],
            capture_output=True, text=True, timeout=5
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    # Get Hyprland windows (skip if disabled)
    if HYPRLAND_ENABLED:
        windows_by_pid, all_windows = get_hyprland_windows()
    else:
        windows_by_pid, all_windows = {}, []

    all_instances = []

    for adapter in TOOL_ADAPTERS:
        adapter_pids = set()
        instances = []

        # First pass: collect matching PIDs
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            cmd = parts[5]
            args = " ".join(parts[5:])
            if "claude-ps" in args or "grep" in args:
                continue
            if not adapter["match_process"](args, cmd):
                continue
            adapter_pids.add(int(parts[0]))

        # Second pass: build instances
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            cmd = parts[5]
            args = " ".join(parts[5:])
            if "claude-ps" in args or "grep" in args:
                continue
            if not adapter["match_process"](args, cmd):
                continue

            pid = int(parts[0])
            ppid = int(parts[1])
            cpu = float(parts[2])
            rss = int(parts[3])
            elapsed = parts[4]

            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                cwd = "unknown"

            source = adapter["detect_source"](args)
            is_subagent = adapter["is_subagent"](args, pid, ppid, adapter_pids)

            inst = {
                "pid": pid,
                "ppid": ppid,
                "cpu": cpu,
                "rss": rss,
                "elapsed": elapsed,
                "cwd": cwd,
                "source": source,
                "is_subagent": is_subagent,
                "tool": adapter["name"],
                "workspace": None,
                "window_address": None,
            }

            ws, addr = match_window(inst, windows_by_pid, all_windows)
            inst["workspace"] = ws
            inst["window_address"] = addr
            instances.append(inst)

        # Assign tasks and states per cwd
        by_cwd = {}
        for inst in instances:
            by_cwd.setdefault(inst["cwd"], []).append(inst)

        for cwd, cwd_instances in by_cwd.items():
            sessions = adapter["get_sessions"](cwd)
            states = adapter["detect_state"](cwd)
            sorted_insts = sorted(cwd_instances, key=lambda i: i["pid"])
            for i, inst in enumerate(sorted_insts):
                if i < len(sessions):
                    inst["task"] = sessions[i][1]
                    inst["ctx_pct"] = sessions[i][3]
                else:
                    inst["task"] = ""
                    inst["ctx_pct"] = None
                if i < len(states):
                    inst["state"] = states[i][1]
                else:
                    inst["state"] = "unknown"

        all_instances.extend(instances)

    return all_instances


def group_by_workspace(instances):
    groups = {}
    for inst in instances:
        ws = inst.get("workspace") or "?"
        if ws not in groups:
            groups[ws] = []
        groups[ws].append(inst)
    sorted_groups = []
    for ws in sorted(groups.keys(), key=lambda w: (w == "?", w)):
        insts = sorted(groups[ws], key=lambda i: (i["is_subagent"], i["pid"]))
        sorted_groups.append((ws, insts))
    return sorted_groups


def group_by_directory(instances):
    groups = {}
    for inst in instances:
        cwd = inst["cwd"]
        if cwd not in groups:
            groups[cwd] = []
        groups[cwd].append(inst)
    sorted_groups = []
    for cwd in sorted(groups.keys()):
        insts = sorted(groups[cwd], key=lambda i: (i["is_subagent"], i["pid"]))
        sorted_groups.append((cwd, insts))
    return sorted_groups


def group_instances(instances):
    if GROUP_BY == "workspace" and HYPRLAND_ENABLED:
        return group_by_workspace(instances), "workspace"
    return group_by_directory(instances), "directory"


def build_lines(groups, max_x=80, max_y=24, group_mode="workspace"):
    """Build display lines. Returns (lines, selectable_indices, instance_map).

    instance_map maps selectable index -> instance dict (for focus action).
    Adapts layout to available terminal size.
    """
    lines = []
    selectable_indices = []
    instance_map = {}

    content_height = max_y - 4  # header(2) + footer(2)
    # Count total content lines needed (compact) to decide spacing
    total_items = sum(len(insts) for _, insts in groups)
    total_compact = len(groups) + total_items  # headers + instances
    # Extra vertical space available
    extra_v = max(0, content_height - total_compact)
    # Distribute: extra blank lines between groups, and padding within groups
    group_spacing = min(2, extra_v // max(len(groups), 1)) if extra_v > 0 else 0
    # If lots of space, show task lines; otherwise skip them
    show_tasks = extra_v > total_items
    # If even more space, add vertical padding at top
    top_pad = min(2, extra_v // 4) if extra_v > len(groups) * 2 + total_items else 0

    for _ in range(top_pad):
        lines.append(("blank", "", []))

    task_max_len = max(40, max_x - 10)

    for gi, (group_key, instances) in enumerate(groups):
        count = len(instances)
        label = "instance" if count == 1 else "instances"
        if group_mode == "workspace":
            ws_label = WORKSPACE_LABELS.get(str(group_key), str(group_key))
            header_text = f"  Workspace {group_key}: {ws_label}  ({count} {label})"
        else:
            header_text = f"  {shorten_path(group_key)}  ({count} {label})"
        remaining = max_x - len(header_text) - 1
        if remaining > 2:
            header_text += " " + "─" * remaining
        lines.append(("dir", header_text, []))

        for inst in instances:
            pid = inst["pid"]
            cpu = inst["cpu"]
            mem = format_mem(inst["rss"])
            elapsed = format_elapsed(inst["elapsed"])
            source = inst["source"]
            is_sub = inst["is_subagent"]
            active = cpu > 1.0
            ws = inst["workspace"]

            dir_short = os.path.basename(inst["cwd"])
            state = inst.get("state", "unknown")

            if state == "permission":
                marker = "!"
                state_label = "NEEDS INPUT"
            elif state == "working":
                marker = "●"
                state_label = "working"
            elif state == "input":
                marker = "○"
                state_label = "idle"
            else:
                marker = "●" if active else "○"
                state_label = ""

            ctx_pct = inst.get("ctx_pct")
            ctx_str = f"ctx:{ctx_pct}%" if ctx_pct is not None else ""

            # Build columns with consistent spacing
            if is_sub:
                text = f"      └ {pid}  {source:<9s} {dir_short:<16s} {cpu:>5.1f}%  {mem:>5s}  {elapsed:>7s}  {ctx_str}"
            else:
                if state_label:
                    text = f"    {marker} {pid}  {source:<9s} {dir_short:<16s} {state_label:<12s} {cpu:>5.1f}%  {mem:>5s}  {elapsed:>7s}  {ctx_str}"
                else:
                    text = f"    {marker} {pid}  {source:<9s} {dir_short:<16s} {cpu:>5.1f}%  {mem:>5s}  {elapsed:>7s}  {ctx_str}"

            attrs = [(state, is_sub, source, ctx_pct)]
            sel_idx = len(selectable_indices)
            selectable_indices.append(len(lines))
            instance_map[sel_idx] = inst
            lines.append(("inst", text, attrs))

            # Task line — show when space allows, use full width
            task = inst.get("task", "")
            if task and show_tasks:
                task_preview = task[:task_max_len].replace("\n", " ")
                indent = "        " if is_sub else "      "
                lines.append(("task", f"{indent}{task_preview}", []))

        # Spacing between groups
        for _ in range(group_spacing + 1):
            lines.append(("blank", "", []))

    return lines, selectable_indices, instance_map


def main(stdscr):
    curses.curs_set(0)
    stdscr.timeout(200)
    curses.use_default_colors()

    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_MAGENTA, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)
    curses.init_pair(5, curses.COLOR_WHITE, -1)
    curses.init_pair(6, curses.COLOR_BLACK, -1)
    curses.init_pair(7, curses.COLOR_RED, -1)

    COLOR_ACTIVE = curses.color_pair(1)
    COLOR_IDLE = curses.color_pair(2)
    COLOR_VSCODE = curses.color_pair(3)
    COLOR_DIR = curses.color_pair(4) | curses.A_BOLD
    COLOR_HEADER = curses.color_pair(5) | curses.A_BOLD
    COLOR_DIM = curses.color_pair(6) | curses.A_DIM
    COLOR_PERMISSION = curses.color_pair(7) | curses.A_BOLD | curses.A_BLINK

    selected = 0
    last_refresh = 0
    lines = []
    selectable = []
    instance_map = {}
    scroll_offset = 0
    total = 0
    status_msg = ""
    status_time = 0

    prev_size = (0, 0)

    while True:
        now = time.monotonic()
        max_y, max_x = stdscr.getmaxyx()

        # Rebuild on data refresh or terminal resize
        size_changed = (max_y, max_x) != prev_size
        if now - last_refresh >= REFRESH_INTERVAL or last_refresh == 0 or size_changed:
            prev_size = (max_y, max_x)
            instances = collect_instances()
            groups, group_mode = group_instances(instances)
            lines, selectable, instance_map = build_lines(groups, max_x, max_y, group_mode)
            total = len(instances)
            last_refresh = now
            if selectable:
                selected = min(selected, len(selectable) - 1)
                selected = max(selected, 0)

        # Clear status after 3 seconds
        if status_msg and now - status_time > 3:
            status_msg = ""

        stdscr.erase()

        # Header
        title = " Claude Code Monitor "
        count_str = f" {total} instance{'s' if total != 1 else ''} "
        pad = max(0, max_x - len(title) - len(count_str))
        header = title + "─" * pad + count_str
        try:
            stdscr.addnstr(0, 0, header[:max_x], max_x, COLOR_HEADER)
            stdscr.addnstr(1, 0, "─" * max_x, max_x, COLOR_HEADER)
        except curses.error:
            pass

        # Footer
        footer_text = "  q: quit  j/↑: up  k/↓: down  r: refresh"
        if HYPRLAND_ENABLED:
            footer_text += "  Enter: focus"
        try:
            stdscr.addnstr(max_y - 2, 0, "─" * max_x, max_x, COLOR_HEADER)
            if status_msg:
                stdscr.addnstr(max_y - 1, 0, f"  {status_msg}"[:max_x], max_x, COLOR_ACTIVE)
            else:
                stdscr.addnstr(max_y - 1, 0, footer_text[:max_x], max_x, COLOR_DIM | curses.A_NORMAL)
        except curses.error:
            pass

        content_start = 2
        content_height = max_y - 4

        if content_height < 1:
            stdscr.refresh()
            key = stdscr.getch()
            if key == ord("q"):
                break
            continue

        # Adjust scroll
        if selectable:
            sel_line = selectable[selected]
            if sel_line < scroll_offset:
                scroll_offset = sel_line
            elif sel_line >= scroll_offset + content_height:
                scroll_offset = sel_line - content_height + 1

        # Draw lines
        for i in range(scroll_offset, min(scroll_offset + content_height, len(lines))):
            row = content_start + (i - scroll_offset)
            if row >= max_y - 2:
                break

            kind, text, attrs = lines[i]
            is_selected = (i in selectable and selectable.index(i) == selected) if selectable else False

            try:
                if kind == "dir":
                    style = COLOR_DIR
                    stdscr.addnstr(row, 0, text[:max_x], max_x, style)

                elif kind == "inst":
                    if attrs:
                        state, is_sub, source, ctx_pct = attrs[0]
                        if is_sub:
                            base_style = COLOR_DIM
                        elif state == "permission":
                            base_style = COLOR_PERMISSION
                        elif state == "working":
                            base_style = COLOR_ACTIVE
                        elif state == "input":
                            base_style = COLOR_IDLE
                        else:
                            base_style = COLOR_IDLE

                        if is_selected:
                            cursor = " >"
                            stdscr.addnstr(row, 0, cursor, max_x, COLOR_ACTIVE | curses.A_BOLD)
                            stdscr.addnstr(row, len(cursor), text[len(cursor):max_x], max_x - len(cursor), base_style | curses.A_BOLD)
                        else:
                            stdscr.addnstr(row, 0, text[:max_x], max_x, base_style)

                        # Highlight "vscode" in magenta
                        if source == "vscode" and not is_selected:
                            vs_pos = text.find("vscode")
                            if 0 <= vs_pos < max_x - 6:
                                stdscr.addnstr(row, vs_pos, "vscode", min(6, max_x - vs_pos), COLOR_VSCODE)

                        # Highlight ctx usage by threshold
                        if ctx_pct is not None:
                            ctx_pos = text.find("ctx:")
                            if 0 <= ctx_pos < max_x:
                                ctx_text = f"ctx:{ctx_pct}%"
                                if ctx_pct > 90:
                                    ctx_color = COLOR_PERMISSION
                                elif ctx_pct > 70:
                                    ctx_color = COLOR_IDLE
                                else:
                                    ctx_color = COLOR_ACTIVE
                                stdscr.addnstr(row, ctx_pos, ctx_text, min(len(ctx_text), max_x - ctx_pos), ctx_color)

                elif kind == "task":
                    stdscr.addnstr(row, 0, text[:max_x], max_x, COLOR_DIM | curses.A_ITALIC)

            except curses.error:
                pass

        if not lines:
            msg = "No Claude Code instances running."
            try:
                stdscr.addnstr(content_start + 1, 2, msg, max_x - 2, COLOR_IDLE)
            except curses.error:
                pass

        stdscr.refresh()

        # Input
        key = stdscr.getch()
        if key == ord("q") or key == 27:
            break
        elif key in (ord("k"), curses.KEY_UP):
            if selectable and selected > 0:
                selected -= 1
        elif key in (ord("j"), curses.KEY_DOWN):
            if selectable and selected < len(selectable) - 1:
                selected += 1
        elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            if selectable and selected in instance_map:
                inst = instance_map[selected]
                addr = inst.get("window_address")
                if addr:
                    focus_window(addr)
                    status_msg = f"Focused PID {inst['pid']} on WS:{inst.get('workspace', '?')}"
                    status_time = time.monotonic()
                else:
                    status_msg = f"No window found for PID {inst['pid']}"
                    status_time = time.monotonic()
        elif key == ord("r"):
            last_refresh = 0
        elif key == curses.KEY_RESIZE:
            pass


if __name__ == "__main__":
    curses.wrapper(main)
