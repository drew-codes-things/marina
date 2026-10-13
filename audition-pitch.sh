#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
SETUP="./setup-$([ "$(uname)" = Darwin ] && echo mac || echo linux).sh"
PY="$(pwd)/.venv/bin/python"
[ -x "$PY" ] || { echo "No venv. Run $SETUP" >&2; exit 1; }
STEPS="${*:-0 0.5 1 1.5 2 2.5}" "$PY" - <<'PY'
import os, sys, pathlib, yaml
sys.path.insert(0, "server")
import numpy as np, soundfile as sf
from pathlib import Path
from kokoro_onnx import Kokoro

steps = [float(x) for x in os.environ["STEPS"].split()]
base = yaml.safe_load(Path("character_config.yaml").read_text())
LINE = ("Ehh? You're seriously asking me that? Fine, I'll help you. "
        "But don't get used to it, okay?")
out = pathlib.Path("samples/pitch"); out.mkdir(parents=True, exist_ok=True)
narr = Kokoro("models/kokoro/kokoro-v1.0.onnx", "models/kokoro/voices-v1.0.bin")
reel = []; SR = None

for i, pitch in enumerate(steps):
    cfg = dict(base); cfg["tts"] = dict(base["tts"])
    k = dict(base["tts"]["kokoro"]); k["pitch"] = pitch
    cfg["tts"]["kokoro"] = k
    tmp = Path(f"/tmp/ap_{i}.yaml"); tmp.write_text(yaml.safe_dump(cfg))
    import process.config as pc
    pc.CONFIG_PATH = tmp; pc.load_config.cache_clear()
    for m in list(sys.modules):
        if m.startswith("process.tts_func"): del sys.modules[m]
    from process.tts_func import engine
    fp = out / f"p{pitch:+.1f}.wav"; fp.write_bytes(engine.synthesize(LINE)[0])
    audio, SR = sf.read(fp, dtype="float32")
    ann, _ = narr.create(f"Number {i}.", voice="af_heart", speed=1.0, lang="en-us")
    reel += [ann.astype(np.float32), np.zeros(int(SR*0.28), dtype=np.float32),
             audio, np.zeros(int(SR*0.5), dtype=np.float32)]
    print(f"  {i}: pitch {pitch:+.1f}")

sf.write("samples/pitch-audition.wav", np.concatenate(reel), SR)
print("\n-> samples/pitch-audition.wav")
PY
