"""Incremental, read-only projection of trace events into inspectable steps.

Only explicit model text and recorded tool arguments are explanations. No hidden
reasoning is inferred. IDs are byte offsets, stable across dashboard restarts.
"""

from __future__ import annotations

import copy
import difflib
import json
import threading
from pathlib import Path

TITLES = {
    "set_pathtrace_scene": "构建写实场景",
    "edit_pathtrace_scene": "调整写实场景",
    "init_painting": "创建笔触画布",
    "paint_strokes": "绘制笔触",
    "set_paint_layer": "调整绘画图层",
    "remove_paint_strokes": "移除笔触并重画",
    "plan_creation": "规划作品",
    "read_artwork": "读取作品状态",
    "write_program": "编写绘制程序",
    "patch_program": "修改绘制代码",
    "put_object": "绘制对象",
    "edit_object": "调整对象",
    "animate_object": "调整动画轨迹",
    "render_frames": "渲染画面",
    "render_clip": "预览视频片段",
    "observe_requirement": "观察画面",
    "observe_requirements": "检查创作要求",
    "record_review": "记录评审结论",
    "complete_checkpoint": "检查阶段目标",
    "export_artifact": "导出作品",
    "finalize_artwork": "验收交付",
    "inspect_video": "检查编码视频",
    "generate_asset": "生成素材",
    "inspect_asset": "查看素材",
    "inspect_assets": "批量查看素材",
    "plan_asset": "规划素材",
    "place_background": "合成背景",
    "compose_asset": "合成素材",
    "restore_version": "恢复历史内容",
    "compare_versions": "比较版本",
    "inspect_edit_source": "查看编辑起点",
    "revise_edit_requirement": "调整编辑要求",
    "finish_draft": "保存草稿",
}
CONTAINERS = {"execute_steps", "resume_steps"}


def explicit_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") in {"text", "output_text"}
        )
    return ""


class ProcessIndex:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.offset = 0
        self.inode = None
        self.steps = {}
        self.stack = []
        self.model = None
        self.revision = 0
        self.statement = ""

    def add(self, offset, event, kind, title):
        key = str(offset)
        step = {
            "id": key,
            "kind": kind,
            "title": title,
            "tool": event.get("tool"),
            "time": event.get("time"),
            "before_revision": self.revision,
            "revision": self.revision,
            "status": "running",
            "summary": "",
            "explanation_source": "",
            "events": [],
            "media": [],
        }
        self.steps[key] = step
        return step

    def consume(self, offset, event):
        kind = event.get("event")
        if kind == "project_created":
            self.revision = event.get("revision", 0)
        if kind == "model_start":
            step = self.add(offset, event, "model", f"模型决策 · 第 {event.get('call', '?')} 轮")
            self.model = step["id"]
            self.statement = ""
        elif kind == "model_response" and self.model:
            step = self.steps[self.model]
            self.statement = explicit_text(event.get("content"))
            step["summary"] = self.statement or "选择工具：" + "、".join(
                c.get("name", "") for c in event.get("tool_calls", [])
            )
            step["explanation_source"] = (
                "模型公开说明" if self.statement else "工具选择记录（未提供文字说明）"
            )
            step["events"].append(event)
            step["status"] = "ok"
        elif kind in {"model_end", "model_error"} and self.model:
            step = self.steps[self.model]
            step["status"] = "error" if kind == "model_error" else "ok"
            step["events"].append(event)
            self.model = None
        elif kind == "tool_start":
            self.revision = event.get("revision", self.revision)
            name = event.get("tool", "")
            args = event.get("arguments") or {}
            step = self.add(offset, event, "tool", TITLES.get(name, name))
            if args.get("object_id"):
                step["title"] += " · " + str(args["object_id"])
            explanation = args.get("reason") or args.get("capability_assessment") or args.get("explanation")
            step["summary"] = explanation or self.statement or "本步骤未记录文字说明；可查看实际参数与结果。"
            step["explanation_source"] = (
                "工具参数中的操作说明"
                if explanation
                else "本轮模型公开说明"
                if self.statement
                else "无文字说明"
            )
            step["events"].append(event)
            step["container"] = name in CONTAINERS
            self.stack.append(step["id"])
        elif kind == "revision":
            self.revision = event["to"]
            for key in self.stack:
                self.steps[key]["revision"] = self.revision
        if self.stack and kind != "tool_start":
            step = self.steps[self.stack[-1]]
            step["events"].append(event)
            if kind in {"evidence", "evidence_reused"}:
                evidence = event
                if kind == "evidence_reused":
                    eid = event.get("evidence_id", "")
                    if isinstance(eid, str) and eid.isalnum():
                        path = self.root / "evidence" / (eid + ".json")
                        if path.is_file():
                            evidence = json.loads(path.read_text())
                for i, path in enumerate(evidence.get("paths", [])):
                    times = evidence.get("timestamps", [])
                    step["media"].append(
                        {
                            "path": path,
                            "time": times[i] if i < len(times) else None,
                            "kind": evidence.get("kind"),
                            "revision": evidence.get("revision"),
                            "evidence_id": evidence.get("id"),
                            "scope": evidence.get("metadata", {}).get("scope"),
                            "crop": evidence.get("kind") == "crop"
                            or bool(evidence.get("metadata", {}).get("box"))
                            or any(evidence.get("metadata", {}).get("regions") or []),
                        }
                    )
            if kind == "tool_end":
                # Nested batches are sequential and close in reverse stack order.
                name = event.get("tool")
                if name == step["tool"]:
                    result = event.get("result")
                    step["status"] = (
                        "error"
                        if event.get("status") == "error"
                        or (isinstance(result, dict) and result.get("error"))
                        else "ok"
                    )
                    step["error"] = event.get("error") or (
                        result.get("error") if isinstance(result, dict) else None
                    )
                    step["seconds"] = event.get("seconds")
                    self.stack.pop()
        if kind in {"run_end", "run_error", "budget_exhausted"}:
            for key in self.stack:
                self.steps[key]["status"] = "interrupted"
            self.stack.clear()
            if self.model:
                self.steps[self.model]["status"] = "interrupted"
                self.model = None

    def refresh(self):
        path = self.root / "trace.jsonl"
        if not path.exists():
            return
        stat = path.stat()
        if stat.st_ino != self.inode or stat.st_size < self.offset:
            self.reset()
            self.inode = stat.st_ino
        with path.open("rb") as stream:
            stream.seek(self.offset)
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line.endswith(b"\n"):
                    break
                self.offset = stream.tell()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                self.consume(offset, event)

    def read(self, step_id=None):
        with self.lock:
            self.refresh()
            if step_id is not None:
                if step_id not in self.steps:
                    raise ValueError("步骤不存在")
                return copy.deepcopy(self.steps[step_id])
            return {
                "cursor": self.offset,
                "steps": [
                    {k: v for k, v in s.items() if k not in {"events", "media"}}
                    | {"summary": s["summary"][:220], "media_count": len(s["media"])}
                    for s in self.steps.values()
                    if not s.get("container") or s["status"] == "error"
                ],
            }


_INDEXES = {}
_INDEX_LOCK = threading.Lock()


def process_index(root):
    key = str(Path(root).resolve())
    with _INDEX_LOCK:
        if key not in _INDEXES:
            # Bound memory across browsing many projects. Eviction only drops a read cache.
            if len(_INDEXES) >= 8:
                del _INDEXES[next(iter(_INDEXES))]
            _INDEXES[key] = ProcessIndex(root)
        return _INDEXES[key]


def revision_changes(store, art, base_revision):
    from .editing import snapshot

    before = snapshot(store, base_revision)

    def sources(value):
        result = {}
        if value.program:
            result["program"] = store.path(value.program.path).read_text()
        for obj in value.objects:
            if obj.draw:
                result["object/" + obj.id] = store.path(obj.draw.path).read_text()
        # Transform / motion are executed by the renderer outside object source.
        result["scene.json"] = json.dumps(
            {
                "objects": [o.model_dump(exclude={"draw"}) for o in value.objects],
                "events": [e.model_dump() for e in value.events],
            },
            ensure_ascii=False,
            indent=2,
        )
        return result

    old, new = sources(before), sources(art)
    changes = []
    for name in sorted(old.keys() | new.keys()):
        left, right = old.get(name, ""), new.get(name, "")
        if left == right:
            continue
        diff = "\n".join(
            difflib.unified_diff(
                left.splitlines(),
                right.splitlines(),
                fromfile=f"v{base_revision}/{name}",
                tofile=f"v{art.revision}/{name}",
                lineterm="",
            )
        )
        changes.append({"name": name, "diff": diff})
    return {"base_revision": base_revision, "revision": art.revision, "files": changes}
