"""Recipe validation logic."""

import re
from dataclasses import dataclass
from typing import Any

from .models import Recipe


@dataclass
class ValidationResult:
    """Result of recipe validation."""

    is_valid: bool
    errors: list[str]
    warnings: list[str]


def validate_recipe(recipe: Recipe, coordinator: Any = None) -> ValidationResult:
    """
    Comprehensive recipe validation.

    Args:
        recipe: Recipe to validate
        coordinator: Optional coordinator for agent availability checking

    Returns:
        ValidationResult with errors and warnings
    """
    errors = []
    warnings = []

    # Basic structure validation
    structure_errors = recipe.validate()
    errors.extend(structure_errors)

    # Variable reference validation
    var_errors = check_variable_references(recipe)
    errors.extend(var_errors)

    # Agent availability (if coordinator provided)
    if coordinator:
        agent_warnings = check_agent_availability(recipe, coordinator)
        warnings.extend(agent_warnings)

    # Dependency validation
    dep_errors = check_step_dependencies(recipe)
    errors.extend(dep_errors)

    # Parallel foreach + recipe step warning
    all_steps = list(recipe.steps)
    for stage in recipe.stages:
        all_steps.extend(stage.steps)
    for step in all_steps:
        if step.foreach and step.parallel and step.type == "recipe":
            warnings.append(
                f"Step '{step.id}': parallel foreach with type='recipe' may cause issues "
                f"if the sub-recipe has approval gates "
                f"(parallel approval gates are undefined behavior)"
            )

    is_valid = len(errors) == 0

    return ValidationResult(
        is_valid=is_valid,
        errors=errors,
        warnings=warnings,
    )


def _validate_dot_path(
    var: str,
    step_id: str,
    recipe_context: dict[str, Any],
) -> str | None:
    """Traverse a dot-path variable into known context dicts.

    Called only after the caller has confirmed the prefix exists in
    recipe_context.  This function is purely about dict traversal —
    prefix-routing (reserved, step-output, loop-var, unknown) is handled
    by ``_check_var_ref``.

    Returns an error message string if invalid, or None if valid/skipped.
    """
    parts = var.split(".")
    prefix = parts[0]

    value = recipe_context.get(prefix)
    if not isinstance(value, dict):
        # Prefix exists in context but is not a dict — dot-access is invalid.
        # parts[1] is always safe: this function is only called for dot-paths
        # (caller checks "." in var), so len(parts) >= 2.
        return (
            f"Step '{step_id}': Variable {{{{{var}}}}} — "
            f"key '{prefix}' is not a dict "
            f"(cannot access '{parts[1]}' on type {type(value).__name__})"
        )

    # Traverse remaining parts
    current = value
    for i, part in enumerate(parts[1:], start=1):
        if not isinstance(current, dict):
            return (
                f"Step '{step_id}': Variable {{{{{var}}}}} — "
                f"key '{parts[i - 1]}' is not a dict "
                f"(cannot access '{part}' on type {type(current).__name__})"
            )
        if part not in current:
            parent_path = ".".join(parts[:i]) if i > 1 else parts[0]
            available_keys = ", ".join(sorted(current.keys()))
            return (
                f"Step '{step_id}': Variable {{{{{var}}}}} — "
                f"key '{part}' not found in '{parent_path}'. "
                f"Available keys: {available_keys}"
            )
        current = current[part]

    return None  # Valid


def _check_var_ref(
    var: str,
    step_id: str,
    field_label: str,
    recipe_context: dict[str, Any],
    reserved: set[str],
    available: set[str],
    step_local_vars: set[str],
) -> str | None:
    """Check a single variable reference.

    Handles both simple variables (``name``) and dot-path references
    (``requirements.character_design``).  For dot-paths whose prefix is
    a known recipe-context dict, delegates to ``_validate_dot_path`` for
    deeper key traversal.

    Returns an error message or *None* on success.
    """
    if "." in var:
        prefix = var.split(".")[0]

        # Unknown prefix → error
        if (
            prefix not in reserved
            and prefix not in available
            and prefix not in step_local_vars
        ):
            return (
                f"Step '{step_id}': {field_label} {{{{{var}}}}} "
                f"references unknown namespace '{prefix}'"
            )

        # Reserved namespaces (recipe, session, step) — runtime-only, skip
        if prefix in reserved:
            return None

        # Step outputs — runtime-only, can't validate nested keys.
        # (Must come after the reserved check above; a context key that
        #  shadows a reserved name is already handled.)
        if prefix in available and prefix not in recipe_context:
            return None

        # Loop variables — skip
        if prefix in step_local_vars:
            return None

        # Prefix is in recipe context — attempt deeper dict traversal
        return _validate_dot_path(var, step_id, recipe_context)

    # Simple (non-dot) variable
    if var not in available and var not in step_local_vars:
        return (
            f"Step '{step_id}': {field_label} {{{{{var}}}}} is not defined. "
            f"Available variables: {', '.join(sorted(available | step_local_vars))}"
        )
    return None


def check_variable_references(recipe: Recipe) -> list[str]:
    """Check all {{variable}} references are defined or will be defined."""
    errors = []

    # Reserved variables always available
    reserved = {"recipe", "session", "step"}

    # Build set of available variables step by step
    available = set(recipe.context.keys()) | reserved

    for step in recipe.get_all_steps():
        # For foreach loops, the loop variable is available within the step
        step_local_vars: set[str] = set()
        if step.foreach:
            loop_var = step.as_var or "item"
            step_local_vars.add(loop_var)

        # --- Check each field that can contain {{variable}} refs ----------

        # Prompt (agent steps)
        if step.prompt:
            for var in extract_variables(step.prompt):
                err = _check_var_ref(
                    var,
                    step.id,
                    "Variable",
                    recipe.context,
                    reserved,
                    available,
                    step_local_vars,
                )
                if err:
                    errors.append(err)

        # Command (bash steps)
        if step.command:
            for var in extract_variables(step.command):
                err = _check_var_ref(
                    var,
                    step.id,
                    "Command variable",
                    recipe.context,
                    reserved,
                    available,
                    step_local_vars,
                )
                if err:
                    errors.append(err)

        # Working directory
        if step.cwd:
            for var in extract_variables(step.cwd):
                err = _check_var_ref(
                    var,
                    step.id,
                    "cwd variable",
                    recipe.context,
                    reserved,
                    available,
                    step_local_vars,
                )
                if err:
                    errors.append(err)

        # Environment variables
        if step.env:
            for env_key, env_value in step.env.items():
                if isinstance(env_value, str):
                    for var in extract_variables(env_value):
                        err = _check_var_ref(
                            var,
                            step.id,
                            f"env['{env_key}'] variable",
                            recipe.context,
                            reserved,
                            available,
                            step_local_vars,
                        )
                        if err:
                            errors.append(err)

        # Step context (recipe steps)
        if step.step_context:
            for key, value in step.step_context.items():
                if isinstance(value, str):
                    for var in extract_variables(value):
                        err = _check_var_ref(
                            var,
                            step.id,
                            f"Context key '{key}' variable",
                            recipe.context,
                            reserved,
                            available,
                            step_local_vars,
                        )
                        if err:
                            errors.append(err)

        # Recipe path (dynamic recipe paths)
        if step.recipe:
            for var in extract_variables(step.recipe):
                err = _check_var_ref(
                    var,
                    step.id,
                    "Recipe path variable",
                    recipe.context,
                    reserved,
                    available,
                    step_local_vars,
                )
                if err:
                    errors.append(err)

        # --- Accumulate outputs for subsequent steps ---------------------

        # Add this step's output to available variables for next steps
        if step.output:
            available.add(step.output)

        # Add collect variable to available variables for next steps (foreach)
        if step.collect:
            available.add(step.collect)

        # Add output_exit_code variable to available variables for next steps (bash)
        if step.output_exit_code:
            available.add(step.output_exit_code)

    return errors


def extract_variables(template: str) -> set[str]:
    """Extract all {{variable}} references from template string."""
    pattern = r"\{\{(\w+(?:\.\w+)*)\}\}"
    matches = re.findall(pattern, template)
    return set(matches)


def check_agent_availability(recipe: Recipe, coordinator: Any) -> list[str]:
    """
    Check if agents referenced in recipe are available.

    Note: This returns warnings, not errors, since agent availability
    may vary by environment and profile.
    """
    warnings = []

    # Get available agents from coordinator (if supported)
    # This is a best-effort check
    try:
        available_agents = getattr(coordinator, "available_agents", None)
        if available_agents is None:
            # Can't check - skip this validation
            return warnings

        if callable(available_agents):
            available_agents = available_agents()

        # Type guard for available_agents
        if not isinstance(available_agents, list | set | dict):
            return warnings

        for step in recipe.get_all_steps():
            if step.agent not in available_agents:
                warnings.append(
                    f"Step '{step.id}': Agent '{step.agent}' may not be available. "
                    f"Ensure it's installed before running this recipe."
                )

    except Exception:
        # Agent availability check failed - not critical
        pass

    return warnings


def check_step_dependencies(recipe: Recipe) -> list[str]:
    """Check step dependencies are valid and acyclic."""
    errors = []

    all_steps = recipe.get_all_steps()
    step_ids = {step.id for step in all_steps}

    # Check each step's dependencies
    for i, step in enumerate(all_steps):
        for dep_id in step.depends_on:
            # Check dependency exists
            if dep_id not in step_ids:
                errors.append(
                    f"Step '{step.id}': depends_on references unknown step '{dep_id}'"
                )
                continue

            # Check dependency appears before this step
            dep_step = recipe.get_step(dep_id)
            if dep_step:
                dep_index = all_steps.index(dep_step)
                if dep_index >= i:
                    errors.append(
                        f"Step '{step.id}': depends_on '{dep_id}' but '{dep_id}' "
                        f"appears later in recipe (index {dep_index} >= {i})"
                    )

        # Check for circular dependencies (simplified check)
        if step.id in step.depends_on:
            errors.append(f"Step '{step.id}': cannot depend on itself")

    return errors
