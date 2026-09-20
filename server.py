#!/usr/bin/env python3
"""
Course Studio server.

Serves the VideoGenerator site untouched inside a player shell, adds section
navigation, and answers questions against the narration transcript.

    python3 server.py --port 8900

Routes
    /                        course index
    /c/<id>                  the player
    /c/<id>/course.json      sections, beats, generated slides
    /c/<id>/site/<file>      the original build, byte for byte
    POST /c/<id>/ask         {question, section_id} -> chat reply
    POST /c/<id>/reset       drop generated slides
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import qa

ROOT = Path(__file__).parent
COURSES = ROOT / "courses"
PLAYER = ROOT / "player"

_lock = threading.Lock()


# Shown in the player header. Set CS_BRAND to your own name; blank hides it.
BRAND = os.environ.get("CS_BRAND", "Course Studio")

# Where a course's audio lives. Empty means "next to the course", which is what the
# bundled demo does. Point it at an R2 / CDN origin and narration is fetched from there
# instead, so a repo never has to carry megabytes of mp3:
#     CS_MEDIA_BASE=https://media.example.com/courses
# The player resolves <base>/<course-id>/voice.mp3.
MEDIA_BASE = os.environ.get("CS_MEDIA_BASE", "").rstrip("/")


def html_escape(t: str) -> str:
    """BRAND comes from the environment, so it is escaped before it reaches the page."""
    return (t.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def load_env_key() -> None:
    """Take the OpenRouter key from a local .env if it is not already exported.

    Override the location with CS_ENV_FILE. Nothing is read from outside the
    working directory unless you point it there yourself.
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        return
    env = Path(os.environ.get("CS_ENV_FILE", ".env"))
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line.startswith("OPENROUTER_API_KEY="):
            os.environ["OPENROUTER_API_KEY"] = line.split("=", 1)[1].strip().strip("\"'")
            return


def course_path(cid: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid or ""):
        return None
    p = COURSES / cid / "course.json"
    return p if p.exists() else None


def read_course(cid: str) -> dict | None:
    p = course_path(cid)
    return json.loads(p.read_text()) if p else None


def write_course(cid: str, course: dict) -> None:
    p = course_path(cid)
    if p:
        p.write_text(json.dumps(course, indent=2))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CourseStudio"

    def log_message(self, fmt, *args):
        pass

    # -- helpers ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._json(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        # Range support so the browser can seek the narration audio.
        rng = self.headers.get("Range")
        if rng and (m := re.match(r"bytes=(\d+)-(\d*)", rng)):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else len(data) - 1
            end = min(end, len(data) - 1)
            chunk = data[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
            return
        self._send(200, data, ctype, {"Accept-Ranges": "bytes"})

    # -- routes ----------------------------------------------------------

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path in ("/", "/index.html"):
            courses = []
            for d in sorted(COURSES.glob("*/course.json")):
                c = json.loads(d.read_text())
                courses.append(
                    {
                        "id": d.parent.name,
                        "title": c.get("title"),
                        "duration": c.get("duration"),
                        "sections": len(c.get("sections") or []),
                    }
                )
            html = (PLAYER / "index.html").read_text().replace(
                "{{COURSES}}", json.dumps(courses)
            ).replace("{{BRAND}}", html_escape(BRAND))
            self._send(200, html.encode(), "text/html; charset=utf-8")
            return

        m = re.match(r"^/c/([A-Za-z0-9_-]+)$", path)
        if m:
            if not course_path(m.group(1)):
                self._json(404, {"error": "no such course"})
                return
            html = ((PLAYER / "player.html").read_text()
                    .replace("{{COURSE_ID}}", m.group(1))
                    .replace("{{BRAND}}", html_escape(BRAND)))
            self._send(200, html.encode(), "text/html; charset=utf-8")
            return

        m = re.match(r"^/c/([A-Za-z0-9_-]+)/course\.json$", path)
        if m:
            c = read_course(m.group(1))
            if c and MEDIA_BASE:
                # The course keeps its own relative filename; only the origin is rewritten,
                # so the same course.json works locally and behind a CDN.
                c = dict(c, media_base=f"{MEDIA_BASE}/{m.group(1)}")
            self._json(200 if c else 404, c or {"error": "no such course"})
            return

        m = re.match(r"^/c/([A-Za-z0-9_-]+)/site/(.+)$", path)
        if m:
            cid, rel = m.group(1), m.group(2)
            if ".." in rel:
                self._json(400, {"error": "bad path"})
                return
            target = COURSES / cid / "site" / rel
            # The narration is referenced from inside the course page, so when media lives on a
            # CDN the rewrite has to happen here rather than in course.json. Only the page is
            # touched, and only in memory — the build on disk stays byte for byte.
            if MEDIA_BASE and rel.endswith(".html") and target.exists():
                page = target.read_text().replace('src="voice.mp3"',
                                                  f'src="{MEDIA_BASE}/{cid}/voice.mp3"')
                self._send(200, page.encode(), "text/html; charset=utf-8")
                return
            self._file(target)
            return

        m = re.match(r"^/c/([A-Za-z0-9_-]+)/generated/([A-Za-z0-9_.-]+)$", path)
        if m:
            self._file(COURSES / m.group(1) / "generated" / m.group(2))
            return

        m = re.match(r"^/player/(.+)$", path)
        if m and ".." not in m.group(1):
            self._file(PLAYER / m.group(1))
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            self._json(400, {"error": "bad body"})
            return

        m = re.match(r"^/c/([A-Za-z0-9_-]+)/ask$", path)
        if m:
            cid = m.group(1)
            course = read_course(cid)
            if not course:
                self._json(404, {"error": "no such course"})
                return
            result = qa.ask(
                course,
                payload.get("question", ""),
                payload.get("section_id"),
                history=payload.get("history") or [],
                course_dir=COURSES / cid,
            )
            if result.get("kind") == "slide":
                with _lock:
                    fresh = read_course(cid) or course
                    fresh.setdefault("generated", []).append(result["slide"])
                    write_course(cid, fresh)
            self._json(200, result)
            return

        m = re.match(r"^/c/([A-Za-z0-9_-]+)/reset$", path)
        if m:
            cid = m.group(1)
            with _lock:
                course = read_course(cid)
                if not course:
                    self._json(404, {"error": "no such course"})
                    return
                course["generated"] = []
                write_course(cid, course)
            self._json(200, {"ok": True})
            return

        self._json(404, {"error": "not found"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    load_env_key()
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("  ! OPENROUTER_API_KEY not found — questions will error, playback still works")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    n = len(list(COURSES.glob("*/course.json")))
    print(f"  Course Studio on http://{args.host}:{args.port}  ({n} course{'s' if n != 1 else ''})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
