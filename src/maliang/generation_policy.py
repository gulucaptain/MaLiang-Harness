"""Auditable conservative routing; semantic necessity remains a model judgment."""


def validate_generation_plan(settings, prompt, components):
    if not settings.conservative_planning:
        return
    generated = [c for c in components if c["route"] != "code"]
    if len(generated) > settings.max_generated_components:
        raise ValueError(
            "GENERATION_RESTRAINT: too many generated components; prefer code or reuse a component"
        )
    normalized_prompt = " ".join(prompt.casefold().split())
    for component in generated:
        quote = " ".join(component.get("brief_evidence", "").casefold().split())
        # Quotation punctuation is presentation, not invented brief content.
        quote = quote.strip('"\'“”‘’「」『』`').strip()
        if not quote or quote not in normalized_prompt:
            raise ValueError(
                "GENERATION_JUSTIFICATION_REQUIRED: brief_evidence must quote the immutable user brief"
            )
        if not component.get("code_limitation", "").strip() or not component.get("minimal_scope", "").strip():
            raise ValueError(
                "GENERATION_JUSTIFICATION_REQUIRED: explain the specific code limitation and smallest necessary generated part"
            )
