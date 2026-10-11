"""Cuts what the mic hears into phrases, for the wake word and for push to
talk that stops by itself.

Two ways to use an Ear:

  Ear(vad)   phrases: runs for as long as it is open, hands back each
             stretch of speech the VAD finds, and keeps nothing else beyond
             a short pre-roll. For listening out for her name.
  Ear()      one: starts capturing straight away, as when you press the mic
             button, and ends at the first long enough pause after you have
             said something, at `max_seconds`, or when finish() is called.

Events come out of `events` as ("start",) when speech begins and
("phrase", audio) when it ends, audio being 16 kHz mono float32.
"""
import collections
import math
import queue
import threading
import time

from process.asr_func.recorder import (
    PREROLL_SECONDS,
    SAMPLERATE,
    close_input,
    open_input,
)

FRAME = int(SAMPLERATE * 0.03)

# For "one": below this nothing counts as speech, however quiet the room.
SPEECH_DB = -42.0
SPEECH_MS = 250
PAUSE_MS = 900


def _db(frame):
    import numpy as np

    rms = float(np.sqrt(np.mean(np.square(frame, dtype="float64")) + 1e-12))
    return 20.0 * math.log10(rms + 1e-9)


class Ear:
    def __init__(self, vad=None, max_seconds=10.0, pause_ms=PAUSE_MS):
        self.vad = vad
        self.max_seconds = float(max_seconds)
        self.max_samples = int(max_seconds * SAMPLERATE)
        self.pause_frames = max(1, int(pause_ms / 30))
        self.events = queue.Queue()
        self._lock = threading.Lock()
        self._stream = None
        self._pending = None
        self._preroll = collections.deque(maxlen=max(1, int(PREROLL_SECONDS / 0.03)))
        self._phrase = None
        self._finish = False
        self._opened = None
        self._phrase_began = None
        # "one" mode
        self._loud = 0
        self._quiet = 0
        self._heard = False
        self._peak = -120.0

    def open(self):
        import numpy as np

        self._pending = np.zeros(0, dtype="float32")
        if self.vad is None:
            self._phrase = []
        self._opened = time.monotonic()
        self._phrase_began = None
        self._stream = open_input(self._callback)
        return self

    def close(self):
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            close_input(stream)

    def finish(self):
        """Stop now and hand back what has been said so far."""
        self._finish = True

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _callback(self, indata, _frames, _time, status):
        import numpy as np

        self._pending = np.concatenate((self._pending, indata.reshape(-1)))
        while len(self._pending) >= FRAME:
            frame, self._pending = self._pending[:FRAME], self._pending[FRAME:]
            if self.vad is not None:
                self._phrases(frame)
            else:
                self._one(frame)

    def _overdue(self, since):
        """The limit by the clock as well as by samples, since samples go
        missing when the callback can't keep up."""
        return since is not None and time.monotonic() - since >= self.max_seconds

    def _emit(self):
        import numpy as np

        audio = np.concatenate(self._phrase) if self._phrase else np.zeros(0, "float32")
        self._phrase = None
        self.events.put(("phrase", audio))

    def _phrases(self, frame):
        event = self.vad.update(frame)
        if self._phrase is None:
            self._preroll.append(frame)
            if event == "start":
                self._phrase = list(self._preroll)
                self._preroll.clear()
                self._phrase_began = time.monotonic()
                self.events.put(("start",))
            return
        self._phrase.append(frame)
        # A TV or a long ramble mustn't make one endless phrase.
        if (event == "end" or len(self._phrase) * FRAME >= self.max_samples
                or self._overdue(self._phrase_began)):
            self._emit()

    def _one(self, frame):
        if self._phrase is None:
            return
        self._phrase.append(frame)
        db = _db(frame)
        self._peak = max(self._peak, db)
        # Loud means near the loudest you have been, so the pause is judged
        # against your own voice rather than a fixed level.
        loud = db > max(SPEECH_DB, self._peak - 20.0)
        if not self._heard:
            self._loud = self._loud + 1 if loud else max(0, self._loud - 1)
            if self._loud * 30 >= SPEECH_MS:
                self._heard = True
                self.events.put(("start",))
        else:
            self._quiet = 0 if loud else self._quiet + 1
        done = (self._finish
                or (self._heard and self._quiet >= self.pause_frames)
                or len(self._phrase) * FRAME >= self.max_samples
                or self._overdue(self._opened))
        if done:
            if not self._heard and not self._finish:
                self._phrase = None
                self.events.put(("silence",))
            else:
                self._emit()

    @property
    def heard_speech(self):
        return self._heard


# Words people say before a name when they call someone.
_CALLS = r"(?:(?:hey|hi|hello|ok|okay|oi|oy|yo|um|uh|so|right|and)[\s,]+)*"


def wake_match(text, words):
    """If `text` is said to her, return the request in it (possibly empty).
    Otherwise None.

    Her name has to be how the sentence starts, after at most a "hey" or an
    "okay", or how it ends: "Marina, what time is it" or "what time is it,
    Marina". Her name in the middle is someone talking about a marina."""
    import re

    for word in words:
        name = re.escape(word.lower())
        lowered = text.lower()
        m = re.match(rf"\s*{_CALLS}{name}\b[\s,.!?:;-]*", lowered)
        if m:
            return text[m.end():].strip(" ,.!?:;-")
        # At the end she is addressed with a comma: "is it, Marina?". Without
        # one it is "we went to the marina".
        m = re.search(rf",\s*{name}[\s.!?]*$", lowered)
        if m:
            return text[:m.start()].strip(" ,.!?:;-")
    return None
