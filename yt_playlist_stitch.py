#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Download a YouTube playlist (your own/unlisted link) and stitch all items into one MP4.
- Orders by playlist index.
- Default: robust re-encode (works for mixed codecs/sizes/fps).
- Optional: fast concat (-c copy) when sources are compatible.
- Optional: chapter markers at clip boundaries.
- Optional: pass browser cookies for private playlists.

Usage examples:
  python yt_playlist_stitch.py "https://www.youtube.com/playlist?list=XXXX" -o merged.mp4
  python yt_playlist_stitch.py "https://www.youtube.com/playlist?list=XXXX" -o merged.mp4 --chapters
  python yt_playlist_stitch.py "https://www.youtube.com/playlist?list=XXXX" -o merged.mp4 --fast-copy
  python yt_playlist_stitch.py "https://www.youtube.com/playlist?list=XXXX" -o merged.mp4 --cookies cookies.txt
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

def run(cmd):
    print("+", cmd)
    p = subprocess.run(cmd, shell=True)
    if p.returncode != 0:
        sys.exit(p.returncode)

def probe_duration(path):
    # Returns duration in seconds as float, or None
    try:
        cmd = (
            f'ffprobe -v error -select_streams v:0 -show_entries format=duration '
            f'-of json {shlex.quote(str(path))}'
        )
        out = subprocess.check_output(cmd, shell=True, text=True)
        data = json.loads(out)
        return float(data["format"]["duration"])
    except Exception:
        return None

def write_ffmetadata_chapters(chapter_durations, out_path, title="Merged Video"):
    """
    chapter_durations: list of tuples (title, duration_seconds)
    Writes a FFmpeg metadata file with chapters starting at cumulative offsets.
    """
    lines = [";FFMETADATA1", f"title={title}"]
    t = 0.0
    for i, (ch_title, dur) in enumerate(chapter_durations, 1):
        start = int(t * 1000)      # milliseconds
        end = int((t + max(dur, 0.001)) * 1000)
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start}",
            f"END={end}",
            f"title={ch_title}"
        ]
        t += dur
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")

def sanitize(name):
    # Keep it simple/cross-platform
    return re.sub(r'[\\/*?:"<>|]+', "_", name).strip()

def main():
    ap = argparse.ArgumentParser(description="Download a YouTube playlist and stitch into one MP4.")
    ap.add_argument("playlist_url", help="YouTube playlist URL (your unlisted playlist link).")
    ap.add_argument("-o", "--out", default="merged.mp4", help="Output file (mp4).")
    ap.add_argument("--workdir", default=None, help="Optional working dir. Defaults to a temp folder.")
    ap.add_argument("--width", type=int, default=1280, help="Target width when re-encoding (keeps AR).")
    ap.add_argument("--fps", type=int, default=30, help="Target FPS when re-encoding.")
    ap.add_argument("--crf", type=int, default=20, help="x264 CRF for re-encode.")
    ap.add_argument("--audio-kbps", type=int, default=192, help="AAC kbps for re-encode.")
    ap.add_argument("--chapters", action="store_true", help="Write chapter markers at each clip boundary.")
    ap.add_argument("--fast-copy", action="store_true",
                    help="Attempt fast concat (-c copy) via concat demuxer; fails if codecs differ.")
    ap.add_argument("--cookies", default=None,
                    help="Path to cookies file (Netscape format) for private playlists (optional).")
    ap.add_argument("--max-retries", type=int, default=5, help="yt-dlp download retries.")
    args = ap.parse_args()

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="yt_stitch_"))
    inputs_dir = workdir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    print(f"[i] Working directory: {workdir}")

    # 1) Download playlist with yt-dlp preserving order
    #    Using playlist_index in filenames to keep sort order deterministic.
    outtmpl = str(inputs_dir / "%(playlist_index)03d-%(title).200B.%(ext)s")
    ytdlp_cmd = [
        "yt-dlp",
        "--no-overwrites",
        "--ignore-errors",
        "--yes-playlist",
        "--playlist-reverse",  # ensure 001 is the first video in playlist display (toggle if needed)
        "--retries", str(args.max_retries),
        "-f", "bv*+ba/b",              # best video+audio
        "-o", shlex.quote(outtmpl),
        shlex.quote(args.playlist_url),
    ]
    if args.cookies:
        ytdlp_cmd[1:1] = ["--cookies", shlex.quote(args.cookies)]

    run(" ".join(ytdlp_cmd))

    # Collect downloaded files (video containers)
    exts = (".mp4", ".mkv", ".mov", ".m4v", ".webm")
    files = sorted([p for p in inputs_dir.iterdir() if p.suffix.lower() in exts])

    if len(files) < 2:
        print("[!] Need at least 2 videos in the playlist after download.")
        sys.exit(2)

    # 2) Either FAST concat (no re-encode) or robust re-encode path
    out_path = Path(args.out).resolve()

    if args.fast_copy:
        # Try concat demuxer: write a list file
        list_file = workdir / "concat.txt"
        list_file.write_text(
            "\n".join([f"file '{f.as_posix()}'" for f in files]),
            encoding="utf-8"
        )
        try:
            run(f'ffmpeg -y -f concat -safe 0 -i {shlex.quote(str(list_file))} -c copy {shlex.quote(str(out_path))}')
            print(f"[✓] Fast concat succeeded → {out_path}")
            # Chapters are not easily injected without re-mux; we can still add them via metadata remux:
            if args.chapters:
                # Build chapter durations by probing
                chaps = []
                for p in files:
                    dur = probe_duration(p) or 0.0
                    chaps.append((sanitize(p.name), dur))
                meta = workdir / "chapters.ffmeta"
                write_ffmetadata_chapters(chaps, meta, title=sanitize(out_path.stem))
                tmp_mux = workdir / "tmp_mux.mp4"
                run(f'ffmpeg -y -i {shlex.quote(str(out_path))} -i {shlex.quote(str(meta))} '
                    f'-map_metadata 1 -codec copy {shlex.quote(str(tmp_mux))}')
                os.replace(tmp_mux, out_path)
                print("[✓] Chapters added.")
            return
        except SystemExit:
            print("[!] Fast concat failed. Falling back to robust re-encode…")

    # Robust re-encode (handles mixed codecs/sizes/fps)
    # Build dynamic filter_complex for concat
    ins = []
    vlabels, alabels = [], []
    for i, f in enumerate(files):
        ins.append(f'-i {shlex.quote(str(f))}')
        vlabels.append(f'[{i}:v]scale={args.width}:-2,setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v{i}]')
        alabels.append(f'[{i}:a]aresample=48000,asetpts=PTS-STARTPTS[a{i}]')

    pairs = "".join([f'[v{i}][a{i}]' for i in range(len(files))])
    concat = f'{pairs}concat=n={len(files)}:v=1:a=1[v][a]'
    flt = "; ".join(vlabels + alabels + [concat])

    cmd = (
        f'ffmpeg -y {" ".join(ins)} -filter_complex "{flt}" '
        f'-map "[v]" -map "[a]" -r {args.fps} '
        f'-c:v libx264 -preset veryfast -crf {args.crf} '
        f'-c:a aac -b:a {args.audio_kbps}k '
        f'{shlex.quote(str(out_path))}'
    )
    run(cmd)
    print(f"[✓] Re-encoded concat → {out_path}")

    # 3) Optional: chapters
    if args.chapters:
        chaps = []
        for p in files:
            dur = probe_duration(p) or 0.0
            # Friendlier title: strip index prefix
            base = p.name
            base = re.sub(r'^\d{3}-', '', base)
            base = re.sub(r'\.[^.]+$', '', base)
            chaps.append((sanitize(base), dur))

        meta = workdir / "chapters.ffmeta"
        write_ffmetadata_chapters(chaps, meta, title=sanitize(out_path.stem))

        tmp_mux = workdir / "tmp_mux.mp4"
        run(f'ffmpeg -y -i {shlex.quote(str(out_path))} -i {shlex.quote(str(meta))} '
            f'-map_metadata 1 -codec copy {shlex.quote(str(tmp_mux))}')
        os.replace(tmp_mux, out_path)
        print("[✓] Chapters added.")

    print("[Done]")

if __name__ == "__main__":
    main()
