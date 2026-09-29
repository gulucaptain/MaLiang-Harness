from __future__ import annotations

from ..store import ProjectStore
from .output import dump


def reuse(args, store=None, settings=None, settings_path=None):
    from filelock import FileLock

    from ..reuse import reuse_content

    with FileLock(str(store.root / ".run.lock"), timeout=0):
        dump(
            reuse_content(
                store, ProjectStore(args.source_project), store.load().revision
            ).model_dump()
        )
    return

