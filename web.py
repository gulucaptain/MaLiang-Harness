"""Local browser dashboard for the existing inference entry point.

Uses only the Python standard library. Bind to loopback because run traces contain
full prompts, model replies and tool arguments.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import mimetypes
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
WEB_LOGS = RUNS / ".web-logs"
PREVIEW_LOCK = threading.Lock()


class Runs:
    def __init__(self):
        self.lock = threading.Lock()
        self.processes = {}

    def start(
        self,
        prompt: str,
        kind: str,
        resume_id: str | None = None,
        orientation="auto",
        resolution="profile",
        duration=None,
        fps=None,
        image=None,
        edit_from=None,
        revision=None,
        max_output_tokens=None,
    ):
        if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens < 1 or max_output_tokens > 1000000):
            raise ValueError("单次输出上限须为 1–1000000 的整数；实际支持范围取决于模型")
        source = None
        if edit_from is not None:
            if (
                resume_id
                or image is not None
                or duration is not None
                or fps is not None
                or orientation != "auto"
                or resolution != "profile"
            ):
                raise ValueError("编辑继承原规格，不能同时续跑或上传新素材")
            from maliang.editing import snapshot
            from maliang.store import ProjectStore

            source = self.project(edit_from)
            snapshot(ProjectStore(source), revision)
        elif revision is not None:
            raise ValueError("版本号必须与编辑源任务一起提供")
        if not isinstance(prompt, str) or (not resume_id and (not prompt.strip() or len(prompt) > 20000)):
            raise ValueError("请输入 1–20000 字的创作要求")
        if kind not in {"image", "video", "paint", "pathtrace"}:
            raise ValueError("任务类型必须为 image、video、paint 或 pathtrace")
        if orientation not in ("auto", "portrait", "landscape", "square") or resolution not in (
            "profile",
            "standard",
            "high",
        ):
            raise ValueError("无效的方向或分辨率选项")
        if resume_id and any(value is not None for value in (duration, fps, image)):
            raise ValueError("续跑不接受新素材或输出规格")
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or not 1 <= duration <= 120
        ):
            raise ValueError("视频时长须为 1–120 秒")
        if fps is not None and (type(fps) is not int or not 1 <= fps <= 30):
            raise ValueError("帧率须为 1–30 的整数")
        if kind != "video" and (duration is not None or fps is not None):
            raise ValueError("图像任务不接受视频时长和帧率")
        if kind == "video" and not resume_id and source is None:
            from maliang.settings import load_harness_settings

            settings = load_harness_settings(ROOT / "harness.json")
            spec = settings.profile("video").output
            frames = math.ceil(
                (duration if duration is not None else spec.duration) * (fps if fps is not None else spec.fps)
            )
            if frames > settings.budget.max_frames:
                raise ValueError(
                    f"成片需要 {frames} 帧，超过当前 {settings.budget.max_frames} 帧预算；请缩短时长、降低帧率或调整 harness.json 预算。预览还需额外预算。"
                )
        if kind == "paint" and not resume_id and source is None:
            from maliang.paint_native import require_library

            require_library()
        if kind == "pathtrace" and not resume_id and source is None:
            from maliang.pathtrace import engine_status

            status = engine_status()
            if not status["available"]:
                raise ValueError(status["message"])
        image_bytes = None
        if image is not None:
            from maliang.input_images import normalize_image

            if not isinstance(image, str) or len(image) > 13_400_000:
                raise ValueError("上传图像过大或格式错误")
            try:
                image_bytes = normalize_image(base64.b64decode(image, validate=True))
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"无效图像：{exc}") from exc
        with self.lock:
            if any(process.poll() is None for process in self.processes.values()):
                raise ValueError("已有任务正在运行；请等待结束后提交下一项")
            run_id = resume_id or f"web-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
            if resume_id:
                project = self.project(resume_id)
                problem = resume_budget_problem(project)
                if problem:
                    raise ValueError(problem)
                if not (project / "artwork.json").is_file():
                    raise ValueError("任务尚未初始化，请重新提交创作要求")
            project = (RUNS / run_id).resolve()
            # inference.py requires a new project path to be absent or empty. Keep
            # the subprocess log in a sibling directory so it does not make the
            # project nonempty before `maliang init` runs.
            WEB_LOGS.mkdir(parents=True, exist_ok=True)
            input_path = None
            if image_bytes is not None:
                # Keep staging outside the new project: inference requires an empty directory.
                input_dir = RUNS / ".web-inputs"
                input_dir.mkdir(parents=True, exist_ok=True)
                input_path = input_dir / f"{run_id}.png"
                input_path.write_bytes(image_bytes)
            log = (WEB_LOGS / f"{run_id}.log").open("ab" if resume_id else "wb")
            command = [sys.executable, "-u", "inference.py", "--project", str(project)]
            if source is not None:
                command += ["--edit-from", str(source), "--revision", str(revision), "--prompt", prompt]
            else:
                command += ["--resume"] if resume_id else ["--task", kind, "--prompt", prompt]
            if not resume_id and source is None:
                command += ["--orientation", orientation, "--resolution", resolution]
                if duration is not None:
                    command += ["--duration", str(duration)]
                if fps is not None:
                    command += ["--fps", str(fps)]
                if input_path is not None:
                    command += ["--input-image", str(input_path)]
            if max_output_tokens is not None:
                command += ["--max-output-tokens", str(max_output_tokens)]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
            finally:
                log.close()
            self.processes[run_id] = process
            return run_id

    def project(self, run_id: str):
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", run_id):
            raise ValueError("无效任务 ID")
        project = RUNS / run_id
        if project.is_symlink() or (run_id not in self.processes and not project.is_dir()):
            raise ValueError("任务不存在")
        return project.resolve()

    def delete(self, run_id):
        with self.lock, PREVIEW_LOCK:
            project = self.project(run_id)
            if any(process.poll() is None for process in self.processes.values()):
                raise ValueError("有任务正在运行，请等待结束后再删除创作")
            # Staging and logs live outside the project. Never follow a replaced
            # staging directory into an unrelated location.
            extras = [WEB_LOGS / f"{run_id}.log", RUNS / ".web-inputs" / f"{run_id}.png"]
            for path in extras:
                if path.parent.is_symlink():
                    raise ValueError("任务附属目录是符号链接，无法安全删除")
            for path in extras:
                path.unlink(missing_ok=True)
            if project.exists():
                shutil.rmtree(project)
            self.processes.pop(run_id, None)


RUN_MANAGER = Runs()


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def resume_budget_problem(project):
    """Reject unchanged exhausted token budgets before spawning another paid run."""
    status = read_json(project / "status.json") or {}
    if status.get("status") != "budget_exhausted":
        return None
    from maliang.settings import load_harness_settings

    budget = load_harness_settings(ROOT / "harness.json").budget
    usage = read_json(project / "usage.json") or {}
    for field in ("input_tokens", "output_tokens"):
        limit = getattr(budget, f"max_{field}")
        if usage.get(field, 0) >= limit:
            return f"累计 {field} 已使用 {usage[field]:,}，当前预算 {limit:,}。请先在 harness.json 调整预算再续跑；现有输出已保留，续跑不会清零累计用量。"
    return None


def read_events(path: Path, offset: int):
    if not path.is_file():
        return [], 0
    events = []
    with path.open("rb") as stream:
        size = path.stat().st_size
        stream.seek(offset if 0 <= offset <= size else 0)
        while len(events) < 200:
            start = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                stream.seek(start)
                break
            try:
                events.append(json.loads(line))
            except (UnicodeDecodeError, ValueError):
                continue
        return events, stream.tell()


@lru_cache(maxsize=2048)
def media_digest(path: str, mtime_ns: int, size: int):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resolve_media_path(project, relative):
    path = (project / relative).resolve()
    # Older trace entries keep their original URL after delivery/archive moves.
    if (
        path.is_relative_to(project)
        and not path.is_file()
        and re.fullmatch(r"outputs/[a-f0-9]{32}\.mp4", relative)
    ):
        record = project / "evidence" / (path.stem + ".json")
        if record.is_file():
            data = json.loads(record.read_text())
            if data.get("paths"):
                path = (project / data["paths"][0]).resolve()
    return path


def event_media_hashes(project, events):
    for event in events:
        if event.get("event") != "evidence":
            continue
        hashes = {}
        for relative in event.get("paths", []):
            path = resolve_media_path(project, relative)
            if not path.is_relative_to(project) or not path.is_file():
                continue
            stat = path.stat()
            hashes[relative] = media_digest(str(path), stat.st_mtime_ns, stat.st_size)
        event["media_hashes"] = hashes


class Handler(BaseHTTPRequestHandler):
    def allowed_host(self):
        return self.headers.get("Host") in {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }

    def send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.allowed_host():
            return self.send(403, {"error": "Local access only"})
        if self.path not in {"/api/runs", "/api/preview", "/api/runs/delete"}:
            return self.send(404, {"error": "Not found"})
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self.send(403, {"error": "Cross-origin requests are disabled"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 14_000_000:
                raise ValueError("请求太大")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求必须为 JSON 对象")
            if self.path == "/api/runs/delete":
                if data.get("confirmed") is not True:
                    raise ValueError("请先确认删除")
                RUN_MANAGER.delete(data.get("run"))
                return self.send(200, {"deleted": data.get("run")})
            if self.path == "/api/preview":
                from maliang.editing import history_preview
                from maliang.store import ProjectStore

                if not PREVIEW_LOCK.acquire(blocking=False):
                    return self.send(409, {"error": "另一份预览正在渲染，请稍后重试"})
                try:
                    return self.send(
                        200,
                        history_preview(
                            ProjectStore(RUN_MANAGER.project(data.get("run"))), data.get("revision")
                        ),
                    )
                finally:
                    PREVIEW_LOCK.release()
            run_id = RUN_MANAGER.start(
                data.get("prompt", ""),
                data.get("kind", "image"),
                data.get("resume"),
                data.get("orientation", "auto"),
                data.get("resolution", "profile"),
                data.get("duration"),
                data.get("fps"),
                data.get("image"),
                data.get("edit_from"),
                data.get("revision"),
                data.get("max_output_tokens"),
            )
            self.send(201, {"run_id": run_id})
        except (ValueError, TypeError, KeyError, OSError) as exc:
            self.send(400, {"error": str(exc)})
        except Exception as exc:
            self.send(409, {"error": f"操作未完成，可重试：{type(exc).__name__}: {str(exc)[:500]}"})

    def do_GET(self):
        if not self.allowed_host():
            return self.send(403, {"error": "Local access only"})
        url = urlparse(self.path)
        if url.path in {"/", "/studio"}:
            page = "chat.html" if url.path == "/" else "index.html"
            return self.send(200, (ROOT / "web" / page).read_bytes(), "text/html; charset=utf-8")
        if url.path in {"/chat/app.js", "/chat/app.css", "/chat/app.js.LEGAL.txt"}:
            path = ROOT / "web" / url.path.lstrip("/")
            content_type = "text/javascript" if path.suffix == ".js" else "text/css" if path.suffix == ".css" else "text/plain"
            return self.send(200, path.read_bytes(), content_type + "; charset=utf-8")
        if url.path in {"/studio.js", "/studio.css"}:
            path = ROOT / "web" / url.path[1:]
            return self.send(
                200,
                path.read_bytes(),
                "text/javascript; charset=utf-8" if path.suffix == ".js" else "text/css; charset=utf-8",
            )
        if url.path == "/assets/logo.png":
            return self.send(200, (ROOT / "assets" / "logo.png").read_bytes(), "image/png")
        if url.path == "/api/pathtrace":
            from maliang.pathtrace import engine_status

            return self.send(200, engine_status())
        if url.path == "/api/paint":
            from maliang.paint import PRESETS
            from maliang.paint_native import native_status

            return self.send(200, {**native_status(), "brushes": PRESETS})
        if url.path == "/api/runs":
            projects = [
                p
                for p in RUNS.glob("*")
                if p.is_dir()
                and not p.is_symlink()
                and (p / "artwork.json").is_file()
                and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", p.name)
            ]
            projects.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            records = []
            for project in projects:
                edit = read_json(project / "edit.json")
                art = read_json(project / "artwork.json") or {}
                records.append(
                    {
                        "id": project.name,
                        "title": (edit["instruction"] if edit else art.get("prompt", project.name))[:70],
                        "is_edit": bool(edit),
                        "kind": "pathtrace" if (art.get("program") or {}).get("backend") == "pathtrace" or art.get("allowed_backends") == ["pathtrace"] else "paint" if (art.get("program") or {}).get("backend") == "paint" or art.get("allowed_backends") == ["paint"] else "video" if art.get("spec", {}).get("format") == "mp4" else "image",
                        "source_run": edit["source_run"] if edit else None,
                        "status": (read_json(project / "status.json") or {}).get("status"),
                    }
                )
            return self.send(200, {"runs": [p.name for p in projects], "records": records})
        query = parse_qs(url.query)
        run_id = query.get("run", [""])[0]
        try:
            project = RUN_MANAGER.project(run_id)
        except ValueError as exc:
            return self.send(404, {"error": str(exc)})
        if url.path in {"/api/process", "/api/step"}:
            from maliang.process_view import process_index

            try:
                index = process_index(project)
                return self.send(
                    200, index.read(query.get("id", [""])[0] if url.path == "/api/step" else None)
                )
            except (ValueError, OSError) as exc:
                return self.send(400, {"error": str(exc)})
        if url.path in {"/api/versions", "/api/version"}:
            from maliang.editing import existing_media, snapshot, version_index
            from maliang.store import ProjectStore

            try:
                store = ProjectStore(project)
                edit = read_json(project / "edit.json")
                if url.path == "/api/versions":
                    return self.send(200, {"versions": version_index(store), "edit": edit})
                revision = int(query.get("revision", [""])[0])
                art = snapshot(store, revision)
                from maliang.process_view import revision_changes

                base = int(query.get("base", [str(max(0, revision - 1))])[0])
                changes = revision_changes(store, art, base)
                sources = {}
                if art.program:
                    sources["program"] = store.path(art.program.path).read_text()
                for obj in art.objects:
                    if obj.draw:
                        sources[obj.id] = store.path(obj.draw.path).read_text()
                cached = read_json(store.path(f".history/{revision:06d}/preview.json")) or {}
                media = existing_media(store, revision)
                cached_media = [p for p in cached.get("media", []) if store.path(p["path"]).is_file()]
                return self.send(
                    200,
                    {
                        "artwork": art.model_dump(),
                        "sources": sources,
                        "changes": changes,
                        "media": cached_media + media,
                        "edit": edit,
                    },
                )
            except (ValueError, OSError) as exc:
                return self.send(400, {"error": str(exc)})
        if url.path == "/api/state":
            try:
                offset = int(query.get("offset", ["0"])[0])
            except ValueError:
                return self.send(400, {"error": "Invalid offset"})
            events, next_offset = (
                ([], offset)
                if query.get("compact") == ["1"]
                else read_events(project / "trace.jsonl", offset)
            )
            event_media_hashes(project, events)
            art = read_json(project / "artwork.json") or {}
            process = RUN_MANAGER.processes.get(run_id)
            status = read_json(project / "status.json") or {}
            exit_code = process.poll() if process else -1
            if not process and project.is_dir():
                from filelock import FileLock, Timeout

                try:
                    with FileLock(str(project / ".run.lock"), timeout=0):
                        pass
                except Timeout:
                    exit_code = None
            if exit_code is None and status.get("status") in {"failed", "budget_exhausted", "interrupted"}:
                status = {"status": "running", "revision": status.get("revision")}
            if exit_code is not None and status.get("status") in {None, "working"}:
                status = {
                    **status,
                    "status": "interrupted" if not process else "failed",
                    "reason": "进程已退出或服务已重启；请查看终端日志，可尝试续跑。",
                }
            log_path = WEB_LOGS / f"{run_id}.log"
            if not log_path.exists():
                log_path = project / "web.log"
            log = log_path.read_text(encoding="utf-8", errors="replace")[-80000:] if log_path.exists() else ""
            budget_problem = resume_budget_problem(project)
            return self.send(
                200,
                {
                    "revision": art.get("revision"),
                    "prompt": art.get("prompt", ""),
                    "budget": (read_json(project / "run_config.json") or {}).get("budget", {}),
                    "resume_block_reason": budget_problem,
                    "events": events,
                    "offset": next_offset,
                    "usage": read_json(project / "usage.json") or {},
                    "status": status,
                    "assets": art.get("assets", []),
                    "spec": art.get("spec"),
                    "video_plan": art.get("video_plan"),
                    "exit_code": exit_code,
                    "has_more": len(events) == 200,
                    "can_resume": (project / "artwork.json").exists()
                    and exit_code is not None
                    and status.get("status") not in {"completed", "baseline_exported"}
                    and not budget_problem,
                    "log": log,
                },
            )
        if url.path == "/media":
            relative = query.get("path", [""])[0]
            path = resolve_media_path(project, relative)
            if (
                not path.is_relative_to(project)
                or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".mp4"}
                or not path.is_file()
            ):
                return self.send(404, {"error": "Media not found"})
            size = path.stat().st_size
            start, end = 0, size - 1
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                if not match or not any(match.groups()):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(int(right), end) if right else end
                else:
                    start = max(0, size - int(right))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            self.send_response(206 if requested else 200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.send(404, {"error": "Not found"})


def main():
    parser = argparse.ArgumentParser(description="MaLiang-Harness local web dashboard")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"MaLiang-Harness Web: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
