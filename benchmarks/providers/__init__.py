"""Transport differences only: scheduling, accounting and reports remain shared."""

from importlib import import_module

ADAPTERS = {
    "openai": ("langchain_openai", "ChatOpenAI"),
    "kimi_reasoning": ("benchmarks.providers.kimi_reasoning", "KimiReasoningChatOpenAI"),
    "kimi_k3": ("benchmarks.providers.kimi_k3", "KimiK3ChatOpenAI"),
    "text_only": ("benchmarks.providers.text_only", "TextOnlyChatOpenAI"),
}


def model_class(adapter: str):
    """Resolve an explicit supported adapter; never infer it from a model ID."""
    if adapter not in ADAPTERS:
        raise ValueError(f"Unknown model adapter: {adapter}")
    module, name = ADAPTERS[adapter]
    return getattr(import_module(module), name)
