"""Kimi K3 uses the shared reasoning-history transport."""
from .kimi_reasoning import KimiReasoningChatOpenAI


class KimiK3ChatOpenAI(KimiReasoningChatOpenAI):
    """Named adapter retained for explicit per-model configuration."""
