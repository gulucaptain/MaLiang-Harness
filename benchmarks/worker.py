"""One isolated process per model/case/repeat. Invoked by batch_eval.py."""

import argparse
import importlib.metadata
import os
import shutil
import sys
import time
from pathlib import Path

from .common import digest, extract_inference, inside, model_client, read, validate_media, write


def initialize(task, store, batch):
    from PIL import Image

    from maliang.models import Artwork, Asset

    art = Artwork(
        prompt=task["prompt"],
        spec=task["spec"],
        workflow_policy=task.get("workflow_policy") or "guided",
        allowed_backends=task["allowed_backends"],
        allow_generated_assets=task["allow_generated_assets"],
        requirements=task.get("initial_requirements", []),
    )
    for asset in task["input_assets"]:
        source = inside(batch, asset["path"])
        if digest(source) != asset["sha256"]:
            raise ValueError("Frozen input hash mismatch")
        path, sha = store.blob(source.read_bytes(), source.suffix.lower())
        with Image.open(source) as im:
            provenance = dict(source="user_upload", width=im.width, height=im.height)
        art.assets.append(
            Asset(
                id=asset["id"], path=path, sha256=sha, media_type=asset["media_type"], provenance=provenance
            )
        )
    # No baseline source, plan, final requirements, generated assets, or GPT messages are loaded.
    store.create(art)


def execute(job, attempt, batch):
    from maliang.agent import make_context, run_agent
    from maliang.guidance import runtime_capabilities
    from maliang.models import Budget
    from maliang.store import ProjectStore

    started = time.monotonic()
    settings = job["harness"]
    project = attempt / "project"
    if project.exists():
        raise ValueError("Existing project refused; retries must use a fresh attempt")
    store = ProjectStore(project)
    result = dict(
        outcome="worker_error",
        harness_status=None,
        output_path=None,
        media_valid=False,
        self_reported_completed=False,
        usage={},
        error_type=None,
    )
    try:
        initialize(job["task"], store, batch)
        model = model_client(job["model"], settings["model"], os.environ)
        os.environ["MALIANG_MODEL"] = job["model"]["model"]
        # Existing capability disclosure checks these standard names. Keep an existing
        # OPENAI key (possibly used by the image tool); controller requests use explicit credentials.
        if os.environ.get(job["model"]["api_key_env"]):
            os.environ.setdefault("OPENAI_API_KEY", os.environ[job["model"]["api_key_env"]])
        write(
            project / "run_config.json",
            dict(
                mode="maliang",
                model=job["model"]["model"],
                model_options={
                    "timeout": settings["model"]["timeout_seconds"],
                    "max_retries": settings["model"]["max_retries"],
                    "max_tokens": settings["model"]["max_output_tokens_per_call"],
                },
                budget=settings["budget"],
                efficiency=settings["efficiency"],
                image_generation=settings["image_generation"],
                live_llm=True,
                bench_adapter=job["model"],
                harness_config=str(batch / "plan.json"),
            ),
        )
        context = make_context(project, Budget.model_validate(settings["budget"]), "maliang")
        write(project / "environment.json", runtime_capabilities(context))
        write(
            attempt / "runtime.json",
            {
                name: importlib.metadata.version(name)
                for name in ("langchain-openai", "langgraph", "deepagents", "pydantic", "av")
            },
        )
        run_agent(context, model)
        result["outcome"] = "not_completed"
    except Exception as exc:
        # Trace contains the harness diagnostic; do not echo provider exception payloads or credentials.
        result["error_type"] = type(exc).__name__
    finally:
        status = read(project / "status.json", {})
        validation = read(project / "validation.json", {})
        art = read(project / "artwork.json", {})
        result["harness_status"] = status.get("status")
        result["self_reported_completed"] = status.get("status") == "completed"
        result["usage"] = read(project / "usage.json", {})
        result["wall_seconds"] = time.monotonic() - started
        export_id = status.get("export_id")
        evidence = read(project / "evidence" / f"{export_id}.json", {}) if export_id else {}
        current = (
            evidence.get("revision")
            == art.get("revision")
            == status.get("revision")
            == validation.get("revision")
        )
        try:
            paths = [inside(project, p) for p in evidence.get("paths", [])]
            source = next(
                (p for p in paths if p.is_file() and p.suffix == "." + job["task"]["spec"]["format"]), None
            )
            if source:
                expected_sha = evidence.get("metadata", {}).get("sha256")
                if expected_sha and digest(source) != expected_sha:
                    raise ValueError("Export evidence hash mismatch")
                result["media"] = validate_media(source, job["task"]["spec"])
                result["media_valid"] = True
                target = attempt / ("output" + source.suffix)
                shutil.copy2(source, target)
                result["output_path"] = str(target.relative_to(batch))
                result["output_sha256"] = digest(target)
            checks = validation.get("technical_checks", [])
            result["technical_pass"] = bool(checks) and all(c.get("passed") for c in checks)
            result["eligible_to_finalize"] = validation.get("eligible_to_finalize", False)
            if (
                result["error_type"] is None
                and result["self_reported_completed"]
                and current
                and result["media_valid"]
                and result["technical_pass"]
                and result["eligible_to_finalize"]
            ):
                result["outcome"] = "success"
        except Exception as exc:
            result["outcome"] = "invalid_output"
            result["error_type"] = type(exc).__name__
        extract_inference(project, attempt / "inference.txt")
        write(attempt / "result.json", result)
    return 0 if result["outcome"] == "success" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    sys.exit(execute(read(args.job), args.attempt.resolve(), args.batch.resolve()))
