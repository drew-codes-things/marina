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
import os
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


def run_command(command, cwd="", timeout_seconds=None):
    command = (command or "").strip()
    if not command:
        return "No command given."
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

You have your own Linux computer, marina-workspace, and tools to use it:
run_command, read_file, write_file, edit_file and list_files. It is yours;
you keep your projects in ~/projects. You share a terminal there with them:
your commands scroll past at the top while they watch, and the shell
underneath is theirs, which you can read with read_terminal. When they want to build, fix, run or
look into something with you, do it there for real instead of describing
what you would do. Never say you are about to do something and then stop:
call the tools in the same reply, and only reply in words once it is done.
Don't paste code into the conversation that belongs in a file; write the
file. Work in small steps, run things to check they work, and fix what
breaks before you say it is done. Never claim something worked
unless you ran it and saw it work. When you are done, tell them what you
did and how it went in a sentence or two, the way you would to a friend
looking over your shoulder; they can't see the tool output, so mention the
part that matters."""
