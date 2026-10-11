"""HTTP bridge between the desktop app and the ASR / LLM / TTS pipeline.

    python server/marina_server.py
"""
import base64
import json
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "server"))

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from openai import APIConnectionError
from pydantic import BaseModel

from process.asr_func.asr_push_to_talk import build_model, transcribe_file
from process.asr_func.recorder import Recorder
from process.asr_func.vad import VAD
from process.config import CONFIG_PATH, load_config
from process.llm_funcs.llm_scr import (
    active_endpoint,
    active_model,
    describe_endpoint,
    llm_response,
    llm_stream,
    note_exchange,
    reset_history,
    save_turn,
)
from process.tts_func.engine import (
    TTSError,
    describe as describe_tts,
    kokoro_voices,
    synthesize,
)
from process.tts_func.engine import warmup as warmup_tts
from process.text_func.speech import SentenceSplitter, split_reply
from process import backend
from process import config as cfg
from process import idle
from process.tools import registry as tools
from process.memory import store as memory
from process.vision.look import (
    ENABLED as VISION_ENABLED,
    MODEL as VISION_MODEL,
    VisionError,
    describe as describe_screen,
)

config = load_config()

app = FastAPI(title="Marina bridge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_whisper = None
recorder = Recorder()


def whisper():
    """Load Whisper on first use."""
    global _whisper
    if _whisper is None:
        print("Loading Faster-Whisper...", flush=True)
        _whisper = build_model()
        print("Faster-Whisper ready.", flush=True)
    return _whisper


class ChatIn(BaseModel):
    text: str
    speak: bool = True


def llm_error_message(e):
    msg = getattr(getattr(e, "response", None), "text", "") or str(e)
    if "invalid_api_key" in msg or "Incorrect API key" in msg:
        return f"That API key was rejected. Set llm.api_key in {CONFIG_PATH}."
    if "rate_limit" in msg or "429" in msg:
        return "The endpoint rate-limited the request. Wait a moment and try again."
    if isinstance(e, APIConnectionError) or "Connection" in type(e).__name__:
        if backend.mode() == "auto" and len(backend.BACKENDS) > 1:
            tried = ", ".join(f"{b['label']} ({b['base_url'] or 'OpenAI'})"
                              for b in backend.BACKENDS)
            return f"None of Marina's brains answered. Tried {tried}."
        return (
            f"No language model answered at {describe_endpoint()}. Marina needs "
            "one to talk. Install Ollama and run 'ollama pull llama3.2:3b', or "
            f"point llm.base_url at any OpenAI-compatible server in {CONFIG_PATH}."
        )
    return f"LLM call failed: {type(e).__name__}: {str(e)[:200]}"


def _respond(user_text, speak, transcript=None):
    out = {"transcript": transcript if transcript is not None else user_text,
           "reply": "",
           "speech": "",
           "cues": [],
           "audio": None,
           "error": None}

    try:
        reply = llm_response(user_text)
    except Exception as e:
        out["error"] = llm_error_message(e)
        print(f"[llm] {out['error']}", flush=True)
        return out

    parts = split_reply(reply)
    out["reply"] = parts["display"]
    out["speech"] = parts["speech"]
    out["backend"] = active_endpoint()
    out["model"] = active_model()
    out["cues"] = parts["cues"]

    if not speak:
        return out

    try:
        if out["speech"]:
            wav, visemes = synthesize(out["speech"])
            out["audio"] = base64.b64encode(wav).decode("ascii")
            out["visemes"] = visemes
    except TTSError as e:
        out["error"] = str(e)
        print(f"[tts] {e}", flush=True)

    return out


@app.get("/health")
def health():
    return {
        "ok": True,
        "model": active_model(),
        "llm_endpoint": describe_endpoint(),
        "llm_using": active_endpoint(),
        "llm_mode": backend.mode(),
        "llm_labels": backend.labels(),
        "preset": cfg.preset(),
        "tts": describe_tts(),
        "whisper_loaded": _whisper is not None,
        "recording": recorder.is_recording,
        "memories": len(memory.all_facts()),
        "vision": VISION_MODEL if VISION_ENABLED else None,
        "barge_in": _barge_enabled(),
        "idle": idle.status(),
    }


@app.post("/chat")
def chat(body: ChatIn):
    text = body.text.strip()
    if not text:
        return {"transcript": "", "reply": "", "speech": "", "cues": [],
                "audio": None, "error": "Empty message."}
    print(f"[you] {text}", flush=True)
    idle.note_interaction()
    result = _respond(text, body.speak)
    print(f"[marina] {result['reply']}", flush=True)
    return result


_stream = {"id": 0, "stop_at": None}
_stream_lock = threading.Lock()


def _ndjson(event):
    return json.dumps(event, ensure_ascii=False) + "\n"


def _speak_segment(segment, index, speak):
    parts = split_reply(segment)
    event = {
        "type": "chunk",
        "index": index,
        "reply": parts["display"],
        "speech": parts["speech"],
        "cues": parts["cues"],
        "audio": None,
        "visemes": [],
        "backend": active_endpoint(),
        "model": active_model(),
    }
    if speak and parts["speech"]:
        try:
            wav, visemes = synthesize(parts["speech"])
            event["audio"] = base64.b64encode(wav).decode("ascii")
            event["visemes"] = visemes
        except TTSError as e:
            event["error"] = str(e)
            print(f"[tts] {e}", flush=True)
    return event


def _stream_reply(user_text, speak=True, transcript=None, extra_system=None,
                  record=True):
    """Stream the reply one sentence at a time, synthesizing as it goes."""
    with _stream_lock:
        _stream["id"] += 1
        _stream["stop_at"] = None
        stream_id = _stream["id"]

    def stop_after():
        with _stream_lock:
            if _stream["id"] != stream_id:
                return index
            return _stream["stop_at"]

    yield _ndjson({"type": "start", "id": stream_id,
                   "transcript": transcript if transcript is not None else user_text,
                   "unprompted": user_text is None})

    splitter = SentenceSplitter()
    spoken = []
    index = 0
    interrupted = False
    generator = llm_stream(user_text, extra_system=extra_system)

    def emit(segment):
        nonlocal index
        event = _speak_segment(segment, index, speak)
        if not event["speech"] and not event["cues"]:
            return None
        spoken.append(segment)
        index += 1
        return _ndjson(event)

    try:
        try:
            for delta in generator:
                limit = stop_after()
                if limit is not None:
                    interrupted = True
                    break
                for segment in splitter.feed(delta):
                    event = emit(segment)
                    if event:
                        yield event
            if not interrupted:
                tail = splitter.flush()
                if tail:
                    event = emit(tail)
                    if event:
                        yield event
        finally:
            generator.close()
    except Exception as e:
        message = llm_error_message(e)
        print(f"[llm] {message}", flush=True)
        if record:
            save_turn(user_text, " ".join(spoken))
        yield _ndjson({"type": "error", "message": message})
        return

    limit = stop_after()
    if limit is not None:
        interrupted = True
        heard = " ".join(spoken[:max(0, limit)]).rstrip()
        heard = (heard + " \u2014") if heard else ""
    else:
        heard = " ".join(spoken)

    if record:
        save_turn(user_text, heard)
    idle.note_interaction()

    print(f"[marina] {heard}", flush=True)
    yield _ndjson({"type": "interrupted" if interrupted else "done",
                   "chunks": index, "reply": heard})


@app.post("/chat/stream")
def chat_stream(body: ChatIn):
    text = body.text.strip()
    if not text:
        return StreamingResponse(
            iter([_ndjson({"type": "error", "message": "Empty message."})]),
            media_type="application/x-ndjson")
    print(f"[you] {text}", flush=True)
    return StreamingResponse(_stream_reply(text, body.speak),
                             media_type="application/x-ndjson")


class InterruptIn(BaseModel):
    chunks: int = 0


@app.post("/interrupt")
def interrupt(body: InterruptIn):
    """Stop the current reply. Only the first `chunks` sentences get saved."""
    with _stream_lock:
        _stream["stop_at"] = max(0, body.chunks)
        stream_id = _stream["id"]
    return {"ok": True, "id": stream_id, "heard": max(0, body.chunks)}


@app.post("/voice")
def voice(audio: UploadFile = File(...), speak: bool = True):
    suffix = Path(audio.filename or "clip.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio.file.read())
        tmp_path = Path(tmp.name)

    try:
        transcript = transcribe_file(whisper(), tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    print(f"[you] {transcript}", flush=True)

    if not transcript:
        return {"transcript": "", "reply": "", "speech": "", "cues": [],
                "audio": None, "error": "Didn't catch that."}

    result = _respond(transcript, speak, transcript=transcript)
    print(f"[marina] {result['reply']}", flush=True)
    return result


@app.post("/listen/start")
def listen_start():
    threading.Thread(target=whisper, daemon=True).start()
    if recorder.is_monitoring:
        recorder.cancel()
    started = recorder.start()
    return {"ok": True, "started": started, "already_recording": not started}


@app.post("/listen/stop")
def listen_stop(speak: bool = True):
    path = recorder.stop()
    if path is None:
        return {"transcript": "", "reply": "", "speech": "", "cues": [],
                "audio": None, "error": "Nothing recorded."}

    try:
        transcript = transcribe_file(whisper(), path)
    finally:
        path.unlink(missing_ok=True)

    print(f"[you] {transcript}", flush=True)

    if not transcript:
        return {"transcript": "", "reply": "", "speech": "", "cues": [],
                "audio": None, "error": "Didn't catch that."}

    result = _respond(transcript, speak, transcript=transcript)
    print(f"[marina] {result['reply']}", flush=True)
    return result


def _stream_error(message):
    return StreamingResponse(
        iter([_ndjson({"type": "error", "message": message})]),
        media_type="application/x-ndjson")


def _stream_transcript(path):
    try:
        transcript = transcribe_file(whisper(), path)
    finally:
        path.unlink(missing_ok=True)

    if not transcript:
        return _stream_error("Didn't catch that.")

    print(f"[you] {transcript}", flush=True)
    idle.note_interaction()
    return StreamingResponse(
        _stream_reply(transcript, True, transcript=transcript),
        media_type="application/x-ndjson")


@app.post("/listen/stop/stream")
def listen_stop_stream():
    path = recorder.stop()
    if path is None:
        return _stream_error("Nothing recorded.")
    return _stream_transcript(path)


@app.post("/voice/stream")
def voice_stream(audio: UploadFile = File(...)):
    """Like /listen/stop/stream, for a clip recorded somewhere else, such as
    a phone's browser."""
    suffix = Path(audio.filename or "clip.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio.file.read())
    return _stream_transcript(Path(tmp.name))


@app.post("/listen/cancel")
def listen_cancel():
    recorder.cancel()
    return {"ok": True}


_barge = config.get("barge_in") or {}


def _barge_enabled():
    """Read on every call: presentation mode turns it off, since a busy room
    would keep cutting her off."""
    return bool(cfg.setting("barge_in", "enabled", True))


def _make_vad():
    from process.asr_func.recorder import SAMPLERATE

    return VAD(
        SAMPLERATE,
        margin_db=float(_barge.get("margin_db", 12.0)),
        speech_ms=int(_barge.get("speech_ms", 220)),
        silence_ms=int(_barge.get("silence_ms", 800)),
    )


def _barge_stream(timeout):
    """Listen while she talks. If the user cuts in, transcribe it and stream
    the next reply on the same connection."""
    started = recorder.start(_make_vad())
    if not started:
        yield _ndjson({"type": "error", "message": "Microphone already in use."})
        return

    yield _ndjson({"type": "armed"})

    deadline = time.time() + timeout
    heard = False
    try:
        while True:
            if time.time() > deadline:
                return
            if not recorder.is_monitoring:
                return
            try:
                event = recorder.events.get(timeout=0.25)
            except queue.Empty:
                yield _ndjson({"type": "waiting"})
                continue

            if event == "start" and not heard:
                heard = True
                yield _ndjson({"type": "speech"})
                deadline = time.time() + 60
            elif event == "end" and heard:
                break

        path = recorder.stop(from_onset=True)
        if path is None:
            yield _ndjson({"type": "cancelled"})
            return

        try:
            transcript = transcribe_file(whisper(), path)
        finally:
            path.unlink(missing_ok=True)

        if not transcript:
            yield _ndjson({"type": "cancelled"})
            return

        print(f"[you, cutting in] {transcript}", flush=True)
        yield _ndjson({"type": "transcript", "text": transcript})
        yield from _stream_reply(transcript, True, transcript=transcript)
    finally:
        if recorder.is_monitoring:
            recorder.cancel()


@app.get("/barge/listen")
def barge_listen(timeout: float = 45.0):
    if not _barge_enabled():
        return StreamingResponse(
            iter([_ndjson({"type": "disabled"})]),
            media_type="application/x-ndjson")
    threading.Thread(target=whisper, daemon=True).start()
    return StreamingResponse(_barge_stream(timeout),
                             media_type="application/x-ndjson")


class SeeIn(BaseModel):
    image: str
    mime: str = "image/jpeg"
    question: str = ""
    speak: bool = True


@app.post("/see")
def see(body: SeeIn):
    out = {"transcript": body.question or "(looked at the screen)",
           "reply": "", "speech": "", "cues": [], "audio": None, "error": None}

    try:
        raw = base64.b64decode(body.image)
    except Exception:
        out["error"] = "Screenshot was not valid base64."
        return out

    print(f"[see] {len(raw)/1024:.0f} KB screenshot", flush=True)

    try:
        answer = describe_screen(raw, body.question, body.mime)
    except VisionError as e:
        out["error"] = str(e)
        print(f"[see] {e}", flush=True)
        return out

    parts = split_reply(answer)
    out["reply"] = parts["display"]
    out["speech"] = parts["speech"]
    out["backend"] = active_endpoint()
    out["model"] = active_model()
    out["cues"] = parts["cues"]
    print(f"[marina] {parts['speech']}", flush=True)

    note_exchange(body.question or "What is on my screen?",
                  f"(looked at your screen) {answer}")

    if body.speak and out["speech"]:
        try:
            wav, visemes = synthesize(out["speech"])
            out["audio"] = base64.b64encode(wav).decode("ascii")
            out["visemes"] = visemes
        except TTSError as e:
            out["error"] = str(e)

    return out


def _list_models(base_url, api_key):
    if not base_url:
        return []
    try:
        from openai import OpenAI
        c = OpenAI(api_key=api_key or "not-needed", base_url=base_url,
                   timeout=4.0, max_retries=0)
        return sorted(m.id for m in c.models.list().data)
    except Exception:
        return []


@app.get("/models")
def models_list():
    from concurrent.futures import ThreadPoolExecutor
    # Ask every backend at once, so one box that is off costs 4 seconds total
    # rather than 4 seconds each.
    with ThreadPoolExecutor(len(backend.BACKENDS)) as pool:
        found = list(pool.map(
            lambda b: _list_models(b["base_url"], b["api_key"]), backend.BACKENDS))
    out = {b["name"]: m for b, m in zip(backend.BACKENDS, found)}
    out.update({
        "backends": [{"name": b["name"], "label": b["label"], "models": m}
                     for b, m in zip(backend.BACKENDS, found)],
        "selected": backend.models(),
        "mode": backend.mode(),
        "current": backend.current(),
        **_preset_info(),
    })
    return out


class ModelIn(BaseModel):
    backend: str
    model: str


@app.post("/model")
def model_set(body: ModelIn):
    try:
        backend.set_model(body.backend, body.model)
        backend.set_mode(body.backend)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    print(f"[llm] using {body.model} on {body.backend}", flush=True)
    return {"ok": True, "backend": body.backend, "model": body.model,
            "mode": backend.mode()}


class BackendIn(BaseModel):
    mode: str


@app.get("/backend")
def backend_get():
    return {
        "mode": backend.mode(),
        "current": backend.current(),
        "choices": list(backend.VALID),
    }


@app.post("/backend")
def backend_set(body: BackendIn):
    try:
        backend.set_mode(body.mode)
    except ValueError as e:
        return {"ok": False, "error": str(e), "mode": backend.mode()}
    print(f"[llm] backend switched to {backend.mode()}", flush=True)
    return {"ok": True, "mode": backend.mode(), "current": backend.current()}


def _preset_info():
    return {
        "preset": cfg.preset(),
        "presets": [{"name": n, "label": cfg.preset_label(n)} for n in cfg.presets()],
    }


@app.get("/preset")
def preset_get():
    return _preset_info()


class PresetIn(BaseModel):
    name: str


@app.post("/preset")
def preset_set(body: PresetIn):
    try:
        cfg.set_preset(body.name)
    except ValueError as e:
        return {"ok": False, "error": str(e), **_preset_info()}
    print(f"[config] personality switched to {cfg.preset_label(cfg.preset())}", flush=True)
    return {"ok": True, **_preset_info(), "idle": idle.status(),
            "barge_in": _barge_enabled()}


class MemoryIn(BaseModel):
    text: str


@app.get("/memory")
def memory_list():
    facts = sorted(memory.all_facts(), key=lambda f: f.get("updated", ""), reverse=True)
    return {"count": len(facts), "facts": facts}


@app.post("/memory")
def memory_add(body: MemoryIn):
    fact = memory.add(body.text, source="manual")
    return {"ok": True, "added": fact, "duplicate": fact is None}


@app.delete("/memory/{fact_id}")
def memory_delete(fact_id: str):
    return {"ok": memory.remove(fact_id)}


@app.delete("/memory")
def memory_clear():
    memory.clear()
    return {"ok": True}


@app.post("/reset")
def reset():
    reset_history()
    return {"ok": True}


class MuteIn(BaseModel):
    muted: bool


@app.get("/idle/status")
def idle_status():
    return idle.status()


@app.post("/idle/mute")
def idle_mute(body: MuteIn):
    value = idle.set_muted(body.muted)
    print(f"[idle] openers {'muted' if value else 'unmuted'}", flush=True)
    return {"ok": True, "muted": value}


def _idle_stream(timeout):
    """Long poll: returns when she has something unprompted to say."""
    waited = 0.0
    while waited < timeout:
        announcement = tools.pending_announcement()
        if announcement:
            idle.note_interaction()
            print("[idle] a timer went off", flush=True)
            yield from _stream_reply(None, True, transcript="",
                                     extra_system="\n\n" + announcement)
            return

        if idle.due():
            idle.note_spoken()
            print("[idle] saying something unprompted", flush=True)
            yield from _stream_reply(None, True, transcript="",
                                     extra_system=idle.OPENER_INSTRUCTION)
            return
        time.sleep(2.0)
        waited += 2.0
        yield _ndjson({"type": "waiting"})
    yield _ndjson({"type": "idle"})


@app.get("/idle/listen")
def idle_listen(timeout: float = 120.0):
    if not idle.enabled():
        return StreamingResponse(iter([_ndjson({"type": "disabled"})]),
                                 media_type="application/x-ndjson")
    return StreamingResponse(_idle_stream(timeout),
                             media_type="application/x-ndjson")


@app.get("/voices")
def voices():
    return {"voices": kokoro_voices()}


@app.post("/warmup")
def warmup():
    whisper()
    warmup_tts()
    return {"ok": True}


_web = config.get("web") or {}
if _web.get("enabled"):
    # Last, because it mounts the avatar UI at / and would shadow later routes.
    from process import web
    web.mount(app, _web)


if __name__ == "__main__":
    import uvicorn

    bridge = config.get("bridge", {})
    uvicorn.run(
        app,
        host=bridge.get("host", "127.0.0.1"),
        port=int(bridge.get("port", 8765)),
        log_level="warning",
    )
