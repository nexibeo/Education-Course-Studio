#!/usr/bin/env python3
"""
publish_r2.py — push a course's narration to Cloudflare R2, so the repo stays small.

Why this exists
---------------
A course is mostly audio. The markup and metadata for an hour-long course are a
couple of hundred kilobytes; the narration is four to ten megabytes. Keeping
that in git works until it doesn't — every re-narration writes a new blob and
the history grows forever, because git never forgets a binary.

So media goes to an object store and the repo keeps only the text. Set
`CS_MEDIA_BASE` and the server rewrites the course page's <audio> to point
there; leave it unset and the course plays from disk exactly as before. The
build on disk is never modified either way.

    # one-time, per bucket
    npx wrangler r2 bucket create course-media

    # per course
    python3 publish_r2.py --course courses/hotel --bucket course-media
    python3 publish_r2.py --course courses/hotel --bucket course-media --dry-run

Then serve with:

    CS_MEDIA_BASE=https://<your-r2-public-url> python3 server.py

Public access is yours to decide. R2 buckets are private by default; you expose
one either by attaching a custom domain or by enabling its r2.dev URL, both in
the Cloudflare dashboard. This script does not do that for you on purpose —
making a bucket world-readable should be a deliberate click, not a side effect
of running a helper.

Requires `npx` (ships with Node). It shells out to wrangler rather than signing
S3 requests so it reuses whatever `wrangler login` you already have.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

MEDIA_SUFFIXES = {".mp3", ".m4a", ".wav", ".ogg", ".mp4", ".webm", ".jpg", ".jpeg", ".png", ".webp"}

CONTENT_TYPES = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".mp4": "video/mp4", ".webm": "video/webm", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
}


def size_of(p: Path) -> str:
    n = p.stat().st_size
    for unit, div in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= div:
            return f"{n/div:.1f}{unit}"
    return f"{n}B"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", required=True, help="a courses/<id> directory")
    ap.add_argument("--bucket", required=True, help="R2 bucket name")
    ap.add_argument("--prefix", default="", help="key prefix inside the bucket")
    ap.add_argument("--remote", action="store_true", default=True,
                    help="write to the real bucket (default); --local writes to the dev simulation")
    ap.add_argument("--local", dest="remote", action="store_false")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    course = Path(a.course)
    if not (course / "course.json").exists():
        print(f"not a course directory: {course}", file=sys.stderr)
        return 1

    cid = course.name
    files = sorted(p for p in course.rglob("*")
                   if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES)
    if not files:
        print(f"no media in {course} — nothing to publish")
        return 0

    total = sum(p.stat().st_size for p in files)
    print(f"{cid}: {len(files)} file(s), {total/1024**2:.1f}MB -> r2://{a.bucket}")

    for p in files:
        rel = p.relative_to(course)
        # Keep the course id in the key so one bucket can hold many courses, and so
        # CS_MEDIA_BASE/<id>/voice.mp3 resolves without any per-course configuration.
        key = "/".join(x for x in (a.prefix.strip("/"), cid, str(rel)) if x)
        ct = CONTENT_TYPES.get(p.suffix.lower(), "application/octet-stream")
        cmd = ["npx", "--yes", "wrangler@latest", "r2", "object", "put",
               f"{a.bucket}/{key}", "--file", str(p), "--content-type", ct]
        if a.remote:
            cmd.append("--remote")
        print(f"  {size_of(p):>8}  {key}")
        if a.dry_run:
            continue
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print((r.stderr or r.stdout).strip()[-600:], file=sys.stderr)
            return r.returncode

    if a.dry_run:
        print("\ndry run — nothing uploaded")
        return 0

    print(f"\ndone. serve with:\n"
          f"  CS_MEDIA_BASE=https://<your-r2-public-url>{'/' + a.prefix.strip('/') if a.prefix.strip('/') else ''} "
          f"python3 server.py")
    print("\nThe bucket is private until you attach a custom domain or enable its r2.dev URL "
          "in the Cloudflare dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
