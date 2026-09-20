#!/usr/bin/env python3
"""
Narration with Kokoro-82M — a drop-in for VideoGenerator's `resemble_tts.py`.

Same inputs, same outputs, same contract:

    python3 kokoro_tts.py --script <script.md> --workdir <work/>

    work/voice.mp3    the joined narration, with --gap seconds of air between beats
    work/beats.json   {"duration": float, "beats": [{n, type, planned, match, vo, dur, start, end}]}

`beats.json` is what the sections spec is built from — a frame takes its `t0`
from a beat's start — so the timings here decide where every visual lands.
That is why the parse and the join below mirror `resemble_tts.py` exactly
rather than doing something equivalent-but-different: a spec built against
slightly different numbers puts every mockup on the wrong word.

The reason to swap it at all: Resemble is paid and remote, Kokoro runs locally
and free (see ~/dev/kokoro-tts/tts-kokoro/, or any OpenAI-compatible
/v1/audio/speech endpoint). Nothing else in the pipeline changes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

TTS_URL = "http://127.0.0.1:8880/v1/audio/speech"
VOICE = "bm_george"          # British male, closest to the channel's presenter
SR = 24000                   # Kokoro's native rate; resampled once at the join


def parse_script(md: Path) -> list[dict]:
    """Beats of a script in this project's house format.

    `### Beat NN · \\`type\\` · start–end`, a `**match:**` line, then the VO as a
    blockquote. Kept byte-identical to resemble_tts.parse_script so a script
    that narrates there narrates here.
    """
    text = md.read_text()
    beats = []
    for m in re.finditer(
        r"^### Beat (\d+) · `([^`]+)` · ([\d:]+)–([\d:]+)\s*\n"
        r"\*\*match:\*\* \"([^\"]+)\"\s*\n\s*\n\*\*VO\*\*\s*\n> (.+?)\n",
        text,
        re.S | re.M,
    ):
        n, typ, t0, t1, match, vo = m.groups()
        vo = " ".join(l.strip().lstrip(">").strip() for l in vo.strip().split("\n") if l.strip())
        beats.append({"n": int(n), "type": typ, "planned": [t0, t1], "match": match, "vo": vo})
    if not beats:
        sys.exit(f"no beats found in {md} — expected '### Beat NN · `type` · m:ss–m:ss' headings")
    return beats


def speak(text: str, voice: str, out: Path, url: str, retries: int = 2) -> Path:
    body = json.dumps({"model": "kokoro-v1", "input": text, "voice": voice, "speed": 1.0}).encode()
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                data = r.read()
            if len(data) < 1000:
                raise RuntimeError(f"suspiciously small response ({len(data)} bytes)")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            return out
        except Exception as ex:  # noqa: BLE001 — retried, then reported
            last = ex
    raise RuntimeError(f"TTS failed after {retries + 1} attempts: {last}")


def _dur(p: Path) -> float:
    """Decode and count. Container duration is unreliable on concatenated audio."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip().splitlines()[0])
    except Exception:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True,
        )
        return float(r.stdout.strip() or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--voice", default=VOICE)
    ap.add_argument("--url", default=TTS_URL)
    ap.add_argument("--gap", type=float, default=0.35, help="air between beats, part of the timeline")
    ap.add_argument("--force", action="store_true", help="re-synthesise even if a clip is cached")
    a = ap.parse_args()

    script, workdir = Path(a.script), Path(a.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    vo_dir = workdir / "vo"
    vo_dir.mkdir(exist_ok=True)

    beats = parse_script(script)
    print(f"  {len(beats)} beats from {script.name}")

    for b in beats:
        clip = vo_dir / f"beat_{b['n']:02d}.wav"
        # Cache on the text, not the index: an edited beat must re-synthesise,
        # an untouched one must not be paid for twice.
        stamp = vo_dir / f"beat_{b['n']:02d}.txt"
        cached = clip.exists() and stamp.exists() and stamp.read_text() == b["vo"]
        if cached and not a.force:
            b["file"] = str(clip)
            b["dur"] = _dur(clip)
            print(f"  {b['n']:02d} cached ({b['dur']:.1f}s)")
            continue
        print(f"  {b['n']:02d} {b['vo'][:56]}…", end="", flush=True)
        speak(b["vo"], a.voice, clip, a.url)
        stamp.write_text(b["vo"])
        b["file"] = str(clip)
        b["dur"] = _dur(clip)
        print(f"  {b['dur']:.1f}s", flush=True)

    # Join with a fixed gap. The gap is part of the timeline, so it is written
    # into beats.json too — otherwise every frame after the first drifts.
    lst = workdir / "_concat.txt"
    silence = vo_dir / "_gap.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=mono",
         "-t", str(a.gap), str(silence)], check=True,
    )

    lines, t, timed = [], 0.0, []
    for i, b in enumerate(beats):
        lines.append(f"file '{Path(b['file']).resolve()}'")
        timed.append({**{k: v for k, v in b.items() if k != "file"},
                      "start": round(t, 3), "end": round(t + b["dur"], 3)})
        t += b["dur"]
        if i < len(beats) - 1:
            lines.append(f"file '{silence.resolve()}'")
            t += a.gap
    lst.write_text("\n".join(lines) + "\n")

    out = workdir / "voice.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", str(SR),
         "-codec:a", "libmp3lame", "-q:a", "2", str(out)], check=True,
    )
    lst.unlink(missing_ok=True)

    total = _dur(out)
    meta = {"script": str(script), "voice": a.voice, "gap": a.gap,
            "duration": round(total, 3), "beats": timed}
    (workdir / "beats.json").write_text(json.dumps(meta, indent=1))

    print(f"\nvoice -> {out}  ({total/60:.2f} min, {len(beats)} beats)")
    print(f"beats -> {workdir/'beats.json'}")
    for b in timed[:6]:
        print(f"  {b['n']:02d} {b['start']:7.2f}–{b['end']:7.2f}  {b['type']:9s} {b['match'][:44]}")
    if len(timed) > 6:
        print(f"  … {len(timed)-6} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
