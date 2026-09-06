"""The declarative ``context:`` entry -- recognised, resolved, or refused.

A recipe's ``context:`` block is a variable -> VALUE mapping. Authors
nonetheless reach for the shape every other tool's input schema uses::

    context:
      topic:
        type: string
        required: true
        description: "What to investigate"
      continue_from:
        type: string
        default: ""

Before this module, nothing recognised that shape. ``{**recipe.context,
**context_vars}`` bound the *declaration dict itself* as the variable's value,
so ``{{continue_from}}`` substituted ``{'type': 'string', 'default': '',
'description': '...'}`` into prompts (as noise) and into conditions (as a hard
failure -- ``Invalid expression: Unexpected character '{' at position 0``).
The recipe had never been runnable and nothing said so (``recipes-u2f``).

This module removes the silent third option. An entry recognised as a
declaration is either **bound to its ``default:``**, or **reported by name** --
never bound as the dict.

Recognition
-----------
A value is a declaration when it is a **non-empty mapping whose every key is
drawn from** :data:`DECLARATION_KEYS`. A mapping carrying any other key is an
ordinary literal value and is bound unchanged, which is what keeps existing
recipes with dict-valued context entries working. An empty mapping ``{}`` is a
literal too.

Resolution
----------
* ``default:`` present -> that value is bound.
* else ``required: true`` -> the caller must supply it; if no value is
  supplied at execution time the run fails naming the variable
  (:class:`ContextDeclarationError`).
* else -> the variable is **not bound at all**. Referencing it then fails
  loudly through the engine's existing "variable not found" path rather than
  silently substituting a dict.

A caller-supplied value always wins, exactly as it did before.

Malformed declarations
----------------------
:func:`declaration_issues` reports a declaration whose own fields are wrong --
a non-boolean ``required``, an unknown ``type``, a non-string ``description``,
an ``enum`` that is not a non-empty list, or a ``default`` outside its
``enum`` -- as an error **naming the variable**. ``required: true`` beside a
``default:`` is contradictory but harmless (the default binds, so the variable
is never missing), so it is a warning, not an error.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DECLARATION_KEYS",
    "DECLARED_TYPES",
    "ContextDeclarationError",
    "ResolvedContext",
    "declaration_issues",
    "is_context_declaration",
    "merge_recipe_context",
    "resolve_context",
]

#: The keys a schema-form ``context:`` entry may carry. A mapping whose keys
#: are all drawn from this set (and which is non-empty) is a declaration.
DECLARATION_KEYS: frozenset[str] = frozenset(
    {"type", "required", "default", "description", "enum"}
)

#: Accepted values of ``type:``. Documentation only -- the engine never
#: coerces a value -- but a typo is still worth naming.
DECLARED_TYPES: frozenset[str] = frozenset(
    {"string", "number", "integer", "boolean", "array", "object", "any"}
)


class ContextDeclarationError(ValueError):
    """A ``context:`` declaration is malformed, or a required value is unset.

    Carries the offending variable names so a caller can report them without
    re-parsing the message.
    """

    def __init__(self, message: str, *, variables: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.variables = variables


@dataclass(frozen=True)
class ResolvedContext:
    """The outcome of reading a recipe's ``context:`` block."""

    #: Variable -> value, with every declaration replaced by its ``default:``.
    #: A declaration with no default is absent, not None.
    values: dict[str, Any]

    #: Variables declared ``required: true`` with no ``default:`` -- the caller
    #: must supply each one.
    required: tuple[str, ...] = ()

    #: Malformed declarations, one message per problem, each naming its
    #: variable.
    errors: tuple[str, ...] = ()

    #: Non-fatal oddities, each naming its variable.
    warnings: tuple[str, ...] = ()


def is_context_declaration(value: Any) -> bool:
    """True when ``value`` is a schema-form declaration rather than a value."""
    if not isinstance(value, Mapping) or not value:
        return False
    return all(isinstance(key, str) and key in DECLARATION_KEYS for key in value)


def declaration_issues(name: str, declaration: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for one declaration, naming ``name``."""
    errors: list[str] = []
    warnings: list[str] = []
    where = f"Context variable '{name}'"

    if "required" in declaration and not isinstance(declaration["required"], bool):
        errors.append(
            f"{where}: 'required' must be true or false, got "
            f"{declaration['required']!r}"
        )

    if "type" in declaration:
        declared = declaration["type"]
        if not isinstance(declared, str) or declared not in DECLARED_TYPES:
            errors.append(
                f"{where}: 'type' must be one of "
                f"{', '.join(sorted(DECLARED_TYPES))}, got {declared!r}"
            )

    if "description" in declaration and not isinstance(declaration["description"], str):
        errors.append(
            f"{where}: 'description' must be a string, got "
            f"{type(declaration['description']).__name__}"
        )

    if "enum" in declaration:
        choices = declaration["enum"]
        if not isinstance(choices, list) or not choices:
            errors.append(f"{where}: 'enum' must be a non-empty list, got {choices!r}")
        elif "default" in declaration and declaration["default"] not in choices:
            errors.append(
                f"{where}: default {declaration['default']!r} is not one of its "
                f"'enum' values {choices!r}"
            )

    if declaration.get("required") is True and "default" in declaration:
        warnings.append(
            f"{where} declares both 'required: true' and a default; the default "
            f"is bound, so the variable is never missing (drop one)"
        )

    return errors, warnings


def resolve_context(context: Mapping[str, Any] | None) -> ResolvedContext:
    """Read a recipe's ``context:`` block into values plus diagnostics.

    Never raises and never binds a declaration dict: a malformed declaration
    contributes an error and is left unbound.
    """
    values: dict[str, Any] = {}
    required: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []

    for name, value in (context or {}).items():
        if not is_context_declaration(value):
            values[name] = value
            continue

        key = str(name)
        entry_errors, entry_warnings = declaration_issues(key, value)
        errors.extend(entry_errors)
        warnings.extend(entry_warnings)
        if entry_errors:
            # Malformed: report it, bind nothing. Binding the dict is exactly
            # the failure this module exists to end.
            continue

        if "default" in value:
            values[name] = value["default"]
        elif value.get("required") is True:
            required.append(key)
        # else: declared but optional and defaultless -- deliberately unbound.

    return ResolvedContext(
        values=values,
        required=tuple(required),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def merge_recipe_context(
    recipe_context: Mapping[str, Any] | None,
    context_vars: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge a recipe's declared context with caller-supplied variables.

    The replacement for ``{**recipe.context, **context_vars}``: identical for
    every recipe that does not use the declarative form.

    Raises:
        ContextDeclarationError: if any declaration is malformed, or if a
            variable declared ``required: true`` has no default and no
            caller-supplied value. The message names the variables.
    """
    resolved = resolve_context(recipe_context)
    supplied = dict(context_vars or {})

    if resolved.errors:
        malformed = _names(resolved.errors)
        raise ContextDeclarationError(
            "Malformed context declaration(s) in recipe: "
            + "; ".join(resolved.errors),
            variables=malformed,
        )

    missing = tuple(name for name in resolved.required if name not in supplied)
    if missing:
        listed = ", ".join(repr(name) for name in missing)
        raise ContextDeclarationError(
            f"Required context variable(s) not supplied: {listed}. Pass a value "
            f"for each (context={{'{missing[0]}': ...}}), or give the "
            f"declaration a 'default:'.",
            variables=missing,
        )

    return {**resolved.values, **supplied}


def _names(errors: tuple[str, ...]) -> tuple[str, ...]:
    """Variable names mentioned by ``errors``, in first-seen order."""
    found: list[str] = []
    for message in errors:
        _, _, rest = message.partition("Context variable '")
        name, _, _ = rest.partition("'")
        if name and name not in found:
            found.append(name)
    return tuple(found)
