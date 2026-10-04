"""Fail unless the video has the expected length and a mid-video frame that is not blank."""
import json
import subprocess
import sys

from PIL import Image, ImageStat

video, frame = sys.argv[1], sys.argv[1] + ".png"
info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                                  "-of", "json", video], capture_output=True, text=True, check=True).stdout)
duration = float(info["format"]["duration"])
kinds = sorted(s["codec_type"] for s in info["streams"])
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(duration / 2), "-i", video, "-frames:v", "1", frame], check=True)
spread = ImageStat.Stat(Image.open(frame).convert("L")).stddev[0]
print(f"{video}: {duration:.1f}s, streams {kinds}, mid-frame contrast {spread:.1f}")
if duration < 5 or kinds != ["audio", "video"] or spread < 5:
    sys.exit("video check failed")
