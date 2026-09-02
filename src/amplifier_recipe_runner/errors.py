"""Typed error model for the recipe runner.

Contract: ``recipe-runner-lib.v1`` Core 8 -- preflight failures (undeclared
agent, collision, trust refusal, provenance mismatch) are **distinct, typed,
and raised before any recipe step runs**. A missing artifact or a refused
dependency is a real result, never a fabricated success.

Every preflight error carries the structured facts a host needs to render an
actionable message: what was referenced, where it came from, and the remedy
(``recipe-dependency-manifest.v1`` Core 6 -- "fails naming the undeclared
reference and the remedy").

This module deliberately imports nothing from Amplifier: the error model is
part of the neutral public surface (lib Core 3).
"""

from __future__ import annotations

__all__ = [
    "AgentCollisionError",
    "LegacyRecipeError",
    "ManifestValidationError",
    "PreflightError",
    "ProvenanceMismatchError",
    "RecipeRunnerError",
    "TrustRefusedError",
    "UndeclaredAgentError",
]


class RecipeRunnerError(Exception):
    """Base class for every error this library raises.

    Hosts may catch this to distinguish runner failures from their own.
    """

    #: Human-actionable next step. Subclasses set a specific default.
    remedy: str | None = None

    def __init__(self, message: str, *, remedy: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if remedy is not None:
            self.remedy = remedy

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.remedy:
            return f"{self.message} Remedy: {self.remedy}"
        return self.message


class PreflightError(RecipeRunnerError):
    """Raised strictly before any recipe step executes and before side effects.

    Contract: lib Core 8 / manifest Core 6. Catching ``PreflightError`` is the
    supported way for a host to say "nothing ran".
    """


class UndeclaredAgentError(PreflightError):
    """A step referenced an agent no declared dependency supplies.

    Closed-world agent resolution (manifest Core 3): the caller session's agent
    map never satisfies a reference.
    """

    def __init__(
        self,
        agent: str,
        *,
        step_id: str | None = None,
        declared_agents: tuple[str, ...] = (),
        remedy: str | None = None,
    ) -> None:
        self.agent = agent
        self.step_id = step_id
        self.declared_agents = declared_agents
        where = f" referenced by step {step_id!r}" if step_id else ""
        super().__init__(
            f"Agent {agent!r}{where} is not supplied by any declared dependency.",
            remedy=remedy
            or (
                f"Declare a dependency that supplies {agent!r} in the recipe's "
                "`dependencies` block (and list it under `required_agents`)."
            ),
        )


class AgentCollisionError(PreflightError):
    """Two or more declared dependencies supply the same agent name.

    Contract: manifest Core 5 -- duplicate agent names across the dependency
    closure are a preflight ERROR, never resolved by precedence.
    """

    def __init__(
        self,
        agent: str,
        *,
        sources: tuple[str, ...] = (),
        remedy: str | None = None,
    ) -> None:
        self.agent = agent
        self.sources = sources
        listed = ", ".join(sources) if sources else "multiple dependencies"
        super().__init__(
            f"Agent name {agent!r} is supplied by more than one dependency: {listed}.",
            remedy=remedy
            or (
                "Remove or narrow one of the colliding dependencies; agent name "
                "collisions are never resolved by precedence."
            ),
        )


class TrustRefusedError(PreflightError):
    """The effective trust policy refused a source before fetch or activation.

    Contract: lib Core 6 / manifest Core 6 -- refusal happens *before* any
    remote fetch or module activation.
    """

    def __init__(
        self,
        source: str,
        *,
        reason: str | None = None,
        policy: str | None = None,
        remedy: str | None = None,
    ) -> None:
        self.source = source
        self.reason = reason
        self.policy = policy
        because = f" ({reason})" if reason else ""
        by = f" by policy {policy!r}" if policy else ""
        super().__init__(
            f"Trust policy refused dependency source {source!r}{because}{by}. "
            "Nothing was fetched or activated.",
            remedy=remedy
            or (
                "Supply a trust policy that permits this source, or pin it to an "
                "immutable ref the current policy allows."
            ),
        )


class ProvenanceMismatchError(PreflightError):
    """A resume (or locked run) resolved differently than the recorded run.

    Contract: manifest Core 8 -- a provenance mismatch fails visibly and never
    silently re-resolves.
    """

    def __init__(
        self,
        source: str,
        *,
        expected: str | None = None,
        actual: str | None = None,
        run_id: str | None = None,
        remedy: str | None = None,
    ) -> None:
        self.source = source
        self.expected = expected
        self.actual = actual
        self.run_id = run_id
        detail = ""
        if expected is not None or actual is not None:
            detail = f" expected {expected!r}, resolved {actual!r};"
        run = f" (run {run_id})" if run_id else ""
        super().__init__(
            f"Recorded provenance for {source!r} does not match{detail} refusing to "
            f"re-resolve silently{run}.",
            remedy=remedy
            or (
                "Resume against the recorded revision, or start a new run with "
                "`update-lock` if the change is intended."
            ),
        )


class LegacyRecipeError(PreflightError):
    """A legacy (pre-``schema_version: 2``) recipe was given to a surface that
    does not accept one.

    Contract: manifest Core 10 -- legacy recipes run ONLY through the embedded
    Amplifier tool adapter in explicitly labeled caller-bound mode; the
    standalone runner rejects them with an actionable error.
    """

    def __init__(
        self,
        recipe: str,
        *,
        reason: str | None = None,
        remedy: str | None = None,
    ) -> None:
        self.recipe = recipe
        self.reason = reason
        because = f" {reason}" if reason else ""
        super().__init__(
            f"Recipe {recipe!r} is a legacy recipe (no `schema_version: 2` "
            f"dependency manifest).{because}",
            remedy=remedy
            or (
                "Add `schema_version: 2` and a `dependencies` block, or run it "
                "through the Amplifier tool adapter's labeled legacy mode."
            ),
        )


class ManifestValidationError(PreflightError):
    """The recipe manifest failed strict parse/validation.

    Contract: manifest Core 1 -- unknown manifest keys are a parse ERROR, never
    silently ignored.
    """

    def __init__(
        self,
        message: str,
        *,
        recipe: str | None = None,
        location: str | None = None,
        remedy: str | None = None,
    ) -> None:
        self.recipe = recipe
        self.location = location
        where = f" at {location}" if location else ""
        in_recipe = f" in {recipe!r}" if recipe else ""
        super().__init__(
            f"Invalid recipe manifest{in_recipe}{where}: {message}",
            remedy=remedy or "Fix the manifest to match RECIPE_SCHEMA v2.",
        )
