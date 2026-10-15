"""The parser profile is a pinned build input, never a runtime download."""

from __future__ import annotations

from importlib import metadata

from .model import ShellSourceError


def require_parser_profile() -> None:
    try:
        import tree_sitter_bash
        from tree_sitter import Language

        versions = (
            metadata.version("tree-sitter"),
            metadata.version("tree-sitter-bash"),
        )
        language = Language(tree_sitter_bash.language())
        actual = (
            language.abi_version,
            language.name,
            language.semantic_version,
            language.node_kind_count,
            language.field_count,
        )
        required = (15, "bash", (0, 25, 1), 280, 19)
        kinds = {
            "command": 207,
            "command_name": 208,
            "word": 1,
            "string": 237,
            "raw_string": 105,
            "variable_assignment": 209,
            "file_redirect": 212,
            "test_command": 203,
            "arithmetic_expansion": 227,
            "command_substitution": 253,
            "expansion": 242,
            "simple_expansion": 241,
            "heredoc_redirect": 213,
        }
        if (
            versions != ("0.25.2", "0.25.1")
            or actual != required
            or any(
                language.id_for_node_kind(name, True) != identity
                for name, identity in kinds.items()
            )
        ):
            raise ShellSourceError(
                "PARSER_PROFILE",
                "installed Bash parser does not match the qualified profile",
            )
    except (ImportError, metadata.PackageNotFoundError) as error:
        raise ShellSourceError(
            "PARSER_PROFILE", "pinned Bash parser dependencies are not installed"
        ) from error
