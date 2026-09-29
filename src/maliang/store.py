from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from filelock import FileLock

from .models import Artwork, Evidence, Review


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class ProjectStore:
    """Immutable content + snapshots. Single-run writer locking is owned by the runner."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.root / ".state.lock"))

    def path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("Path must remain inside this project")
        return path

    def load(self) -> Artwork:
        return Artwork.model_validate_json(self.path("artwork.json").read_text())

    def create(self, artwork: Artwork):
        with self.lock:
            if self.path("artwork.json").exists():
                raise ValueError("Project already exists; use run --resume or a new directory")
            self._commit(artwork)
        self.log("project_created", {"revision": 0})

    def _commit(self, artwork: Artwork):
        snapshot = self.path(f"versions/{artwork.revision:06d}.json")
        if snapshot.exists():
            raise ValueError("Revision already exists")
        atomic_json(snapshot, artwork.model_dump())
        atomic_json(self.path("artwork.json"), artwork.model_dump())
        atomic_json(self.path("status.json"), {"status": "working", "revision": artwork.revision})

    def mutate(self, expected_revision: int, change: Callable[[dict], None]) -> Artwork:
        with self.lock:
            current = self.load()
            if current.revision != expected_revision:
                raise ValueError(f"STALE_STATE: expected {expected_revision}, actual {current.revision}")
            data = current.model_dump()
            change(data)
            data["revision"] = current.revision + 1
            new = Artwork.model_validate(data)
            self._commit(new)
        self.log("revision", {"from": current.revision, "to": new.revision})
        return new

    def restore(self, revision: int, expected_revision: int) -> Artwork:
        if revision < 0:
            raise ValueError("Invalid revision")
        previous = Artwork.model_validate_json(self.path(f"versions/{revision:06d}.json").read_text())

        def change(data):
            # Task definition and capability permissions cannot be bypassed by restoring old content.
            for key in ("plan", "video_plan", "objects", "events", "assets", "asset_tasks", "program"):
                data[key] = previous.model_dump()[key]

        restored = self.mutate(expected_revision, change)
        self.log(
            "recovery",
            {
                "restored_from": revision,
                "revision": restored.revision,
                "next": "Re-render current revision and re-review requirements",
            },
        )
        return restored

    def blob(self, content: bytes, suffix: str) -> tuple[str, str]:
        if not re.fullmatch(r"\.[a-z0-9]+", suffix):
            raise ValueError("Invalid blob suffix")
        sha = digest(content)
        relative = f"blobs/{sha}{suffix}"
        path = self.path(relative)
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
        return relative, sha

    def log(self, event: str, details: dict):
        with self.lock, self.path("trace.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {"time": time.time(), "event": event, **details}, ensure_ascii=False, allow_nan=False
                )
                + "\n"
            )

    def add_evidence(self, evidence: Evidence):
        atomic_json(self.path(f"evidence/{evidence.id}.json"), evidence.model_dump())
        self.log("evidence", evidence.model_dump())

    def evidence(self, evidence_id: str) -> Evidence:
        if not re.fullmatch(r"[a-f0-9]{32}", evidence_id):
            raise ValueError("Invalid evidence ID")
        result = Evidence.model_validate_json(self.path(f"evidence/{evidence_id}.json").read_text())
        for path in result.paths:
            if not self.path(path).is_file():
                raise ValueError("Evidence file missing")
        return result

    def reusable_evidence(self, kind: str, **metadata) -> Evidence | None:
        """Find intact evidence at the current immutable artwork revision."""
        revision = self.load().revision
        for path in sorted(self.root.glob("evidence/*.json")):
            record = Evidence.model_validate_json(path.read_text())
            if record.revision != revision or record.kind != kind:
                continue
            if any(record.metadata.get(key) != value for key, value in metadata.items()):
                continue
            if not record.paths or not all(self.path(p).is_file() for p in record.paths):
                continue
            if record.metadata.get("sha256") and digest(self.path(record.paths[0]).read_bytes()) != record.metadata["sha256"]:
                continue
            self.log("evidence_reused", {"evidence_id": record.id, "kind": kind, "revision": revision})
            return record
        return None

    def publish_video(self, evidence_id: str):
        """Publish a verified delivery; retain candidates for trace replay."""
        evidence = self.evidence(evidence_id)
        if evidence.kind != "export" or not evidence.paths[0].endswith(".mp4"):
            return evidence
        if evidence.revision != self.load().revision:
            raise ValueError("STALE_EVIDENCE")
        relative = f"outputs/{evidence.id}.mp4"
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Previously delivered versions remain available as intermediate exports.
        for old in target.parent.glob("*.mp4"):
            if old == target:
                continue
            archive = self.path(f"exports/{old.name}")
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(archive))
            for record in self.root.glob("evidence/*.json"):
                ev = Evidence.model_validate_json(record.read_text())
                old_relative = str(old.relative_to(self.root))
                if old_relative in ev.paths:
                    ev.paths = [str(archive.relative_to(self.root)) if p == old_relative else p for p in ev.paths]
                    self.add_evidence(ev)
        if self.path(evidence.paths[0]) != target:
            shutil.copy2(self.path(evidence.paths[0]), target)
        evidence.paths = [relative]
        self.add_evidence(evidence)
        return evidence

    def add_review(self, review: Review):
        art = self.load()
        if review.revision != art.revision:
            raise ValueError("Review is for an outdated revision")
        if review.requirement_id not in {r.id for r in art.requirements}:
            raise ValueError("Unknown requirement")
        requirement = next(r for r in art.requirements if r.id == review.requirement_id)
        if review.verdict == "pass" and requirement.required_capability == "audio_track":
            raise ValueError("Audio-track output is unavailable; record fail/uncertain or finish_draft")
        times = set()
        bound = []
        guided_visual = art.workflow_policy == "guided" and requirement.kind in {
            "visual",
            "style",
            "temporal",
        }
        for eid in review.evidence_ids:
            evidence = self.evidence(eid)
            if evidence.revision != art.revision:
                raise ValueError("STALE_EVIDENCE: render the current revision first")
            is_bound = (
                evidence.kind in {"frames", "crop"}
                and evidence.metadata.get("requirement_id") == requirement.id
            )
            if is_bound:
                bound.append(eid)
            # Supplementary exports/crops/decoded video can support a review, but cannot
            # replace its requirement observation or supply missing temporal samples.
            if is_bound or not guided_visual:
                times.update(evidence.timestamps)
        if guided_visual and review.verdict == "pass" and not bound:
            available = []
            for path in self.root.glob("evidence/*.json"):
                ev = json.loads(path.read_text())
                if (
                    ev.get("revision") == art.revision
                    and ev.get("kind") in {"frames", "crop"}
                    and ev.get("metadata", {}).get("requirement_id") == requirement.id
                ):
                    available.append(ev["id"])
            raise ValueError(
                f"Pass requires evidence tied to this requirement. Include one of {available}, or call observe_requirement(requirement_id={requirement.id!r}). Other current evidence may accompany it."
            )
        if requirement.kind == "temporal" and review.verdict == "pass" and len(times) < 3:
            raise ValueError(
                "Temporal pass needs at least three distinct sampled times from requirement-bound observations; still not proof of continuity"
            )
        atomic_json(self.path(f"reviews/{art.revision}-{review.requirement_id}.json"), review.model_dump())
        # A later review must invalidate a previously completed result.
        atomic_json(self.path("status.json"), {"status": "working", "revision": art.revision})
        self.log("review", review.model_dump())

    def reviews(self) -> dict[str, Review]:
        art = self.load()
        return {
            r.requirement_id: r
            for p in self.root.glob(f"reviews/{art.revision}-*.json")
            if (r := Review.model_validate_json(p.read_text()))
        }

    def new_id(self) -> str:
        return uuid.uuid4().hex
