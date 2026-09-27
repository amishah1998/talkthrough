"""Voiceover backends. Each turns one shot's narration into a WAV and reports its length plus word
timings, which the renderer uses to sync captions to the speech.

Backends that return word timings (Cartesia, ElevenLabs) are used as is. For the others the text is
voiced one sentence at a time, so every sentence's start and end are measured exactly and only the
words inside a sentence are spread by length."""
import base64
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

GAP_S = 0.35
SR = 44100

DEFAULTS = {
    "cartesia": {"model": "sonic-3.6", "voice": "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4", "speed": 1.0},
    "elevenlabs": {"model": "eleven_multilingual_v2", "voice": "JBFqnCBsd6RMkjVDRZzb"},
    "openai": {"model": "gpt-4o-mini-tts", "voice": "coral",
               "instructions": "Warm, clear teacher walking someone through a page. Natural pace, no drama."},
    "kokoro": {"voice": "af_heart", "speed": 1.0},
    "say": {"voice": "Samantha", "rate": 185},
}
ORDER = [("cartesia", "CARTESIA_API_KEY"), ("elevenlabs", "ELEVENLABS_API_KEY"), ("openai", "OPENAI_API_KEY")]


def kokoro_available():
    try:
        import kokoro_onnx  # noqa: F401
        return True
    except ImportError:
        return False


def pick_backend(job):
    want = (job.get("tts") or "auto").lower()
    if want != "auto":
        return want
    for name, env in ORDER:
        if os.environ.get(env):
            return name
    if kokoro_available():
        return "kokoro"
    if platform.system() == "Darwin" and shutil.which("say"):
        return "say"
    raise SystemExit("No voice available. Set CARTESIA_API_KEY, ELEVENLABS_API_KEY or OPENAI_API_KEY, "
                     "or install the free local voice: pip install 'page-walkthrough[local]'.")


def _opt(job, backend, key):
    """shots.json voice settings apply only to the backend they were written for (its "tts" field)."""
    if (job.get("tts") or "auto").lower() == backend and job.get(key) is not None:
        return job[key]
    return DEFAULTS[backend].get(key)


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *map(str, args)], check=True)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def _request(url, headers, body):
    return urllib.request.Request(url, data=json.dumps(body).encode(),
                                  headers={**headers, "Content-Type": "application/json"})


def _open(req):
    try:
        return urllib.request.urlopen(req, timeout=180)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{req.full_url} returned {e.code}: {e.read().decode(errors='replace')[:400]}")


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


# Each backend writes a mono WAV at SR to `wav` and returns word timings, or None if it has none.

def _cartesia(text, job, wav):
    body = {"model_id": _opt(job, "cartesia", "model"), "transcript": text,
            "voice": {"mode": "id", "id": _opt(job, "cartesia", "voice")}, "language": job.get("language", "en"),
            "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SR},
            "add_timestamps": True, "generation_config": {"speed": _opt(job, "cartesia", "speed")}}
    req = _request("https://api.cartesia.ai/tts/sse",
                   {"Authorization": f"Bearer {os.environ['CARTESIA_API_KEY']}", "Cartesia-Version": "2026-08-14"},
                   body)
    pcm, words = bytearray(), []
    with _open(req) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:].strip())
            if ev.get("type") == "chunk" and ev.get("data"):
                pcm += base64.b64decode(ev["data"])
            elif ev.get("type") == "timestamps":
                ts = ev.get("word_timestamps") or {}
                words += list(zip(ts.get("words", []), ts.get("start", []), ts.get("end", [])))
            elif ev.get("type") == "error":
                raise SystemExit(f"Cartesia: {ev.get('title')}: {ev.get('message')}")
    with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
        f.write(pcm)
    _ffmpeg("-f", "s16le", "-ar", SR, "-ac", 1, "-i", f.name, wav)
    os.unlink(f.name)
    return words or None


def _elevenlabs(text, job, wav):
    voice, model = _opt(job, "elevenlabs", "voice"), _opt(job, "elevenlabs", "model")
    req = _request(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128",
                   {"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}, {"text": text, "model_id": model})
    with _open(req) as r:
        data = json.loads(r.read())
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(base64.b64decode(data["audio_base64"]))
    _ffmpeg("-i", f.name, "-ar", SR, "-ac", 1, wav)
    os.unlink(f.name)
    al = data.get("alignment") or {}
    words = _words_from_chars(al.get("characters", []), al.get("character_start_times_seconds", []),
                              al.get("character_end_times_seconds", []))
    return words or None


def _openai(text, job, wav):
    body = {"model": _opt(job, "openai", "model"), "voice": _opt(job, "openai", "voice"), "input": text,
            "response_format": "wav"}
    if "tts-1" not in body["model"]:
        body["instructions"] = _opt(job, "openai", "instructions")
    req = _request("https://api.openai.com/v1/audio/speech",
                   {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, body)
    with _open(req) as r, tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(r.read())
    _ffmpeg("-i", f.name, "-ar", SR, "-ac", 1, wav)
    os.unlink(f.name)
    return None


def _kokoro(text, job, wav):
    from .local_voice import speak
    speak(text, _opt(job, "kokoro", "voice"), _opt(job, "kokoro", "speed"), wav, SR)
    return None


def _say(text, job, wav):
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "s.aiff"
        subprocess.run(["say", "-o", str(aiff), "-v", _opt(job, "say", "voice"), "-r", str(_opt(job, "say", "rate")),
                        text], check=True)
        _ffmpeg("-i", aiff, "-ar", SR, "-ac", 1, wav)
    return None


BACKENDS = {"cartesia": _cartesia, "elevenlabs": _elevenlabs, "openai": _openai, "kokoro": _kokoro, "say": _say}
TIMED = {"cartesia", "elevenlabs"}


def sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def _spread(sentence, t0, t1):
    words = sentence.split()
    weights = [len(w) + 1 for w in words]
    total, acc, out = sum(weights), 0, []
    for w, k in zip(words, weights):
        a = t0 + (t1 - t0) * acc / total
        acc += k
        out.append((w, a, t0 + (t1 - t0) * acc / total))
    return out


def synthesize(text, job, wav):
    """Voice one shot into `wav` (with a short pause after it). Returns (seconds, word timings)."""
    backend = pick_backend(job)
    if backend not in BACKENDS:
        raise SystemExit(f"unknown tts backend {backend!r}; pick one of {', '.join(BACKENDS)} or auto")
    fn = BACKENDS[backend]
    wav = Path(wav)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if backend in TIMED:
            words = fn(text, job, tmp / "all.wav")
            parts = [tmp / "all.wav"]
        else:
            words, parts, t = [], [], 0.0
            for i, s in enumerate(sentences(text)):
                part = tmp / f"s{i:03d}.wav"
                fn(s, job, part)
                d = duration(part)
                words += _spread(s, t, t + d)
                parts.append(part)
                t += d
        listing = tmp / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        _ffmpeg("-f", "concat", "-safe", 0, "-i", listing, "-af", f"apad=pad_dur={GAP_S}", "-ar", SR, "-ac", 1, wav)
    return duration(wav), words or None
