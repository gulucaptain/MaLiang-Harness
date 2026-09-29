"""Bounded DashScope async image adapter. Persist job IDs to avoid resubmitting on timeout."""

from __future__ import annotations

import math
import os
import time
from urllib.parse import quote, urlsplit

import httpx

from ..store import atomic_json, digest


def generate(context, task, prompt=None):
    cfg, store = context.image_generation, context.store
    key = os.environ.get(cfg.api_key_env)
    if not key:
        raise ValueError(f"Missing image credential environment variable: {cfg.api_key_env}")
    size = cfg.size
    if task.role == "background":
        spec = store.load().spec
        supported = ["1664*928", "928*1664", "1328*1328", "1472*1140", "1140*1472"]
        size = min(
            supported,
            key=lambda item: abs(
                math.log((int(item.split("*")[0]) / int(item.split("*")[1])) / (spec.width / spec.height))
            ),
        )
    request = {
        "model": cfg.model,
        "input": {"prompt": prompt or task.prompt},
        "parameters": {"size": size, "n": 1, "prompt_extend": cfg.prompt_extend},
    }
    import json

    fingerprint = digest(json.dumps([cfg.base_url, request], sort_keys=True).encode())
    record_path = store.path(f"asset_jobs/{task.asset_id}-{fingerprint}.json")
    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    deadline = time.monotonic() + cfg.timeout_seconds

    def remaining():
        seconds = min(deadline - time.monotonic(), context.meter.remaining_seconds())
        if seconds <= 0:
            raise TimeoutError(
                "Image job timed out; retry the SAME asset_id to poll its saved job, not regenerate"
            )
        return max(0.01, min(seconds, 30))

    def payload(response):
        # Do not include request headers, signed URLs, or raw provider messages in logs/errors.
        if response.status_code != 200:
            raise RuntimeError(f"Qwen API HTTP {response.status_code}; check region, key, model and quota")
        return response.json().get("output", {})

    try:
        with httpx.Client(headers={"Authorization": f"Bearer {key}"}, follow_redirects=False) as client:
            if not record:
                context.meter.consume("asset_api_calls")
                # An uncertain POST must never be automatically retried and charged again.
                atomic_json(record_path, {"status": "submission_uncertain"})
                output = payload(
                    client.post(
                        cfg.base_url.rstrip("/") + "/services/aigc/text2image/image-synthesis",
                        headers={"X-DashScope-Async": "enable"},
                        json=request,
                        timeout=remaining(),
                    )
                )
                if not output.get("task_id"):
                    raise RuntimeError(
                        "Qwen returned no task ID; submission will not be automatically retried"
                    )
                record = {"task_id": output["task_id"], "status": "submitted"}
                atomic_json(record_path, record)
            if not record.get("task_id"):
                raise RuntimeError(
                    "Previous submission outcome is uncertain; inspect provider history before a new asset ID"
                )
            while True:
                output = payload(
                    client.get(
                        cfg.base_url.rstrip("/") + "/tasks/" + quote(record["task_id"], safe=""),
                        timeout=remaining(),
                    )
                )
                status = output.get("task_status")
                if status == "SUCCEEDED":
                    results = output.get("results", [])
                    if not results or not results[0].get("url"):
                        raise RuntimeError("Qwen succeeded without an image URL")
                    url = results[0]["url"]
                    if urlsplit(url).scheme != "https":
                        raise ValueError("Qwen image download requires HTTPS")
                    break
                if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                    atomic_json(record_path, {**record, "status": status})
                    raise RuntimeError(
                        f"Qwen job {status}; use a new asset ID only for an intentional new attempt"
                    )
                time.sleep(min(cfg.poll_interval_seconds, remaining()))
        # Separate client: never forward the API credential to the object-storage download host.
        with httpx.Client(follow_redirects=False) as download:
            with download.stream("GET", url, timeout=remaining()) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"Image download HTTP {response.status_code}; retry same asset ID")
                data = bytearray()
                for chunk in response.iter_bytes():
                    remaining()
                    data.extend(chunk)
                    if len(data) > 20_000_000:
                        raise ValueError("Asset exceeds 20 MB")
        atomic_json(record_path, {**record, "status": "downloaded"})
        return bytes(data), {
            "source": "qwen",
            "model": cfg.model,
            "task_id": record["task_id"],
            "size": size,
            "prompt_extend": cfg.prompt_extend,
        }
    except httpx.HTTPError:
        raise RuntimeError(
            "Qwen network/timeout error; check proxy and endpoint, then retry the same asset ID"
        ) from None
