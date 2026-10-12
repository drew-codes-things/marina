"""An OpenAI-style API, so chat front ends like Open WebUI can talk to her.

Two models:

  marina        her, with her personality, the memory she shares with every
                other way of talking to her, and her tools. Each chat in the
                front end is its own thread, separate from the personal
                conversation, but what she learns about you in one is kept.
  marina-plain  the same model with none of that, for the front end's own
                housekeeping, like naming chats.

Turned on by `api.key`. From the network the bridge then answers /v1/ to
requests that carry it (see the guard in marina_server).
"""
import json
import time
import uuid

from fastapi import Body
from fastapi.responses import JSONResponse, StreamingResponse

from process.config import load_config
from process.llm_funcs import llm_scr
from process.memory import extract as memory_extract

KEY = str((load_config().get("api") or {}).get("key") or "").strip()

WINDOW = 40

CHAT_WINDOW = """

Right now you are in a chat window on their screen, not speaking and not
texting on a phone. It renders markdown, so here code blocks, lists and
headings are fine when they help, and longer answers are fine when the
task needs them. You are still you: same voice, same opinions. No *actions*
in asterisks."""


def enabled():
    return bool(KEY)


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _chunk(cid, model, delta, finish=None):
    return "data: " + json.dumps({
        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }, ensure_ascii=False) + "\n\n"


def _completion(cid, model, text):
    return {
        "id": cid, "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
    }


def _her(body):
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    their_system = "\n\n".join(_text(m.get("content")) for m in messages
                               if m.get("role") == "system").strip()
    chat = [{"role": m["role"], "content": m.get("content") or ""}
            for m in messages if m.get("role") in ("user", "assistant")]
    if not chat or chat[-1]["role"] != "user":
        return None, None, None
    *before, last = chat
    extra = CHAT_WINDOW + (f"\n\nThey added this for the chat:\n{their_system}"
                           if their_system else "")
    stream = llm_scr.llm_stream(last["content"], extra_system=extra,
                                history=before[-WINDOW:])
    return stream, _text(last["content"]), before


def _remember(user_text, reply):
    if llm_scr._remember() and user_text:
        memory_extract.remember_async(*llm_scr._extract_backend(), user_text, reply)


def mount(app):
    @app.get("/v1/models")
    def models():
        now = int(time.time())
        return {"object": "list", "data": [
            {"id": "marina", "object": "model", "created": now, "owned_by": "marina"},
            {"id": "marina-plain", "object": "model", "created": now, "owned_by": "marina"},
        ]}

    # Not async: her reply loop blocks, so this runs on the thread pool.
    @app.post("/v1/chat/completions")
    def chat(body: dict = Body(...)):
        model = body.get("model") or "marina"
        streaming = bool(body.get("stream"))
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        if model == "marina-plain":
            kw = {k: body[k] for k in ("temperature", "max_tokens", "top_p") if k in body}
            if streaming:
                def plain():
                    for ev in llm_scr.chat_completion(body.get("messages") or [],
                                                      stream=True, **kw):
                        yield "data: " + ev.model_dump_json(exclude_none=True) + "\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(plain(), media_type="text/event-stream")
            out = llm_scr.chat_completion(body.get("messages") or [], **kw)
            return JSONResponse(out.model_dump(exclude_none=True))

        stream, user_text, _before = _her(body)
        if stream is None:
            return JSONResponse({"error": {"message": "The last message has to be from the user."}},
                                status_code=400)
        print(f"[chat ui] you: {user_text[:120]}", flush=True)

        if not streaming:
            reply = "".join(stream).strip()
            _remember(user_text, reply)
            return JSONResponse(_completion(cid, "marina", reply))

        def events():
            said = []
            yield _chunk(cid, "marina", {"role": "assistant", "content": ""})
            try:
                for piece in stream:
                    said.append(piece)
                    yield _chunk(cid, "marina", {"content": piece})
            except Exception as e:
                yield _chunk(cid, "marina", {"content": f"\n\n(my brain didn't answer: {type(e).__name__})"})
            yield _chunk(cid, "marina", {}, "stop")
            yield "data: [DONE]\n\n"
            reply = "".join(said).strip()
            print(f"[chat ui] marina: {reply[:120]}", flush=True)
            _remember(user_text, reply)

        return StreamingResponse(events(), media_type="text/event-stream")
