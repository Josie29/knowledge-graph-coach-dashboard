"""Single switch point for the Claude model used by all agents.

Per CLAUDE.md: the model comes from ``settings.anthropic_model`` (default
Haiku while validating, Opus for demo runs) and is never hardcoded at a call
site. Haiku 4.5 supports neither the ``effort`` parameter nor adaptive
thinking, so those settings are applied only when the configured model
supports them — this is the one place that branches on the model.
"""

from __future__ import annotations

from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings

from app.config import ClaudeModel, settings

# Models that accept output_config.effort / adaptive thinking.
_EFFORT_CAPABLE = {ClaudeModel.OPUS_5, ClaudeModel.SONNET_5}


def build_model() -> AnthropicModel:
    """The configured Claude model, constructed per request cheaply."""
    return AnthropicModel(settings.anthropic_model.value)


def build_model_settings(
    *, effort: str | None = None, max_tokens: int = 4096
) -> AnthropicModelSettings:
    """Model settings with prompt caching on the static instruction preamble.

    Args:
        effort: Desired effort level for effort-capable models (the workout
            generator targets ~5s latency with ``"low"``). Silently dropped on
            Haiku, which rejects the parameter.
        max_tokens: Output budget for the call.
    """
    model_settings = AnthropicModelSettings(
        max_tokens=max_tokens,
        # The system/ontology preamble is stable across requests; caching it
        # is the main latency/cost lever (see issue #8).
        anthropic_cache_instructions=True,
    )
    if effort is not None and settings.anthropic_model in _EFFORT_CAPABLE:
        model_settings["anthropic_effort"] = effort
    return model_settings
