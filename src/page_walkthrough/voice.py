"""Voiceover backends. Each turns one shot's narration into a WAV and reports its length,
plus word timings when the backend provides them (used to sync captions to the speech)."""
import base64
import json
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

GAP_S = 0.35

DEFAULTS = {
    "elevenlabs": {"model": "eleven_multilingual_v2", "voice": "JBFqnCBsd6RMkjVDRZzb"},
    "openai": {"model": "gpt-4o-mini-tts", "voice": "coral",
               "instructions": "Warm, clear teacher walking a student through a page. Natural pace, no drama."},
    "kokoro": {"voice": "af_heart"},
    "say": {"voice": "Samantha", "rate": 185},
}


def pick_backend(job):
    want = (job.get("tts") or "auto").lower()
    if want != "auto":
        return want
    if os.environ.get("ELEVENLABS_API_KEY"):
        return "elevenlabs"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    try:
        import kokoro_onnx  # noqa: F401
        return "kokoro"
    except ImportError:
        pass
    if platform.system() == "Darwin" and shutil.which("say"):
        return "say"
    raise SystemExit("No voice available. Set ELEVENLABS_API_KEY or OPENAI_API_KEY, "
                     "or install the local voice: pip install 'page-walkthrough[local]'.")


def _opt(job, backend, key):
    """shots.json settings apply only to the backend they were written for (its "tts" field)."""
    if (job.get("tts") or "auto").lower() == backend and job.get(key):
        return job[key]
    return DEFAULTS[backend].get(key)


def _ffmpeg_to_wav(src, wav):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af", f"apad=pad_dur={GAP_S}",
                    "-ar", "48000", "-ac", "1", str(wav)], check=True)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def _post(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{url} returned {e.code}: {e.read().decode(errors='replace')[:400]}")


def _words_from_chars(chars, starts, ends):
    words, cur, t0, t1 = [], "", None, None
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                words.append((cur, t0, t1))
            cur, t0 = "", None
            continue
        if t0 is None:
            t0 = s
        cur, t1 = cur + ch, e
    if cur:
        words.append((cur, t0, t1))
    return words


def _elevenlabs(text, job, wav):
    voice, model = _opt(job, "elevenlabs", "voice"), _opt(job, "elevenlabs", "model")
    raw = _post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128",
                {"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}, {"text": text, "model_id": model})
    data = json.loads(raw)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(base64.b64decode(data["audio_base64"]))
    _ffmpeg_to_wav(f.name, wav)
    os.unlink(f.name)
    al = data.get("alignment") or {}
    words = _words_from_chars(al.get("characters", []), al.get("character_start_times_seconds", []),
                              al.get("character_end_times_seconds", []))
    return words or None


def _openai(text, job, wav):
    body = {"model": _opt(job, "openai", "model"), "voice": _opt(job, "openai", "voice"), "input": text,
            "response_format": "wav"}
    instructions = _opt(job, "openai", "instructions")
    if instructions and "tts-1" not in body["model"]:
        body["instructions"] = instructions
    raw = _post("https://api.openai.com/v1/audio/speech",
                {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, body)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(raw)
    _ffmpeg_to_wav(f.name, wav)
    os.unlink(f.name)
    return None


def _kokoro(text, job, wav):
    from . import local_voice
    return local_voice.speak(text, _opt(job, "kokoro", "voice"), job.get("speed", 1.0), wav, _ffmpeg_to_wav)


def _say(text, job, wav):
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "s.aiff"
        cmd = ["say", "-o", str(aiff), "-v", _opt(job, "say", "voice"), "-r", str(_opt(job, "say", "rate"))]
        subprocess.run(cmd + [text], check=True)
        _ffmpeg_to_wav(aiff, wav)
    return None


BACKENDS = {"elevenlabs": _elevenlabs, "openai": _openai, "kokoro": _kokoro, "say": _say}


def synthesize(text, job, wav):
    backend = pick_backend(job)
    if backend not in BACKENDS:
        raise SystemExit(f"unknown tts backend {backend!r}; pick one of {', '.join(BACKENDS)} or auto")
    words = BACKENDS[backend](text, job, Path(wav))
    return duration(wav), words
