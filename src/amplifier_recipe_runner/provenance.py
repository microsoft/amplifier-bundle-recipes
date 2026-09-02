"""Run manifest: recording what a run resolved, and refusing to drift on resume.

Contracts: ``recipe-dependency-manifest.v1`` Core 7 and Core 8 (the resume
half), and ``recipe-runner-lib.v1`` Core 7.

Core 7 names exactly what run state must record, and this module records all
of it, from an :class:`~amplifier_recipe_runner.api.ExecutionPlan`:

===============================  ==========================================
Core 7 requirement               :class:`RunManifest` field
===============================  ==========================================
recipe digest                    :attr:`RunManifest.recipe_digest`
each declared URI/ref            ``dependencies[].uri`` / ``requested_ref``
resolved revision/content digest ``dependencies[].resolved_revision`` /
                                 ``content_digest``
included partials                :attr:`RunManifest.partials`
agent-to-dependency map          :attr:`RunManifest.agents`
runner/foundation versions       ``runner_version`` / ``foundation_version``
effective trust + capability     :attr:`RunManifest.policy`
===============================  ==========================================

The manifest persists as JSON (:func:`write_run_manifest`) with sorted keys,
so two runs that resolved identically produce byte-identical records and a
diff of two run manifests is a diff of the two resolved graphs.

Resume (Core 8)
---------------
:func:`check_resume_provenance` compares a recorded manifest against a fresh
re-resolution. Any difference -- a changed revision, a dependency that
vanished, one that appeared, a changed recipe digest -- raises
:class:`~amplifier_recipe_runner.errors.ProvenanceMismatchError` naming both
sides. There is deliberately no "prefer the new one" branch and no tolerance
window: silently re-resolving is the failure this clause exists to forbid.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from .api import RUN_MANIFEST_VERSION
from .api import AgentProvenance
from .api import DependencyKind
from .api import EffectivePolicy
from .api import ExecutionPlan
from .api import LockMode
from .api import ResolvedDependency
from .errors import ProvenanceMismatchError

__all__ = [
    "RUN_MANIFEST_FILENAME",
    "RunManifest",
    "check_resume_provenance",
    "dependency_identity",
    "read_run_manifest",
    "run_manifest_from_plan",
    "run_manifest_path_for",
    "write_run_manifest",
]

#: Conventional filename inside a run's state directory.
RUN_MANIFEST_FILENAME: Final[str] = "run-manifest.json"

#: Stand-in source name used when the *recipe itself* is what mismatched.
_RECIPE_SOURCE: Final[str] = "<recipe>"


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunManifest:
    """The fully resolved graph of one run, as persisted (Core 7).

    Built from a plan, never from ambient state: everything here is a fact the
    planner already established, so a manifest can be compared against a fresh
    plan field by field.
    """

    run_id: str
    recipe_digest: str
    schema_version: int
    dependencies: tuple[ResolvedDependency, ...] = ()
    agents: Mapping[str, AgentProvenance] = field(default_factory=dict)
    """Agent name -> supplying dependency (the Core 7 provenance map)."""

    step_ids: tuple[str, ...] = ()
    policy: EffectivePolicy | None = None
    runner_version: str | None = None
    foundation_version: str | None = None
    manifest_version: int = RUN_MANIFEST_VERSION
    created_at: str | None = None
    """UTC ISO-8601 timestamp; recorded, never compared."""

    # -- Core 7 views ------------------------------------------------------

    @property
    def partials(self) -> tuple[str, ...]:
        """Declared sources that contributed only a behavior partial."""
        return tuple(dep.uri for dep in self.dependencies if dep.subdirectory)

    @property
    def agent_dependency_map(self) -> Mapping[str, str]:
        """Flat ``agent -> supplying dependency URI`` view of :attr:`agents`."""
        return {name: prov.supplied_by for name, prov in self.agents.items()}

    def dependency_for(self, uri: str) -> ResolvedDependency | None:
        for dep in self.dependencies:
            if dep.uri == uri:
                return dep
        return None

    # -- serialisation -----------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        """JSON-ready form. Enums become their values; ``None`` is preserved."""
        return {
            "manifest_version": self.manifest_version,
            "run_id": self.run_id,
            "recipe_digest": self.recipe_digest,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "runner_version": self.runner_version,
            "foundation_version": self.foundation_version,
            "policy": _policy_to_mapping(self.policy),
            "dependencies": [_dependency_to_mapping(dep) for dep in self.dependencies],
            "agents": {name: _agent_to_mapping(prov) for name, prov in sorted(self.agents.items())},
            "partials": list(self.partials),
            "step_ids": list(self.step_ids),
        }

    @classmethod
    def from_mapping(cls, data: Any) -> RunManifest:
        if not isinstance(data, Mapping):
            raise ValueError(f"Run manifest must be a mapping, got {type(data).__name__}.")
        raw_deps = data.get("dependencies") or []
        raw_agents = data.get("agents") or {}
        return cls(
            run_id=str(data.get("run_id") or ""),
            recipe_digest=str(data.get("recipe_digest") or ""),
            schema_version=int(data.get("schema_version") or 0),
            dependencies=tuple(_dependency_from_mapping(item) for item in raw_deps),
            agents={str(name): _agent_from_mapping(value) for name, value in raw_agents.items()},
            step_ids=tuple(str(sid) for sid in (data.get("step_ids") or ())),
            policy=_policy_from_mapping(data.get("policy")),
            runner_version=_opt_str(data.get("runner_version")),
            foundation_version=_opt_str(data.get("foundation_version")),
            manifest_version=int(data.get("manifest_version") or RUN_MANIFEST_VERSION),
            created_at=_opt_str(data.get("created_at")),
        )


# --------------------------------------------------------------------------
# Build / persist / load
# --------------------------------------------------------------------------


def run_manifest_from_plan(
    plan: ExecutionPlan,
    *,
    run_id: str,
    created_at: str | None = None,
) -> RunManifest:
    """Record ``plan`` as the run manifest for ``run_id`` (Core 7)."""
    return RunManifest(
        run_id=run_id,
        recipe_digest=plan.recipe_digest,
        schema_version=plan.schema_version,
        dependencies=tuple(plan.dependencies),
        agents=dict(plan.agents),
        step_ids=tuple(plan.step_ids),
        policy=plan.policy,
        runner_version=plan.runner_version,
        foundation_version=plan.foundation_version,
        manifest_version=plan.manifest_version,
        created_at=created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def run_manifest_path_for(run_dir: str | Path) -> Path:
    """Conventional manifest path inside a run's state directory."""
    return Path(run_dir) / RUN_MANIFEST_FILENAME


def write_run_manifest(path: str | Path, manifest: RunManifest) -> Path:
    """Persist ``manifest`` as JSON, atomically and deterministically."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest.to_mapping(), indent=2, sort_keys=True) + "\n"
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target


def read_run_manifest(path: str | Path) -> RunManifest:
    """Load a persisted run manifest."""
    return RunManifest.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# Resume comparison (Core 8)
# --------------------------------------------------------------------------


def dependency_identity(dependency: ResolvedDependency) -> str:
    """The immutable identity a dependency resolved to.

    Git sources report their revision; local sources have none and report a
    content digest instead. One string, so a mismatch names both sides without
    the caller having to know which field applied.
    """
    if dependency.resolved_revision is not None:
        return dependency.resolved_revision
    if dependency.content_digest is not None:
        return dependency.content_digest
    return "<unresolved>"


def check_resume_provenance(
    recorded: RunManifest,
    plan: ExecutionPlan,
    *,
    run_id: str | None = None,
    compare_recipe_digest: bool = True,
) -> None:
    """Compare a recorded run against a fresh re-resolution (Core 8).

    Args:
        recorded: The run manifest persisted when the run started.
        plan: The plan produced by re-resolving now.
        run_id: Named in the error; defaults to ``recorded.run_id``.
        compare_recipe_digest: When True (default), an edited recipe body is
            itself a mismatch -- resuming into different steps is exactly the
            silent drift this clause forbids.

    Raises:
        ProvenanceMismatchError: on the first difference found, naming the
            source and *both* identities. Never re-resolves silently, and
            never prefers the newly resolved value.
    """
    where = run_id or recorded.run_id or None

    if compare_recipe_digest and recorded.recipe_digest != plan.recipe_digest:
        raise ProvenanceMismatchError(
            _RECIPE_SOURCE,
            expected=recorded.recipe_digest,
            actual=plan.recipe_digest,
            run_id=where,
            remedy=(
                "Resume the recipe as recorded, or start a new run -- a resumed run never "
                "adopts an edited recipe."
            ),
        )

    current = {dep.uri: dep for dep in plan.dependencies}

    for dep in recorded.dependencies:
        fresh = current.get(dep.uri)
        if fresh is None:
            raise ProvenanceMismatchError(
                dep.uri,
                expected=dependency_identity(dep),
                actual="<not declared>",
                run_id=where,
                remedy=("Restore the dependency declaration recorded for this run, or start a new run."),
            )
        expected = dependency_identity(dep)
        actual = dependency_identity(fresh)
        if expected != actual:
            raise ProvenanceMismatchError(
                dep.uri,
                expected=expected,
                actual=actual,
                run_id=where,
            )

    recorded_sources = {dep.uri for dep in recorded.dependencies}
    for dep in plan.dependencies:
        if dep.uri not in recorded_sources:
            raise ProvenanceMismatchError(
                dep.uri,
                expected="<not recorded>",
                actual=dependency_identity(dep),
                run_id=where,
                remedy=(
                    "A dependency was added after this run started; start a new run rather "
                    "than resuming into it."
                ),
            )


# --------------------------------------------------------------------------
# (de)serialisation helpers
# --------------------------------------------------------------------------


def _dependency_to_mapping(dep: ResolvedDependency) -> dict[str, Any]:
    return {
        "uri": dep.uri,
        "kind": dep.kind.value if isinstance(dep.kind, DependencyKind) else str(dep.kind),
        "requested_ref": dep.requested_ref,
        "resolved_revision": dep.resolved_revision,
        "content_digest": dep.content_digest,
        "subdirectory": dep.subdirectory,
        "required_agents": list(dep.required_agents),
        "version": dep.version,
        "local_path": dep.local_path,
        "namespace": dep.namespace,
    }


def _dependency_from_mapping(data: Any) -> ResolvedDependency:
    if not isinstance(data, Mapping):
        raise ValueError(f"Dependency record must be a mapping, got {type(data).__name__}.")
    required = data.get("required_agents") or ()
    if isinstance(required, (str, bytes)) or not isinstance(required, Sequence):
        required = ()
    return ResolvedDependency(
        uri=str(data.get("uri") or ""),
        kind=DependencyKind(str(data.get("kind") or DependencyKind.BUNDLE.value)),
        requested_ref=_opt_str(data.get("requested_ref")),
        resolved_revision=_opt_str(data.get("resolved_revision")),
        content_digest=_opt_str(data.get("content_digest")),
        subdirectory=_opt_str(data.get("subdirectory")),
        required_agents=tuple(str(name) for name in required),
        version=_opt_str(data.get("version")),
        local_path=_opt_str(data.get("local_path")),
        namespace=_opt_str(data.get("namespace")),
    )


def _agent_to_mapping(prov: AgentProvenance) -> dict[str, Any]:
    return {
        "agent": prov.agent,
        "supplied_by": prov.supplied_by,
        "dependency_digest": prov.dependency_digest,
        "alias": prov.alias,
        "local_path": prov.local_path,
        "resolved_revision": prov.resolved_revision,
    }


def _agent_from_mapping(data: Any) -> AgentProvenance:
    if not isinstance(data, Mapping):
        raise ValueError(f"Agent provenance record must be a mapping, got {type(data).__name__}.")
    return AgentProvenance(
        agent=str(data.get("agent") or ""),
        supplied_by=str(data.get("supplied_by") or ""),
        dependency_digest=_opt_str(data.get("dependency_digest")),
        alias=_opt_str(data.get("alias")),
        local_path=_opt_str(data.get("local_path")),
        resolved_revision=_opt_str(data.get("resolved_revision")),
    )


def _policy_to_mapping(policy: EffectivePolicy | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    mode = policy.lock_mode
    return {
        "lock_mode": mode.value if isinstance(mode, LockMode) else str(mode),
        "trust_policy": policy.trust_policy,
        "capabilities": list(policy.capabilities),
        "isolated": policy.isolated,
    }


def _policy_from_mapping(data: Any) -> EffectivePolicy | None:
    if data is None:
        return None
    if not isinstance(data, Mapping):
        raise ValueError(f"Policy record must be a mapping, got {type(data).__name__}.")
    caps = data.get("capabilities") or ()
    if isinstance(caps, (str, bytes)) or not isinstance(caps, Sequence):
        caps = ()
    return EffectivePolicy(
        lock_mode=LockMode(str(data.get("lock_mode") or LockMode.LOCKED.value)),
        trust_policy=_opt_str(data.get("trust_policy")),
        capabilities=tuple(str(cap) for cap in caps),
        isolated=bool(data.get("isolated", True)),
    )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
