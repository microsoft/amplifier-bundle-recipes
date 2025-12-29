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

    is_valid = len(errors) == 0

    return ValidationResult(
        is_valid=is_valid,
        errors=errors,
        warnings=warnings,
    )


def check_variable_references(recipe: Recipe) -> list[str]:
    """Check all {{variable}} references are defined or will be defined."""
    errors = []

    # Reserved variables always available
    reserved = {"recipe", "session", "step"}

    # Build set of available variables step by step
    available = set(recipe.context.keys()) | reserved

    for step in recipe.steps:
        # For foreach loops, the loop variable is available within the step
        step_local_vars = set()
        if step.foreach:
            loop_var = step.as_var or "item"
            step_local_vars.add(loop_var)

        # Check prompt variables (agent steps only - recipe steps have no prompt)
        if step.prompt:
            prompt_vars = extract_variables(step.prompt)
            for var in prompt_vars:
                # Check if it's a nested reference (recipe.name, session.id, etc.)
                if "." in var:
                    prefix = var.split(".")[0]
                    # Check if prefix is in reserved (recipe/session/step) OR available (step outputs)
                    if prefix not in reserved and prefix not in available and prefix not in step_local_vars:
                        errors.append(
                            f"Step '{step.id}': Variable {{{{{var}}}}} references unknown namespace '{prefix}'"
                        )
                elif var not in available and var not in step_local_vars:
                    errors.append(
                        f"Step '{step.id}': Variable {{{{{var}}}}} is not defined. "
                        f"Available variables: {', '.join(sorted(available | step_local_vars))}"
                    )

        # Check bash command variables (bash steps only)
        if step.command:
            command_vars = extract_variables(step.command)
            for var in command_vars:
                if "." in var:
                    prefix = var.split(".")[0]
                    if prefix not in reserved and prefix not in available and prefix not in step_local_vars:
                        errors.append(
                            f"Step '{step.id}': Command variable {{{{{var}}}}} references unknown namespace '{prefix}'"
                        )
                elif var not in available and var not in step_local_vars:
                    errors.append(
                        f"Step '{step.id}': Command variable {{{{{var}}}}} is not defined. "
                        f"Available variables: {', '.join(sorted(available | step_local_vars))}"
                    )

        # Check bash cwd variables
        if step.cwd:
            cwd_vars = extract_variables(step.cwd)
            for var in cwd_vars:
                if "." in var:
                    prefix = var.split(".")[0]
                    if prefix not in reserved and prefix not in available and prefix not in step_local_vars:
                        errors.append(
                            f"Step '{step.id}': cwd variable {{{{{var}}}}} references unknown namespace '{prefix}'"
                        )
                elif var not in available and var not in step_local_vars:
                    errors.append(
                        f"Step '{step.id}': cwd variable {{{{{var}}}}} is not defined. "
                        f"Available variables: {', '.join(sorted(available | step_local_vars))}"
                    )

        # Check bash env variables
        if step.env:
            for env_key, env_value in step.env.items():
                if isinstance(env_value, str):
                    env_vars = extract_variables(env_value)
                    for var in env_vars:
                        if "." in var:
                            prefix = var.split(".")[0]
                            if prefix not in reserved and prefix not in available and prefix not in step_local_vars:
                                errors.append(
                                    f"Step '{step.id}': env['{env_key}'] variable {{{{{var}}}}} references unknown namespace '{prefix}'"
                                )
                        elif var not in available and var not in step_local_vars:
                            errors.append(
                                f"Step '{step.id}': env['{env_key}'] variable {{{{{var}}}}} is not defined. "
                                f"Available variables: {', '.join(sorted(available | step_local_vars))}"
                            )

        # Check recipe step context variables (recipe steps only)
        if step.step_context:
            for key, value in step.step_context.items():
                if isinstance(value, str):
                    context_vars = extract_variables(value)
                    for var in context_vars:
                        if "." in var:
                            prefix = var.split(".")[0]
                            # Check if prefix is in reserved (recipe/session/step) OR available (step outputs)
                            if prefix not in reserved and prefix not in available and prefix not in step_local_vars:
                                errors.append(
                                    f"Step '{step.id}': Context key '{key}' variable {{{{{var}}}}} references unknown namespace '{prefix}'"
                                )
                        elif var not in available and var not in step_local_vars:
                            errors.append(
                                f"Step '{step.id}': Context key '{key}' variable {{{{{var}}}}} is not defined. "
                                f"Available variables: {', '.join(sorted(available | step_local_vars))}"
                            )

        # Check recipe path variables (for dynamic recipe paths)
        if step.recipe:
            recipe_vars = extract_variables(step.recipe)
            for var in recipe_vars:
                if "." in var:
                    prefix = var.split(".")[0]
                    # Check if prefix is in reserved (recipe/session/step) OR available (step outputs)
                    if prefix not in reserved and prefix not in available and prefix not in step_local_vars:
                        errors.append(
                            f"Step '{step.id}': Recipe path variable {{{{{var}}}}} references unknown namespace '{prefix}'"
                        )
                elif var not in available and var not in step_local_vars:
                    errors.append(
                        f"Step '{step.id}': Recipe path variable {{{{{var}}}}} is not defined. "
                        f"Available variables: {', '.join(sorted(available | step_local_vars))}"
                    )

        # Add this step's output to available variables for next steps
        if step.output:
            available.add(step.output)

        # Add collect variable to available variables for next steps (foreach)
        if step.collect:
            available.add(step.collect)

    return errors


def extract_variables(template: str) -> set[str]:
    """Extract all {{variable}} references from template string."""
    pattern = r"\{\{(\w+(?:\.\w+)?)\}\}"
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

        for step in recipe.steps:
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

    step_ids = {step.id for step in recipe.steps}

    # Check each step's dependencies
    for i, step in enumerate(recipe.steps):
        for dep_id in step.depends_on:
            # Check dependency exists
            if dep_id not in step_ids:
                errors.append(f"Step '{step.id}': depends_on references unknown step '{dep_id}'")
                continue

            # Check dependency appears before this step
            dep_step = recipe.get_step(dep_id)
            if dep_step:
                dep_index = recipe.steps.index(dep_step)
                if dep_index >= i:
                    errors.append(
                        f"Step '{step.id}': depends_on '{dep_id}' but '{dep_id}' "
                        f"appears later in recipe (index {dep_index} >= {i})"
                    )

        # Check for circular dependencies (simplified check)
        if step.id in step.depends_on:
            errors.append(f"Step '{step.id}': cannot depend on itself")

    return errors
