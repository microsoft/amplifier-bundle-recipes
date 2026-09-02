"""Tests for lock semantics and run provenance (manifest.v1 Core 7, 8; lib.v1 Core 7).

Everything here is local and offline: plans are constructed directly from the
public :mod:`amplifier_recipe_runner.api` dataclasses with *fake* resolved
revisions, because the clause under test is what the runner does with a
resolved identity -- not how git produces one. No network, no clone, no
Foundation import.

The discriminating pairs the contract names are all present:

* GOOD -- a lock round-trips write -> read unchanged, and a locked run whose
  resolution matches passes with the lockfile byte-for-byte untouched.
* GOOD -- ``update-lock`` rewrites the lock, explicitly and only then.
* GOOD -- a run manifest records every Core 7 fact and reloads identically.
* BAD -- a locked run whose dependency now resolves to a different revision
  fails visibly, naming both revisions.
* BAD -- a locked run with a missing (or extra) entry fails before anything runs.
* BAD -- a resume whose re-resolution differs raises ProvenanceMismatchError
  rather than silently adopting the new revision.
* WARN -- ``unlocked`` resolves with no lock and returns the warning as a result.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from amplifier_recipe_runner.api import RUN_MANIFEST_VERSION
from amplifier_recipe_runner.api import AgentProvenance
from amplifier_recipe_runner.api import DependencyKind
from amplifier_recipe_runner.api import EffectivePolicy
from amplifier_recipe_runner.api import ExecutionPlan
from amplifier_recipe_runner.api import LockMode
from amplifier_recipe_runner.api import ResolvedDependency
from amplifier_recipe_runner.errors import PreflightError
from amplifier_recipe_runner.errors import ProvenanceMismatchError
from amplifier_recipe_runner.lockfile import LOCK_VERSION
from amplifier_recipe_runner.lockfile import LockEntry
from amplifier_recipe_runner.lockfile import LockEntryMissingError
from amplifier_recipe_runner.lockfile import LockEntryUnexpectedError
from amplifier_recipe_runner.lockfile import LockError
from amplifier_recipe_runner.lockfile import Lockfile
from amplifier_recipe_runner.lockfile import LockfileMissingError
from amplifier_recipe_runner.lockfile import LockVersionError
from amplifier_recipe_runner.lockfile import apply_lock_mode
from amplifier_recipe_runner.lockfile import canonical_source
from amplifier_recipe_runner.lockfile import lock_from_plan
from amplifier_recipe_runner.lockfile import lock_path_for
from amplifier_recipe_runner.lockfile import read_lock
from amplifier_recipe_runner.lockfile import verify_lock
from amplifier_recipe_runner.lockfile import write_lock
from amplifier_recipe_runner.provenance import RUN_MANIFEST_FILENAME
from amplifier_recipe_runner.provenance import RunManifest
from amplifier_recipe_runner.provenance import check_resume_provenance
from amplifier_recipe_runner.provenance import dependency_identity
from amplifier_recipe_runner.provenance import read_run_manifest
from amplifier_recipe_runner.provenance import run_manifest_from_plan
from amplifier_recipe_runner.provenance import run_manifest_path_for
from amplifier_recipe_runner.provenance import write_run_manifest

# Fake -- and deliberately so: see the module docstring.
ACME_URI = "git+https://example.invalid/acme@main"
ACME_REV = "1111111111111111111111111111111111111111"
ACME_REV_MOVED = "2222222222222222222222222222222222222222"
BEHAVIOR_URI = "git+https://example.invalid/widget@v2#subdirectory=behaviors/review.yaml"
BEHAVIOR_REV = "3333333333333333333333333333333333333333"
LOCAL_URI = "./bundles/local"
LOCAL_DIGEST = "sha256:" + "a" * 64
RECIPE_DIGEST = "sha256:" + "b" * 64


# --------------------------------------------------------------------------
# fixtures -- plans built from public dataclasses, no resolver involved
# --------------------------------------------------------------------------


def _dependencies() -> tuple[ResolvedDependency, ...]:
    return (
        ResolvedDependency(
            uri=ACME_URI,
            kind=DependencyKind.BUNDLE,
            requested_ref="main",
            resolved_revision=ACME_REV,
            required_agents=("acme:reviewer",),
            version="1.2.0",
            local_path="/cache/acme",
            namespace="acme",
        ),
        ResolvedDependency(
            uri=BEHAVIOR_URI,
            kind=DependencyKind.BEHAVIOR,
            requested_ref="v2",
            resolved_revision=BEHAVIOR_REV,
            subdirectory="behaviors/review.yaml",
            local_path="/cache/widget",
            namespace="widget",
        ),
        ResolvedDependency(
            uri=LOCAL_URI,
            kind=DependencyKind.BUNDLE,
            content_digest=LOCAL_DIGEST,
            local_path="/repo/bundles/local",
            namespace="local",
        ),
    )


def make_plan(
    *,
    dependencies: tuple[ResolvedDependency, ...] | None = None,
    recipe_digest: str = RECIPE_DIGEST,
    lock_mode: LockMode = LockMode.LOCKED,
) -> ExecutionPlan:
    """An ExecutionPlan with the shape the planner produces, built by hand."""
    return ExecutionPlan(
        recipe_digest=recipe_digest,
        schema_version=2,
        dependencies=_dependencies() if dependencies is None else dependencies,
        agents={
            "acme:reviewer": AgentProvenance(
                agent="acme:reviewer",
                supplied_by=ACME_URI,
                alias="reviewer",
                local_path="/cache/acme/agents/reviewer.md",
                resolved_revision=ACME_REV,
            ),
            "local:writer": AgentProvenance(
                agent="local:writer",
                supplied_by=LOCAL_URI,
                dependency_digest=LOCAL_DIGEST,
                local_path="/repo/bundles/local/bundle.yaml",
            ),
        },
        step_ids=("review", "write"),
        policy=EffectivePolicy(
            lock_mode=lock_mode,
            trust_policy="strict",
            capabilities=("read",),
            isolated=True,
        ),
        runner_version="0.1.0",
        foundation_version="9.9.9",
    )


def moved_plan() -> ExecutionPlan:
    """The same plan, except ``acme`` now resolves to a different revision."""
    deps = list(_dependencies())
    deps[0] = replace(deps[0], resolved_revision=ACME_REV_MOVED)
    return make_plan(dependencies=tuple(deps))


def written_lock(tmp_path: Path, plan: ExecutionPlan | None = None) -> Path:
    path = lock_path_for(tmp_path / "pipeline.yaml")
    write_lock(path, lock_from_plan(plan or make_plan()))
    return path


def snapshot(path: Path) -> tuple[bytes, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_mtime_ns


# --------------------------------------------------------------------------
# paths and canonicalisation
# --------------------------------------------------------------------------


def test_lock_path_is_a_sidecar_of_the_recipe(tmp_path: Path) -> None:
    assert lock_path_for(tmp_path / "pipeline.yaml") == tmp_path / "pipeline.lock.yaml"
    assert lock_path_for(tmp_path / "pipeline.yml") == tmp_path / "pipeline.lock.yaml"
    # A recipe with no YAML suffix still gets an unambiguous sidecar.
    assert lock_path_for(tmp_path / "recipe") == tmp_path / "recipe.lock.yaml"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (ACME_URI, "git+https://example.invalid/acme"),
        (BEHAVIOR_URI, "git+https://example.invalid/widget"),
        (LOCAL_URI, LOCAL_URI),
        ("git+ssh://git@example.invalid/acme@v1", "git+ssh://git@example.invalid/acme"),
    ],
)
def test_canonical_source_strips_ref_and_fragment(source: str, expected: str) -> None:
    assert canonical_source(source) == expected


# --------------------------------------------------------------------------
# round trip
# --------------------------------------------------------------------------


def test_lock_round_trips_write_then_read(tmp_path: Path) -> None:
    plan = make_plan()
    path = written_lock(tmp_path, plan)

    loaded = read_lock(path)

    assert loaded.lock_version == LOCK_VERSION
    assert loaded.recipe_digest == RECIPE_DIGEST
    assert loaded == lock_from_plan(plan)
    assert loaded.sources == (ACME_URI, BEHAVIOR_URI, LOCAL_URI)

    acme = loaded.entry_for(ACME_URI)
    assert acme is not None
    assert acme.declared_source == ACME_URI
    assert acme.canonical_source == "git+https://example.invalid/acme"
    assert acme.requested_ref == "main"
    assert acme.resolved_revision == ACME_REV
    assert acme.identity == ACME_REV

    behavior = loaded.entry_for(BEHAVIOR_URI)
    assert behavior is not None
    assert behavior.subdirectory == "behaviors/review.yaml"
    assert behavior.kind == DependencyKind.BEHAVIOR.value

    local = loaded.entry_for(LOCAL_URI)
    assert local is not None
    assert local.resolved_revision is None
    assert local.content_digest == LOCAL_DIGEST
    assert local.identity == LOCAL_DIGEST


def test_lock_is_written_deterministically(tmp_path: Path) -> None:
    first = written_lock(tmp_path)
    content = first.read_bytes()
    write_lock(first, lock_from_plan(make_plan()))
    assert first.read_bytes() == content


def test_lock_document_shape_is_yaml_with_lock_version(tmp_path: Path) -> None:
    data = yaml.safe_load(written_lock(tmp_path).read_text(encoding="utf-8"))
    assert data["lock_version"] == 1
    assert data["recipe_digest"] == RECIPE_DIGEST
    assert [d["declared_source"] for d in data["dependencies"]] == [ACME_URI, BEHAVIOR_URI, LOCAL_URI]
    # None-valued fields are omitted rather than written as nulls.
    assert "content_digest" not in data["dependencies"][0]


def test_unknown_lock_keys_and_bad_versions_are_errors(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.lock.yaml"

    path.write_text("lock_version: 1\ndependencies: []\nsurprise: true\n", encoding="utf-8")
    with pytest.raises(LockError, match="surprise"):
        read_lock(path)

    path.write_text("lock_version: 2\ndependencies: []\n", encoding="utf-8")
    with pytest.raises(LockVersionError):
        read_lock(path)

    path.write_text(
        "lock_version: 1\ndependencies:\n  - declared_source: x\n    nonsense: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(LockError, match="nonsense"):
        read_lock(path)


# --------------------------------------------------------------------------
# locked mode
# --------------------------------------------------------------------------


def test_locked_run_passes_and_never_rewrites_the_lock(tmp_path: Path) -> None:
    """Core 8: locks are never updated silently on run."""
    path = written_lock(tmp_path)
    before = snapshot(path)

    result = apply_lock_mode(make_plan(), path=path, mode=LockMode.LOCKED)

    assert result.mode is LockMode.LOCKED
    assert result.rewritten is False
    assert result.warnings == ()
    assert snapshot(path) == before


def test_locked_run_leaves_lock_untouched_even_when_it_fails(tmp_path: Path) -> None:
    path = written_lock(tmp_path)
    before = snapshot(path)

    with pytest.raises(ProvenanceMismatchError):
        apply_lock_mode(moved_plan(), path=path, mode=LockMode.LOCKED)

    assert snapshot(path) == before


def test_locked_mismatch_names_both_revisions(tmp_path: Path) -> None:
    path = written_lock(tmp_path)

    with pytest.raises(ProvenanceMismatchError) as excinfo:
        apply_lock_mode(moved_plan(), path=path, mode=LockMode.LOCKED)

    error = excinfo.value
    assert error.source == ACME_URI
    assert error.expected == ACME_REV
    assert error.actual == ACME_REV_MOVED
    message = str(error)
    assert ACME_REV in message
    assert ACME_REV_MOVED in message
    assert isinstance(error, PreflightError)


def test_locked_requires_an_entry_for_every_declared_dependency(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.lock.yaml"
    full = lock_from_plan(make_plan())
    write_lock(path, replace(full, entries=full.entries[:1]))

    with pytest.raises(LockEntryMissingError) as excinfo:
        apply_lock_mode(make_plan(), path=path, mode=LockMode.LOCKED)

    assert excinfo.value.source == BEHAVIOR_URI
    assert "update-lock" in str(excinfo.value)


def test_locked_rejects_an_entry_no_dependency_declares(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.lock.yaml"
    full = lock_from_plan(make_plan())
    stale = LockEntry(
        declared_source="git+https://example.invalid/ghost@main",
        canonical_source="git+https://example.invalid/ghost",
        resolved_revision="4" * 40,
    )
    write_lock(path, replace(full, entries=(*full.entries, stale)))

    with pytest.raises(LockEntryUnexpectedError) as excinfo:
        apply_lock_mode(make_plan(), path=path, mode=LockMode.LOCKED)

    assert excinfo.value.source == "git+https://example.invalid/ghost@main"


def test_locked_flags_a_changed_requested_ref(tmp_path: Path) -> None:
    path = written_lock(tmp_path)
    deps = list(_dependencies())
    deps[0] = replace(deps[0], requested_ref="next")

    with pytest.raises(ProvenanceMismatchError, match="requested_ref"):
        apply_lock_mode(make_plan(dependencies=tuple(deps)), path=path, mode=LockMode.LOCKED)


def test_locked_without_a_lockfile_fails_with_a_remedy(tmp_path: Path) -> None:
    path = lock_path_for(tmp_path / "pipeline.yaml")

    with pytest.raises(LockfileMissingError) as excinfo:
        apply_lock_mode(make_plan(), path=path, mode=LockMode.LOCKED)

    assert "update-lock" in str(excinfo.value)
    assert not path.exists()


def test_edited_recipe_body_warns_but_does_not_fail_locked_mode(tmp_path: Path) -> None:
    """Locked mode pins the dependency graph, not the prose of a step."""
    path = written_lock(tmp_path)
    edited = make_plan(recipe_digest="sha256:" + "c" * 64)

    result = apply_lock_mode(edited, path=path, mode=LockMode.LOCKED)

    assert result.rewritten is False
    assert len(result.warnings) == 1
    assert "recipe digest" in result.warnings[0]


def test_a_string_mode_is_accepted_but_an_unknown_one_never_relaxes(tmp_path: Path) -> None:
    path = written_lock(tmp_path)

    assert apply_lock_mode(make_plan(), path=path, mode="locked").mode is LockMode.LOCKED  # type: ignore[arg-type]

    with pytest.raises(LockError, match="Unsupported lock mode"):
        apply_lock_mode(make_plan(), path=path, mode="loose")  # type: ignore[arg-type]


def test_lock_entry_recomputes_a_missing_canonical_source(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.lock.yaml"
    path.write_text(
        f"lock_version: 1\ndependencies:\n  - declared_source: {ACME_URI}\n    resolved_revision: {ACME_REV}\n",
        encoding="utf-8",
    )

    entry = read_lock(path).entry_for(ACME_URI)

    assert entry is not None
    assert entry.canonical_source == "git+https://example.invalid/acme"


def test_lock_mode_defaults_to_the_plan_policy(tmp_path: Path) -> None:
    path = written_lock(tmp_path)
    before = snapshot(path)

    result = apply_lock_mode(make_plan(lock_mode=LockMode.LOCKED), path=path)

    assert result.mode is LockMode.LOCKED
    assert snapshot(path) == before


# --------------------------------------------------------------------------
# update-lock
# --------------------------------------------------------------------------


def test_update_lock_creates_the_lock_when_absent(tmp_path: Path) -> None:
    path = lock_path_for(tmp_path / "pipeline.yaml")

    result = apply_lock_mode(make_plan(), path=path, mode=LockMode.UPDATE_LOCK)

    assert result.rewritten is True
    assert path.exists()
    assert read_lock(path) == lock_from_plan(make_plan())


def test_update_lock_rewrites_a_moved_revision(tmp_path: Path) -> None:
    path = written_lock(tmp_path)
    assert read_lock(path).entry_for(ACME_URI).resolved_revision == ACME_REV  # type: ignore[union-attr]

    result = apply_lock_mode(moved_plan(), path=path, mode=LockMode.UPDATE_LOCK)

    assert result.rewritten is True
    entry = read_lock(path).entry_for(ACME_URI)
    assert entry is not None
    assert entry.resolved_revision == ACME_REV_MOVED
    # And the rewritten lock now satisfies a locked run.
    assert apply_lock_mode(moved_plan(), path=path, mode=LockMode.LOCKED).rewritten is False


def test_update_lock_drops_entries_no_longer_declared(tmp_path: Path) -> None:
    path = written_lock(tmp_path)
    trimmed = make_plan(dependencies=_dependencies()[:1])

    apply_lock_mode(trimmed, path=path, mode=LockMode.UPDATE_LOCK)

    assert read_lock(path).sources == (ACME_URI,)


# --------------------------------------------------------------------------
# unlocked
# --------------------------------------------------------------------------


def test_unlocked_warns_and_touches_no_lockfile(tmp_path: Path) -> None:
    path = lock_path_for(tmp_path / "pipeline.yaml")

    result = apply_lock_mode(make_plan(), path=path, mode=LockMode.UNLOCKED)

    assert result.mode is LockMode.UNLOCKED
    assert result.lock is None
    assert result.rewritten is False
    assert len(result.warnings) == 1
    assert "unlocked" in result.warnings[0]
    assert not path.exists()


def test_unlocked_neither_reads_nor_rewrites_an_existing_lock(tmp_path: Path) -> None:
    path = written_lock(tmp_path)
    before = snapshot(path)

    # A moved revision would fail locked mode; unlocked does not consult it.
    result = apply_lock_mode(moved_plan(), path=path, mode=LockMode.UNLOCKED)

    assert result.warnings
    assert result.lock is None
    assert snapshot(path) == before


# --------------------------------------------------------------------------
# verify_lock directly
# --------------------------------------------------------------------------


def test_verify_lock_accepts_the_lock_it_generated() -> None:
    plan = make_plan()
    assert verify_lock(lock_from_plan(plan), plan) == ()


def test_verify_lock_tolerates_a_lock_with_no_recorded_recipe_digest() -> None:
    plan = make_plan()
    lock = Lockfile(recipe_digest=None, entries=lock_from_plan(plan).entries)
    assert verify_lock(lock, plan) == ()


# --------------------------------------------------------------------------
# run manifest (Core 7)
# --------------------------------------------------------------------------


def test_run_manifest_records_every_core_7_fact() -> None:
    manifest = run_manifest_from_plan(make_plan(), run_id="run-1")

    assert manifest.run_id == "run-1"
    assert manifest.manifest_version == RUN_MANIFEST_VERSION
    assert manifest.recipe_digest == RECIPE_DIGEST
    assert manifest.schema_version == 2
    assert manifest.runner_version == "0.1.0"
    assert manifest.foundation_version == "9.9.9"
    assert manifest.step_ids == ("review", "write")

    # declared URI/ref -> resolved revision / content digest
    acme = manifest.dependency_for(ACME_URI)
    assert acme is not None
    assert (acme.requested_ref, acme.resolved_revision) == ("main", ACME_REV)
    local = manifest.dependency_for(LOCAL_URI)
    assert local is not None
    assert local.content_digest == LOCAL_DIGEST

    # included partials, agent map, effective policy
    assert manifest.partials == (BEHAVIOR_URI,)
    assert manifest.agent_dependency_map == {"acme:reviewer": ACME_URI, "local:writer": LOCAL_URI}
    assert manifest.policy is not None
    assert manifest.policy.lock_mode is LockMode.LOCKED
    assert manifest.policy.trust_policy == "strict"
    assert manifest.policy.capabilities == ("read",)
    assert manifest.created_at


def test_run_manifest_round_trips_through_json(tmp_path: Path) -> None:
    manifest = run_manifest_from_plan(make_plan(), run_id="run-1", created_at="2026-09-01T00:00:00+00:00")
    path = run_manifest_path_for(tmp_path / "runs" / "run-1")

    write_run_manifest(path, manifest)
    loaded = read_run_manifest(path)

    assert path.name == RUN_MANIFEST_FILENAME
    assert loaded == manifest
    assert loaded.dependencies == manifest.dependencies
    assert loaded.agents == manifest.agents
    assert loaded.policy == manifest.policy

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["manifest_version"] == RUN_MANIFEST_VERSION
    assert data["partials"] == [BEHAVIOR_URI]
    assert data["agents"]["acme:reviewer"]["supplied_by"] == ACME_URI


def test_run_manifest_json_is_deterministic(tmp_path: Path) -> None:
    manifest = run_manifest_from_plan(make_plan(), run_id="run-1", created_at="2026-09-01T00:00:00+00:00")
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"

    write_run_manifest(first, manifest)
    write_run_manifest(second, read_run_manifest(first))

    assert first.read_bytes() == second.read_bytes()


def test_dependency_identity_prefers_revision_then_digest() -> None:
    deps = _dependencies()
    assert dependency_identity(deps[0]) == ACME_REV
    assert dependency_identity(deps[2]) == LOCAL_DIGEST
    bare = ResolvedDependency(uri="x", kind=DependencyKind.BUNDLE)
    assert dependency_identity(bare) == "<unresolved>"


# --------------------------------------------------------------------------
# resume (Core 8)
# --------------------------------------------------------------------------


def test_resume_against_an_identical_re_resolution_passes(tmp_path: Path) -> None:
    path = run_manifest_path_for(tmp_path)
    write_run_manifest(path, run_manifest_from_plan(make_plan(), run_id="run-1"))

    check_resume_provenance(read_run_manifest(path), make_plan())


def test_resume_with_a_moved_revision_raises_naming_both(tmp_path: Path) -> None:
    """Core 8: a provenance mismatch fails visibly, never silently re-resolves."""
    path = run_manifest_path_for(tmp_path)
    write_run_manifest(path, run_manifest_from_plan(make_plan(), run_id="run-1"))
    recorded = read_run_manifest(path)

    with pytest.raises(ProvenanceMismatchError) as excinfo:
        check_resume_provenance(recorded, moved_plan())

    error = excinfo.value
    assert error.source == ACME_URI
    assert error.expected == ACME_REV
    assert error.actual == ACME_REV_MOVED
    assert error.run_id == "run-1"
    message = str(error)
    assert ACME_REV in message and ACME_REV_MOVED in message
    assert "re-resolve silently" in message
    # The recorded manifest is evidence, not something the check rewrites.
    assert read_run_manifest(path) == recorded


def test_resume_flags_a_changed_local_content_digest() -> None:
    recorded = run_manifest_from_plan(make_plan(), run_id="run-2")
    deps = list(_dependencies())
    deps[2] = replace(deps[2], content_digest="sha256:" + "d" * 64)

    with pytest.raises(ProvenanceMismatchError) as excinfo:
        check_resume_provenance(recorded, make_plan(dependencies=tuple(deps)))

    assert excinfo.value.source == LOCAL_URI
    assert excinfo.value.expected == LOCAL_DIGEST


def test_resume_flags_a_dropped_dependency() -> None:
    recorded = run_manifest_from_plan(make_plan(), run_id="run-3")

    with pytest.raises(ProvenanceMismatchError) as excinfo:
        check_resume_provenance(recorded, make_plan(dependencies=_dependencies()[:2]))

    assert excinfo.value.source == LOCAL_URI
    assert excinfo.value.actual == "<not declared>"


def test_resume_flags_an_added_dependency() -> None:
    recorded = run_manifest_from_plan(make_plan(dependencies=_dependencies()[:2]), run_id="run-4")

    with pytest.raises(ProvenanceMismatchError) as excinfo:
        check_resume_provenance(recorded, make_plan())

    assert excinfo.value.source == LOCAL_URI
    assert excinfo.value.expected == "<not recorded>"


def test_resume_flags_an_edited_recipe_and_can_be_told_not_to() -> None:
    recorded = run_manifest_from_plan(make_plan(), run_id="run-5")
    edited = make_plan(recipe_digest="sha256:" + "e" * 64)

    with pytest.raises(ProvenanceMismatchError) as excinfo:
        check_resume_provenance(recorded, edited)
    assert excinfo.value.expected == RECIPE_DIGEST

    check_resume_provenance(recorded, edited, compare_recipe_digest=False)


def test_resume_mismatch_is_a_preflight_error() -> None:
    recorded = run_manifest_from_plan(make_plan(), run_id="run-6")
    with pytest.raises(PreflightError):
        check_resume_provenance(recorded, moved_plan())


def test_run_manifest_from_mapping_rejects_a_non_mapping() -> None:
    with pytest.raises(ValueError, match="mapping"):
        RunManifest.from_mapping(["not", "a", "mapping"])
