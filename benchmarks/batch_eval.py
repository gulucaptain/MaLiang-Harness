#!/usr/bin/env python3
"""Plan, execute, resume and summarize cross-LLM Harness evaluations."""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .common import (
    BENCH,
    CODE,
    HARNESS,
    digest,
    identity,
    inside,
    load_cases,
    load_config,
    load_environment,
    read,
    source_fingerprint,
    write,
)


def now():
    return datetime.now(UTC).isoformat()


def batch_path(value):
    return Path(value).expanduser().resolve()


def prepare(config_path, batch, modality="all", model_ids=None, case_ids=None, limit=None, repeats=1, dataset=None):
    from maliang.models import Artwork

    if batch.exists():
        raise ValueError("Batch directory already exists; choose another or run it with --resume")
    config = load_config(config_path)
    models = config["models"]
    if model_ids:
        if set(model_ids) - {m["id"] for m in models}:
            raise ValueError("Unknown model ID")
        models = [m for m in models if m["id"] in model_ids]
    dataset_root = BENCH
    if dataset is None:
        cases = load_cases(modality, case_ids, limit)
    elif Path(dataset).is_dir():
        dataset_root = Path(dataset).resolve()
        cases = load_cases(modality, case_ids, limit, bench=dataset_root)
    else:
        from .datasets import load_tasks

        cases = load_tasks(dataset, modality, case_ids, limit)
        dataset_root = Path(dataset).resolve().parent
    if repeats < 1:
        raise ValueError("repeats must be positive")
    for c in cases:
        t = c["task"]
        Artwork(
            prompt=t["prompt"],
            spec=t["spec"],
            allowed_backends=t["allowed_backends"],
            workflow_policy=t.get("workflow_policy") or "guided",
            allow_generated_assets=t["allow_generated_assets"],
            requirements=t.get("initial_requirements", []),
        )
    batch.mkdir(parents=True)
    jobs = []
    for case in cases:
        task = copy.deepcopy(case["task"])
        # Freeze model-facing input bytes; the original baseline is never copied into a job.
        for asset in task["input_assets"]:
            original = inside(dataset_root, asset["path"])
            relative = f"inputs/{asset['sha256']}{original.suffix.lower()}"
            target = inside(batch, relative)
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(original.read_bytes())
            asset["path"] = relative
        for model in models:
            for repeat in range(1, repeats + 1):
                jid = f"{model['id']}/{task['modality']}/{task['case_id']}/repeat-{repeat:03d}"
                clean_job = dict(job_id=jid, task=task, model=model, harness=config["harness"], repeat=repeat)
                relative = f"jobs/{jid}/job.json"
                write(batch / relative, clean_job)
                jobs.append(
                    dict(
                        job_id=jid,
                        job_path=relative,
                        job_sha256=digest(batch / relative),
                        model_id=model["id"],
                        modality=task["modality"],
                        case_id=task["case_id"],
                        repeat=repeat,
                        baseline_path=case["baseline_path"],
                        baseline_sha256=case["baseline_sha256"],
                        historical_config=case["historical_config"],
                    )
                )
    plan = dict(
        schema_version=1,
        created_at=now(),
        config_source=str(Path(config_path).resolve()),
        env_file=config["env_file"],
        models=models,
        harness=config["harness"],
        jobs=jobs,
        source_files=source_fingerprint(),
        repeats=repeats,
        note="Shared Harness settings; optional historical baselines are external references, not controlled reruns.",
    )
    plan["fingerprint"] = identity(plan)
    write(batch / "plan.json", plan)
    return plan


def validate_plan(batch):
    plan = read(batch / "plan.json")
    if not plan:
        raise ValueError("plan.json missing")
    original = dict(plan)
    fingerprint = original.pop("fingerprint")
    if identity(original) != fingerprint:
        raise ValueError("Plan was modified; create a fresh batch")
    if plan["source_files"] != source_fingerprint():
        raise ValueError("Harness or evaluation scripts changed since planning; create a fresh batch")
    for row in plan["jobs"]:
        path = inside(batch, row["job_path"])
        if digest(path) != row["job_sha256"]:
            raise ValueError("Frozen job changed")
        task = read(path)["task"]
        for asset in task["input_assets"]:
            if digest(inside(batch, asset["path"])) != asset["sha256"]:
                raise ValueError("Frozen input changed")
    return plan


def attempts(batch, row):
    return sorted((batch / row["job_path"]).parent.glob("attempt-[0-9][0-9][0-9]"))


def jobs_to_run(batch, plan, resume, retry_failed):
    pending = []
    for row in plan["jobs"]:
        previous = attempts(batch, row)
        if previous and not resume:
            raise ValueError("Existing attempts; use --resume to skip recorded jobs")
        if previous:
            last = read(previous[-1] / "result.json", {})
            # Interrupted/failed attempts remain in the denominator; no implicit paid retries.
            if last.get("outcome") == "success" or not retry_failed:
                continue
        pending.append(row)
    return pending


def terminate_process(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def run_one(batch, row, env, timeout, stop, index=1, total=1, progress_interval=30):
    if stop.is_set():
        return "not_started"
    folder = (batch / row["job_path"]).parent
    number = max([int(p.name.split("-")[1]) for p in attempts(batch, row)] or [0]) + 1
    if number > 999:
        raise ValueError("Too many attempts")
    attempt = folder / f"attempt-{number:03d}"
    attempt.mkdir()
    started = time.monotonic()
    label = f"第 {index}/{total} 条 | {row['model_id']} | {row['case_id']}"
    print(f"[开始] {label} | 第 {number} 次尝试", flush=True)
    next_progress = started + progress_interval
    write(attempt / "attempt.json", dict(started_at=now(), hard_timeout_seconds=timeout, attempt=number))
    worker_env = dict(env)
    worker_env["PYTHONDONTWRITEBYTECODE"] = "1"
    worker_env["PYTHONUNBUFFERED"] = "1"
    # Keep subprocess temporary output in benchmarks as well.
    scratch = attempt / "tmp"
    scratch.mkdir()
    worker_env["TMPDIR"] = str(scratch)
    forced = None
    try:
        with (attempt / "worker.log").open("w") as log:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.worker",
                    "--job",
                    str(batch / row["job_path"]),
                    "--attempt",
                    str(attempt),
                    "--batch",
                    str(batch),
                ],
                cwd=HARNESS,
                env=worker_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            while proc.poll() is None:
                if stop.wait(0.2):
                    forced = "interrupted"
                elif time.monotonic() - started >= timeout:
                    forced = "timeout"
                if forced:
                    terminate_process(proc)
                    break
                if time.monotonic() >= next_progress:
                    print(f"[运行中] {label} | 已用 {time.monotonic() - started:.0f} 秒", flush=True)
                    next_progress = time.monotonic() + progress_interval
        result = read(attempt / "result.json", {})
        if forced or not result:
            from .common import extract_inference

            result.update(
                outcome=forced or "worker_crash",
                wall_seconds=time.monotonic() - started,
                usage=read(attempt / "project/usage.json", {}),
                output_path=result.get("output_path"),
            )
            extract_inference(attempt / "project", attempt / "inference.txt")
        elif proc.returncode != 0 and result.get("outcome") == "success":
            result["outcome"] = "worker_crash"
        result.update(exit_code=proc.returncode, finished_at=now(), attempt=number)
    except Exception as exc:
        result = dict(
            outcome="launch_error",
            error_type=type(exc).__name__,
            wall_seconds=time.monotonic() - started,
            attempt=number,
            finished_at=now(),
        )
    write(attempt / "result.json", result)
    return result["outcome"]


def execute(batch, resume=False, retry_failed=False, concurrency=1, timeout=2100, progress_interval=30):
    from filelock import FileLock

    from .report import generate_report

    if concurrency < 1 or timeout <= 0 or progress_interval <= 0:
        raise ValueError("concurrency, timeout and progress_interval must be positive")
    if retry_failed and not resume:
        raise ValueError("--retry-failed requires --resume")
    with FileLock(str(batch / ".batch.lock"), timeout=0):
        plan = validate_plan(batch)
        pending = jobs_to_run(batch, plan, resume, retry_failed)
        total = len(plan["jobs"])
        indexes = {row["job_id"]: i for i, row in enumerate(plan["jobs"], 1)}
        if not pending:
            generate_report(batch)
            print("No pending jobs. Report refreshed.")
            return 0
        env = load_environment(plan["env_file"])
        needed = {read(batch / r["job_path"])["model"]["api_key_env"] for r in pending}
        image = plan["harness"]["image_generation"]
        if image["enabled"] and any(
            read(batch / r["job_path"])["task"]["allow_generated_assets"] for r in pending
        ):
            needed.add(image["api_key_env"])
        missing = sorted(k for k in needed if not env.get(k, "").strip())
        if missing:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}. No jobs started.")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            chromium = env.get("MALIANG_CHROMIUM") or pw.chromium.executable_path
            if not Path(chromium).is_file():
                raise ValueError("Chromium missing: set MALIANG_CHROMIUM or install Playwright Chromium")
            env["MALIANG_CHROMIUM"] = chromium
            browser_env = {k: env[k] for k in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if k in env}
            try:
                browser = pw.chromium.launch(
                    executable_path=chromium, headless=True, chromium_sandbox=True, env=browser_env
                )
                browser.close()
            except Exception:
                raise ValueError(
                    "Chromium cannot launch; no model requests started. Run benchmarks/self_check.py "
                    "from a local terminal with browser execution permission."
                ) from None
        stop = threading.Event()
        old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        for sig in old:
            signal.signal(sig, lambda *_: stop.set())
        try:
            print(
                f"[批次] 共 {total} 条 | 本轮待运行 {len(pending)} 条 | 跳过 {total - len(pending)} 条"
                f" | 并发 {concurrency} | 真实API调用会产生费用。",
                flush=True,
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(
                        run_one,
                        batch,
                        row,
                        env,
                        timeout,
                        stop,
                        indexes[row["job_id"]],
                        total,
                        progress_interval,
                    ): row
                    for row in pending
                }
                finished_count = 0
                for future in concurrent.futures.as_completed(futures):
                    row = futures[future]
                    outcome = future.result()
                    if outcome == "not_started":
                        continue
                    finished_count += 1
                    result = read(attempts(batch, row)[-1] / "result.json", {})
                    label = "成功" if outcome == "success" else "未成功"
                    print(
                        f"[{label}] 第 {indexes[row['job_id']]}/{total} 条 | {row['case_id']}"
                        f" | 状态 {outcome} | 耗时 {result.get('wall_seconds', 0):.1f} 秒"
                        f" | 本轮已结束 {finished_count}/{len(pending)} 条",
                        flush=True,
                    )
                    if result.get("output_path"):
                        print(f"  输出：{batch / result['output_path']}", flush=True)
                    generate_report(batch)
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
            generate_report(batch)
        return 130 if stop.is_set() else 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Freeze cases and settings; no API calls")
    plan.add_argument("--config", type=Path, required=True)
    plan.add_argument("--dataset", type=Path, default=CODE / "examples/tasks.jsonl",
                      help="Task JSONL or legacy dataset directory; defaults to the small authored example")
    plan.add_argument("--batch", required=True, help="New output directory; may be outside the repository")
    plan.add_argument("--modality", choices=["all", "image", "video"], default="all")
    plan.add_argument("--models", nargs="+", help="Model IDs from config")
    plan.add_argument("--cases", nargs="+", help="Explicit case IDs")
    plan.add_argument("--limit", type=int, help="Global case limit in image-then-video order")
    plan.add_argument("--repeats", type=int, default=1)
    run = sub.add_parser("run", help="Execute an existing plan; invokes real APIs")
    run.add_argument("--batch", required=True)
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument(
        "--timeout", type=float, default=2100, help="Hard seconds per job, includes initialization"
    )
    run.add_argument(
        "--progress-interval", type=float, default=30, help="Seconds between running-status updates"
    )
    run.add_argument(
        "--resume", action="store_true", help="Skip already attempted jobs; continue unstarted jobs"
    )
    run.add_argument(
        "--retry-failed",
        action="store_true",
        help="With --resume, start fresh attempts for failed/interrupted jobs",
    )
    report = sub.add_parser("report", help="Refresh tables without model calls")
    report.add_argument("--batch", required=True)
    args = p.parse_args()
    try:
        batch = batch_path(args.batch)
        if args.command == "plan":
            value = prepare(
                args.config, batch, args.modality, args.models, args.cases, args.limit, args.repeats, args.dataset
            )
            from .report import generate_report

            generate_report(batch)
            print(f"Planned {len(value['jobs'])} jobs: {batch}/plan.json. No API calls made.")
        elif args.command == "run":
            return execute(
                batch, args.resume, args.retry_failed, args.concurrency, args.timeout, args.progress_interval
            )
        else:
            from .report import generate_report

            generate_report(batch)
            print(f"Report: {batch}/report.md")
        return 0
    except Exception as exc:
        # Config and local validation errors contain no secret values.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
