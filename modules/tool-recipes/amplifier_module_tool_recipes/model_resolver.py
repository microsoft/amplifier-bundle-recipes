"""Model pattern resolution using glob matching.

Resolves model patterns (e.g., "claude-sonnet-*") to concrete model names
by matching against available models from the provider.
"""

import fnmatch
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ModelResolutionResult:
    """Result of model pattern resolution."""

    def __init__(
        self,
        resolved_model: str,
        pattern: str | None,
        available_models: list[str] | None,
        matched_models: list[str] | None,
    ):
        self.resolved_model = resolved_model
        self.pattern = pattern  # Original pattern (None if not a pattern)
        self.available_models = available_models  # All available from provider
        self.matched_models = matched_models  # Models that matched pattern


def is_glob_pattern(model_hint: str) -> bool:
    """Check if model_hint contains glob pattern characters."""
    return any(c in model_hint for c in "*?[")


async def resolve_model_pattern(
    model_hint: str,
    provider_name: str | None,
    coordinator: Any,
) -> ModelResolutionResult:
    """Resolve a model pattern to a concrete model name.

    Args:
        model_hint: Exact model name or glob pattern (e.g., "claude-sonnet-4-5-*")
        provider_name: Provider to query for available models (e.g., "anthropic")
        coordinator: Amplifier coordinator for accessing providers

    Returns:
        ModelResolutionResult with resolved model and resolution metadata

    Resolution strategy:
    1. If not a glob pattern, return as-is
    2. Query provider for available models
    3. Filter with fnmatch
    4. Sort descending (latest date/version wins)
    5. Return first match, or original if no matches
    """
    # Check if it's a pattern
    if not is_glob_pattern(model_hint):
        logger.debug("Model '%s' is not a pattern, using as-is", model_hint)
        return ModelResolutionResult(
            resolved_model=model_hint,
            pattern=None,
            available_models=None,
            matched_models=None,
        )

    # Need provider to resolve pattern
    if not provider_name:
        logger.warning(
            "Model pattern '%s' specified but no provider - cannot resolve, using as-is",
            model_hint,
        )
        return ModelResolutionResult(
            resolved_model=model_hint,
            pattern=model_hint,
            available_models=None,
            matched_models=None,
        )

    # Try to get available models from provider
    available_models: list[str] = []
    try:
        # Get provider from coordinator
        providers = coordinator.get("providers")
        if providers:
            # providers is a dict of mounted providers by name
            provider = None
            for name, p in providers.items():
                # Match by name (flexible: "anthropic", "provider-anthropic", etc.)
                if provider_name in (
                    name,
                    name.replace("provider-", ""),
                    f"provider-{provider_name}",
                ):
                    provider = p
                    break

            if provider and hasattr(provider, "list_models"):
                models = await provider.list_models()
                # Handle both list of strings and list of model objects
                available_models = [
                    m if isinstance(m, str) else getattr(m, "id", str(m))
                    for m in models
                ]
                logger.debug(
                    "Provider '%s' has %d available models",
                    provider_name,
                    len(available_models),
                )
            else:
                logger.debug(
                    "Provider '%s' does not support list_models()",
                    provider_name,
                )
    except Exception as e:
        logger.warning(
            "Failed to query models from provider '%s': %s",
            provider_name,
            e,
        )

    if not available_models:
        logger.warning(
            "No available models from provider '%s' for pattern '%s' - using pattern as-is",
            provider_name,
            model_hint,
        )
        return ModelResolutionResult(
            resolved_model=model_hint,
            pattern=model_hint,
            available_models=[],
            matched_models=[],
        )

    # Match pattern against available models
    matched = fnmatch.filter(available_models, model_hint)

    if not matched:
        logger.warning(
            "Pattern '%s' matched no models from provider '%s'. "
            "Available: %s. Using pattern as-is.",
            model_hint,
            provider_name,
            ", ".join(available_models[:10])
            + ("..." if len(available_models) > 10 else ""),
        )
        return ModelResolutionResult(
            resolved_model=model_hint,
            pattern=model_hint,
            available_models=available_models,
            matched_models=[],
        )

    # Sort descending (latest date/version typically sorts last alphabetically,
    # so reverse sort puts newest first)
    matched.sort(reverse=True)
    resolved = matched[0]

    logger.info(
        "Resolved model pattern '%s' -> '%s' (matched %d of %d available: %s)",
        model_hint,
        resolved,
        len(matched),
        len(available_models),
        ", ".join(matched[:5]) + ("..." if len(matched) > 5 else ""),
    )

    return ModelResolutionResult(
        resolved_model=resolved,
        pattern=model_hint,
        available_models=available_models,
        matched_models=matched,
    )
