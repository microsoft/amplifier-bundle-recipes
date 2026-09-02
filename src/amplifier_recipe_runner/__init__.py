"""amplifier-recipe-runner -- library-first, dependency-declared recipe execution.

This package is the **one execution home** (``recipe-runner-lib.v1`` Core 1):
manifest parsing, dependency planning, collision detection, resolution,
provenance, run state, and execution orchestration live here. The standalone
``recipe-runner`` CLI, the Amplifier ``recipes`` tool module, and any other
host are thin adapters over this surface.

``__all__`` below is the deliberate public API. Anything not listed is
internal and may change without notice.

Nothing in this exported surface imports Amplifier: the library is usable
without the Amplifier CLI installed (lib Core 2), and Amplifier's
``coordinator``/session objects are not public API (lib Core 3).

Manifest parsing types (``recipe-dependency-manifest.v1``) and the concrete
runner implementation land in sibling modules and are exported as they arrive.

Two module-level entry points are wired (lib Core 2):

* :func:`plan` -- parse the manifest and resolve the dependency closure into an
  :class:`ExecutionPlan`. No fetch beyond reading dependencies, no module
  activation, no session, no step.
* :func:`run` -- plan, then execute the recipe in a session the *recipe* owns,
  whose entire agent surface is that plan (``recipe-dependency-manifest.v1``
  Core 3, 4, 5).

Both take a :class:`RunRequest` and are usable with no UI and no Amplifier CLI.
"""

from __future__ import annotations

from .api import RUN_MANIFEST_VERSION
from .api import AgentProvenance
from .api import DependencyKind
from .api import EffectivePolicy
from .api import ExecutionPlan
from .api import ExecutionSession
from .api import LockMode
from .api import RecipeRunner
from .api import ResolvedDependency
from .api import RunRequest
from .api import RunResult
from .api import RunStatus
from .api import TrustPolicy
from .api import ValidationIssue
from .api import ValidationReport
from .errors import AgentCollisionError
from .errors import LegacyRecipeError
from .errors import ManifestValidationError
from .errors import PreflightError
from .errors import ProvenanceMismatchError
from .errors import RecipeRunnerError
from .errors import TrustRefusedError
from .errors import UndeclaredAgentError
from .execution import plan
from .execution import run
from .ports import HOST_PORTS
from .ports import ApprovalCallback
from .ports import ApprovalDecision
from .ports import ApprovalRequest
from .ports import CancellationToken
from .ports import EventSink
from .ports import HostServices
from .ports import ProviderAccess
from .ports import ProviderHandle
from .ports import RunEvent
from .ports import WorkspacePath

__version__ = "0.1.0"

__all__ = [
    # version / constants
    "HOST_PORTS",
    "RUN_MANIFEST_VERSION",
    "__version__",
    # entry points (lib Core 2) -- resolve a closure, then run in it
    "plan",
    "run",
    # api -- requests, results, plan
    "AgentProvenance",
    "DependencyKind",
    "EffectivePolicy",
    "ExecutionPlan",
    "ExecutionSession",
    "LockMode",
    "RecipeRunner",
    "ResolvedDependency",
    "RunRequest",
    "RunResult",
    "RunStatus",
    "TrustPolicy",
    "ValidationIssue",
    "ValidationReport",
    # ports -- the five host seams and their payloads
    "ApprovalCallback",
    "ApprovalDecision",
    "ApprovalRequest",
    "CancellationToken",
    "EventSink",
    "HostServices",
    "ProviderAccess",
    "ProviderHandle",
    "RunEvent",
    "WorkspacePath",
    # errors -- typed preflight model
    "AgentCollisionError",
    "LegacyRecipeError",
    "ManifestValidationError",
    "PreflightError",
    "ProvenanceMismatchError",
    "RecipeRunnerError",
    "TrustRefusedError",
    "UndeclaredAgentError",
]

# recipes-4qf exports
# ``resume`` is the third module-level entry point (lib Core 2 names
# validate/plan/run/resume): ``run`` with the steps a recorded run already
# completed skipped, on the same execution path rather than a second one. The
# docstring above predates it and still says "two"; the count is stale, this
# list is not.
from .execution import resume  # noqa: E402

__all__ += ["resume"]
