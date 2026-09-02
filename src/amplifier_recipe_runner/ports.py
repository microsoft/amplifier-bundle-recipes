"""Host ports -- the complete, closed set of integration seams.

Contract: ``recipe-runner-lib.v1`` Core 4::

    Hosts integrate exclusively through narrow, explicitly named ports:
    provider access, approval callback, event sink, workspace path,
    cancellation. No port grants the host's ambient agent map to the recipe.

There are exactly five ports and :data:`HOST_PORTS` names them. Adding a sixth
is a contract change (``Reserved: Additional host port names``), not a code
change -- which is why the count is asserted by the test suite.

**What is deliberately absent.** No port accepts, returns, or otherwise exposes
an agent map, agent catalog, caller session, or coordinator. A recipe's agents
resolve *only* from its declared dependency closure plus the runner baseline
(``recipe-dependency-manifest.v1`` Core 3, Core 4). A host that could hand its
own agents across this seam would defeat isolation, so the seam has no shape
that could carry them.

This module imports nothing from Amplifier (lib Core 3).
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Final
from typing import NewType
from typing import Protocol
from typing import runtime_checkable

__all__ = [
    "HOST_PORTS",
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
]


#: The five host ports, in contract order. Exhaustive by construction.
HOST_PORTS: Final[tuple[str, ...]] = (
    "provider_access",
    "approval_callback",
    "event_sink",
    "workspace",
    "cancellation",
)


# --------------------------------------------------------------------------
# Port 1: provider access
# --------------------------------------------------------------------------

#: An opaque, host-owned provider client. The runner passes it through and
#: never introspects it, so hosts stay free to hand over whatever their
#: provider layer uses.
ProviderHandle = NewType("ProviderHandle", object)


@runtime_checkable
class ProviderAccess(Protocol):
    """Grants the run access to *approved* model providers, and nothing else."""

    def roles(self) -> Sequence[str]:
        """Model roles this host is willing to serve (e.g. ``("general",)``)."""
        ...

    def resolve(self, role: str) -> ProviderHandle:
        """Return an opaque provider handle for ``role``.

        Raise ``KeyError`` if the host does not serve that role -- an
        unavailable provider is a real failure, never a silent downgrade.
        """
        ...


# --------------------------------------------------------------------------
# Port 2: approval callback
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A pause point the host must answer before the run continues."""

    run_id: str
    stage: str
    prompt: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """The host's answer to an :class:`ApprovalRequest`."""

    approved: bool
    message: str | None = None


@runtime_checkable
class ApprovalCallback(Protocol):
    """Answers approval gates. Absent callback means "no gate may pass"."""

    async def __call__(self, request: ApprovalRequest) -> ApprovalDecision: ...


# --------------------------------------------------------------------------
# Port 3: event sink
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunEvent:
    """A single observable moment in a run.

    The event *schema* is deliberately unstable: ``recipe-runner-lib.v1``
    lists "streaming/event schema stabilization" as Backlogged, with the
    promotion trigger "first external consumer parsing events
    programmatically". Until then ``kind`` and ``data`` are advisory.
    """

    kind: str
    run_id: str
    data: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class EventSink(Protocol):
    """Receives run events. Must not raise; a sink failure never fails a run."""

    def emit(self, event: RunEvent) -> None: ...


# --------------------------------------------------------------------------
# Port 4: workspace path
# --------------------------------------------------------------------------

#: The directory a run may read and write. The runner treats it as the only
#: filesystem location it is entitled to touch.
WorkspacePath = NewType("WorkspacePath", Path)


# --------------------------------------------------------------------------
# Port 5: cancellation
# --------------------------------------------------------------------------


@runtime_checkable
class CancellationToken(Protocol):
    """Lets a host stop a run cooperatively."""

    @property
    def cancelled(self) -> bool:
        """True once the host has requested cancellation."""
        ...

    def raise_if_cancelled(self) -> None:
        """Raise the host's cancellation exception if cancellation was requested."""
        ...


# --------------------------------------------------------------------------
# The bundle a host hands to the runner
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HostServices:
    """Exactly the five ports, bundled.

    Field names match :data:`HOST_PORTS` one-for-one. There is no ``agents``,
    ``session``, or ``coordinator`` field, and adding one would be a contract
    change.
    """

    provider_access: ProviderAccess
    workspace: WorkspacePath
    approval_callback: ApprovalCallback | None = None
    event_sink: EventSink | None = None
    cancellation: CancellationToken | None = None
