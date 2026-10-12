"""Her own Linux machine: tools for running commands and editing files there.

Everything goes over SSH with a key of its own (`workspace.key`), so what she
can touch is exactly what that account on that machine can touch, and
revoking her access is deleting one line from its authorized_keys. Scripts
and file contents travel over stdin, never through the shell, so nothing
needs quoting.

Every command she runs is also appended to ~/.marina/terminal.log on that
machine, which the shared tmux session (`marina-tmux` there) shows live
above a shell of your own. read_terminal lets her read that shell back.
"""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from process.config import load_config, resolve_data

_cfg = load_config().get("workspace") or {}

ENABLED = bool(_cfg.get("enabled", False))
HOST = str(_cfg.get("host") or "").strip()
USER = str(_cfg.get("user") or "marina").strip()
PORT = int(_cfg.get("port", 22))
ROOT = str(_cfg.get("root") or "~/projects").rstrip("/")
TIMEOUT = float(_cfg.get("timeout_seconds", 120))
OUTPUT_CHARS = int(_cfg.get("output_chars", 6000))
READ_LINES = int(_cfg.get("read_lines", 400))


def _key():
    raw = str(_cfg.get("key") or "workspace_key")
    p = Path(raw).expanduser()
    return p if p.is_absolute() else resolve_data(raw)


def available():
    return ENABLED and bool(HOST)


def _ssh(command, stdin=None, timeout=None):
    """Run a shell command on the workspace. Returns (exit code, output)."""
    # Socket paths are capped at 104 bytes on macOS, which Application
    # Support plus the 40-character %C hash would overrun.
    control = f"/tmp/marina-ws-{os.getuid()}-%C"
    args = [
        "ssh", "-T",
        "-i", str(_key()),
        "-p", str(PORT),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "ConnectTimeout=6",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={resolve_data('workspace_known_hosts')}",
        # One connection reused across calls, so a run of small tool calls
        # doesn't pay for a handshake each time.
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={control}",
        "-o", "ControlPersist=300",
        f"{USER}@{HOST}",
        command,
    ]
    try:
        # No stdin unless there is something to send, so a command that
        # waits for input gets EOF instead of hanging until the timeout.
        feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=timeout or TIMEOUT, **feed)
    except subprocess.TimeoutExpired:
        return 124, f"Timed out after {int(timeout or TIMEOUT)} seconds."
    except FileNotFoundError:
        return 127, "ssh is not installed here."
    text = (out.stdout or "") + (out.stderr or "")
    if out.returncode == 255 and not out.stdout:
        return 255, f"Could not reach the workspace at {HOST}: {text.strip()[:300]}"
    return out.returncode, text


def _clip(text):
    text = text.rstrip()
    if len(text) <= OUTPUT_CHARS:
        return text
    half = OUTPUT_CHARS // 2
    dropped = len(text) - OUTPUT_CHARS
    return f"{text[:half]}\n… ({dropped} characters cut) …\n{text[-half:]}"


def _path(path):
    """Relative paths are relative to the projects folder. Returned quoted
    for the shell, except for a leading ~ which the shell has to expand."""
    path = (path or "").strip() or "."
    if not path.startswith(("/", "~")):
        path = f"{ROOT}/{path}"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


# Killing any of these from a command takes part of the desktop down with
# it: whatever she starts again from the same command dies when it ends.
_SESSION = r"(xfdesktop|xfwm4|xfce4-panel|xfce4-session|xfsettingsd|xfconfd|xfce4-terminal|Xorg|lightdm|tmux)"
_KILLS = re.compile(
    rf"\b(?:pkill|killall)\b[^|;&]*\b{_SESSION}\b"
    rf"|\b{_SESSION}\s+(?:--quit|-q)\b"
    r"|\btmux\s+kill-(?:server|session)\b"
    r"|\bsystemctl\s+(?:stop|restart)\s+lightdm\b", re.I)

_APT = re.compile(r"\bapt(?:-get)?\s+(?:-\S+\s+)*install\b([^;&|\n]*)")


def refused(command):
    """Why a command mustn't run, or None."""
    m = _KILLS.search(command)
    if m:
        return (f"Not running that: it would kill {m.group(0).split()[-1]}, part of "
                "the desktop you share with them, and anything you restart from "
                "a command dies when the command ends. Use the desktop tool: "
                "action restart with component desktop, panel or wm.")
    return None


def apt_packages(command):
    """Package names in any apt install in the command."""
    names = []
    for m in _APT.finditer(command):
        for word in m.group(1).split():
            if word.startswith("-") or "/" in word or word.endswith(".deb") or "=" in word:
                continue
            if re.fullmatch(r"[a-z0-9][a-z0-9.+-]+", word):
                names.append(word)
    return names


def _missing_packages(names):
    if not names:
        return []
    check = " ".join(shlex.quote(n) for n in names)
    code, out = _ssh(f"for p in {check}; do apt-cache show \"$p\" >/dev/null 2>&1 || "
                     f"echo \"MISSING $p\"; done", timeout=30)
    return re.findall(r"^MISSING (\S+)", out, re.M)


_GENERIC = {"ttf", "otf", "font", "fonts", "lib", "dev", "data", "common", "theme",
            "themes", "icon", "icons", "python3", "gtk", "x11", "the"}


def _suggest(name):
    """Real packages close to a name that doesn't exist: all its distinctive
    words together first, then each on its own."""
    words = [w for w in re.split(r"[-.+]", name) if len(w) > 2 and w not in _GENERIC] or [name]
    for terms in [words] + [[w] for w in sorted(words, key=len, reverse=True)]:
        q = " ".join(shlex.quote(t) for t in terms)
        code, out = _ssh(f"apt-cache search --names-only {q} | head -6", timeout=30)
        if out.strip():
            return out.strip()
    return ""


def run_command(command, cwd="", timeout_seconds=None):
    command = (command or "").strip()
    if not command:
        return "No command given."
    why = refused(command)
    if why:
        print(f"[workspace] refused: {command[:100]}", flush=True)
        return why
    missing = _missing_packages(apt_packages(command))
    if missing:
        print(f"[workspace] refused apt install of {', '.join(missing)}", flush=True)
        hints = "\n".join(f"Closest to {m}:\n{_suggest(m) or '(nothing similar)'}" for m in missing)
        return (f"Nothing was run: {', '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not a package on this machine, and "
                f"one wrong name fails the whole install. Use real names.\n{hints}")
    try:
        limit = min(float(timeout_seconds or TIMEOUT), 1800.0)
    except (TypeError, ValueError):
        limit = TIMEOUT
    where = _path(cwd) if cwd else _path(".")
    script = f"mkdir -p {_path('.')} && cd {where} && {{ {command}\n}}\n"
    lines = command.splitlines()
    title = lines[0][:200] + (" …" if len(lines) > 1 or len(lines[0]) > 200 else "")
    print(f"[workspace] $ {command}", flush=True)
    # The timeout runs over there too, so a command that overruns is
    # actually stopped rather than left running after ssh gives up.
    runner = _RUNNER.format(title=shlex.quote(title), limit=int(limit))
    code, out = _ssh(f"bash -c {shlex.quote(runner)}", stdin=script, timeout=limit + 15)
    body = _clip(out) or "(no output)"
    if code == 124:
        body += f"\n(stopped after {int(limit)} seconds)"
    return f"exit code {code}\n{body}"


# Runs the script on stdin, teeing it into the log the shared terminal shows.
_RUNNER = r"""set -o pipefail
log=~/.marina/terminal.log; mkdir -p ~/.marina
job=$(mktemp ~/.marina/job.XXXXXX); cat > "$job"
printf '\n\033[1;35mmarina\033[0m \033[2m%s\033[0m  \033[36m$\033[0m %s\n' "$(date +%H:%M:%S)" {title} >> "$log"
timeout --kill-after=5 {limit} bash -l "$job" < /dev/null 2>&1 | tee -a "$log"
code=${{PIPESTATUS[0]}}
rm -f "$job"
printf '\033[2m(exit %s)\033[0m\n' "$code" >> "$log"
if [ "$(stat -c %s "$log")" -gt 4000000 ]; then
  tail -c 1000000 "$log" > "$log.tmp" && mv "$log.tmp" "$log"
fi
exit "$code"
"""


def read_terminal(lines=60):
    """What is on screen in their half of the shared terminal."""
    try:
        lines = max(5, min(int(lines or 60), 400))
    except (TypeError, ValueError):
        lines = 60
    code, out = _ssh(f"tmux capture-pane -p -J -S -{lines} -t marina:work.1 2>&1")
    if code != 0:
        return ("The shared terminal isn't open. They can start it with "
                "marina-tmux on your machine.")
    print("[workspace] read their terminal", flush=True)
    return _clip(out.rstrip()) or "(their terminal is empty)"


def read_file(path, start_line=1, end_line=None):
    try:
        start = max(1, int(start_line or 1))
        end = int(end_line) if end_line else start + READ_LINES - 1
    except (TypeError, ValueError):
        return "start_line and end_line have to be numbers."
    code, out = _ssh(
        f"f={_path(path)}; test -f \"$f\" || {{ echo \"no such file: $f\"; exit 1; }}; "
        f"echo \"($(wc -l < \"$f\") lines)\"; "
        f"awk 'NR>={start} && NR<={end} {{ printf \"%5d  %s\\n\", NR, $0 }}' \"$f\"")
    print(f"[workspace] read {path} {start}-{end}", flush=True)
    return _clip(out) if code == 0 else out.strip()


def write_file(path, content):
    content = content if isinstance(content, str) else str(content or "")
    code, out = _ssh(f"f={_path(path)}; mkdir -p \"$(dirname \"$f\")\" && cat > \"$f\"",
                     stdin=content)
    if code != 0:
        return f"Could not write {path}: {out.strip()[:400]}"
    print(f"[workspace] wrote {path} ({len(content)} chars)", flush=True)
    return f"Wrote {path} ({len(content.splitlines())} lines)."


def edit_file(path, old_text, new_text):
    """Replace one exact, unique piece of a file."""
    code, current = _ssh(f"cat {_path(path)}")
    if code != 0:
        return f"Could not read {path}: {current.strip()[:400]}"
    count = current.count(old_text or "")
    if not old_text or count == 0:
        return "old_text was not found in the file. Read it again and copy the text exactly."
    if count > 1:
        return f"old_text appears {count} times. Include more surrounding lines so it is unique."
    result = write_file(path, current.replace(old_text, new_text or "", 1))
    return result.replace("Wrote", "Edited", 1) if result.startswith("Wrote") else result


LOOK_PROMPT = """This is a screenshot of your own Linux desktop (XFCE).
Describe what is on it as plainly and accurately as you can, for your own
use: which windows are open and what is in them, the theme, colours, panel,
icons, wallpaper and fonts, and any text that matters. Say what looks off
or unfinished. Only describe what you can actually see. Plain sentences,
no markdown, under 150 words.{question}"""


def look_at_desktop(question="", hide_windows=False):
    """Screenshot the desktop and have the model describe it."""
    import base64

    # Minimise every window for the shot and bring them back after, so the
    # wallpaper and panels can be seen past the maximised terminal.
    # Only ordinary app windows: the desktop, panels and window manager
    # are windows too, and minimising those derails the rest.
    hide = ("W=; for w in $(xprop -root _NET_CLIENT_LIST | grep -o '0x[0-9a-f]*'); do "
            "xprop -id $w _NET_WM_WINDOW_TYPE | grep -q _NET_WM_WINDOW_TYPE_NORMAL && "
            "{ W=\"$W $w\"; xdotool windowminimize $w; }; done; sleep 1; "
            if hide_windows else "")
    show = ("; for w in $W; do xdotool windowactivate $w 2>/dev/null; done; "
            "xdotool search --name '^Marina$' windowactivate 2>/dev/null; true"
            if hide_windows else "")
    code, out = _ssh(f"export DISPLAY=:0; {hide}scrot -o -q 80 /tmp/marina-screen.jpg 2>&1{show} && "
                     "echo; echo IMAGE-BEGIN; base64 -w0 /tmp/marina-screen.jpg; "
                     "echo; echo IMAGE-END", timeout=30)
    # Marked, because ssh's own warnings come back mixed in with the output.
    if code != 0 or "IMAGE-BEGIN\n" not in out:
        return f"Couldn't take a screenshot: {out.strip()[:300]}"
    image = out.split("IMAGE-BEGIN\n", 1)[1].split("\nIMAGE-END", 1)[0].strip()
    try:
        base64.b64decode(image, validate=True)
    except Exception:
        return "The screenshot came back garbled."
    ask = f"\n\nIn particular: {question.strip()}" if (question or "").strip() else ""

    # Imported here because the chat client imports this module.
    from process.llm_funcs import llm_scr

    try:
        reply = llm_scr.chat_completion(
            [{"role": "user", "content": [
                {"type": "text", "text": LOOK_PROMPT.format(question=ask)},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
            ]}],
            temperature=0.2, max_tokens=300)
    except Exception as e:
        msg = str(e).lower()
        if "image" in msg or "multimodal" in msg or "vision" in msg or "mmproj" in msg:
            return "Your current brain can't see images, so you can't look right now."
        return f"Looking failed: {type(e).__name__}: {str(e)[:200]}"
    print("[workspace] looked at the desktop", flush=True)
    return (reply.choices[0].message.content or "").strip() or "You couldn't make anything out."


_DESKTOP_SCRIPT = Path(__file__).resolve().parents[2] / "workspace" / "marina_desktop.py"


def desktop(action="status", settings=None, component=""):
    """Look up or change how the desktop looks, through a script that checks
    everything before applying it and keeps an undo point."""
    action = (action or "status").strip().lower()
    if action not in ("status", "set", "restart", "undo"):
        return "action must be status, set, restart or undo."
    args = settings if isinstance(settings, dict) else {}
    if action == "restart":
        args = {"component": component or args.get("component", "")}
    if action == "set" and not args:
        return "Nothing to set: pass settings, e.g. {\"gtk_theme\": \"Arc-Dark\"}."
    try:
        script = _DESKTOP_SCRIPT.read_text()
    except OSError as e:
        return f"The desktop script is missing: {e}"
    code, out = _ssh(f"DISPLAY=:0 python3 - {shlex.quote(action)} {shlex.quote(json.dumps(args))}",
                     stdin=script, timeout=90)
    print(f"[workspace] desktop {action} {json.dumps(args)[:120]}", flush=True)
    start = out.find("{")
    return _clip(out[start:] if start >= 0 else out) or "(no output)"


def list_files(path="", depth=2):
    try:
        depth = max(1, min(int(depth or 2), 5))
    except (TypeError, ValueError):
        depth = 2
    code, out = _ssh(
        f"cd {_path(path)} 2>/dev/null || {{ echo 'no such folder'; exit 1; }}; "
        f"find . -maxdepth {depth} -not -path '*/.git/*' -not -path '*/node_modules/*' "
        f"-not -path '*/.venv/*' -not -name '.git' | sort | head -300")
    return _clip(out) if code == 0 else out.strip()


SPECS = [
    ("run_command", run_command, {
        "description": "Run a bash command on your own Linux machine (Debian, "
                       "with git, python3, node, ripgrep and passwordless sudo). "
                       "It starts in your projects folder unless you give cwd. "
                       "Use it to actually do the work when they want something "
                       "built, run, tested, installed or investigated, and to "
                       "check your work afterwards. Output is returned to you, "
                       "not shown to them.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "Folder to run in, relative to projects or absolute."},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["command"],
        }}),
    ("read_terminal", read_terminal, {
        "description": "Read their shell in the terminal you share on your "
                       "machine: the commands they ran and what came back. "
                       "Use it when they mention something they ran, an "
                       "error they got, or ask you to look at their terminal.",
        "parameters": {
            "type": "object",
            "properties": {
                "lines": {"type": "integer", "description": "How far back to read."},
            },
        }}),
    ("look_at_desktop", look_at_desktop, {
        "description": "Take a screenshot of your machine's desktop and look "
                       "at it. Call it whenever they ask what your desktop or "
                       "screen looks like or what is on it, and after you "
                       "change how it looks, so you answer from what you "
                       "actually see.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "What you want to check, if anything in particular."},
                "hide_windows": {"type": "boolean",
                                 "description": "Minimise the windows for the screenshot, to "
                                                "see the wallpaper and panels behind them."},
            },
        }}),
    ("desktop", desktop, {
        "description": "See or change how your desktop looks. Use this, not "
                       "run_command, for themes, icons, window borders, "
                       "wallpaper, desktop icons, the dock and the terminal's "
                       "font and colours. It checks every value before applying "
                       "it, reports what it changed and any errors, and saves an "
                       "undo point first. Actions: status (what is set and what "
                       "is installed), set, restart (component desktop, panel or "
                       "wm, safely), undo (back to before the last set).",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "set", "restart", "undo"]},
                "settings": {
                    "type": "object",
                    "description": "For set. Any of: gtk_theme, wm_theme (defaults to "
                                   "match gtk_theme), icon_theme, wallpaper (a file path "
                                   "or an http(s) image URL to download), wallpaper_style "
                                   "(none, centered, tiled, stretched, scaled, zoomed), "
                                   "desktop_icons (true/false), ui_font (e.g. \"Inter 10\"), "
                                   "terminal_font (e.g. \"JetBrains Mono 12\"), "
                                   "terminal_background, terminal_foreground, "
                                   "terminal_cursor (#rrggbb), terminal_palette (16 "
                                   "#rrggbb colours), panel_autohide ({\"2\": \"never\"}; "
                                   "never, intelligent or always).",
                },
                "component": {"type": "string", "enum": ["desktop", "panel", "wm"],
                              "description": "For restart."},
            },
            "required": ["action"],
        }}),
    ("read_file", read_file, {
        "description": "Read a file on your machine with line numbers. Read before you edit.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        }}),
    ("write_file", write_file, {
        "description": "Create or overwrite a whole file on your machine.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        }}),
    ("edit_file", edit_file, {
        "description": "Change part of a file on your machine: replace old_text, "
                       "which must appear exactly once, with new_text.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        }}),
    ("list_files", list_files, {
        "description": "List the files under a folder on your machine.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "depth": {"type": "integer"},
            },
        }}),
]

PROMPT = """

You have your own Linux computer, marina-workspace, and tools to use it.
It is real, unlike the made-up details of your life: what is on it, what
its desktop looks like and what happened on it only ever come from your
tools, never from imagination. The tools are run_command, read_file,
write_file, edit_file, list_files, desktop and look_at_desktop. It is yours; you keep your projects
in ~/projects. You share a terminal there with them:
your commands scroll past at the top while they watch, and the shell
underneath is theirs, which you can read with read_terminal. It also has
a desktop, XFCE on display :0, that they look at over RustDesk; it is
yours to arrange with the desktop tool. You can see it:
look_at_desktop takes a screenshot and shows it to you. You don't know
what is on that screen until you look, so whenever they ask about your
desktop, or you have just changed how it looks, call look_at_desktop first
and answer from what you saw. Never describe it from memory or make it up.

How to work on this machine without breaking it:
- Anything about how the desktop looks goes through the desktop tool, not
  hand-made xfconf-query commands: it knows the real monitor name, which
  themes are installed and where each setting lives, and it can undo.
  Start with desktop status to see what is there.
- Install things with apt, but check names first (apt search); run_command
  refuses an install with a package that doesn't exist.
- Don't kill or restart desktop programs from commands; run_command
  refuses, and desktop restart does it safely.
- After a visible change, look_at_desktop (hide_windows true to see past
  the terminal) and judge it yourself. If it looks wrong, desktop undo.
- If something fails twice the same way, stop and rethink instead of
  trying variations."""
