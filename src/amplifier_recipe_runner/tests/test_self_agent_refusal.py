"""``agent: self`` under ``schema_version: 2`` -- refused, and refused twice.

recipes-80q. ``self`` is the legacy pseudo-agent meaning "spawn the calling
session's own agent". Under v2 it is neither resolvable nor declarable:

* **Not declarable.** No ``dependencies:`` block can supply it -- it names no
  bundle agent. So the generic ``UndeclaredAgentError`` remedy ("declare a
  dependency supplying 'self'") is an instruction with no followable action in
  it. That is what planning such a recipe emitted before this change.
* **Not safely resolvable.** The host's spawner defines ``self`` as an EMPTY
  overlay merged onto the *parent session's* config, and in the closed-world
  path that parent session is the CALLER's. Honouring it would restore the
  caller's whole world -- agent map included -- for that step, silently.

So v2 refuses it with :class:`SelfAgentUnsupportedError`, which names the
supported alternative instead. The refusal lives at BOTH the planner (a
preflight) and the catalog (the last hop before a spawn); the pair is the
point, and ``test_the_catalog_refuses_self_even_when_a_plan_carries_it`` is the
test that proves the second one is load-bearing rather than a mirror of the
first.

The containment property itself -- an undeclared HOST agent proven unreachable
from a ``self`` step on the real in-session engine -- is
``modules/tool-recipes/tests/test_self_agent_containment.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from amplifier_recipe_runner.api import AgentProvenance
from amplifier_recipe_runner.errors import SELF_AGENT
from amplifier_recipe_runner.errors import SelfAgentUnsupportedError
from amplifier_recipe_runner.errors import UndeclaredAgentError
from amplifier_recipe_runner.execution import PlanCatalog

from .test_planner import ACME
from .test_planner import planned
from .test_planner import write_recipe

#: The sentence the pre-recipes-80q error emitted, in the shape it emitted it.
#: No refusal on any surface may say this: it asks the author to do something
#: the manifest schema cannot express.
IMPOSSIBLE_REMEDY = "declare a dependency supplying 'self'"


def _says_the_impossible_thing(text: str) -> bool:
    return IMPOSSIBLE_REMEDY in text.lower()


# --------------------------------------------------------------------------
# Plan time
# --------------------------------------------------------------------------


def test_a_self_step_is_refused_at_plan_time_naming_the_alternative(tmp_path: Path) -> None:
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
            required_agents: [acme:reviewer]
        steps:
          - id: validate_inputs
            agent: "self"
            prompt: p
        """,
    )

    with pytest.raises(SelfAgentUnsupportedError) as excinfo:
        planned(recipe)

    error = excinfo.value
    assert error.agent == SELF_AGENT
    assert error.step_id == "validate_inputs"
    assert "acme:reviewer" in error.declared_agents

    message = str(error)
    # Says WHY, in the author's own vocabulary -- not "not supplied by any
    # declared dependency", which reads like a typo they can fix.
    assert "`dependencies:` block can supply it" in message
    # Names the supported alternative: a declared agent by name...
    assert "acme:reviewer" in message
    # ...or staying legacy, where `self` keeps its labeled caller-bound meaning.
    assert "schema_version" in message
    assert not _says_the_impossible_thing(message)


def test_a_self_step_nested_in_a_loop_body_is_refused_too(tmp_path: Path) -> None:
    """The planner's walk reaches nested bodies, so the refusal does too.

    A ``foreach``/``while`` body is a list of raw dicts the engine parses on
    the fly. A guard that only saw top-level steps would let a ``self`` step
    inside a loop through -- which is precisely where the expensive ones live.
    """
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        steps:
          - id: outer
            foreach: "{{{{ items }}}}"
            while_steps:
              - id: inner
                agent: "self"
                prompt: p
        """,
    )

    with pytest.raises(SelfAgentUnsupportedError) as excinfo:
        planned(recipe)

    assert excinfo.value.step_id == "inner"


def test_an_alias_cannot_smuggle_self_past_the_planner(tmp_path: Path) -> None:
    """``agents: {self: acme:reviewer}`` is refused, not honoured.

    It is tempting as a one-line migration shim: the reference resolves to a
    declared agent and gets real provenance, so it is *safe*. It is refused
    anyway, because it leaves ``agent: self`` in the file reading as the legacy
    caller-bound meaning while doing something else entirely. A reader cannot
    tell the two apart, and the whole value of the refusal is that a v2 step's
    identity is legible from the step.

    This is also why the guard sits BEFORE alias resolution rather than after.
    """
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
        agents:
          self: acme:reviewer
        steps:
          - id: review
            agent: "self"
            prompt: p
        """,
    )

    with pytest.raises(SelfAgentUnsupportedError):
        planned(recipe)


def test_a_recipe_with_no_self_step_still_plans(tmp_path: Path) -> None:
    """Premise check: the refusal is about ``self``, not about this fixture."""
    recipe = write_recipe(
        tmp_path,
        f"""
        schema_version: 2
        dependencies:
          - source: {ACME}
            kind: bundle
            required_agents: [acme:reviewer]
        steps:
          - id: review
            agent: "acme:reviewer"
            prompt: p
        """,
    )

    plan = planned(recipe)

    assert "acme:reviewer" in plan.agents
    assert SELF_AGENT not in plan.agents


# --------------------------------------------------------------------------
# Resolve time
# --------------------------------------------------------------------------


def _provenance(name: str) -> AgentProvenance:
    return AgentProvenance(
        agent=name,
        supplied_by="fixtures/acme",
        dependency_digest="sha256:dep",
        local_path=f"/fixtures/acme/agents/{name.split(':')[-1]}.md",
    )


def test_the_catalog_refuses_self_even_when_a_plan_carries_it() -> None:
    """The second guard is load-bearing, not a mirror of the first.

    This is the naive fix, staged: a plan that ALREADY admits ``self`` -- which
    is exactly what exempting it at the planner produces. If the only refusal
    lived at plan time, resolution here would hand the step straight to the
    spawn, where the host resolves ``self`` against the CALLER's session.

    So the catalog refuses on its own authority, with no reference to how the
    plan was built.
    """
    catalog = PlanCatalog(
        MappingProxyType(
            {
                "acme:reviewer": _provenance("acme:reviewer"),
                # What a planner exemption would leave behind.
                SELF_AGENT: _provenance(SELF_AGENT),
            }
        )
    )

    # Premise: the entry really is in the catalog's map...
    assert SELF_AGENT in catalog

    # ...and resolution refuses it anyway.
    with pytest.raises(SelfAgentUnsupportedError) as excinfo:
        catalog.resolve(SELF_AGENT, step_id="verification_loop")

    assert excinfo.value.step_id == "verification_loop"
    assert not _says_the_impossible_thing(str(excinfo.value))

    # The declared agent beside it is untouched -- this refuses `self`, not the
    # catalog.
    assert catalog.resolve("acme:reviewer").agent == "acme:reviewer"


def test_a_host_catching_the_general_case_still_catches_this_one() -> None:
    """Deliberate subclassing: no host loses coverage by upgrading.

    Every host that already handles ``UndeclaredAgentError`` -- rendering its
    message and remedy -- keeps handling this. A host that wants to say
    something specific about ``self`` now can.
    """
    catalog = PlanCatalog(MappingProxyType({"acme:reviewer": _provenance("acme:reviewer")}))

    with pytest.raises(UndeclaredAgentError):
        catalog.resolve(SELF_AGENT)


def test_the_refusal_is_a_preflight_failure_not_a_run_failure() -> None:
    """lib Core 8: nothing has run when this is raised.

    Asserted by type rather than by narration -- ``PreflightError`` is the
    supported way for a host to say "nothing ran", and the CLI maps it to the
    preflight exit code on that basis.
    """
    from amplifier_recipe_runner.cli import EXIT_PREFLIGHT
    from amplifier_recipe_runner.cli import exit_code_for

    assert exit_code_for(SelfAgentUnsupportedError()) == EXIT_PREFLIGHT


def test_the_shipped_error_text_is_what_an_author_would_act_on() -> None:
    """A refusal with no closure to name still names the other way out."""
    text = str(SelfAgentUnsupportedError())

    assert "none" in text  # honest about an empty closure
    assert "omit `schema_version: 2`" in text
    assert not _says_the_impossible_thing(text)
