from __future__ import annotations

import base64
import json
import os

from PIL import Image


class AssetGeneration:
    def generate(self, expected_revision, asset_id):
        art = self.store.load()
        if not art.allow_generated_assets:
            raise ValueError("Generated assets are forbidden for this task")
        if art.revision != expected_revision:
            raise ValueError("STALE_STATE")
        if asset_id in {a.id for a in art.assets}:
            raise ValueError("Asset ID already exists; reuse it or plan a new replacement")
        task = next((t for t in art.asset_tasks if t.asset_id == asset_id), None)
        if task is None:
            raise ValueError("ASSET_PLAN_REQUIRED: call plan_asset before generation")
        self.validate_component(art, task.model_dump())
        prompt = task.prompt
        if task.role == "background":
            if self.config.workflow != "code_directed":
                raise ValueError("Background plates require code_directed workflow")
            prompt += (
                " Generate a coherent environment plate, including shared ground, contact shadows and lighting. "
                "Do not generate the separately controlled actors listed below. Preserve their reserved empty spaces. "
                "No isolated-white-background product image. Layout positions use the target canvas coordinate system "
                f"{art.spec.width} by {art.spec.height}, scale proportionally to output resolution. "
                + json.dumps(task.background_layout.model_dump(), ensure_ascii=False)
            )
        if task.extraction == "white_background":
            prompt += (
                " Render ONLY the isolated subject, fully visible and centered, on a "
                "uniform pure white (#FFFFFF) seamless background. No floor, setting, "
                "shadow, text, watermark or other objects. Keep natural outer contours "
                "clearly separated from the white background."
            )
        if self.config.provider == "qwen":
            from ..qwen import generate as generate_qwen

            data, provenance = generate_qwen(self.context, task, prompt=prompt)
        else:
            from openai import OpenAI

            self.context.meter.consume("asset_api_calls")
            result = OpenAI(
                api_key=os.environ[self.config.api_key_env], timeout=self.config.timeout_seconds, max_retries=0
            ).images.generate(model=self.config.model, prompt=prompt, n=1)
            if not result.data or not result.data[0].b64_json:
                raise RuntimeError("OpenAI provider must return b64_json image data")
            data = base64.b64decode(result.data[0].b64_json)
            provenance = {"source": "openai_images", "model": self.config.model}
        updated = self.persist(
            data, asset_id, expected_revision, {**provenance, "asset_task": task.model_dump()}
        )
        with Image.open(self.store.path(updated["assets"][-1]["path"])) as image:
            dimensions = [image.width, image.height]
        self.store.log(
            "visual_operation",
            {
                "action": "generate_asset",
                "asset_id": asset_id,
                "revision": updated["revision"],
                "provider": self.config.provider,
            },
        )
        return {
            "revision": updated["revision"],
            "asset_id": asset_id,
            "dimensions": dimensions,
            "next": "inspect_asset -> assess layout -> place_background -> align code actors -> observe requirements"
            if task.role == "background"
            else "inspect_asset, compose using code, observe final requirements; not a final output",
        }


