"""Split an LLM reply into speech and animation cues.

  display - text for the speech bubble
  speech  - cleaned text for TTS (no markdown, emoji or *actions*)
  cues    - the *actions*, mapped to animations, with their position in speech
"""
import re


CUE_PATTERNS = [
    # First, so "smiles and waves" still waves.
    ("wave",      r"\bwav(e|es|ing)\b"),
    ("eyeroll",   r"\broll(s|ing)?\s+(her\s+|his\s+|their\s+)?eyes\b|\beyeroll\b"),
    ("shake",     r"\bshak(e|es|ing)\s+(her\s+|his\s+|their\s+)?head\b|\bshakes\b"),
    ("nod",       r"\bnod(s|ding)?\b"),
    ("tilt",      r"\btilt(s|ing)?\b|\bcocks?\s+(her|his|their)?\s*head\b"),
    ("shrug",     r"\bshrug(s|ging)?\b"),
    ("lean",      r"\blean(s|ing)?\b"),
    ("wink",      r"\bwink(s|ing)?\b"),
    ("laugh",     r"\blaugh(s|ing)?\b|\bgiggl(e|es|ing)\b|\bchuckl(e|es|ing)\b|\bsnicker(s|ing)?\b|\bsnort(s|ing)?\b"),
    ("blush",     r"\bblush(es|ing)?\b|\bflustered\b|\bembarrassed\b"),
    ("sigh",      r"\bsigh(s|ing)?\b|\bgroan(s|ing)?\b|\bhuff(s|ing)?\b|\bexhal(e|es|ing)\b"),
    ("pout",      r"\bpout(s|ing)?\b|\bfrown(s|ing)?\b|\bscowl(s|ing)?\b|\bglar(e|es|ing)\b|\bglow(er|ers|ering)\b"),
    ("surprised", r"\bgasp(s|ing)?\b|\bblink(s|ing)?\s+in\s+surprise\b|\bstartled\b|\bjolt(s|ing)?\b"),
    ("smile",     r"\bsmil(e|es|ing)\b|\bgrin(s|ning)?\b|\bsmirk(s|ing)?\b|\bbeam(s|ing)?\b"),
    ("sad",       r"\bfrowns?\s+sadly\b|\blooks?\s+(down|away)\b|\bwilts?\b|\bdroop(s|ing)?\b"),
    ("think",     r"\bthink(s|ing)?\b|\bponder(s|ing)?\b|\bconsider(s|ing)?\b|\bhums?\b"),
    ("brow",      r"\b(rais|arch|quirk|lift)(e[sd]|ing)?\s+(an?\s+|her\s+|his\s+|their\s+)?(eye)?brow"),
    ("stare",     r"\bstar(e|es|ing)\b|\bglanc(e|es|ing)\b|\bpeer(s|ing)?\b|\bsquint(s|ing)?\b"),
    ("yawn",      r"\byawn(s|ing)?\b|\bstretch(es|ing)?\b"),
    ("think",     r"\bpaus(e|es|ing)\b|\btrails?\s+off\b"),
    ("emote",     r"\bsnap(s|ping)?\b|\bgestur(e|es|ing)\b"),
]

_COMPILED = [(name, re.compile(pat, re.I)) for name, pat in CUE_PATTERNS]

_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U0000FE00-\U0000FE0F"
    "\U00002B00-\U00002BFF"
    "]+",
    flags=re.UNICODE,
)

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)
_HEADING = re.compile(r"^\s*#{1,6}\s*", re.M)
_QUOTE = re.compile(r"^\s*>\s*", re.M)
_WS = re.compile(r"\s+")

_SPAN = re.compile(
    r"\*\*(?P<bold>[^\n]+?)\*\*"
    r"|\*(?P<italic>[^*\n]+?)\*"
    r"|\((?P<paren>[^)\n]{1,80})\)"
    r"|\[(?P<bracket>[^\]\n]{1,80})\]"
    r"|\*(?P<unclosed>[^\n]+)",
    re.M,
)

_RESIDUAL = str.maketrans({c: " " for c in "*_#`~<>|"})


def classify(action):
    """Map an action phrase to an animation name."""
    for name, pattern in _COMPILED:
        if pattern.search(action):
            return name
    return "emote"


_NOT_ACTION = {
    "yes", "no", "this", "that", "these", "those", "his", "hers", "theirs",
    "its", "us", "was", "is", "as", "less", "plus", "always", "perhaps",
    "nothing", "everything", "anything", "something", "everyone", "anyone",
    "someone", "please", "really", "actually", "very", "just", "never",
    "ever", "all", "none", "obviously", "literally", "definitely",
}

_VERBISH = re.compile(r"^[a-z]+(?:s|es|ing|ed)$", re.I)


def _is_action(inner, kind):
    """Is this *span* an action ("snaps", "walks off") or emphasis ("so annoying")?"""
    if classify(inner) != "emote":
        return True
    if kind == "bracket":
        return True
    if kind == "paren":
        return False

    words = inner.split()
    if not words:
        return False
    first = words[0].strip(".,!?;:'\"").lower()
    if first in _NOT_ACTION:
        return False
    return bool(_VERBISH.match(first))


def split_reply(text):
    """Return {display, speech, cues} for an LLM reply."""
    display = (text or "").strip()

    if not display:
        return {"display": "", "speech": "", "cues": []}

    body = _FENCE.sub(" ", display)
    body = _INLINE_CODE.sub(r"\1", body)
    body = _LINK.sub(r"\1", body)
    body = _BULLET.sub("", body)
    body = _HEADING.sub("", body)
    body = _QUOTE.sub("", body)

    speech_parts = []
    cues = []
    cursor = 0
    length = 0

    for match in _SPAN.finditer(body):
        kind = match.lastgroup
        inner = _WS.sub(" ", match.group(kind) or "").strip()
        if not inner:
            continue

        before = body[cursor:match.start()]
        speech_parts.append(before)
        length += len(before)
        cursor = match.end()

        if _is_action(inner, kind):
            cues.append({
                "action": inner,
                "animation": classify(inner),
                "at": length,
            })
        else:
            speech_parts.append(inner)
            length += len(inner)

    speech_parts.append(body[cursor:])
    speech = "".join(speech_parts)

    speech = _EMOJI.sub(" ", speech)
    speech = speech.translate(_RESIDUAL)
    speech = _WS.sub(" ", speech).strip()

    speech = re.sub(r"\s+([,.!?;:])", r"\1", speech)
    speech = re.sub(r"([,.!?;:])\1+", r"\1", speech)
    speech = re.sub(r"^[\s,.;:]+", "", speech)

    total = max(len(speech), 1)
    for cue in cues:
        cue["fraction"] = min(1.0, max(0.0, cue["at"] / total))

    if speech:
        shown = speech
    elif cues:
        shown = " ".join(c["action"] for c in cues).strip()
    else:
        shown = _WS.sub(" ", display.translate(_RESIDUAL)).strip()

    return {"display": shown, "speech": speech, "cues": cues}


FIRST_MIN_CHARS = 12
LATER_MIN_CHARS = 70
MAX_CHARS = 320

_SENTENCE_END = re.compile(r"[.!?…]['\")\]]*(\s|$)|[\n]+")
_SOFT_BREAK = re.compile(r"[,;:—–]\s|\s")

_PAIRS = {"(": ")", "[": "]"}


def _open_spans(text):
    if text.count("```") % 2:
        return True
    if text.count("*") % 2:
        return True
    for opener, closer in _PAIRS.items():
        if text.count(opener) > text.count(closer):
            return True
    return False


def _decimal_point(text, index):
    if text[index] != ".":
        return False
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    return before.isdigit() and after.isdigit()


class SentenceSplitter:
    """Buffers streamed text and returns it a sentence at a time.

    `feed` returns any complete segments, `flush` returns what's left.
    """

    def __init__(self, first_min=FIRST_MIN_CHARS, later_min=LATER_MIN_CHARS):
        self._buf = ""
        self._first_min = first_min
        self._later_min = later_min
        self._emitted = 0

    @property
    def minimum(self):
        return self._first_min if self._emitted == 0 else self._later_min

    def feed(self, delta):
        self._buf += delta or ""
        out = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            segment, self._buf = self._buf[:cut].strip(), self._buf[cut:].lstrip()
            if segment:
                out.append(segment)
                self._emitted += 1
        return out

    def _find_cut(self):
        buf = self._buf
        minimum = self.minimum

        for match in _SENTENCE_END.finditer(buf):
            end = match.end()
            if end < minimum:
                continue
            if end >= len(buf):
                continue
            if _decimal_point(buf, match.start()):
                continue
            if _open_spans(buf[:end]):
                continue
            return end

        if len(buf) >= MAX_CHARS:
            last = None
            for match in _SOFT_BREAK.finditer(buf):
                if match.end() < minimum:
                    continue
                if match.end() > MAX_CHARS:
                    break
                if _open_spans(buf[:match.end()]):
                    continue
                last = match.end()
            if last:
                return last
        return None

    def flush(self):
        segment, self._buf = self._buf.strip(), ""
        if segment:
            self._emitted += 1
        return segment or None
