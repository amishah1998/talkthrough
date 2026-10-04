"""The free local voice: Kokoro-82M (Apache-2.0 weights) through kokoro-onnx. No torch, no GPU.
The model is downloaded once into the user cache folder."""
import os
import sys
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {"model": "kokoro-v1.0.onnx", "voices": "voices-v1.0.bin"}
_engine = None


def cache_dir():
    base = os.environ.get("TALKTHROUGH_CACHE") or os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    d = Path(base) / "talkthrough" / "kokoro"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fetch(name):
    path = cache_dir() / name
    if path.exists() and path.stat().st_size > 0:
        return path
    print(f"downloading {name} (one time) ...", file=sys.stderr, flush=True)
    tmp = path.with_suffix(".part")
    urllib.request.urlretrieve(RELEASE + name, tmp)
    tmp.rename(path)
    return path


def engine():
    global _engine
    if _engine is None:
        from kokoro_onnx import Kokoro
        _engine = Kokoro(str(_fetch(FILES["model"])), str(_fetch(FILES["voices"])))
    return _engine


def speak(text, voice, speed, wav, sample_rate):
    import soundfile as sf
    samples, sr = engine().create(text, voice=voice, speed=speed, lang="en-us")
    tmp = Path(wav).with_suffix(".raw.wav")
    sf.write(tmp, samples, sr)
    from .voice import _ffmpeg
    _ffmpeg("-i", tmp, "-ar", sample_rate, "-ac", 1, wav)
    tmp.unlink()
