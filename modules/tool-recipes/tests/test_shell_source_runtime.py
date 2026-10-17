from pathlib import Path

import pytest
from amplifier_module_tool_recipes.shell_source import runtime
from amplifier_module_tool_recipes.shell_source.model import ShellSourceError


def test_actual_selected_bash_image_is_qualified_without_a_probe_process(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("probe process"))
    result = runtime.require_runtime_profile("/bin/bash")
    assert result.version == "5.2.21"
    assert len(result.sha256) == 64
    assert result.executable == str(Path("/bin/bash").resolve())


@pytest.mark.parametrize("change", ["old", "unknown", "multiple", "wrapper"])
def test_unqualified_selected_images_fail_before_launch(tmp_path, change):
    image = Path("/bin/bash").read_bytes()
    if change == "old":
        image = image.replace(b"Bash version 5.2.21", b"Bash version 4.2.21")
    elif change == "unknown":
        image = image.replace(b"Bash version ", b"Unknown text ")
    elif change == "multiple":
        image += b"@(#)Bash version 5.2.21(1) release GNU\x00"
    else:
        image = b"#!/bin/sh\n# @(#)Bash version 5.2.21(1) release GNU\x00\n"
    path = tmp_path / "unqualified-bash"
    path.write_bytes(image)
    with pytest.raises(ShellSourceError, match="BASH_RUNTIME_PROFILE"):
        runtime.require_runtime_profile(str(path))


def test_changed_file_metadata_during_read_is_rejected(monkeypatch):
    original = runtime._identity
    calls = 0

    def changing(info):
        nonlocal calls
        calls += 1
        return original(info) + (int(calls > 1),)

    monkeypatch.setattr(runtime, "_identity", changing)
    with pytest.raises(ShellSourceError, match="changed during qualification"):
        runtime.require_runtime_profile("/bin/bash")


def test_unchanged_zero_hole_script_does_not_add_a_runtime_requirement():
    assert (
        runtime.qualify_template_runtime("printf unchanged", "/not/a/runtime") is None
    )
