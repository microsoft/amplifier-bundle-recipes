"""Engine provenance: which tool-recipes code actually ran, and how we prove it.

The failure these tests pin is not a crash.  It is a *clean pass* that proved
nothing: a lane repoints the shared bundle cache (recipes-669) or leaves the
CLI's editable ``.pth`` rewritten (recipes-ecd), and from then on every
``amplifier tool invoke recipes`` on the host runs somebody else's engine while
reporting success.  Nothing in the run said so, because nothing in the run knew
who it was.

So the tests below are about identity and refusal, in that order:

* the record says which file ran, where it came from, and whether the path is
  shadowed (symlinked cache, disagreeing ``.pth``);
* it rides beside a result rather than inside a legacy payload, because the
  legacy-compat baselines are pinned byte-for-byte;
* ``steps.jsonl`` carries the same identity in a header line, once per process;
* and ``scripts/live-proof.sh`` exits non-zero, naming the path that actually
  answered, whenever that path is not the worktree under test.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from amplifier_core import ToolResult

from amplifier_module_tool_recipes import ENGINE_IN_OUTPUT
from amplifier_module_tool_recipes import RecipesTool
from amplifier_module_tool_recipes import steps_log
from amplifier_module_tool_recipes.engine_provenance import engine_provenance
from amplifier_module_tool_recipes.engine_provenance import engine_provenance_of
from amplifier_module_tool_recipes.engine_provenance import label_engine_provenance
from amplifier_module_tool_recipes.executor import RecipeExecutor
from amplifier_module_tool_recipes.session import SessionManager
from amplifier_module_tool_recipes.steps_log import STEPS_LOG_FILENAME

# The package re-exports the ``engine_provenance`` *function*, which shadows the
# submodule of the same name on the package namespace -- so reach the module
# itself explicitly rather than through the attribute.
ep = importlib.import_module("amplifier_module_tool_recipes.engine_provenance")

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_PROOF = REPO_ROOT / "scripts" / "live-proof.sh"


@pytest.fixture(autouse=True)
def _fresh_provenance():
    """Every test starts from a cold record; none leaks into the next."""
    ep.reset_cache()
    yield
    ep.reset_cache()


def _tool(config: dict[str, Any] | None = None) -> RecipesTool:
    return RecipesTool(MagicMock(), MagicMock(), MagicMock(), config or {})


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


class TestTheRecord:
    def test_it_names_the_file_that_is_actually_running(self):
        record = engine_provenance()
        assert record["module"] == "amplifier_module_tool_recipes"
        assert Path(record["module_file"]).name == "__init__.py"
        assert Path(record["module_dir"]).is_dir()
        # The file under test *is* the file that answered.
        assert os.path.realpath(record["module_dir"]) == os.path.realpath(
            Path(ep.__file__).parent
        )

    def test_it_reports_the_resolved_path_beside_the_imported_one(self):
        record = engine_provenance()
        assert record["module_dir_real"] == os.path.realpath(record["module_dir"])
        assert record["module_dir_is_symlink"] == (
            record["module_dir"] != record["module_dir_real"]
        )

    def test_it_carries_the_fields_a_reader_needs_to_place_the_code(self):
        record = engine_provenance()
        for key in (
            "package_version",
            "git_sha",
            "git_ref",
            "git_root",
            "bundle_cache_dir",
            "bundle_cache_commit",
            "editable_pth",
            "python_executable",
            "warnings",
        ):
            assert key in record, key

    def test_the_record_is_cached_and_refreshable(self):
        first = engine_provenance()
        assert engine_provenance() == first
        assert engine_provenance(refresh=True)["module_file"] == first["module_file"]

    def test_a_collector_that_explodes_never_takes_the_run_down(self, monkeypatch):
        monkeypatch.setattr(ep, "_collect", lambda: 1 / 0)
        ep.reset_cache()
        record = engine_provenance()
        assert record["module"] == "amplifier_module_tool_recipes"
        assert record["warnings"], "an unavailable record must say so"


# ---------------------------------------------------------------------------
# git resolution -- the worktree shapes that a naive reader gets wrong
# ---------------------------------------------------------------------------


class TestGitResolution:
    def test_a_plain_repo_resolves_its_head(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        (git_dir / "refs" / "heads").mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        (git_dir / "refs" / "heads" / "main").write_text("a" * 40 + "\n")

        found = ep.find_git_dir(tmp_path / "modules" / "tool-recipes")
        assert found is not None
        resolved_dir, root = found
        assert root == tmp_path
        assert ep.resolve_head(resolved_dir) == ("a" * 40, "refs/heads/main")

    def test_a_linked_worktree_reads_its_ref_from_the_common_dir(self, tmp_path: Path):
        # The shape this repo actually has: `.git` is a FILE naming a gitdir
        # under the main repo, whose refs live in the shared commondir.
        common = tmp_path / "main.git"
        (common / "refs" / "heads" / "lane").mkdir(parents=True)
        (common / "refs" / "heads" / "lane" / "x").write_text("b" * 40 + "\n")

        wt_git = common / "worktrees" / "wt"
        wt_git.mkdir(parents=True)
        (wt_git / "HEAD").write_text("ref: refs/heads/lane/x\n")
        (wt_git / "commondir").write_text("../..\n")

        work = tmp_path / "work"
        work.mkdir()
        (work / ".git").write_text(f"gitdir: {wt_git}\n")

        found = ep.find_git_dir(work)
        assert found is not None
        assert ep.resolve_head(found[0]) == ("b" * 40, "refs/heads/lane/x")

    def test_a_packed_ref_still_resolves(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        (git_dir / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{'c' * 40} refs/heads/main\n"
        )
        assert ep.resolve_head(git_dir) == ("c" * 40, "refs/heads/main")

    def test_a_detached_head_reports_the_sha_and_no_ref(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("d" * 40 + "\n")
        assert ep.resolve_head(git_dir) == ("d" * 40, None)

    def test_an_unresolvable_ref_is_reported_not_guessed(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/ghost\n")
        assert ep.resolve_head(git_dir) == (None, "refs/heads/ghost")


# ---------------------------------------------------------------------------
# The bundle cache, and the two silent substitutions
# ---------------------------------------------------------------------------


class TestCacheAndShadowing:
    def test_the_cache_marker_supplies_the_commit_the_bundle_claims(
        self, tmp_path: Path
    ):
        cache = tmp_path / "amplifier-bundle-recipes-abc123"
        (cache / "modules" / "tool-recipes").mkdir(parents=True)
        (cache / ep.CACHE_META_FILENAME).write_text(
            json.dumps({"ref": "main", "commit": "e" * 40})
        )
        found = ep.find_cache_meta(cache / "modules" / "tool-recipes")
        assert found is not None
        cache_dir, meta = found
        assert cache_dir == cache
        assert meta["commit"] == "e" * 40

    def test_a_corrupt_cache_marker_is_survived_not_raised(self, tmp_path: Path):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / ep.CACHE_META_FILENAME).write_text("{not json")
        found = ep.find_cache_meta(cache)
        assert found is not None and found[1] == {}

    def test_a_symlinked_cache_is_named_on_both_sides(self):
        # recipes-669 exactly: the cache key still looks like the cache, and
        # every run on the host silently executes the tree it points at.
        record = {
            "module_dir": "/home/u/.amplifier/cache/bundle/modules/tool-recipes/pkg",
            "module_dir_real": "/lanes/other/modules/tool-recipes/pkg",
            "bundle_cache_dir": "/home/u/.amplifier/cache/bundle",
            "bundle_cache_resolves_to": "/lanes/other",
            "bundle_cache_is_symlink": True,
            "editable_pth": [],
        }
        warnings = ep.shadow_warnings(record)
        assert len(warnings) == 1
        assert "/home/u/.amplifier/cache/bundle" in warnings[0]
        assert "/lanes/other" in warnings[0]
        assert "SYMLINK" in warnings[0]

    def test_an_editable_pth_pointing_elsewhere_is_named_on_both_sides(self):
        # recipes-ecd: the cache directory was restored correctly and the leak
        # survived anyway, because the .pth still named a lane worktree.
        record = {
            "module_dir": "/home/u/.amplifier/cache/bundle/modules/tool-recipes/pkg",
            "module_dir_real": "/home/u/.amplifier/cache/bundle/modules/tool-recipes/pkg",
            "bundle_cache_dir": "/home/u/.amplifier/cache/bundle",
            "bundle_cache_resolves_to": "/home/u/.amplifier/cache/bundle",
            "bundle_cache_is_symlink": False,
            "editable_pth": [
                {
                    "pth": "/venv/site-packages/_editable_impl_x.pth",
                    "target": "/lanes/other/modules/tool-recipes",
                    "target_real": "/lanes/other/modules/tool-recipes",
                }
            ],
        }
        warnings = ep.shadow_warnings(record)
        assert len(warnings) == 1
        assert "_editable_impl_x.pth" in warnings[0]
        assert "/lanes/other/modules/tool-recipes" in warnings[0]
        assert "/home/u/.amplifier/cache/bundle" in warnings[0]

    def test_an_agreeing_pth_is_not_a_warning(self):
        record = {
            "module_dir": "/cache/bundle/modules/tool-recipes/pkg",
            "module_dir_real": "/cache/bundle/modules/tool-recipes/pkg",
            "bundle_cache_dir": "/cache/bundle",
            "bundle_cache_resolves_to": "/cache/bundle",
            "bundle_cache_is_symlink": False,
            "editable_pth": [
                {
                    "pth": "/venv/x.pth",
                    "target": "/cache/bundle/modules/tool-recipes",
                    "target_real": "/cache/bundle/modules/tool-recipes",
                }
            ],
        }
        assert ep.shadow_warnings(record) == []

    def test_code_outside_the_cache_is_never_warned_about(self):
        # A lane worktree on PYTHONPATH is the *sanctioned* path. Warning about
        # it would train everyone to ignore the warning that matters.
        record = {
            "module_dir": "/lanes/mine/modules/tool-recipes/pkg",
            "module_dir_real": "/lanes/mine/modules/tool-recipes/pkg",
            "bundle_cache_dir": None,
            "editable_pth": [
                {"pth": "/venv/x.pth", "target": "/somewhere", "target_real": "/somewhere"}
            ],
        }
        assert ep.shadow_warnings(record) == []

    def test_the_guard_logs_rather_than_raises(self, monkeypatch, caplog):
        monkeypatch.setattr(
            ep, "_collect", lambda: {"module": "x", "warnings": ["shadowed: a vs b"]}
        )
        ep.reset_cache()
        with caplog.at_level("WARNING"):
            emitted = ep.warn_if_shadowed()
        assert emitted == ["shadowed: a vs b"]
        assert "shadowed: a vs b" in caplog.text

    def test_the_guard_can_be_silenced_by_environment(self, monkeypatch):
        monkeypatch.setenv(ep.ENV_GUARD, "off")
        monkeypatch.setattr(ep, "_collect", lambda: {"warnings": ["noisy"]})
        ep.reset_cache()
        assert ep.warn_if_shadowed() == []


# ---------------------------------------------------------------------------
# How the record rides on a result
# ---------------------------------------------------------------------------


class TestResultLabelling:
    def test_it_rides_beside_the_payload(self):
        result = ToolResult(success=True, output={"status": "completed"})
        label_engine_provenance(result)
        assert engine_provenance_of(result)["module"] == "amplifier_module_tool_recipes"
        # ... and the payload itself is untouched: the legacy-compat baselines
        # pin exactly this dict byte-for-byte.
        assert result.output == {"status": "completed"}

    def test_an_unlabelled_result_reads_back_as_none(self):
        assert engine_provenance_of(ToolResult(success=True, output={})) is None

    @pytest.mark.asyncio
    async def test_every_operation_answers_with_its_engine_named(self):
        tool = _tool()

        async def _run(_input):
            return ToolResult(success=True, output={"status": "completed"})

        tool._execute_recipe = _run  # type: ignore[method-assign]
        result = await tool.execute({"operation": "execute"})

        assert engine_provenance_of(result) is not None
        # `execute` is baseline-pinned, so the record stays OUT of the payload.
        assert result.output == {"status": "completed"}

    @pytest.mark.asyncio
    async def test_even_an_unknown_operation_says_who_refused_it(self):
        result = await _tool().execute({"operation": "nonsense"})
        assert result.success is False
        assert engine_provenance_of(result) is not None

    @pytest.mark.asyncio
    async def test_read_only_operations_also_carry_it_inside_the_payload(self):
        # A caller that only ever sees the serialized result -- the CLI, an
        # agent -- can read it here. No baseline pins these operations.
        tool = _tool()

        async def _validate(_input):
            return ToolResult(success=True, output={"status": "valid"})

        tool._validate_recipe = _validate  # type: ignore[method-assign]
        result = await tool.execute({"operation": "validate"})
        assert result.output["engine"]["module"] == "amplifier_module_tool_recipes"

    def test_the_in_payload_set_is_read_only_operations_only(self):
        assert "execute" not in ENGINE_IN_OUTPUT
        assert "resume" not in ENGINE_IN_OUTPUT
        assert "approve" not in ENGINE_IN_OUTPUT
        assert "engine_info" in ENGINE_IN_OUTPUT


class TestEngineInfoOperation:
    @pytest.mark.asyncio
    async def test_it_answers_without_running_anything(self):
        tool = _tool()

        async def _explode(_input):  # pragma: no cover - must never be reached
            raise AssertionError("engine_info must not execute a recipe")

        tool._execute_recipe = _explode  # type: ignore[method-assign]
        result = await tool.execute({"operation": "engine_info"})

        assert result.success is True
        assert result.output["module_dir_real"] == os.path.realpath(
            Path(ep.__file__).parent
        )

    @pytest.mark.asyncio
    async def test_it_is_advertised_in_the_schema(self):
        tool = _tool()
        assert "engine_info" in tool.input_schema["properties"]["operation"]["enum"]

    @pytest.mark.asyncio
    async def test_it_reports_the_shadow_warnings_it_found(self, monkeypatch):
        monkeypatch.setattr(
            ep, "_collect", lambda: {"module": "x", "warnings": ["cache is a symlink"]}
        )
        ep.reset_cache()
        result = await _tool().execute({"operation": "engine_info"})
        assert result.output["warnings"] == ["cache is a symlink"]


# ---------------------------------------------------------------------------
# steps.jsonl header
# ---------------------------------------------------------------------------


class TestStepsLogHeader:
    @pytest.fixture(autouse=True)
    def _fresh_headers(self):
        steps_log._HEADED.clear()
        yield
        steps_log._HEADED.clear()

    def test_the_log_opens_by_naming_the_engine_that_writes_it(self, tmp_path: Path):
        log = steps_log.StepLog(tmp_path / STEPS_LOG_FILENAME)
        assert log.ensure_header() is True

        records = steps_log.read_steps_log(tmp_path)
        assert len(records) == 1
        header = records[0]
        assert header["event"] == steps_log.EVENT_HEADER
        assert header["pid"] == os.getpid()
        assert header["engine"]["module_dir_real"] == os.path.realpath(
            Path(ep.__file__).parent
        )

    def test_one_header_per_process_not_one_per_step(self, tmp_path: Path):
        path = tmp_path / STEPS_LOG_FILENAME
        assert steps_log.StepLog(path).ensure_header() is True
        for _ in range(5):
            assert steps_log.StepLog(path).ensure_header() is False
        headers = [
            r
            for r in steps_log.read_steps_log(tmp_path)
            if r.get("event") == steps_log.EVENT_HEADER
        ]
        assert len(headers) == 1

    def test_the_header_never_displaces_a_step_record(self, tmp_path: Path):
        log = steps_log.StepLog(tmp_path / STEPS_LOG_FILENAME)
        log.ensure_header()
        attempt = log.begin(step_id="one")
        attempt.start()
        attempt.finish(steps_log.STATUS_COMPLETED)

        records = steps_log.read_steps_log(tmp_path)
        assert [r["event"] for r in records] == ["header", "started", "finished"]

    def test_an_inert_log_writes_no_header(self, tmp_path: Path):
        assert steps_log.StepLog(None).ensure_header() is False
        assert list(tmp_path.iterdir()) == []

    def test_a_disabled_log_writes_no_header(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv(steps_log.ENV_ENABLED, "0")
        assert steps_log.StepLog(tmp_path / STEPS_LOG_FILENAME).ensure_header() is False
        assert list(tmp_path.iterdir()) == []

    def test_the_executor_heads_the_log_it_opens(self, tmp_path: Path):
        sessions = SessionManager(base_dir=tmp_path / "sessions")
        executor = RecipeExecutor(MagicMock(), sessions)
        log = executor._open_step_log("sid-1", tmp_path / "project")

        assert log.path is not None
        records = steps_log.read_steps_log(log.path.parent)
        assert records[0]["event"] == steps_log.EVENT_HEADER
        assert records[0]["engine"]["module_file"].endswith("__init__.py")

    def test_a_session_manager_without_the_header_still_runs(self, tmp_path: Path):
        """A duck-typed stand-in that predates the header loses the header, not the run."""

        class Ancient:
            def open_steps_log(self, session_id, project_path):
                class Bare:
                    path = None

                return Bare()

        executor = RecipeExecutor(MagicMock(), Ancient())
        assert executor._open_step_log("sid", tmp_path) is not None


# ---------------------------------------------------------------------------
# scripts/live-proof.sh -- the refusal
# ---------------------------------------------------------------------------


def _fake_amplifier(tmp_path: Path, payload: str, name: str = "amplifier") -> Path:
    """A stand-in CLI that prints one canned `tool invoke -o json` envelope."""
    script = tmp_path / name
    script.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"Bundle 'fake' prepared successfully\"\n"
        f"cat <<'JSON'\n{payload}\nJSON\n"
    )
    script.chmod(0o755)
    return script


def _envelope(result: Any) -> str:
    return json.dumps({"status": "success", "tool": "recipes", "result": repr(result)})


def _worktree(tmp_path: Path) -> Path:
    """The minimum shape live-proof.sh insists on before it will vouch for anything."""
    root = tmp_path / "worktree"
    (root / "modules" / "tool-recipes" / "amplifier_module_tool_recipes").mkdir(
        parents=True
    )
    (root / "src").mkdir()
    return root


def _run_live_proof(worktree: Path, fake: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["AMPLIFIER_BIN"] = str(fake)
    env["AMPLIFIER_PYTHON"] = sys.executable
    return subprocess.run(
        ["bash", str(LIVE_PROOF), str(worktree), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.mark.skipif(not LIVE_PROOF.exists(), reason="live-proof.sh not present")
class TestLiveProofScript:
    def test_it_passes_when_the_engine_is_the_worktree_under_test(self, tmp_path: Path):
        worktree = _worktree(tmp_path)
        expected = str(
            (worktree / "modules/tool-recipes/amplifier_module_tool_recipes").resolve()
        )
        fake = _fake_amplifier(tmp_path, _envelope({"module_dir_real": expected}))

        proc = _run_live_proof(worktree, fake)
        assert proc.returncode == 0, proc.stderr
        assert "engine verified" in proc.stdout
        assert expected in proc.stdout

    def test_it_refuses_and_names_the_engine_that_actually_answered(
        self, tmp_path: Path
    ):
        # The recipes-669 shape: the run works, and it is someone else's code.
        worktree = _worktree(tmp_path)
        intruder = "/lanes/some-other-lane/modules/tool-recipes/amplifier_module_tool_recipes"
        fake = _fake_amplifier(tmp_path, _envelope({"module_dir_real": intruder}))

        proc = _run_live_proof(worktree, fake)
        assert proc.returncode == 2
        assert "WRONG ENGINE" in proc.stderr
        assert intruder in proc.stderr, "the refusal must name whose code would have run"
        assert str(worktree) in proc.stderr

    def test_it_refuses_when_the_engine_cannot_report_provenance_at_all(
        self, tmp_path: Path
    ):
        # An engine predating `engine_info` is, by definition, not this worktree.
        worktree = _worktree(tmp_path)
        fake = _fake_amplifier(
            tmp_path, _envelope("Error: Unknown operation: engine_info")
        )

        proc = _run_live_proof(worktree, fake)
        assert proc.returncode == 2
        assert "NO ENGINE PROVENANCE" in proc.stderr
        assert "Unknown operation" in proc.stderr

    def test_it_does_not_run_the_invocation_when_the_engine_is_wrong(
        self, tmp_path: Path
    ):
        worktree = _worktree(tmp_path)
        marker = tmp_path / "it-ran"
        script = tmp_path / "amplifier"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ " $* " == *" operation=engine_info "* ]]; then\n'
            f"  cat <<'JSON'\n{_envelope({'module_dir_real': '/elsewhere'})}\nJSON\n"
            "  exit 0\n"
            "fi\n"
            f"touch {marker}\n"
        )
        script.chmod(0o755)

        proc = _run_live_proof(worktree, script, "--", "recipes", "operation=list")
        assert proc.returncode == 2
        assert not marker.exists(), "a failed assertion must stop before the run"

    def test_it_runs_the_invocation_once_the_engine_is_proven(self, tmp_path: Path):
        worktree = _worktree(tmp_path)
        expected = str(
            (worktree / "modules/tool-recipes/amplifier_module_tool_recipes").resolve()
        )
        marker = tmp_path / "it-ran"
        script = tmp_path / "amplifier"
        script.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ " $* " == *" operation=engine_info "* ]]; then\n'
            f"  cat <<'JSON'\n{_envelope({'module_dir_real': expected})}\nJSON\n"
            "  exit 0\n"
            "fi\n"
            f"touch {marker}\n"
            "exit 7\n"
        )
        script.chmod(0o755)

        proc = _run_live_proof(worktree, script, "--", "recipes", "operation=list")
        assert marker.exists(), "the proven environment must actually run the command"
        assert proc.returncode == 7, "the invocation's own exit status passes through"

    def test_it_puts_the_worktree_first_on_pythonpath(self, tmp_path: Path):
        worktree = _worktree(tmp_path)
        expected = str(
            (worktree / "modules/tool-recipes/amplifier_module_tool_recipes").resolve()
        )
        dump = tmp_path / "pythonpath.txt"
        script = tmp_path / "amplifier"
        script.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s" "$PYTHONPATH" > {dump}\n'
            f"cat <<'JSON'\n{_envelope({'module_dir_real': expected})}\nJSON\n"
        )
        script.chmod(0o755)

        _run_live_proof(worktree, script)
        first = dump.read_text().split(os.pathsep)[0]
        assert first == str(worktree / "modules" / "tool-recipes")

    def test_it_refuses_a_directory_that_is_not_a_bundle_worktree(self, tmp_path: Path):
        fake = _fake_amplifier(tmp_path, _envelope({"module_dir_real": "/x"}))
        proc = _run_live_proof(tmp_path / "worktree-does-not-exist", fake)
        assert proc.returncode == 3
        assert "not a directory" in proc.stderr
