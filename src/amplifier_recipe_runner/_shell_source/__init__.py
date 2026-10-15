"""Recipe-owned compiler for context data embedded in trusted Bash templates."""

from __future__ import annotations

from collections.abc import Callable


def prepare(template: str, resolve: Callable[[str], str]) -> str:
    """Use the owning engine's unchanged STRING converter, once per hole."""
    from .profile import require_parser_profile

    require_parser_profile()
    from .binding import bind

    bound = bind(template, resolve)
    if not bound.occurrences:
        return template
    from .compiler import compile_bound

    return compile_bound(bound).source
