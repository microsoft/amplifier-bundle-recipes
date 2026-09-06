"""Reconstruct the two-session shape a v2 run left behind before recipes-ppu.

Until recipes-ppu the step engine was handed no session and made one of its
own, so a v2 run left two ids on disk: the one the tool bound and *reported*
-- ``completed_steps: []``, ``context: {}`` -- and the one that actually held
the gate and the checkpoint. A run now has exactly one session, so a fresh run
can no longer produce that pair.

The translation that steers a caller from either id onto the run's own
(``_gate_session`` / ``_run_session``) still has to work for runs recorded by
those older builds, whose sessions are still on disk. Rather than assert that
translation on a scenario that no longer contains the hazard, the tests
reconstruct the hazard here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from amplifier_module_tool_recipes import V2_RUN_STATE_KEY
from amplifier_module_tool_recipes.models import Recipe

#: Every key ``SessionManager.set_pending_approval`` writes. The wrapper
#: session never held any of them -- the gate lived in the engine's session.
GATE_KEYS = (
    "pending_approval_stage",
    "pending_approval_prompt",
    "pending_approval_timeout",
    "pending_approval_default",
    "pending_approval_requested_at",
    "stage_approvals",
    "pending_child_approval",
)


def split_into_pre_fix_pair(tool: Any, project: Path, session_id: str) -> str:
    """Rewrite a run's one session into the pre-recipes-ppu PAIR, on disk.

    Args:
        tool: the ``RecipesTool`` whose ``session_manager`` owns the session.
        project: the project path the session lives under.
        session_id: the run's single session, as ``execute`` reported it.

    Returns:
        The new engine session id -- the one now holding the state and the
        gate. ``session_id`` is left holding only the bookkeeping, exactly as
        the wrapper session did.
    """
    sessions = tool.session_manager
    state = sessions.load_state(session_id, project)

    engine_session_id = sessions.create_session(
        Recipe(
            name=state["recipe_name"],
            description="",
            version=state["recipe_version"],
        ),
        project,
        recipe_path=Path(state[V2_RUN_STATE_KEY]["recipe_path"]),
    )

    record = dict(state[V2_RUN_STATE_KEY])
    record["session_id"] = session_id
    record["engine_session_id"] = engine_session_id

    # Everything the run actually did lived over there, gate included.
    engine_state = dict(state)
    engine_state["session_id"] = engine_session_id
    engine_state[V2_RUN_STATE_KEY] = record
    sessions.save_state(engine_session_id, project, engine_state)

    # ...and this is all the id the caller was handed ever held.
    wrapper_state = dict(state)
    wrapper_state[V2_RUN_STATE_KEY] = record
    wrapper_state["current_step_index"] = 0
    wrapper_state["context"] = {}
    wrapper_state["completed_steps"] = []
    for key in GATE_KEYS:
        wrapper_state.pop(key, None)
    sessions.save_state(session_id, project, wrapper_state)

    return engine_session_id
