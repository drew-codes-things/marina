"""The rejected cases are all things the model really tried to store."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process.memory.store import _is_durable_fact

REJECT = [
    "Say hello to elekryo, one of my TikTok followers.",
    "Marina says hi and thats one of her TikTok followers!",
    "(Marina is currently talking to elektrikryo, a person on my tiktok follower list)",
    "I couldn't be bothered to offer suggestions and was annoyed when you asked.",
    "remind me to call mum",
    "we are currently talking about anime",
    "tell them about the new episode",
    "I love this song",
    "what is on the clipboard?",
    "no",
    "The user wants to open YouTube in Chrome",
    "The user wants YouTube opened in Chrome and a text document created for cake instructions.",
    "The user asked for the time in Tokyo",
]
ACCEPT = [
    "The user's name is Jacob",
    "the user is building a friend not an assistant",
    "Jacob is learning Rust",
    "Jacob works as a software engineer",
    "The user has a cat named Milo",
    "Jacob lives in the UK",
    "The user wants to be called Sam",
    "The user wants to learn Rust",
]


def run():
    fails = []
    for t in REJECT:
        if _is_durable_fact(t):
            fails.append(f"should REJECT but kept: {t!r}")
    for t in ACCEPT:
        if not _is_durable_fact(t):
            fails.append(f"should ACCEPT but dropped: {t!r}")
    print(f"  {len(REJECT)} reject + {len(ACCEPT)} accept cases, {len(fails)} failures")
    for f in fails:
        print("   FAIL", f)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(run())
