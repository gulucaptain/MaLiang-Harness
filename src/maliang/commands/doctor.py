from __future__ import annotations

import os
import sys
from pathlib import Path

from .output import dump


def doctor(args, store=None, settings=None, settings_path=None):
    from importlib.metadata import version

    import deepagents

    from ..settings import load_harness_settings

    config_path = Path("harness.json")
    image_settings = (
        load_harness_settings(config_path).image_generation if config_path.is_file() else None
    )
    image_enabled = bool(image_settings and image_settings.enabled)

    from ..paint_native import native_status
    from ..pathtrace import engine_status

    dump(
        {
            "paint": native_status(),
            "pathtrace": engine_status(),
            "python": sys.version.split()[0],
            "deepagents": version("deepagents"),
            "deepagents_source": deepagents.__file__,
            "pyav": version("av"),
            "model_configured": bool(os.environ.get("MALIANG_MODEL")),
            "api_key_configured": bool(os.environ.get("OPENAI_API_KEY")),
            "image_model_configured": image_enabled or bool(os.environ.get("MALIANG_IMAGE_MODEL")),
            "image_provider": image_settings.provider if image_enabled else None,
            "image_api_key_configured": bool(os.environ.get(image_settings.api_key_env))
            if image_enabled
            else False,
        }
    )
    return

