"""A step whose execution errored is never a completed step (lib.v1 Core 8).

recipes-30w: a v2 run reported ``status: completed`` with its only agent step
listed in ``completed_steps`` while that step had actually errored -- the error
showed up only inside a summary. The executor caught exactly two typed
exception families, so any other failure (what a real spawn backend raises,
e.g. ``RuntimeError("No providers available")``) escaped the step loop
entirely and left the caller to decide what had completed.

The distinction these tests defend, which the existing typed-error tests in
``test_execution.py`` do not: an **untyped** step failure must land in the same
place a typed one does -- status FAILED, the error at the top level of the
result, and ``completed_steps`` holding only the steps that really finished.

Async is driven with ``asyncio.run`` rather than a pytest plugin, matching the
rest of this suite: the library's tests take no plugin dependency.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import MappingProxyType

from amplifier_recipe_runner.api import RunRequest
from amplifier_recipe_runner.api import RunStatus
from amplifier_recipe_runner.execution import run as run_recipe
from amplifier_recipe_runner.ports import HostServices
from amplifier_recipe_runner.ports import WorkspacePath
from amplifier_recipe_runner.resolver import ResolvedAgent
from amplifier_recipe_runner.resolver import ResolvedBundle

RECIPE = """\
schema_version: 2
name: two-step
dependencies:
  - source: "bundles/supplier"
    kind: bundle
steps:
  - id: first
    agent: "supplier:reviewer"
    instruction: "Review it"
  - id: second
    agent: "supplier:reviewer"
    instruction: "Review it again"
"""


class StubProviderAccess:
    def roles(self):
        return ("default",)

    def resolve(self, role: str):
        return ()


class StubResolver:
    """Resolves the declared dependency without touching a filesystem."""

    async def resolve(self, dependency, workspace=None):
        return ResolvedBundle(
            source=dependency.source,
            kind=dependency.kind,
            namespace="supplier",
            agents=MappingProxyType({"supplier:reviewer": ResolvedAgent(name="supplier:reviewer")}),
            content_digest="sha256:stub",
        )


class ExplodingBackend:
    """Fails the way a real spawn fails: a plain, untyped error.

    ``fail_on`` is the zero-based index of the call that raises; ``-1`` never
    fails, so the same backend proves the honest-success direction too.
    """

    def __init__(self, fail_on: int = 0) -> None:
        self.fail_on = fail_on
        self.calls = 0

    async def spawn(self, request):
        index = self.calls
        self.calls += 1
        if index == self.fail_on:
            raise RuntimeError("No providers available")
        return "ok"


def _run(tmp_path: Path, backend: ExplodingBackend):
    recipe = tmp_path / "two-step.yaml"
    recipe.write_text(RECIPE, encoding="utf-8")
    request = RunRequest(
        recipe=recipe,
        services=HostServices(
            provider_access=StubProviderAccess(),
            workspace=WorkspacePath(tmp_path),
        ),
    )
    return asyncio.run(run_recipe(request, resolver=StubResolver(), spawn_backend=backend))


def test_errored_first_step_is_not_completed_and_fails_the_run(tmp_path: Path) -> None:
    result = _run(tmp_path, ExplodingBackend(fail_on=0))

    assert result.status is RunStatus.FAILED
    assert result.completed_steps == ()
    assert result.error is not None
    # Surfaced at the TOP LEVEL of the result, not buried in a summary.
    assert "No providers available" in str(result.error)


def test_a_later_errored_step_keeps_only_the_steps_that_really_finished(tmp_path: Path) -> None:
    result = _run(tmp_path, ExplodingBackend(fail_on=1))

    assert result.status is RunStatus.FAILED
    assert result.completed_steps == ("first",)
    assert "second" not in result.completed_steps
    assert isinstance(result.error, RuntimeError)


def test_a_run_whose_steps_all_succeed_still_reports_success(tmp_path: Path) -> None:
    """The other polarity: widening the catch must not turn success into failure."""
    result = _run(tmp_path, ExplodingBackend(fail_on=-1))

    assert result.status is RunStatus.SUCCEEDED
    assert result.completed_steps == ("first", "second")
    assert result.error is None
