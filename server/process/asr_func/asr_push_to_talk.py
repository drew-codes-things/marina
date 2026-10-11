"""Speech-to-text with Faster-Whisper."""
import os

from faster_whisper import WhisperModel

from process.config import DATA_ROOT, load_config, resolve


def _bundled(name):
    """Marina.app ships the model, so a fresh Mac never has to download it."""
    path = resolve(f"models/whisper/{name}")
    return str(path) if (path / "model.bin").exists() else None


def build_model():
    cfg = load_config().get("asr", {})
    name = cfg.get("model", "base.en")
    kwargs = {
        "device": cfg.get("device", "cpu"),
        "compute_type": cfg.get("compute_type", "int8"),
    }

    bundled = _bundled(name)
    if bundled:
        return WhisperModel(bundled, **kwargs)

    # Anything not bundled comes from Hugging Face, cached somewhere writable.
    os.environ.setdefault("HF_HOME", str(DATA_ROOT / "cache" / "huggingface"))

    if cfg.get("offline", True):
        try:
            return WhisperModel(name, local_files_only=True, **kwargs)
        except Exception:
            print(f"[asr] {name} isn't cached yet; downloading it once.",
                  flush=True)

    return WhisperModel(name, **kwargs)


def transcribe_file(model, path):
    segments, _ = model.transcribe(str(path), vad_filter=True)
    return " ".join(segment.text for segment in segments).strip()


def transcribe_audio(model, audio, prompt=None):
    """Same as transcribe_file, for 16 kHz mono float32 already in memory.
    `prompt` nudges Whisper towards words it would otherwise mishear, like
    her name."""
    if audio is None or len(audio) < 16000 * 0.25:
        return ""
    segments, _ = model.transcribe(audio, vad_filter=True, initial_prompt=prompt)
    return " ".join(segment.text for segment in segments).strip()


def record_and_transcribe(model, output_file="recording.wav", samplerate=44100):
    """Terminal push-to-talk. Records until you press ENTER again."""
    import queue

    import sounddevice as sd
    import soundfile as sf

    if os.path.exists(output_file):
        os.remove(output_file)

    print("Press ENTER to start recording...")
    input()
    print("Recording... press ENTER to stop")

    frames = queue.Queue()

    def callback(indata, _frames, _time, status):
        if status:
            print(status)
        frames.put(indata.copy())

    with sd.InputStream(samplerate=samplerate, channels=1, callback=callback):
        input()

    print("Saving audio...")
    chunks = []
    while not frames.empty():
        chunks.append(frames.get())

    if not chunks:
        print("Nothing recorded.")
        return ""

    import numpy as np

    sf.write(output_file, np.concatenate(chunks), samplerate)

    print("Transcribing...")
    transcription = transcribe_file(model, output_file)
    print(f"Transcription: {transcription}")
    return transcription


if __name__ == "__main__":
    print(f"Got: '{record_and_transcribe(build_model())}'")
