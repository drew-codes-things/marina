"""Asking them before she does something risky on their Mac.

The reply loop opens a request, the app shows it with Allow and Deny, and
POST /confirm answers it. No answer within the time limit counts as no, as
does interrupting her.
"""
import threading
import uuid

TIMEOUT = 120.0

_lock = threading.Lock()
_pending = {}


class Ask:
    def __init__(self, text):
        self.id = uuid.uuid4().hex[:10]
        self.text = text
        self.ok = False
        self._answered = threading.Event()


def request(text):
    ask = Ask(text)
    with _lock:
        _pending[ask.id] = ask
    return ask


def answer(ask_id, ok):
    with _lock:
        ask = _pending.pop(ask_id, None)
    if ask is None:
        return False
    ask.ok = bool(ok)
    ask._answered.set()
    return True


def wait(ask, timeout=TIMEOUT):
    """True only if they said yes in time."""
    answered = ask._answered.wait(timeout)
    with _lock:
        _pending.pop(ask.id, None)
    return answered and ask.ok


def cancel_all():
    with _lock:
        ids = list(_pending)
    for ask_id in ids:
        answer(ask_id, False)
