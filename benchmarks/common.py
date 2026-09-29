"""Local batch-evaluation utilities; no changes to the harness package."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

CODE = Path(__file__).resolve().parent
HARNESS = CODE.parent
BENCH = CODE / "datasets"
sys.dont_write_bytecode = True
sys.path.insert(0, str(HARNESS / "src"))


def read(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def digest(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", value):
        raise ValueError(f"Invalid ID: {value!r}")
    return value


def inside(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"Path escapes root: {relative}")
    return path


def jsonl(path):
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


def write_jsonl(path, rows):
    Path(path).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def source_fingerprint():
    paths = [
        p
        for p in (HARNESS / "src/maliang").rglob("*")
        if p.is_file() and p.suffix in {".py", ".js", ".json"} and "__pycache__" not in p.parts
    ]
    paths += list((HARNESS / "vendor/deepagents/libs/deepagents/deepagents").rglob("*.py"))
    paths += [HARNESS / n for n in ("harness.json", "pyproject.toml", "requirements/python312-snapshot.txt")]
    paths += list(CODE.rglob("*.py"))
    return {str(p.relative_to(HARNESS)): digest(p) for p in sorted(paths) if p.is_file()}


def load_environment(env_file=None):
    env = dict(os.environ)
    if env_file:
        for line in Path(env_file).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            key, sep, value = line.partition("=")
            key = key.strip()
            if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                parsed = " ".join(shlex.split(value, comments=True))
                if parsed and not env.get(key):
                    env[key] = parsed
    return env


def load_config(path):
    from maliang.settings import HarnessSettings

    path = Path(path).resolve()
    config = read(path)
    if not isinstance(config, dict) or set(config) - {"harness_config", "env_file", "models"}:
        raise ValueError("Config accepts harness_config, env_file, models only")
    hp = (path.parent / config.get("harness_config", str(HARNESS / "harness.json"))).resolve()
    settings = HarnessSettings.model_validate(read(hp)).model_dump()
    if settings["mode"] != "maliang":
        raise ValueError("Cross-LLM evaluation requires mode=maliang")
    models = config.get("models", [])
    if not models:
        raise ValueError("Configure at least one model")
    seen = set()
    allowed = {
        "id",
        "model",
        "api",
        "base_url",
        "api_key_env",
        "temperature",
        "parallel_tool_calls",
        "extra_body",
        "adapter",
    }
    for model in models:
        if set(model) - allowed:
            raise ValueError(f"Unknown model fields: {set(model) - allowed}; never put API keys in JSON")
        from .providers import ADAPTERS

        if model.get("adapter", "openai") not in ADAPTERS:
            raise ValueError("Unknown model adapter")
        mid = safe_id(model["id"])
        if mid in seen:
            raise ValueError("Duplicate model id")
        seen.add(mid)
        if not isinstance(model.get("model"), str) or not model["model"].strip():
            raise ValueError("Model name is required")
        if model.get("api") not in {"responses", "chat_completions"}:
            raise ValueError("api must be responses or chat_completions")
        adapter = model.get("adapter", "openai")
        if adapter in {"kimi_reasoning", "kimi_k3"} and model["api"] != "chat_completions":
            raise ValueError("Kimi reasoning adapters require chat_completions")
        if adapter == "text_only" and (model["model"] != "deepseek-v4-pro" or model["api"] != "responses"):
            raise ValueError("text_only adapter requires deepseek-v4-pro with responses")
        u = urlsplit(model.get("base_url", ""))
        if (
            u.scheme not in {"http", "https"}
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError("Explicit base_url required, without credentials/query/fragment")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", model.get("api_key_env", "")):
            raise ValueError("api_key_env must name an environment variable")
        if model.get("parallel_tool_calls") not in (None, True, False):
            raise ValueError("parallel_tool_calls must be true/false/null")
        if "extra_body" in model and not isinstance(model["extra_body"], dict):
            raise ValueError("extra_body must be an object containing only non-secret provider parameters")
        if "temperature" in model and (
            isinstance(model["temperature"], bool) or not isinstance(model["temperature"], (int, float))
        ):
            raise ValueError("temperature must be numeric, or omitted")
    env_path = config.get("env_file")
    if env_path:
        env_path = str((path.parent / env_path).resolve())
        if not Path(env_path).is_file():
            raise ValueError("env_file does not exist")
    return dict(models=models, harness=settings, env_file=env_path)


def load_cases(modality="all", case_ids=None, limit=None, bench=BENCH):
    cases = []
    for kind in ["image", "video"] if modality == "all" else [modality]:
        tasks = jsonl(bench / kind / "tasks.jsonl")
        manifests = jsonl(bench / kind / "manifest.jsonl")
        lookup = {r["case_id"]: r for r in manifests}
        if len(lookup) != len(manifests) or len({t["case_id"] for t in tasks}) != len(tasks):
            raise ValueError("Duplicate case IDs")
        dirs = {p.name for p in (bench / kind).iterdir() if p.is_dir() and not p.name.startswith(".")}
        if dirs != set(lookup) or dirs != {t["case_id"] for t in tasks}:
            raise ValueError(f"{kind} directories and indexes disagree; synchronize bench indexes first")
        for task in tasks:
            safe_id(task["case_id"])
            if task["modality"] != kind:
                raise ValueError("Modality mismatch")
            record = lookup[task["case_id"]]
            if read(inside(bench, record["task_path"])) != task:
                raise ValueError(f"task.json differs from tasks.jsonl: {task['case_id']}")
            baseline = None
            if record.get("baseline_path"):
                baseline = inside(bench, record["baseline_path"])
                if digest(baseline) != record["baseline_sha256"]:
                    raise ValueError(f"Baseline hash changed: {task['case_id']}")
            elif record.get("baseline_status") != "pending_generation" or record.get("baseline_sha256"):
                raise ValueError(
                    f"Missing baseline without explicit pending_generation status: {task['case_id']}"
                )
            for asset in task["input_assets"]:
                if digest(inside(bench, asset["path"])) != asset["sha256"]:
                    raise ValueError("Input asset hash mismatch")
            cases.append(
                dict(
                    task=task,
                    baseline_path=str(baseline) if baseline else None,
                    baseline_sha256=record.get("baseline_sha256"),
                    historical_config=record.get("original_config"),
                )
            )
    if case_ids:
        absent = set(case_ids) - {c["task"]["case_id"] for c in cases}
        if absent:
            raise ValueError(f"Unknown case IDs for this modality: {sorted(absent)}")
        cases = [c for c in cases if c["task"]["case_id"] in case_ids]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        cases = cases[:limit]
    if not cases:
        raise ValueError("No cases selected")
    return cases


def model_client(model, settings, env):
    from .providers import model_class

    ChatOpenAI = model_class(model.get("adapter", "openai"))
    key = env.get(model["api_key_env"], "").strip()
    if not key:
        raise ValueError(f"Missing environment variable {model['api_key_env']}")
    kwargs = dict(
        model=model["model"],
        api_key=key,
        base_url=model["base_url"],
        use_responses_api=model["api"] == "responses",
        timeout=settings["timeout_seconds"],
        max_retries=settings["max_retries"],
        max_tokens=settings["max_output_tokens_per_call"],
    )
    if model.get("parallel_tool_calls") is not None:
        kwargs["model_kwargs"] = {"parallel_tool_calls": model["parallel_tool_calls"]}
    for key in ("temperature", "extra_body"):
        if key in model:
            kwargs[key] = model[key]
    return ChatOpenAI(**kwargs)


def validate_media(path, spec):
    from PIL import Image

    path = Path(path)
    if spec["format"] == "png":
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            im.load()
            if im.format != "PNG" or im.size != (spec["width"], spec["height"]):
                raise ValueError("PNG format/dimensions mismatch")
        return {"decoded": True, "width": spec["width"], "height": spec["height"]}
    import av

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        if (stream.width, stream.height) != (spec["width"], spec["height"]):
            raise ValueError("Video dimensions mismatch")
        fps = float(stream.average_rate)
        duration = float(stream.duration * stream.time_base)
        if abs(fps - spec["fps"]) > 0.01 or abs(duration - spec["duration"]) > 1 / spec["fps"] + 0.01:
            raise ValueError("Video fps/duration mismatch")
        n = 0
        for _ in container.decode(video=0):
            n += 1
        if abs(n - round(spec["duration"] * spec["fps"])) > 1:
            raise ValueError("Decoded frame count mismatch")
        return dict(
            decoded=True, width=stream.width, height=stream.height, fps=fps, duration=duration, frames=n
        )


def extract_inference(project, dest):
    lines = ["Recorded model responses and tool calls (not unrecorded internal reasoning).", ""]
    trace = Path(project) / "trace.jsonl"
    if trace.exists():
        for line in trace.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                lines.append("[Incomplete final trace line]")
                continue
            if event["event"] in {"model_response", "tool_start", "model_error", "run_error", "run_end"}:
                lines.extend(
                    [f"=== {event['event']} ===", json.dumps(event, ensure_ascii=False, indent=2), ""]
                )
    Path(dest).write_text("\n".join(lines))
