from __future__ import annotations

from .output import dump


def review(args, store=None, settings=None, settings_path=None):
    from ..models import Review

    record = Review(
        requirement_id=args.requirement,
        revision=store.load().revision,
        evidence_ids=args.evidence,
        verdict=args.verdict,
        explanation=args.explanation,
        source="human",
    )
    store.add_review(record)
    dump(record.model_dump())
    return

