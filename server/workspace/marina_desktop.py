"""How her XFCE desktop looks, changed safely. Runs on her machine.

The workspace tool sends this file over SSH on every call (python3 - ACTION
JSON), so there is nothing to install and it can't drift from the repo.
Everything it sets is checked first (a theme that isn't installed, a
monitor that doesn't exist, a wallpaper that isn't a file) and read back
afterwards, and the settings it manages are saved before each change so
"undo" can put them back.

Actions: status, set, restart, undo. Prints one JSON object.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
BACKUPS = HOME / ".marina" / "desktop-backups"
WALLPAPERS = HOME / "Pictures" / "wallpapers"
ENV = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":0")

STYLES = {"none": 0, "centered": 1, "tiled": 2, "stretched": 3, "scaled": 4, "zoomed": 5}
AUTOHIDE = {"never": 0, "intelligent": 1, "always": 2}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def run(*args, check=False):
    out = subprocess.run(args, capture_output=True, text=True, env=ENV, timeout=30)
    if check and out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip() or f"{args[0]} failed")
    return out.stdout.strip()


def get(channel, prop):
    out = subprocess.run(["xfconf-query", "-c", channel, "-p", prop],
                         capture_output=True, text=True, env=ENV)
    return out.stdout.strip() if out.returncode == 0 else None


def put(channel, prop, kind, value):
    """Set a property, creating it with the right type if it is new."""
    value = {True: "true", False: "false"}.get(value, value) if kind == "bool" else str(value)
    run("xfconf-query", "-c", channel, "-p", prop, "--create", "-t", kind, "-s", value, check=True)


def running(name):
    return subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0


def monitors():
    names = re.findall(r"^\s*\d+:\s+\S+\s+.*\s(\S+)\s*$", run("xrandr", "--listmonitors"), re.M)
    return names or ["0"]


def themes():
    dirs = [Path("/usr/share/themes"), HOME / ".themes", HOME / ".local/share/themes"]
    gtk, wm = set(), set()
    for d in dirs:
        if d.is_dir():
            for t in d.iterdir():
                if (t / "gtk-3.0").is_dir():
                    gtk.add(t.name)
                if (t / "xfwm4").is_dir():
                    wm.add(t.name)
    return sorted(gtk), sorted(wm | {"Default"})


def icon_themes():
    found = set()
    for d in [Path("/usr/share/icons"), HOME / ".icons", HOME / ".local/share/icons"]:
        if d.is_dir():
            found |= {t.name for t in d.iterdir() if (t / "index.theme").is_file()}
    return sorted(found)


def panels():
    out = []
    for pid in re.findall(r"^\d+$", get("xfce4-panel", "/panels") or "", re.M):
        base = f"/panels/panel-{pid}"
        hide = get("xfce4-panel", f"{base}/autohide-behavior") or "0"
        out.append({"id": int(pid), "position": get("xfce4-panel", f"{base}/position"),
                    "size": get("xfce4-panel", f"{base}/size"),
                    "autohide": {v: k for k, v in AUTOHIDE.items()}.get(int(hide), hide)})
    return out


def status():
    gtk, wm = themes()
    mons = monitors()
    return {
        "gtk_theme": get("xsettings", "/Net/ThemeName"),
        "icon_theme": get("xsettings", "/Net/IconThemeName"),
        "wm_theme": get("xfwm4", "/general/theme"),
        "ui_font": get("xsettings", "/Gtk/FontName"),
        "monitors": mons,
        "wallpaper": {m: get("xfce4-desktop", f"/backdrop/screen0/monitor{m}/workspace0/last-image")
                      for m in mons},
        "desktop_icons": get("xfce4-desktop", "/desktop-icons/style") != "0",
        "terminal": {k: get("xfce4-terminal", f"/{k}") for k in (
            "font-name", "color-background", "color-foreground", "color-cursor", "color-palette")},
        "panels": panels(),
        "running": {n: running(n) for n in ("xfdesktop", "xfwm4", "xfce4-panel", "xfsettingsd")},
        "installed_gtk_themes": gtk,
        "installed_wm_themes": wm,
        "installed_icon_themes": icon_themes(),
    }


def _snapshot():
    """The managed settings as a 'set' request, saved for undo."""
    s = status()
    t = s["terminal"]
    snap = {k: s[k] for k in ("gtk_theme", "icon_theme", "wm_theme") if s[k]}
    wall = next((w for w in s["wallpaper"].values() if w), None)
    if wall:
        snap["wallpaper"] = wall
    snap["desktop_icons"] = s["desktop_icons"]
    for key, prop in (("terminal_font", "font-name"), ("terminal_background", "color-background"),
                      ("terminal_foreground", "color-foreground"), ("terminal_cursor", "color-cursor")):
        if t.get(prop):
            snap[key] = t[prop]
    if t.get("color-palette"):
        snap["terminal_palette"] = t["color-palette"].split(";")
    for p in s["panels"]:
        snap.setdefault("panel_autohide", {})[str(p["id"])] = p["autohide"]
    BACKUPS.mkdir(parents=True, exist_ok=True)
    path = BACKUPS / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(snap, indent=2))
    for old in sorted(BACKUPS.glob("*.json"))[:-20]:
        old.unlink()
    return path.name


def _wallpaper(value):
    if re.match(r"^https?://", value):
        WALLPAPERS.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", value.split("?")[0].rsplit("/", 1)[-1]) or "wallpaper"
        dest = WALLPAPERS / name
        req = urllib.request.Request(value, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        value = str(dest)
    path = Path(os.path.expanduser(value))
    if not path.is_file():
        raise ValueError(f"no file at {path}")
    kind = run("file", "-b", "--mime-type", str(path))
    if not kind.startswith("image/"):
        raise ValueError(f"{path} is {kind}, not an image")
    return str(path)


def _refresh_theme(name):
    # xfsettingsd only tells running apps when the value changes.
    other = "Adwaita" if name != "Adwaita" else "Default"
    put("xsettings", "/Net/ThemeName", "string", other)
    time.sleep(0.5)
    put("xsettings", "/Net/ThemeName", "string", name)


def apply(req):
    gtk, wm = themes()
    icons = icon_themes()
    done, errors = [], []

    def attempt(key, fn):
        if key in req and req[key] is not None:
            try:
                fn(req[key])
                done.append(key)
            except Exception as e:
                errors.append(f"{key}: {e}")

    def gtk_theme(v):
        if v not in gtk:
            raise ValueError(f"{v!r} isn't installed. Installed: {', '.join(gtk)}")
        _refresh_theme(v)
        if "wm_theme" not in req:
            put("xfwm4", "/general/theme", "string", v if v in wm else "Default")

    def wm_theme(v):
        if v not in wm:
            raise ValueError(f"{v!r} has no window border theme. Available: {', '.join(wm)}")
        put("xfwm4", "/general/theme", "string", v)

    def icon_theme(v):
        if v not in icons:
            raise ValueError(f"{v!r} isn't installed. Installed: {', '.join(icons)}")
        put("xsettings", "/Net/IconThemeName", "string", v)

    def wallpaper(v):
        path = _wallpaper(v)
        style = STYLES.get(str(req.get("wallpaper_style", "zoomed")), 5)
        for m in monitors():
            base = f"/backdrop/screen0/monitor{m}/workspace0"
            put("xfce4-desktop", f"{base}/last-image", "string", path)
            put("xfce4-desktop", f"{base}/image-style", "int", style)
        if not running("xfdesktop"):
            subprocess.Popen(["setsid", "-f", "xfdesktop"], env=ENV,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def desktop_icons(v):
        put("xfce4-desktop", "/desktop-icons/style", "int", 2 if v else 0)

    def ui_font(v):
        put("xsettings", "/Gtk/FontName", "string", v)

    def terminal_font(v):
        family = re.sub(r"\s+\d+(\.\d+)?$", "", v)
        if not run("fc-list", family):
            raise ValueError(f"no font called {family!r} is installed")
        put("xfce4-terminal", "/font-use-system", "bool", False)
        put("xfce4-terminal", "/font-name", "string", v)

    def colour(prop):
        def set_(v):
            if not HEX.match(v):
                raise ValueError(f"{v!r} isn't a #rrggbb colour")
            put("xfce4-terminal", "/color-use-theme", "bool", False)
            put("xfce4-terminal", prop, "string", v)
        return set_

    def terminal_palette(v):
        colours = v.split(";") if isinstance(v, str) else list(v)
        if len(colours) != 16 or not all(HEX.match(c) for c in colours):
            raise ValueError("the palette needs exactly 16 #rrggbb colours")
        put("xfce4-terminal", "/color-use-theme", "bool", False)
        put("xfce4-terminal", "/color-palette", "string", ";".join(colours))

    def panel_autohide(v):
        known = {str(p["id"]) for p in panels()}
        for pid, mode in (v.items() if isinstance(v, dict) else []):
            if str(pid) not in known:
                raise ValueError(f"there is no panel {pid}; panels are {', '.join(sorted(known))}")
            if mode not in AUTOHIDE:
                raise ValueError(f"autohide must be one of {', '.join(AUTOHIDE)}")
            put("xfce4-panel", f"/panels/panel-{pid}/autohide-behavior", "int", AUTOHIDE[mode])

    backup = _snapshot()
    for key, fn in (("gtk_theme", gtk_theme), ("wm_theme", wm_theme), ("icon_theme", icon_theme),
                    ("wallpaper", wallpaper), ("desktop_icons", desktop_icons), ("ui_font", ui_font),
                    ("terminal_font", terminal_font),
                    ("terminal_background", colour("/color-background")),
                    ("terminal_foreground", colour("/color-foreground")),
                    ("terminal_cursor", colour("/color-cursor")),
                    ("terminal_palette", terminal_palette), ("panel_autohide", panel_autohide)):
        attempt(key, fn)
    unknown = sorted(set(req) - {"gtk_theme", "wm_theme", "icon_theme", "wallpaper", "wallpaper_style",
                                 "desktop_icons", "ui_font", "terminal_font", "terminal_background",
                                 "terminal_foreground", "terminal_cursor", "terminal_palette",
                                 "panel_autohide"})
    if unknown:
        errors.append(f"not settings this tool knows: {', '.join(unknown)}")
    now = status()
    for k in ("installed_gtk_themes", "installed_wm_themes", "installed_icon_themes"):
        now.pop(k)
    return {"applied": done, "errors": errors, "undo_point": backup, "now": now}


def restart(component):
    if component == "desktop":
        if running("xfdesktop"):
            run("xfdesktop", "--reload")
        else:
            subprocess.Popen(["setsid", "-f", "xfdesktop"], env=ENV,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif component == "panel":
        run("xfce4-panel", "-r")
    elif component == "wm":
        subprocess.Popen(["setsid", "-f", "xfwm4", "--replace"], env=ENV,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        raise ValueError("component must be desktop, panel or wm")
    time.sleep(2)
    return {"restarted": component,
            "running": {n: running(n) for n in ("xfdesktop", "xfwm4", "xfce4-panel")}}


def undo():
    saved = sorted(BACKUPS.glob("*.json"))
    if not saved:
        return {"errors": ["nothing to undo"]}
    previous = json.loads(saved[-1].read_text())
    saved[-1].unlink()
    result = apply(previous)
    # apply() saved a snapshot of the state being undone; drop it so a
    # second undo goes further back instead of redoing.
    newest = sorted(BACKUPS.glob("*.json"))
    if newest:
        newest[-1].unlink()
    result["restored"] = saved[-1].name
    return result


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        if action == "status":
            out = status()
        elif action == "set":
            out = apply(args)
        elif action == "restart":
            out = restart(args.get("component", ""))
        elif action == "undo":
            out = undo()
        else:
            out = {"errors": [f"unknown action {action!r}; use status, set, restart or undo"]}
    except Exception as e:
        out = {"errors": [f"{type(e).__name__}: {e}"]}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
