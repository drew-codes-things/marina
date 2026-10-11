"""Listening should end by itself: on a pause after speech, on silence, at
the time limit, or when asked to. The wake listener should only hand back
speech, and her name should split off from the request after it."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process.asr_func.listen import FRAME, Ear, wake_match
from process.asr_func.recorder import SAMPLERATE
from process.asr_func.vad import VAD

RNG = np.random.default_rng(0)


def noise(seconds, level):
    return (RNG.standard_normal(int(SAMPLERATE * seconds)) * level).astype("float32")


def feed(ear, audio):
    """Push audio through the ear in blocks, as the mic callback would."""
    if ear._pending is None:
        ear._pending = np.zeros(0, dtype="float32")
        if ear.vad is None:
            ear._phrase = []
            ear._opened = None  # these tests run faster than real time
    block = 512
    for i in range(0, len(audio), block):
        ear._callback(audio[i:i + block].reshape(-1, 1), None, None, None)


def drain(ear):
    out = []
    while not ear.events.empty():
        out.append(ear.events.get())
    return out


def test_one_ends_on_a_pause_after_speech():
    ear = Ear(max_seconds=10)
    feed(ear, np.concatenate([noise(0.3, 0.0005), noise(1.2, 0.1), noise(1.5, 0.0005)]))
    events = drain(ear)
    assert [e[0] for e in events] == ["start", "phrase"]
    assert 1.5 < len(events[1][1]) / SAMPLERATE < 2.6


def test_one_gives_up_on_silence_at_the_limit():
    ear = Ear(max_seconds=2)
    feed(ear, noise(2.5, 0.0005))
    assert [e[0] for e in drain(ear)] == ["silence"]


def test_one_stops_at_the_limit_while_talking():
    ear = Ear(max_seconds=3)
    feed(ear, noise(5, 0.1))
    events = drain(ear)
    assert [e[0] for e in events] == ["start", "phrase"]
    assert abs(len(events[1][1]) / SAMPLERATE - 3) < 0.1


def test_finish_hands_back_what_was_said():
    ear = Ear(max_seconds=10)
    feed(ear, noise(0.8, 0.1))
    ear.finish()
    feed(ear, noise(0.1, 0.1))
    assert [e[0] for e in drain(ear)] == ["start", "phrase"]


def test_phrases_only_keeps_speech():
    ear = Ear(VAD(SAMPLERATE, margin_db=10, speech_ms=200, silence_ms=600), max_seconds=8)
    quiet = noise(2.0, 0.0005)
    feed(ear, np.concatenate([quiet, noise(1.0, 0.1), quiet, noise(0.8, 0.1), quiet]))
    events = drain(ear)
    assert [e[0] for e in events] == ["start", "phrase", "start", "phrase"]
    # A phrase is the speech plus a little pre-roll and the pause that ended
    # it, never the seconds of quiet before it.
    for _, audio in (e for e in events if e[0] == "phrase"):
        assert len(audio) / SAMPLERATE < 2.2
    assert len(ear._preroll) * FRAME / SAMPLERATE <= 0.4


def test_wake_match():
    words = ["marina", "marena"]
    assert wake_match("Marina, what time is it?", words) == "what time is it"
    assert wake_match("Hey Marena.", words) == ""
    assert wake_match("Marina?", words) == ""
    assert wake_match("OK, Marina set a timer", words) == "set a timer"
    assert wake_match("What time is it, Marina?", words) == "What time is it"
    assert wake_match("The marina was busy today", words) is None
    assert wake_match("I went to the marina yesterday", words) is None
    assert wake_match("We walked down to the marina.", words) is None
    assert wake_match("Maria said hi", words) is None
    assert wake_match("submarinas", words) is None


def test_one_stops_by_the_clock_when_audio_goes_missing():
    import time

    ear = Ear(max_seconds=2)
    feed(ear, noise(0.1, 0.0005))
    ear._opened = time.monotonic() - 3  # 3 s passed, but little audio arrived
    feed(ear, noise(0.1, 0.0005))
    assert [e[0] for e in drain(ear)] == ["silence"]
