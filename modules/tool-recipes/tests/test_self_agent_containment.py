"""CONTAINMENT: a ``self`` step can never reach an undeclared HOST agent.

recipes-80q. This file exists because the obvious fix for ``agent: self`` under
``schema_version: 2`` is a two-line planner exemption that makes the error go
away *and* silently reopens the closed world. The tests below are shaped to
catch exactly that fix.

The hazard, precisely
---------------------
``self`` is not undefined. The host's spawner defines it
(``amplifier_app_cli/session_spawner.py``)::

    if agent_name == "self":
        agent_config = {}          # empty overlay = inherit parent config as-is
    ...
    merge_configs(parent_session.config, agent_config)

and the ``parent_session`` a v2 run hands over is the **caller's**
(``executor.py``: ``parent_session = self.coordinator.session``).
``ClosedWorldCoordinator`` substitutes the agent map and the spawn -- not the
session. So a ``self`` step admitted into a v2 run is composed from the
caller's whole config, agent map included, with no error, no warning and no
provenance entry. ``recipe-dependency-manifest.v1`` Core 3/5 forbid precisely
that.

What is measured
----------------
:class:`SelfAwareSpawn` reproduces the host's rule above and records, per
spawn, **the agent map the child was composed from**. The containment claim is
then a measurement rather than a narration:

    across the whole run, no child composed under ``self`` semantics existed,
    so the host-only agent was never in reach of one.

Why the second test is not redundant
------------------------------------
``test_the_containment_check_actually_bites`` stages the naive fix -- the
catalog exempts ``self`` instead of refusing it -- and shows the host-only
agent becoming reachable. Without it, the assertion above could pass for the
wrong reason (a recipe that never got that far, a fake that never looked), and
a future exemption would land against a test that could not fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplifier_module_tool_recipes import closed_world as cw
from amplifier_module_tool_recipes import runner_adapter as ra

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

requires_runner = pytest.mark.skipif(
    not ra.runner_available(), reason=f"{ra.RUNNER_DISTRIBUTION} is not importable"
)

#: An agent the HOST carries and the recipe's closure does not declare. If it
#: is ever reachable from a step of this recipe, the closed world is open.
HOST_ONLY_AGENT = "host-only:privileged"

AGENT_FILE = """\
---
meta:
  name: reviewer
  description: The reviewer the RECIPE declared
---
You are the declared reviewer.
"""

#: A declared agent step FIRST (the positive control -- it must really run, or
#: a refusal later proves nothing), then the `self` step under test.
RECIPE_WITH_SELF_STEP = """\
schema_version: 2
name: self-step-recipe
description: a declared agent step, then a `self` step
version: "1.0.0"

dependencies:
  - source: "bundles/supplier"
    kind: bundle
    required_agents:
      - "supplier:reviewer"

steps:
  - id: "review"
    agent: "supplier:reviewer"
    prompt: "Review it"
    output: "review_result"

  - id: "summarize"
    agent: "self"
    prompt: "Summarize {{review_result}}"
    output: "summary"
"""


# ---------------------------------------------------------------------------
# Fakes -- deliberately local to this file
# ---------------------------------------------------------------------------
#
# The spawn/coordinator doubles below are what this test MEASURES, so they are
# not shared with the other closed-world suites: a fake loosened elsewhere for
# an unrelated reason would weaken this check invisibly.


class HostSession:
    """The caller's session, as the host spawner consumes it.

    Only ``config`` matters here -- it is the thing ``merge_configs`` reads and
    the thing an inherited ``self`` overlay would restore wholesale.
    """

    def __init__(self, agents: dict[str, Any]) -> None:
        self.config: dict[str, Any] = {
            "agents": dict(agents),
            "providers": [{"module": "provider-anthropic"}],
        }


class SelfAwareSpawn:
    """The host's ``session.spawn``, including its real ``self`` rule.

    Records for every call the agent map the CHILD would have been composed
    with, which is what containment is actually about.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        #: (agent_name, agents-the-child-was-composed-with) per spawn.
        self.composed: list[tuple[str, frozenset[str]]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        agent_name = kwargs.get("agent_name")
        parent_session = kwargs.get("parent_session")
        agent_configs = kwargs.get("agent_configs") or {}

        # The host's own rule, verbatim in shape:
        #   self -> EMPTY overlay; anything else -> that agent's overlay.
        overlay: dict[str, Any] = {}
        if agent_name != "self":
            overlay = dict(agent_configs.get(agent_name) or {})
        parent_config = dict(getattr(parent_session, "config", None) or {})
        merged = {**parent_config, **overlay}

        self.calls.append(dict(kwargs))
        self.composed.append((str(agent_name), frozenset(merged.get("agents") or {})))
        return {"output": "done", "session_id": f"child-{len(self.calls)}"}

    def composed_under_self_semantics(self) -> list[frozenset[str]]:
        """Agent maps of every child composed the way the host composes ``self``."""
        return [agents for name, agents in self.composed if name == "self"]


class HostCoordinator:
    """A caller carrying an agent the recipe never declared."""

    def __init__(self, spawn: Any) -> None:
        agents = {
            "supplier:reviewer": {
                "name": "reviewer",
                "description": "the CALLER's impostor",
                "instruction": "You are the impostor.",
            },
            HOST_ONLY_AGENT: {
                "name": "privileged",
                "description": "the host has this; the recipe never declared it",
            },
        }
        self.config: dict[str, Any] = {
            "agents": agents,
            "providers": [{"module": "provider-anthropic"}],
        }
        # The session is NOT the coordinator: ClosedWorldCoordinator replaces
        # the coordinator's agent map, and this object rides through untouched.
        self.session = HostSession(agents)
        self._capabilities: dict[str, Any] = {"session.spawn": spawn}

    def get_capability(self, name: str) -> Any:
        return self._capabilities.get(name)

    def register_capability(self, name: str, value: Any) -> None:
        self._capabilities[name] = value

    def get(self, name: str) -> Any:
        return self.config.get(name)


class FakeSessionManager:
    """Enough session state for the engine's checkpointing to be real."""

    def __init__(self, tmp_path: Path) -> None:
        self.base = tmp_path / "sessions"
        self.base.mkdir(exist_ok=True)
        self.states: dict[str, dict[str, Any]] = {}

    def create_session(self, *args: Any, **kwargs: Any) -> str:
        session_id = f"recipe_{len(self.states)}"
        self.states[session_id] = {"completed_steps": []}
        return session_id

    def get_session_dir(self, session_id: str, project_path: Path) -> Path:
        path = self.base / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def load_state(self, session_id: str, project_path: Path) -> dict[str, Any]:
        return dict(self.states.setdefault(session_id, {}))

    def save_state(self, session_id: str, project_path: Path, state: dict[str, Any]) -> None:
        self.states[session_id] = dict(state)

    def save_checkpoint(self, *args: Any, **kwargs: Any) -> None:
        return None

    def cleanup_old_sessions(self, project_path: Path) -> int:
        return 0

    def is_cancellation_requested(self, session_id: str, project_path: Path) -> bool:
        return False

    def get_stage_approval_status(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def set_pending_approval(self, *args: Any, **kwargs: Any) -> None:
        return None

    def complete_session(self, *args: Any, **kwargs: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def write_agent(tmp_path: Path) -> Path:
    agents_dir = tmp_path / "supplier" / "agents"
    agents_dir.mkdir(parents=True)
    path = agents_dir / "reviewer.md"
    path.write_text(AGENT_FILE, encoding="utf-8")
    return path


def write_recipe(tmp_path: Path) -> Path:
    path = tmp_path / "self-step.yaml"
    path.write_text(RECIPE_WITH_SELF_STEP, encoding="utf-8")
    return path


def make_plan(agent_path: Path) -> Any:
    """An ``ExecutionPlan`` whose closure is one agent -- and never ``self``."""
    from amplifier_recipe_runner.api import AgentProvenance
    from amplifier_recipe_runner.api import EffectivePolicy
    from amplifier_recipe_runner.api import ExecutionPlan
    from amplifier_recipe_runner.api import LockMode

    return ExecutionPlan(
        recipe_digest="sha256:test",
        schema_version=2,
        dependencies=(),
        agents={
            "supplier:reviewer": AgentProvenance(
                agent="supplier:reviewer",
                supplied_by="bundles/supplier",
                dependency_digest="sha256:dep",
                local_path=str(agent_path),
            )
        },
        step_ids=("review", "summarize"),
        policy=EffectivePolicy(lock_mode=LockMode.LOCKED),
    )


async def _resolved(plan: Any) -> Any:
    """The injected ``plan`` seam: already resolved, nothing re-resolved."""
    return plan


async def _run(tmp_path: Path, spawn: SelfAwareSpawn, coordinator: HostCoordinator) -> Any:
    return await ra.run_v2_recipe_in_session(
        coordinator,
        FakeSessionManager(tmp_path),
        write_recipe(tmp_path),
        {},
        tmp_path,
        plan=lambda request: _resolved(make_plan(tmp_path / "supplier" / "agents" / "reviewer.md")),
    )


# ---------------------------------------------------------------------------
# The containment property
# ---------------------------------------------------------------------------


@requires_runner
class TestSelfStepContainment:
    @pytest.mark.asyncio
    async def test_a_self_step_never_reaches_an_undeclared_host_agent(self, tmp_path: Path):
        write_agent(tmp_path)
        spawn = SelfAwareSpawn()
        coordinator = HostCoordinator(spawn)

        # Premise: the host really does carry the undeclared agent, so there is
        # something for a leak to leak.
        assert HOST_ONLY_AGENT in coordinator.session.config["agents"]

        result = await _run(tmp_path, spawn, coordinator)

        # Positive control: the DECLARED step ran, on the plan's own catalog.
        # Without this, the refusal below could be a recipe that never started.
        assert spawn.calls, "the declared agent step never ran; the refusal proves nothing"
        assert spawn.calls[0]["agent_name"] == "supplier:reviewer"
        assert set(spawn.calls[0]["agent_configs"]) == {"supplier:reviewer"}

        # THE CONTAINMENT CLAIM, measured: no child was ever composed the way
        # the host composes `self`, so the host-only agent was never in reach
        # of one.
        assert spawn.composed_under_self_semantics() == []

        # The boundary this claim is deliberately narrow about, stated rather
        # than hidden: the DECLARED step's child does inherit the caller's
        # roster, because the host composes every child from the parent session
        # (closed_world.py, "One honest boundary"). That is host policy about
        # what a child may delegate to NEXT -- and it is survivable precisely
        # because the step's own IDENTITY still came from the plan, asserted
        # just above (`agent_configs` is the catalog, not the caller's map).
        #
        # `self` has no plan-supplied identity at all. That is the whole
        # difference, and why it must never get as far as a spawn.
        declared_child = dict(spawn.composed)["supplier:reviewer"]
        assert HOST_ONLY_AGENT in declared_child, (
            "premise: the host really does seed a child from the parent session "
            "-- if it did not, this test would prove nothing about `self`"
        )

        # ... and the run said so, loudly, instead of skipping the step.
        from amplifier_recipe_runner.api import RunStatus
        from amplifier_recipe_runner.errors import SelfAgentUnsupportedError

        assert result.status is RunStatus.FAILED
        assert isinstance(result.error, SelfAgentUnsupportedError) or (
            "self" in str(result.error)
        ), result.error
        message = str(result.error)
        assert "`dependencies:` block can supply it" in message
        assert "declare a dependency supplying 'self'" not in message.lower()

    @pytest.mark.asyncio
    async def test_the_containment_check_actually_bites(self, tmp_path: Path, monkeypatch):
        """The naive fix, staged -- and the leak it produces, measured.

        This exempts ``self`` at the catalog exactly as a planner exemption
        would leave it: admitted, unresolved, handed to the host. The host then
        composes the step from the CALLER's session, and the undeclared agent
        arrives.

        If a future change makes the exemption real, the test above stops being
        able to fail. This one is the proof that it can.
        """
        write_agent(tmp_path)
        spawn = SelfAwareSpawn()
        coordinator = HostCoordinator(spawn)

        real_resolve = cw.ClosedWorldAgentCatalog.resolve

        def exempting_resolve(self, reference, *, step_id=None):
            if reference == "self":  # the two-line "fix"
                return None
            return real_resolve(self, reference, step_id=step_id)

        monkeypatch.setattr(cw.ClosedWorldAgentCatalog, "resolve", exempting_resolve)

        await _run(tmp_path, spawn, coordinator)

        leaked = spawn.composed_under_self_semantics()
        assert leaked, "the exemption did not even reach a spawn; this check is not staged right"
        assert HOST_ONLY_AGENT in leaked[0], (
            "with `self` exempted the host-only agent must become reachable -- "
            "if it does not, the containment assertion above cannot fail and is "
            "therefore worthless"
        )
