#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Make an 'astral poster' mix: for each playlist item, use only the FIRST FRAME as visuals
(stylized with a gradient derived from that frame), keep original audio, then stitch.
w
Usage:
  python gradient_playlist_download.py "https://www.youtube.com/playlist?list=XXXX" \
    -o astral_mix.mp4 --chapters --cookies-from-browser chrome

Notes:
- Use only on content you own/are allowed to download.
- For private playlists, pass --cookies or --cookies-from-browser chrome|safari|firefox
- Default visual size: 1280x720 @ 30fps; change with --width/--height/--fps
"""

import argparse, json, os, re, shlex, subprocess, sys, tempfile
from pathlib import Path

# Pillow for color + gradient
from PIL import Image, ImageStat

# --------------------- helpers ---------------------

def run(cmd):
    print("+", cmd)
    p = subprocess.run(cmd, shell=True)
    if p.returncode != 0:
        sys.exit(p.returncode)

def check_output(cmd):
    return subprocess.check_output(cmd, shell=True, text=True)

def probe_duration(path):
    try:
        out = check_output(
            f'ffprobe -v error -select_streams v:0 -show_entries format=duration '
            f'-of json {shlex.quote(str(path))}'
        )
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return None

def probe_resolution(path):
    try:
        out = check_output(
            f'ffprobe -v error -select_streams v:0 -show_entries stream=width,height '
            f'-of json {shlex.quote(str(path))}'
        )
        d = json.loads(out)
        w = d["streams"][0]["width"]; h = d["streams"][0]["height"]
        return int(w), int(h)
    except Exception:
        return None

def sanitize(name):
    return re.sub(r'[\\/*?:"<>|]+', "_", name).strip()

def clamp(x, a, b): return max(a, min(b, x))

def average_rgb(img_path):
    im = Image.open(img_path).convert("RGB")
    # downsample heavily for speed/robustness
    im = im.resize((64, 64), Image.LANCZOS)
    stat = ImageStat.Stat(im)
    r, g, b = [int(v) for v in stat.mean]
    return (r, g, b)

def lighten(rgb, pct=0.15):
    r,g,b = rgb
    return (clamp(int(r + (255 - r)*pct), 0, 255),
            clamp(int(g + (255 - g)*pct), 0, 255),
            clamp(int(b + (255 - b)*pct), 0, 255))

def darken(rgb, pct=0.20):
    r,g,b = rgb
    return (clamp(int(r*(1-pct)), 0, 255),
            clamp(int(g*(1-pct)), 0, 255),
            clamp(int(b*(1-pct)), 0, 255))

def complement(rgb):
    r,g,b = rgb
    return (255-r, 255-g, 255-b)

def lerp(a, b, t):
    return tuple(int(a[i] + (b[i]-a[i])*t) for i in range(3))

def make_astral_gradient(base_rgb, size, out_png, pattern="nebula"):
    """
    Build a left->right 'astral' gradient PNG using base color variations.
    pattern 'nebula': darkened base -> (base) -> complement/light
    """
    W, H = size
    if pattern == "nebula":
        c0 = darken(base_rgb, 0.35)
        c1 = base_rgb
        c2 = lighten(complement(base_rgb), 0.15)
        # two-stage blend: 0..0.6 from c0->c1, 0.6..1.0 from c1->c2
        split = 0.6
        row = []
        for x in range(W):
            t = x/(W-1) if W > 1 else 0
            if t <= split:
                tt = t/split
                c = lerp(c0, c1, tt)
            else:
                tt = (t - split)/(1 - split)
                c = lerp(c1, c2, tt)
            row.append(c)
        # expand to full image
        grad = Image.new("RGB", (W, 1))
        for x,c in enumerate(row):
            grad.putpixel((x,0), c)
        grad = grad.resize((W, H), Image.BILINEAR)
    else:
        # simple two-stop gradient
        grad = Image.new("RGB", (W, H), base_rgb)

    grad.save(out_png)

def write_ffmetadata_chapters(chapter_durations, out_path, title="Astral Mix"):
    lines = [";FFMETADATA1", f"title={title}"]
    t = 0.0
    for i, (ch_title, dur) in enumerate(chapter_durations, 1):
        start = int(t * 1000)
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

# --------------------- main ---------------------

def main():
    ap = argparse.ArgumentParser(description="Astral poster stitcher for YouTube playlists.")
    ap.add_argument("playlist_url", help="YouTube playlist URL (your unlisted/private link).")
    ap.add_argument("-o", "--out", default="astral_mix.mp4", help="Output MP4.")
    ap.add_argument("--workdir", default=None, help="Working dir (default: temp).")
    ap.add_argument("--width", type=int, default=1280, help="Output width.")
    ap.add_argument("--height", type=int, default=720, help="Output height.")
    ap.add_argument("--fps", type=int, default=30, help="Output FPS.")
    ap.add_argument("--crf", type=int, default=20, help="x264 CRF.")
    ap.add_argument("--audio-kbps", type=int, default=192, help="AAC bitrate kbps.")
    ap.add_argument("--opacity", type=float, default=0.60, help="Gradient overlay opacity 0..1.")
    ap.add_argument("--fit", choices=["contain","cover"], default="contain",
                    help="Frame fit mode: contain (letterbox) or cover (zoom+crop).")
    ap.add_argument("--chapters", action="store_true", help="Add chapter markers for each track.")
    ap.add_argument("--cookies", default=None, help="Path to cookies (Netscape format).")
    ap.add_argument("--cookies-from-browser", default=None,
                    help="Read cookies from your browser (chrome|safari|firefox, etc.).")
    ap.add_argument("-N", "--concurrent-fragments", type=int, default=8,
                    help="yt-dlp concurrent fragments (speed-up).")
    ap.add_argument("--max-retries", type=int, default=5, help="yt-dlp retries.")
    args = ap.parse_args()

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="astral_stitch_"))
    inputs_dir = workdir / "inputs"; inputs_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = workdir / "frames"; frames_dir.mkdir(exist_ok=True)
    grads_dir  = workdir / "grads";  grads_dir.mkdir(exist_ok=True)
    segs_dir   = workdir / "segs";   segs_dir.mkdir(exist_ok=True)
    print(f"[i] Working directory: {workdir}")

    # 1) Download playlist → MP4
    outtmpl = str(inputs_dir / "%(playlist_index)03d-%(title).200B.%(ext)s")
    ytdlp_cmd = [
        "yt-dlp",
        "--no-overwrites",
        "--ignore-errors",
        "--yes-playlist",
        "--playlist-reverse",
        "--retries", str(args.max_retries),
        "-N", str(args.concurrent_fragments),
        "-f", "bv*+ba/b",           # best av
        "-o", shlex.quote(outtmpl),
        shlex.quote(args.playlist_url),
        "--recode-video", "mp4"     # ensure mp4 output
    ]
    if args.cookies_from_browser:
        ytdlp_cmd[1:1] = ["--cookies-from-browser", shlex.quote(args.cookies_from_browser)]
    elif args.cookies:
        ytdlp_cmd[1:1] = ["--cookies", shlex.quote(args.cookies)]

    run(" ".join(ytdlp_cmd))

    # Collect files
    files = sorted([p for p in inputs_dir.iterdir() if p.suffix.lower() in (".mp4",)])
    if len(files) < 1:
        print("[!] No MP4s downloaded. Check playlist access/cookies.")
        sys.exit(2)

    # 2) For each file: extract first frame, create gradient, overlay, make segment
    seg_paths = []
    chapters = []
    for idx, vid in enumerate(files, 1):
        base = vid.name
        nice_title = re.sub(r'^\d{3}-', '', base)         # strip index prefix
        nice_title = re.sub(r'\.[^.]+$', '', nice_title)  # strip extension

        # 2a) first frame
        frame_png = frames_dir / f"{vid.stem}_first.png"
        run(f'ffmpeg -y -i {shlex.quote(str(vid))} -frames:v 1 {shlex.quote(str(frame_png))}')

        # 2b) astral gradient from average frame color
        base_rgb = average_rgb(frame_png)
        grad_png = grads_dir / f"{vid.stem}_grad.png"
        make_astral_gradient(base_rgb, (args.width, args.height), grad_png, pattern="nebula")

        # 2c) compose stylized poster (frame -> fit WxH -> overlay gradient via blend=overlay)
        # fit mode
        if args.fit == "contain":
            vf_fit = (f"scale={args.width}:{args.height}:force_original_aspect_ratio=decrease,"
                      f"pad={args.width}:{args.height}:(ow-iw)/2:(oh-ih)/2")
        else:
            vf_fit = (f"scale={args.width}:{args.height}:force_original_aspect_ratio=increase,"
                      f"crop={args.width}:{args.height}")

        poster_png = frames_dir / f"{vid.stem}_poster.png"
        run(
            f'ffmpeg -y -i {shlex.quote(str(frame_png))} -i {shlex.quote(str(grad_png))} '
            f'-filter_complex "[0:v]{vf_fit}[f];[f][1:v]blend=all_mode=overlay:all_opacity={args.opacity},'
            f'format=rgb24" '
            f'-frames:v 1 {shlex.quote(str(poster_png))}'
        )

        # 2d) make a video segment: static poster image + original audio duration
        dur = probe_duration(vid) or 0.0
        seg_mp4 = segs_dir / f"{idx:03d}-{sanitize(nice_title)}.mp4"
        run(
            f'ffmpeg -y -loop 1 -framerate {args.fps} -t {dur:.3f} -i {shlex.quote(str(poster_png))} '
            f'-i {shlex.quote(str(vid))} '
            f'-map 0:v:0 -map 1:a:0 -shortest '
            f'-vf "fps={args.fps},format=yuv420p" '
            f'-c:v libx264 -preset veryfast -crf {args.crf} '
            f'-c:a aac -b:a {args.audio_kbps}k '
            f'-movflags +faststart {shlex.quote(str(seg_mp4))}'
        )
        seg_paths.append(seg_mp4)

        if args.chapters:
            chapters.append((sanitize(nice_title), dur))

    # 3) Concat segments (try fast copy first)
    out_path = Path(args.out).resolve()
    concat_list = workdir / "concat_list.txt"
    concat_list.write_text("\n".join([f"file '{p.as_posix()}'" for p in seg_paths]), encoding="utf-8")

    try:
        run(f'ffmpeg -y -f concat -safe 0 -i {shlex.quote(str(concat_list))} -c copy '
            f'-movflags +faststart {shlex.quote(str(out_path))}')
        print(f"[✓] Fast concat succeeded → {out_path}")
    except SystemExit:
        print("[!] Fast concat failed; falling back to re-encode.")
        # Safe concat (decode/encode)
        ins = " ".join([f"-i {shlex.quote(str(p))}" for p in seg_paths])
        n = len(seg_paths)
        pairs = "".join([f"[{i}:v][{i}:a]" for i in range(n)])
        flt = f'{pairs}concat=n={n}:v=1:a=1[v][a]'
        run(
            f'ffmpeg -y {ins} -filter_complex "{flt}" -map "[v]" -map "[a]" '
            f'-c:v libx264 -preset veryfast -crf {args.crf} -c:a aac -b:a {args.audio_kbps}k '
            f'-movflags +faststart {shlex.quote(str(out_path))}'
        )
        print(f"[✓] Re-encoded concat → {out_path}")

    # 4) Chapters (optional)
    if args.chapters and chapters:
        meta = workdir / "chapters.ffmeta"
        write_ffmetadata_chapters(chapters, meta, title=sanitize(out_path.stem))
        tmp_mux = workdir / "tmp_mux.mp4"
        run(f'ffmpeg -y -i {shlex.quote(str(out_path))} -i {shlex.quote(str(meta))} '
            f'-map_metadata 1 -codec copy {shlex.quote(str(tmp_mux))}')
        os.replace(tmp_mux, out_path)
        print("[✓] Chapters added.")

    print("[Done] →", out_path)

if __name__ == "__main__":
    main()
