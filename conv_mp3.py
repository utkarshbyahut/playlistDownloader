#!/usr/bin/env python3
"""
mp4_to_mp3_with_thumbnail.py

Convert an MP4 to MP3 and embed a thumbnail taken from the video as the MP3 cover art.
- Chooses the middle frame by default (via ffprobe). You can override with --time "hh:mm:ss" or seconds.
- Requires ffmpeg and ffprobe to be installed and available on PATH.

Usage:
  python3 conv_mp3.py input.mp4
  python3 conv_mp3.py input.mp4 -o output.mp3
  python3 conv_mp3.py input.mp4 --time 00:00:05         # use 5s frame
  python3 conv_mp3.py input.mp4 --time 7.2              # 7.2 seconds
  python3 conv_mp3.py input.mp4 --kbps 192              # set bitrate (default VBR q=2)

Notes:
- By default uses high-quality VBR (-q:a 2). You can force CBR via --kbps.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

def check_tool(name: str) -> None:
    if shutil.which(name) is None:
        print(f"[error] '{name}' not found on PATH. Please install FFmpeg (ffmpeg + ffprobe).")
        sys.exit(1)

def run(cmd: list[str]) -> None:
    # Run a command, raising on nonzero
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[error] Command failed: {' '.join(cmd)}")
        sys.exit(e.returncode)

def ffprobe_duration(input_path: str) -> float | None:
    """Return duration in seconds (float) or None if unknown."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        input_path
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode().strip()
        return float(out) if out else None
    except Exception:
        return None

def parse_time_arg(time_arg: str) -> str:
    """
    Accepts "hh:mm:ss[.ms]" or seconds as float, and returns an ffmpeg-friendly timestamp string.
    """
    if ":" in time_arg:
        return time_arg  # assume already in hh:mm:ss[.ms]
    try:
        secs = float(time_arg)
        # format as H:MM:SS.mmm
        hrs = int(secs // 3600)
        mins = int((secs % 3600) // 60)
        s = secs % 60
        return f"{hrs:d}:{mins:02d}:{s:06.3f}"
    except ValueError:
        raise ValueError("Invalid --time value. Use seconds (e.g., 7.5) or hh:mm:ss[.ms]")

def default_frame_time(input_path: str) -> str:
    dur = ffprobe_duration(input_path)
    if dur and dur > 0:
        mid = dur / 2.0
        hrs = int(mid // 3600)
        mins = int((mid % 3600) // 60)
        s = mid % 60
        return f"{hrs:d}:{mins:02d}:{s:06.3f}"
    # Fallback: 1 second
    return "0:00:01.000"

def main():
    parser = argparse.ArgumentParser(description="Convert MP4 to MP3 with embedded thumbnail from the video.")
    parser.add_argument("input", help="Path to input .mp4 file")
    parser.add_argument("-o", "--output", help="Path to output .mp3 (default: same name as input)")
    parser.add_argument("--time", help="Thumbnail frame time (seconds or hh:mm:ss[.ms]); default = middle of video")
    parser.add_argument("--kbps", type=int, default=None, help="CBR audio bitrate in kbps (e.g., 192). If omitted, uses high-quality VBR (-q:a 2).")
    parser.add_argument("--keep-frame", action="store_true", help="Keep the extracted thumbnail image")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[error] Input not found: {in_path}")
        sys.exit(1)
    if in_path.suffix.lower() != ".mp4":
        print("[warn] Input is not .mp4; proceeding anyway.")

    out_path = Path(args.output) if args.output else in_path.with_suffix(".mp3")

    # Ensure tools exist
    check_tool("ffmpeg")
    check_tool("ffprobe")

    # Decide frame time
    if args.time:
        try:
            frame_time = parse_time_arg(args.time)
        except ValueError as e:
            print(f"[error] {e}")
            sys.exit(1)
    else:
        frame_time = default_frame_time(str(in_path))

    print(f"[info] Input:         {in_path}")
    print(f"[info] Output:        {out_path}")
    print(f"[info] Frame time:    {frame_time}")
    print(f"[info] Quality:       {'CBR ' + str(args.kbps) + ' kbps' if args.kbps else 'VBR (q=2)'}")

    # Work in a temp dir for the thumbnail
    with tempfile.TemporaryDirectory() as tmpdir:
        cover_path = Path(tmpdir) / "cover.jpg"

        # 1) Extract one video frame as JPEG at chosen timestamp
        #    -ss before -i for fast seek; -frames:v 1 grabs a single frame
        frame_cmd = [
            "ffmpeg", "-y",
            "-ss", frame_time,
            "-i", str(in_path),
            "-frames:v", "1",
            "-q:v", "2",               # high-quality JPEG
            str(cover_path)
        ]
        run(frame_cmd)
        if not cover_path.exists():
            print("[error] Failed to extract thumbnail frame.")
            sys.exit(1)

        # 2) Build MP3 with embedded cover art.
        #    Map audio from the video and map the image as an attached picture.
        #    Use ID3v2.3 tags so most players show the cover.
        audio_args = (["-c:a", "libmp3lame", "-q:a", "2"]
                      if args.kbps is None
                      else ["-c:a", "libmp3lame", "-b:a", f"{args.kbps}k", "-compression_level", "0"])

        mp3_cmd = [
            "ffmpeg", "-y",
            "-i", str(in_path),
            "-i", str(cover_path),
            "-map", "0:a:0",           # first audio stream from input
            "-map", "1:v:0",           # the cover image
            *audio_args,
            "-id3v2_version", "3",
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)",
            str(out_path)
        ]
        run(mp3_cmd)

        # Optionally keep the frame
        if args.keep_frame:
            keep_to = in_path.with_suffix(".jpg")
            shutil.copy2(cover_path, keep_to)
            print(f"[info] Saved cover frame: {keep_to}")

    print("[done] MP3 created with embedded thumbnail.")

if __name__ == "__main__":
    main()
