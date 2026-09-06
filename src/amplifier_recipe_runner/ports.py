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
    "ProviderSpec",
    "RunEvent",
    "WorkspacePath",
    "provider_specs",
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

#: A host-owned provider client. Still typed as ``object``: a host stays free
#: to hand over whatever its provider layer uses, and the runner carries an
#: uninterpretable handle through untouched exactly as before.
#:
#: **One shape is now understood.** A handle that IS a :class:`ProviderSpec`
#: (or a sequence of them, or a mapping shaped like one) says enough for the
#: runner to *mount* that provider into a recipe's composed session -- which is
#: what lets the ``provider_access`` port serve a recipe whose own closure pins
#: no provider. :func:`provider_specs` is the one place that interpretation
#: happens, and it returns ``()`` for anything else rather than guessing.
ProviderHandle = NewType("ProviderHandle", object)


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One host provider, in the shape a composed bundle can mount.

    Deliberately the same four keys a bundle's own ``providers:`` entry and an
    Amplifier settings ``config.providers`` entry already use, so bridging is a
    copy rather than a translation:

    ``module``
        The provider module's id (e.g. ``provider-anthropic``).
    ``source``
        Where that module comes from (e.g. a ``git+https://`` URI).
    ``config``
        The module's own configuration -- model, endpoint, credentials.
        Carried verbatim; the runner reads only :attr:`model` out of it, for
        provenance.
    ``id``
        Optional instance name, when a host runs several instances of one
        provider module (``sonnet`` and ``opus``, both ``provider-anthropic``).

    Nothing here is an agent, an agent map, or a session: a spec names a model
    provider and its configuration, which is exactly what the port is for.
    """

    module: str
    source: str
    config: Mapping[str, Any] = field(default_factory=dict)
    id: str | None = None

    @property
    def instance(self) -> str:
        """How this provider is named in provenance: instance id, else module."""
        return self.id or self.module

    @property
    def model(self) -> str | None:
        """The model this instance resolves to, when its config names one."""
        for key in ("default_model", "model"):
            value = self.config.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def to_mount(self) -> dict[str, Any]:
        """The bundle mount-plan entry for this provider."""
        entry: dict[str, Any] = {"module": self.module, "source": self.source}
        if self.config:
            entry["config"] = dict(self.config)
        if self.id:
            entry["id"] = self.id
        return entry

    @classmethod
    def coerce(cls, value: Any) -> ProviderSpec | None:
        """``value`` as a spec, or ``None`` when it is not one.

        Accepts a :class:`ProviderSpec` and a mapping carrying ``module`` and
        ``source``. Anything else -- a bare string, a provider-preference chain
        (``{"provider": ..., "model": ...}``, which names no module source) --
        is *not* coerced: inventing a module source from a provider nickname
        would be a guess, and a guessed provider is exactly the silent
        substitution this port refuses to make.
        """
        if isinstance(value, ProviderSpec):
            return value
        if not isinstance(value, Mapping):
            return None
        module = value.get("module")
        source = value.get("source")
        if not isinstance(module, str) or not module or not isinstance(source, str) or not source:
            return None
        config = value.get("config")
        identifier = value.get("id")
        return cls(
            module=module,
            source=source,
            config=dict(config) if isinstance(config, Mapping) else {},
            id=identifier if isinstance(identifier, str) and identifier else None,
        )


def provider_specs(handle: Any) -> tuple[ProviderSpec, ...]:
    """Every mountable :class:`ProviderSpec` inside ``handle``, in order.

    The single interpretation point for a :data:`ProviderHandle`. Returns an
    empty tuple when the handle carries nothing mountable -- which the caller
    must report, never paper over.
    """
    single = ProviderSpec.coerce(handle)
    if single is not None:
        return (single,)
    if isinstance(handle, (str, bytes)) or not isinstance(handle, Sequence):
        return ()
    specs = tuple(spec for spec in (ProviderSpec.coerce(item) for item in handle) if spec is not None)
    return specs


@runtime_checkable
class ProviderAccess(Protocol):
    """Grants the run access to *approved* model providers, and nothing else."""

    def roles(self) -> Sequence[str]:
        """Model roles this host is willing to serve (e.g. ``("general",)``)."""
        ...

    def resolve(self, role: str) -> ProviderHandle:
        """Return the provider handle for ``role``.

        Return a :class:`ProviderSpec` (or a sequence of them) for a handle the
        runner can mount into a recipe's own session; anything else is carried
        through uninterpreted.

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
