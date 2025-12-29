"""Recipe data models and YAML parsing."""

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Literal

import yaml


@dataclass
class RecursionConfig:
    """Recursion protection configuration for recipe composition."""

    max_depth: int = 5  # Default: 5, configurable 1-20
    max_total_steps: int = 100  # Default: 100, configurable 1-1000

    def validate(self) -> list[str]:
        """Validate recursion config."""
        errors = []
        if not 1 <= self.max_depth <= 20:
            errors.append(f"recursion.max_depth must be 1-20, got {self.max_depth}")
        if not 1 <= self.max_total_steps <= 1000:
            errors.append(f"recursion.max_total_steps must be 1-1000, got {self.max_total_steps}")
        return errors


@dataclass
class ApprovalConfig:
    """Approval gate configuration for a stage."""

    required: bool = False  # Whether approval is needed to proceed
    prompt: str = ""  # Message shown to user when requesting approval
    timeout: int = 0  # Seconds to wait for approval (0 = wait forever, which is the default)
    default: Literal["deny", "approve"] = "deny"  # What happens on timeout

    def validate(self) -> list[str]:
        """Validate approval configuration."""
        errors = []
        if self.timeout < 0:
            errors.append("approval.timeout must be non-negative")
        if self.default not in ("deny", "approve"):
            errors.append(f"approval.default must be 'deny' or 'approve', got '{self.default}'")
        if self.required and not self.prompt:
            errors.append("approval.prompt is required when approval.required is true")
        return errors


@dataclass
class Stage:
    """Represents a stage in a multi-stage recipe workflow."""

    name: str
    steps: list["Step"]
    approval: ApprovalConfig | None = None

    def validate(self) -> list[str]:
        """Validate stage structure and constraints."""
        errors = []

        if not self.name:
            errors.append("Stage missing required field: name")

        if not self.name.replace("-", "").replace("_", "").replace(" ", "").isalnum():
            errors.append(f"Stage name must be alphanumeric with hyphens/underscores/spaces, got '{self.name}'")

        if not self.steps:
            errors.append(f"Stage '{self.name}': must have at least one step")

        # Validate each step
        for step in self.steps:
            step_errors = step.validate()
            for err in step_errors:
                errors.append(f"Stage '{self.name}': {err}")

        # Check step ID uniqueness within stage
        step_ids = [step.id for step in self.steps]
        duplicates = [sid for sid in step_ids if step_ids.count(sid) > 1]
        if duplicates:
            errors.append(f"Stage '{self.name}': duplicate step IDs: {', '.join(set(duplicates))}")

        # Validate approval config if present
        if self.approval:
            approval_errors = self.approval.validate()
            for err in approval_errors:
                errors.append(f"Stage '{self.name}': {err}")

        return errors


@dataclass
class Step:
    """Represents a single step in a recipe workflow.

    Step types (mutually exclusive):
    - "agent" (default): Spawn an LLM agent with a prompt
    - "recipe": Execute a sub-recipe
    - "bash": Execute shell command directly (no LLM overhead)
    """

    id: str
    # Agent step fields (required when type="agent")
    agent: str | None = None
    prompt: str | None = None
    mode: str | None = None
    agent_config: dict[str, Any] | None = None

    # Recipe composition fields (required when type="recipe")
    type: Literal["agent", "recipe", "bash"] = "agent"
    recipe: str | None = None  # Path to sub-recipe file
    step_context: dict[str, Any] | None = None  # Context to pass to sub-recipe (YAML: "context")

    # Bash step fields (required when type="bash")
    command: str | None = None  # Shell command to execute
    cwd: str | None = None  # Working directory (supports {{variable}} substitution)
    env: dict[str, str] | None = None  # Environment variables (values support {{variable}})
    output_exit_code: str | None = None  # Variable name to store exit code

    # Common fields
    output: str | None = None
    condition: str | None = None
    foreach: str | None = None
    as_var: str | None = None  # Maps to 'as' in YAML (as is Python reserved)
    collect: str | None = None
    parallel: bool = False  # Run all foreach iterations concurrently
    max_iterations: int = 100
    timeout: int = 600
    retry: dict[str, Any] | None = None
    on_error: str = "fail"
    depends_on: list[str] = field(default_factory=list)

    # JSON parsing control
    parse_json: bool = False  # Default: preserve output as-is, only parse clean JSON

    # Per-step recursion override (for recipe steps only)
    recursion: RecursionConfig | None = None

    def validate(self) -> list[str]:
        """Validate step structure and constraints."""
        errors = []

        # Required fields
        if not self.id:
            errors.append("Step missing required field: id")

        # Type-specific validation
        if self.type == "agent":
            # Agent steps require agent and prompt
            if not self.agent:
                errors.append(f"Step '{self.id}': agent steps require 'agent' field")
            if not self.prompt:
                errors.append(f"Step '{self.id}': agent steps require 'prompt' field")
            # Agent steps cannot have recipe-specific fields
            if self.recipe:
                errors.append(f"Step '{self.id}': agent steps cannot have 'recipe' field")
            if self.step_context:
                errors.append(f"Step '{self.id}': agent steps cannot have 'context' field")
            # Agent steps cannot have bash-specific fields
            if self.command:
                errors.append(f"Step '{self.id}': agent steps cannot have 'command' field")
        elif self.type == "recipe":
            # Recipe steps require recipe path
            if not self.recipe:
                errors.append(f"Step '{self.id}': recipe steps require 'recipe' field")
            # Recipe steps cannot have agent-specific fields
            if self.agent:
                errors.append(f"Step '{self.id}': recipe steps cannot have 'agent' field")
            if self.prompt:
                errors.append(f"Step '{self.id}': recipe steps cannot have 'prompt' field")
            if self.mode:
                errors.append(f"Step '{self.id}': recipe steps cannot have 'mode' field")
            # Recipe steps cannot have bash-specific fields
            if self.command:
                errors.append(f"Step '{self.id}': recipe steps cannot have 'command' field")
            # Validate recursion config if present
            if self.recursion:
                errors.extend(self.recursion.validate())
        elif self.type == "bash":
            # Bash steps require command
            if not self.command:
                errors.append(f"Step '{self.id}': bash steps require 'command' field")
            elif not self.command.strip():
                errors.append(f"Step '{self.id}': bash command cannot be empty or whitespace")
            # Bash steps cannot have agent-specific fields
            if self.agent:
                errors.append(f"Step '{self.id}': bash steps cannot have 'agent' field")
            if self.prompt:
                errors.append(f"Step '{self.id}': bash steps cannot have 'prompt' field")
            if self.mode:
                errors.append(f"Step '{self.id}': bash steps cannot have 'mode' field")
            if self.agent_config:
                errors.append(f"Step '{self.id}': bash steps cannot have 'agent_config' field")
            # Bash steps cannot have recipe-specific fields
            if self.recipe:
                errors.append(f"Step '{self.id}': bash steps cannot have 'recipe' field")
            if self.step_context:
                errors.append(f"Step '{self.id}': bash steps cannot have 'context' field")
            if self.recursion:
                errors.append(f"Step '{self.id}': bash steps cannot have 'recursion' field")
            # Validate output_exit_code name
            if self.output_exit_code:
                if not self.output_exit_code.replace("_", "").isalnum():
                    errors.append(f"Step '{self.id}': output_exit_code must be alphanumeric with underscores")
                if self.output_exit_code in ("recipe", "session", "step"):
                    errors.append(f"Step '{self.id}': output_exit_code '{self.output_exit_code}' is reserved")
        else:
            errors.append(f"Step '{self.id}': type must be 'agent', 'recipe', or 'bash', got '{self.type}'")

        # Field constraints (common to both types)
        if self.timeout <= 0:
            errors.append(f"Step '{self.id}': timeout must be positive")

        if self.on_error not in ("fail", "continue", "skip_remaining"):
            errors.append(f"Step '{self.id}': on_error must be 'fail', 'continue', or 'skip_remaining'")

        # Output name validation
        if self.output:
            if not self.output.replace("_", "").isalnum():
                errors.append(f"Step '{self.id}': output name must be alphanumeric with underscores")
            if self.output in ("recipe", "session", "step"):
                errors.append(f"Step '{self.id}': output name '{self.output}' is reserved")

        # Retry validation
        if self.retry:
            max_attempts = self.retry.get("max_attempts", 1)
            if not isinstance(max_attempts, int) or max_attempts <= 0:
                errors.append(f"Step '{self.id}': retry.max_attempts must be positive integer")

            backoff = self.retry.get("backoff", "exponential")
            if backoff not in ("exponential", "linear"):
                errors.append(f"Step '{self.id}': retry.backoff must be 'exponential' or 'linear'")

        # Loop validation
        if self.foreach:
            if "{{" not in self.foreach:
                errors.append(f"Step '{self.id}': foreach must contain a variable reference (e.g., '{{{{items}}}}')")
            if self.as_var and not self.as_var.replace("_", "").isalnum():
                errors.append(f"Step '{self.id}': 'as' must be a valid variable name")
            if self.collect and not self.collect.replace("_", "").isalnum():
                errors.append(f"Step '{self.id}': 'collect' must be a valid variable name")
            if self.max_iterations <= 0:
                errors.append(f"Step '{self.id}': max_iterations must be positive")

        # Parallel validation
        if self.parallel and not self.foreach:
            errors.append(f"Step '{self.id}': parallel requires foreach")

        return errors


@dataclass
class Recipe:
    """Represents a complete recipe specification.

    Supports two modes:
    1. Flat steps mode (original): steps field contains list of steps
    2. Staged mode (Phase 3): stages field contains list of Stage objects with approval gates

    Only one of 'steps' or 'stages' should be populated.
    """

    name: str
    description: str
    version: str
    steps: list[Step] = field(default_factory=list)  # Flat steps mode
    stages: list[Stage] = field(default_factory=list)  # Staged mode with approval gates
    author: str | None = None
    created: str | None = None
    updated: str | None = None
    tags: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    recursion: RecursionConfig | None = None  # Recipe-level recursion config

    @property
    def is_staged(self) -> bool:
        """Return True if recipe uses staged mode with approval gates."""
        return len(self.stages) > 0

    def get_all_steps(self) -> list[Step]:
        """Get all steps from either flat or staged mode."""
        if self.is_staged:
            all_steps = []
            for stage in self.stages:
                all_steps.extend(stage.steps)
            return all_steps
        return self.steps

    @classmethod
    def _parse_step(cls, step_data: dict[str, Any]) -> Step:
        """Parse a single step from YAML data."""
        if not isinstance(step_data, dict):
            raise ValueError("Each step must be a dictionary")

        step_data_copy = dict(step_data)

        # Map 'as' to 'as_var' since 'as' is Python reserved keyword
        if "as" in step_data_copy:
            step_data_copy["as_var"] = step_data_copy.pop("as")

        # Map 'context' to 'step_context' (context at step level is for sub-recipes)
        if "context" in step_data_copy:
            step_data_copy["step_context"] = step_data_copy.pop("context")

        # Parse step-level recursion config if present
        if "recursion" in step_data_copy and isinstance(step_data_copy["recursion"], dict):
            step_data_copy["recursion"] = RecursionConfig(**step_data_copy["recursion"])

        return Step(**step_data_copy)

    @classmethod
    def _parse_approval_config(cls, approval_data: dict[str, Any] | None) -> ApprovalConfig | None:
        """Parse approval configuration from YAML data."""
        if approval_data is None:
            return None
        if not isinstance(approval_data, dict):
            raise ValueError("approval must be a dictionary")
        return ApprovalConfig(**approval_data)

    @classmethod
    def _parse_stage(cls, stage_data: dict[str, Any]) -> Stage:
        """Parse a single stage from YAML data."""
        if not isinstance(stage_data, dict):
            raise ValueError("Each stage must be a dictionary")

        # Parse steps within stage
        steps_data = stage_data.get("steps", [])
        if not isinstance(steps_data, list):
            raise ValueError("Stage 'steps' must be a list")

        steps = [cls._parse_step(sd) for sd in steps_data]

        # Parse approval config if present
        approval = cls._parse_approval_config(stage_data.get("approval"))

        return Stage(
            name=stage_data.get("name", ""),
            steps=steps,
            approval=approval,
        )

    @classmethod
    def from_yaml(cls, path: Path) -> "Recipe":
        """Load recipe from YAML file."""
        if not path.exists():
            raise FileNotFoundError(f"Recipe file not found: {path}")

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError("Recipe YAML must be a dictionary")

        # Check for exclusive modes: stages vs flat steps
        has_stages = "stages" in data and data["stages"]
        has_steps = "steps" in data and data["steps"]

        if has_stages and has_steps:
            raise ValueError("Recipe cannot have both 'stages' and 'steps' - use one or the other")

        # Parse stages (Phase 3 mode)
        stages: list[Stage] = []
        if has_stages:
            stages_data = data["stages"]
            if not isinstance(stages_data, list):
                raise ValueError("'stages' must be a list")
            stages = [cls._parse_stage(sd) for sd in stages_data]

        # Parse flat steps (original mode)
        steps: list[Step] = []
        if has_steps:
            steps_data = data["steps"]
            if not isinstance(steps_data, list):
                raise ValueError("'steps' must be a list")
            steps = [cls._parse_step(sd) for sd in steps_data]

        # Parse recipe-level recursion config if present
        recursion_config = None
        if "recursion" in data and isinstance(data["recursion"], dict):
            recursion_config = RecursionConfig(**data["recursion"])

        # Create recipe
        recipe = cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            version=data.get("version", ""),
            steps=steps,
            stages=stages,
            author=data.get("author"),
            created=data.get("created"),
            updated=data.get("updated"),
            tags=data.get("tags", []),
            context=data.get("context", {}),
            recursion=recursion_config,
        )

        return recipe

    def validate(self) -> list[str]:
        """Validate recipe structure and constraints."""
        errors = []

        # Required fields
        if not self.name:
            errors.append("Recipe missing required field: name")
        if not self.description:
            errors.append("Recipe missing required field: description")
        if not self.version:
            errors.append("Recipe missing required field: version")

        # Name constraints
        if self.name and not self.name.replace("-", "").replace("_", "").isalnum():
            errors.append("Recipe name must be alphanumeric with hyphens/underscores")

        # Version format (strict semver check - MAJOR.MINOR.PATCH only)
        if self.version:
            # Check for v prefix (not allowed)
            if self.version.startswith("v"):
                errors.append("Recipe version must follow semver format without 'v' prefix (use '1.0.0' not 'v1.0.0')")
            # Check for pre-release or build metadata (not allowed for simplicity)
            elif "-" in self.version or "+" in self.version:
                errors.append(
                    "Recipe version must follow simple semver format (MAJOR.MINOR.PATCH only, no pre-release tags)"
                )
            else:
                parts = self.version.split(".")
                if len(parts) != 3:
                    errors.append("Recipe version must follow semver format (MAJOR.MINOR.PATCH)")
                elif not all(part.isdigit() for part in parts):
                    errors.append("Recipe version parts must be numeric (e.g., '1.0.0' not '1.a.0')")

        # Must have either steps or stages (but not both - checked during parsing)
        if not self.steps and not self.stages:
            errors.append("Recipe must have at least one step or stage")

        # Validate based on mode
        if self.is_staged:
            errors.extend(self._validate_staged_mode())
        else:
            errors.extend(self._validate_flat_mode())

        # Validate recipe-level recursion config
        if self.recursion:
            errors.extend(self.recursion.validate())

        return errors

    def _validate_flat_mode(self) -> list[str]:
        """Validate flat steps mode."""
        errors = []

        # Validate each step
        for step in self.steps:
            step_errors = step.validate()
            errors.extend(step_errors)

        # Check step ID uniqueness
        step_ids = [step.id for step in self.steps]
        duplicates = [sid for sid in step_ids if step_ids.count(sid) > 1]
        if duplicates:
            errors.append(f"Duplicate step IDs: {', '.join(set(duplicates))}")

        # Validate depends_on references
        step_id_set = set(step_ids)
        for step in self.steps:
            for dep_id in step.depends_on:
                if dep_id not in step_id_set:
                    errors.append(f"Step '{step.id}': depends_on references unknown step '{dep_id}'")

        # Check for circular dependencies (simple check)
        for step in self.steps:
            if step.id in step.depends_on:
                errors.append(f"Step '{step.id}': cannot depend on itself")

        return errors

    def _validate_staged_mode(self) -> list[str]:
        """Validate staged mode with approval gates."""
        errors = []

        # Check stage name uniqueness
        stage_names = [stage.name for stage in self.stages]
        duplicates = [name for name in stage_names if stage_names.count(name) > 1]
        if duplicates:
            errors.append(f"Duplicate stage names: {', '.join(set(duplicates))}")

        # Validate each stage
        for stage in self.stages:
            stage_errors = stage.validate()
            errors.extend(stage_errors)

        # Check step ID uniqueness across all stages
        all_step_ids = []
        for stage in self.stages:
            all_step_ids.extend([step.id for step in stage.steps])

        step_duplicates = [sid for sid in all_step_ids if all_step_ids.count(sid) > 1]
        if step_duplicates:
            errors.append(f"Duplicate step IDs across stages: {', '.join(set(step_duplicates))}")

        # Validate depends_on references across all stages
        step_id_set = set(all_step_ids)
        for stage in self.stages:
            for step in stage.steps:
                for dep_id in step.depends_on:
                    if dep_id not in step_id_set:
                        errors.append(
                            f"Stage '{stage.name}', Step '{step.id}': depends_on references unknown step '{dep_id}'"
                        )

        # Check for circular dependencies
        for stage in self.stages:
            for step in stage.steps:
                if step.id in step.depends_on:
                    errors.append(f"Stage '{stage.name}', Step '{step.id}': cannot depend on itself")

        return errors

    def get_step(self, step_id: str) -> Step | None:
        """Get step by ID from either flat or staged mode."""
        for step in self.get_all_steps():
            if step.id == step_id:
                return step
        return None

    def get_stage(self, stage_name: str) -> Stage | None:
        """Get stage by name (staged mode only)."""
        for stage in self.stages:
            if stage.name == stage_name:
                return stage
        return None
