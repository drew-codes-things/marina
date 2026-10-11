"""Terminal-only client (no avatar). Run from the repo root:

    python server/main_chat.py
"""
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "server"))

from process.asr_func.asr_push_to_talk import build_model, record_and_transcribe
from process.llm_funcs.llm_scr import llm_response
from process.tts_func.sovits_ping import play_audio, sovits_gen

AUDIO_DIR = REPO_ROOT / "audio"


def main():
    print("\n========= Starting Chat... =========\n")
    whisper_model = build_model()

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    conversation_recording = AUDIO_DIR / "conversation.wav"

    while True:
        user_spoken_text = record_and_transcribe(whisper_model, conversation_recording)
        if not user_spoken_text:
            continue

        llm_output = llm_response(user_spoken_text)
        print(f"Marina: {llm_output}")

        output_wav_path = AUDIO_DIR / f"output_{uuid.uuid4().hex}.wav"
        if sovits_gen(llm_output, output_wav_path):
            play_audio(output_wav_path)

        for fp in AUDIO_DIR.glob("output_*.wav"):
            fp.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")

