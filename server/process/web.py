"""Serves the avatar UI to browsers, so a phone can use Marina.

Turned on by `web.enabled`. The page is the same renderer the Electron app
loads; web-shim.js stands in for Electron's preload, and the phone records
its own audio and sends it to /voice/stream.

Browsers only allow the microphone on HTTPS, so put something in front of
the bridge that terminates TLS, such as `tailscale serve`.
"""
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from process.config import resolve

_HEAD = """
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Marina">
  <meta name="theme-color" content="#14161c">
  <link rel="manifest" href="manifest.webmanifest">
  <link rel="apple-touch-icon" href="icon.png">
  <script src="web-shim.js"></script>"""


def mount(app, cfg):
    root = resolve(cfg.get("renderer_dir", "app/renderer"))
    model = resolve(cfg.get("model_path", "app/models/model.vrm"))
    icon = resolve(cfg.get("icon_path", "app/build/icon.png"))
    index = (root / "index.html").read_text(encoding="utf-8")
    index = index.replace("<head>", "<head>" + _HEAD, 1)

    @app.get("/", include_in_schema=False)
    def page():
        return HTMLResponse(index, headers={"Cache-Control": "no-cache"})

    @app.get("/model.vrm", include_in_schema=False)
    def avatar():
        return FileResponse(model, media_type="model/gltf-binary")

    @app.get("/icon.png", include_in_schema=False)
    def touch_icon():
        return FileResponse(icon, media_type="image/png")

    app.mount("/", StaticFiles(directory=root), name="web")
    print(f"[web] serving the avatar UI from {root}", flush=True)
