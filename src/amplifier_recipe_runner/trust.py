"""Trust policy: what may be fetched or activated, decided *before* anything is.

Contracts: ``recipe-dependency-manifest.v1`` Core 6 and Core 9;
``recipe-runner-lib.v1`` Core 6.

Two questions live here, and nothing else:

* **May this source be touched at all?** :meth:`TrustPolicy.check_source` is
  asked once per declared dependency, by the planner, *before* the resolver is
  handed a single source (manifest Core 6, lib Core 6). A refusal raises
  :class:`~amplifier_recipe_runner.errors.TrustRefusedError` naming the source
  and the rule that refused it. Nothing has been fetched, cloned, read, or
  activated at that point -- which is the whole reason the check sits where it
  sits.
* **What may it then do?** :func:`intersect_capabilities` computes the Core 9
  three-way intersection -- host policy ∩ runner policy ∩ manifest-declared
  needs -- into an explicit :class:`EffectiveCapabilities` record that says not
  only what was granted but which term withheld each capability that was not.

Design notes that are load-bearing, not incidental
--------------------------------------------------
**Every field is enforced somewhere.** A policy field that no code path reads
is a lie told to the caller: it looks like a control and is not one. So each
field has a method that consults it -- ``allowed_schemes`` / ``allowed_hosts``
/ ``allowed_local_roots`` / ``require_immutable_refs`` in
:meth:`~TrustPolicy.check_source`, ``require_immutable_refs`` and
``require_content_digest`` again in :meth:`~TrustPolicy.check_resolved` (where
resolved facts finally exist), ``allow_dependency_install`` in
:meth:`~TrustPolicy.check_dependency_install`, and ``capability_allowlist`` in
:meth:`~TrustPolicy.effective_capabilities`.

**``None`` means unconstrained; empty means nothing.** ``allowed_hosts=None``
permits any host, ``allowed_hosts=frozenset()`` permits none. The distinction
is deliberate and the two are never conflated -- an empty allowlist silently
meaning "allow everything" is precisely the failure this module exists to
prevent. ``allowed_schemes`` has no ``None`` form: a policy always says which
schemes it accepts.

**Immutable means a full-length hex revision.** A branch or a tag moves, so
neither satisfies ``require_immutable_refs``; only a 40-hex (SHA-1) or 64-hex
(SHA-256) object id does. A ``locked_ref`` supplied by a caller is held to the
same bar -- a lock entry that pins a moving ref pins nothing.

This module imports nothing from Amplifier (lib Core 3), performs no I/O, and
touches no network.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from typing import NoReturn

from .errors import TrustRefusedError
from .resolver import split_source

__all__ = [
    "CONTRACTS",
    "DEFAULT_LOCAL_SCHEMES",
    "DEFAULT_REMOTE_SCHEMES",
    "IMMUTABLE_REF_PATTERN",
    "LOCAL_SCHEMES",
    "EffectiveCapabilities",
    "SourceFacts",
    "TrustPolicy",
    "intersect_capabilities",
    "is_immutable_ref",
    "parse_source",
]

CONTRACTS: Final[tuple[str, ...]] = (
    "recipe-dependency-manifest.v1",
    "recipe-runner-lib.v1",
)

#: Schemes that name something already on this machine. A local source is
#: never subject to the immutable-ref rule (it has no ref) -- it is pinned, if
#: at all, by a content digest recorded at resolution.
LOCAL_SCHEMES: Final[frozenset[str]] = frozenset({"path", "file"})

DEFAULT_LOCAL_SCHEMES: Final[frozenset[str]] = frozenset({"path", "file"})

#: Remote schemes a v1 dependency may plausibly declare. Deliberately does not
#: include ``git+ssh`` or ``git+git``: those carry ambient credentials, so a
#: policy that wants them must say so explicitly.
DEFAULT_REMOTE_SCHEMES: Final[frozenset[str]] = frozenset({"git+https", "https"})

#: A ref is immutable only if it is a full-length git object id. Branches and
#: tags move; short shas are ambiguous.
IMMUTABLE_REF_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

#: Scheme reported for a bare filesystem path (no ``scheme://`` prefix).
_BARE_PATH_SCHEME: Final[str] = "path"


def is_immutable_ref(ref: str | None) -> bool:
    """True only for a full-length hex object id -- never a branch or tag."""
    return bool(ref) and IMMUTABLE_REF_PATTERN.match(ref or "") is not None


# --------------------------------------------------------------------------
# Source parsing -- the facts a policy decides on
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceFacts:
    """A declared dependency source, split into the parts a policy judges.

    Purely syntactic: parsing a source neither reaches it nor confirms it
    exists. That is the point -- the policy must be able to refuse a source
    without touching it.
    """

    raw: str
    """The source exactly as declared."""

    scheme: str
    """``git+https``, ``https``, ``file``, or ``path`` for a bare path."""

    host: str | None
    """Lowercased host, without userinfo or port. ``None`` for local sources."""

    path: str
    """Path portion, with any ``@ref`` suffix removed."""

    requested_ref: str | None
    """The ``@ref`` as declared -- a *request*, never a resolved revision."""

    subdirectory: str | None
    """``#subdirectory=`` fragment, for behavior partials."""

    @property
    def is_local(self) -> bool:
        return self.scheme in LOCAL_SCHEMES


def parse_source(source: str) -> SourceFacts:
    """Split a declared source into :class:`SourceFacts`. No I/O.

    A source with no ``scheme://`` prefix is a filesystem path and is reported
    with scheme ``"path"`` -- it is never guessed to be remote.

    A local source is reported with ``requested_ref=None`` even when its path
    happens to contain an ``@``: a directory named ``bundle@2`` is a directory,
    not a pinned revision, and reading it as one would silently truncate the
    path a root check is about to be run against.
    """
    raw = source.strip()
    base, subdirectory, ref = split_source(raw)
    scheme, separator, remainder = base.partition("://")

    if not separator:
        return SourceFacts(
            raw=raw,
            scheme=_BARE_PATH_SCHEME,
            host=None,
            path=base,
            requested_ref=None,
            subdirectory=subdirectory,
        )

    scheme = scheme.lower()
    authority, _, tail = remainder.partition("/")
    # ``file:///abs/path`` has an empty authority; anything else names a host.
    host = _normalize_host(authority) if authority else None

    if scheme in LOCAL_SCHEMES:
        return SourceFacts(
            raw=raw,
            scheme=scheme,
            host=None,
            path="/" + tail,
            requested_ref=None,
            subdirectory=subdirectory,
        )

    return SourceFacts(
        raw=raw,
        scheme=scheme,
        host=host,
        path=_strip_ref("/" + tail, ref),
        requested_ref=ref,
        subdirectory=subdirectory,
    )


def _strip_ref(base: str, ref: str | None) -> str:
    if ref and base.endswith(f"@{ref}"):
        return base[: -(len(ref) + 1)]
    return base


def _normalize_host(authority: str) -> str:
    """Drop userinfo and port, lowercase the rest."""
    without_userinfo = authority.rpartition("@")[2] or authority
    host = without_userinfo.rsplit(":", 1)[0] if ":" in without_userinfo else without_userinfo
    return host.lower()


# --------------------------------------------------------------------------
# Capability intersection (manifest Core 9)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectiveCapabilities:
    """Result of the Core 9 three-way intersection, with attribution.

    ``granted`` is the answer; the ``withheld_by_*`` tuples are why anything
    the manifest asked for is missing from it. A caller never has to diff sets
    to find out which term said no.
    """

    granted: tuple[str, ...]
    """host ∩ runner ∩ manifest-declared, sorted. Possibly empty."""

    requested: tuple[str, ...]
    """Manifest-declared needs, sorted. The upper bound on ``granted``."""

    withheld_by_host: tuple[str, ...] = ()
    """Requested capabilities the host policy does not permit."""

    withheld_by_runner: tuple[str, ...] = ()
    """Requested capabilities the runner (trust) policy does not permit."""

    @property
    def denied(self) -> tuple[str, ...]:
        """Everything requested but not granted, whichever term withheld it."""
        return tuple(sorted(set(self.withheld_by_host) | set(self.withheld_by_runner)))

    @property
    def is_empty(self) -> bool:
        """True when nothing at all was granted -- a real, reportable outcome."""
        return not self.granted

    def __contains__(self, capability: object) -> bool:
        return capability in self.granted

    def __iter__(self):
        return iter(self.granted)

    def __len__(self) -> int:
        return len(self.granted)


def intersect_capabilities(
    *,
    manifest: Iterable[str],
    host: Iterable[str] | None = None,
    runner: Iterable[str] | None = None,
) -> EffectiveCapabilities:
    """Effective capabilities = host ∩ runner ∩ manifest-declared (Core 9).

    Args:
        manifest: Capabilities the recipe declares it needs. Nothing outside
            this set is ever granted -- an intersection cannot add.
        host: Host policy. ``None`` means the host imposes no constraint;
            an empty iterable means the host permits nothing.
        runner: Runner policy (typically a
            :attr:`TrustPolicy.capability_allowlist`). Same ``None`` vs empty
            distinction as ``host``.

    Returns:
        An :class:`EffectiveCapabilities` record naming what was granted and,
        for anything withheld, which term withheld it. An empty intersection
        is a normal result, not an error: it is reported, never raised.
    """
    requested = frozenset(manifest)
    host_set = None if host is None else frozenset(host)
    runner_set = None if runner is None else frozenset(runner)

    granted = requested
    if host_set is not None:
        granted &= host_set
    if runner_set is not None:
        granted &= runner_set

    withheld_by_host = () if host_set is None else tuple(sorted(requested - host_set))
    withheld_by_runner = () if runner_set is None else tuple(sorted(requested - runner_set))

    return EffectiveCapabilities(
        granted=tuple(sorted(granted)),
        requested=tuple(sorted(requested)),
        withheld_by_host=withheld_by_host,
        withheld_by_runner=withheld_by_runner,
    )


# --------------------------------------------------------------------------
# The policy
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrustPolicy:
    """A caller-supplied decision about what may be fetched or activated.

    Frozen, hashable, and free of I/O: a policy is a value a host hands to the
    runner, and the runner records its :attr:`name` in run provenance
    (manifest Core 7). It structurally satisfies the
    :class:`~amplifier_recipe_runner.api.TrustPolicy` protocol.

    Build one with :meth:`ci` or :meth:`interactive` rather than by hand
    unless you mean something neither posture expresses.
    """

    name: str
    """Stable identifier recorded in run provenance."""

    allowed_schemes: frozenset[str]
    """Permitted source schemes. Always explicit -- there is no "any" form."""

    allowed_hosts: frozenset[str] | None = None
    """Permitted hosts for remote sources.

    ``None`` imposes no host constraint; an empty set permits **no** remote
    host. The two are never conflated.
    """

    allowed_local_roots: tuple[str, ...] | None = None
    """Directories local sources must live under (resolved, absolute).

    ``None`` imposes no location constraint; an empty tuple permits **no**
    local source.
    """

    require_immutable_refs: bool = False
    """Refuse a remote source that is not pinned to a full-length revision.

    Checked twice, deliberately: against the declared ``@ref`` (or a supplied
    ``locked_ref``) in :meth:`check_source`, before anything is fetched, and
    again against the *resolved* revision in :meth:`check_resolved`.
    """

    require_content_digest: bool = False
    """Refuse a resolved dependency that carries no content digest.

    Enforced in :meth:`check_resolved` -- a digest does not exist until the
    dependency has been read, so this cannot be a preflight check.
    """

    allow_dependency_install: bool = True
    """Whether activating a dependency may install packages into the
    environment. A hermetic posture says no."""

    capability_allowlist: frozenset[str] | None = None
    """The runner term of the Core 9 intersection. ``None`` is unconstrained;
    an empty set grants nothing."""

    def __post_init__(self) -> None:
        # Accept any iterable at the boundary, store canonical frozen forms.
        object.__setattr__(self, "allowed_schemes", frozenset(s.lower() for s in self.allowed_schemes))
        if self.allowed_hosts is not None:
            object.__setattr__(self, "allowed_hosts", frozenset(h.lower() for h in self.allowed_hosts))
        if self.allowed_local_roots is not None:
            object.__setattr__(
                self,
                "allowed_local_roots",
                tuple(str(Path(root).expanduser().resolve()) for root in self.allowed_local_roots),
            )
        if self.capability_allowlist is not None:
            object.__setattr__(self, "capability_allowlist", frozenset(self.capability_allowlist))

    # -- named postures ----------------------------------------------------

    @classmethod
    def ci(
        cls,
        *,
        name: str = "ci",
        allowed_hosts: Iterable[str] | None = ("github.com",),
        allowed_schemes: Iterable[str] = DEFAULT_REMOTE_SCHEMES | DEFAULT_LOCAL_SCHEMES,
        allowed_local_roots: Iterable[str | Path] | None = None,
        capability_allowlist: Iterable[str] | None = None,
    ) -> TrustPolicy:
        """A CI posture: immutable refs required, no environment mutation.

        Every remote dependency must be pinned to a full-length revision --
        either by its declared ``@ref`` or by a ``locked_ref`` the caller
        supplies. A floating ref with no lock is refused before any fetch
        (lib Core 6). Resolution must additionally produce a content digest,
        and activating a dependency may not install packages.
        """
        return cls(
            name=name,
            allowed_schemes=frozenset(allowed_schemes),
            allowed_hosts=None if allowed_hosts is None else frozenset(allowed_hosts),
            allowed_local_roots=None if allowed_local_roots is None else tuple(str(r) for r in allowed_local_roots),
            require_immutable_refs=True,
            require_content_digest=True,
            allow_dependency_install=False,
            capability_allowlist=None if capability_allowlist is None else frozenset(capability_allowlist),
        )

    @classmethod
    def interactive(
        cls,
        *,
        name: str = "interactive",
        allowed_hosts: Iterable[str] | None = None,
        allowed_schemes: Iterable[str] = DEFAULT_REMOTE_SCHEMES | DEFAULT_LOCAL_SCHEMES,
        allowed_local_roots: Iterable[str | Path] | None = None,
        capability_allowlist: Iterable[str] | None = None,
    ) -> TrustPolicy:
        """A permissive interactive posture: a human is watching.

        Floating refs are allowed, no content digest is demanded, and a
        dependency may install what it declares. Scheme is still checked --
        permissive is not unconditional.
        """
        return cls(
            name=name,
            allowed_schemes=frozenset(allowed_schemes),
            allowed_hosts=None if allowed_hosts is None else frozenset(allowed_hosts),
            allowed_local_roots=None if allowed_local_roots is None else tuple(str(r) for r in allowed_local_roots),
            require_immutable_refs=False,
            require_content_digest=False,
            allow_dependency_install=True,
            capability_allowlist=None if capability_allowlist is None else frozenset(capability_allowlist),
        )

    # -- preflight: before any fetch or activation -------------------------

    def check_source(self, source: str, *, locked_ref: str | None = None) -> None:
        """Permit ``source``, or raise ``TrustRefusedError`` naming the rule.

        Called by the planner for every declared dependency *before* the
        resolver sees any of them (manifest Core 6, lib Core 6). Nothing has
        been fetched when this raises.

        Args:
            source: The dependency source exactly as declared.
            locked_ref: The revision a lockfile pins this source to, when the
                caller has one. ``None`` means "no lock", which is what makes
                a floating ref refusable under a CI posture. A ``locked_ref``
                that is not itself immutable is refused too -- a lock that
                pins a branch pins nothing.
        """
        facts = parse_source(source)
        self._check_scheme(facts)
        if facts.is_local:
            self._check_local_root(facts)
            return
        self._check_host(facts)
        self._check_ref(facts, locked_ref)

    def check_resolved(
        self,
        source: str,
        *,
        resolved_revision: str | None = None,
        content_digest: str | None = None,
    ) -> None:
        """Re-check a dependency against what resolution actually produced.

        :meth:`check_source` can only judge what was *declared*; this judges
        what was *read*. Under a CI posture a remote source that resolved to
        something other than a full-length revision, or any source that
        produced no content digest, is refused here -- before the dependency
        is activated (manifest Core 6, Core 7).
        """
        facts = parse_source(source)

        if self.require_immutable_refs and not facts.is_local and not is_immutable_ref(resolved_revision):
            self._refuse(
                facts,
                rule="require_immutable_refs",
                detail=(
                    f"resolution produced revision {resolved_revision!r}, which is not a "
                    "full-length immutable object id"
                ),
                remedy=(
                    "Use a resolver that reports the resolved commit sha, or relax "
                    "`require_immutable_refs` for this run."
                ),
            )

        if self.require_content_digest and not content_digest:
            self._refuse(
                facts,
                rule="require_content_digest",
                detail="resolution recorded no content digest",
                remedy=(
                    "Use a resolver that records a content digest, or relax "
                    "`require_content_digest` for this run."
                ),
            )

    def check_dependency_install(self, source: str, *, package: str | None = None) -> None:
        """Refuse environment mutation when the policy forbids it."""
        if self.allow_dependency_install:
            return
        facts = parse_source(source)
        what = f"install {package!r}" if package else "install packages"
        self._refuse(
            facts,
            rule="allow_dependency_install",
            detail=f"activating this dependency would {what}, which this policy forbids",
            remedy=(
                "Pre-install the dependency in the environment, or use a policy with "
                "`allow_dependency_install=True`."
            ),
        )

    # -- capabilities (manifest Core 9) ------------------------------------

    def effective_capabilities(
        self,
        *,
        manifest: Iterable[str],
        host: Iterable[str] | None = None,
    ) -> EffectiveCapabilities:
        """This policy's :attr:`capability_allowlist` as the runner term of the
        Core 9 intersection. See :func:`intersect_capabilities`."""
        return intersect_capabilities(manifest=manifest, host=host, runner=self.capability_allowlist)

    # -- internals ---------------------------------------------------------

    def _check_scheme(self, facts: SourceFacts) -> None:
        if facts.scheme in self.allowed_schemes:
            return
        self._refuse(
            facts,
            rule="allowed_schemes",
            detail=f"scheme {facts.scheme!r} is not permitted (allowed: {_listed(self.allowed_schemes)})",
            remedy=(
                f"Declare the dependency with one of {_listed(self.allowed_schemes)}, or use a "
                f"policy whose `allowed_schemes` includes {facts.scheme!r}."
            ),
        )

    def _check_host(self, facts: SourceFacts) -> None:
        if self.allowed_hosts is None:
            return
        if facts.host is not None and facts.host in self.allowed_hosts:
            return
        permitted = _listed(self.allowed_hosts) if self.allowed_hosts else "no remote host"
        self._refuse(
            facts,
            rule="allowed_hosts",
            detail=f"host {facts.host!r} is not permitted (allowed: {permitted})",
            remedy=(
                f"Host the dependency on {permitted}, or use a policy whose `allowed_hosts` "
                f"includes {facts.host!r}."
            ),
        )

    def _check_local_root(self, facts: SourceFacts) -> None:
        if self.allowed_local_roots is None:
            return
        candidate = Path(facts.path).expanduser().resolve()
        for root in self.allowed_local_roots:
            if candidate == Path(root) or candidate.is_relative_to(Path(root)):
                return
        permitted = _listed(self.allowed_local_roots) if self.allowed_local_roots else "no local root"
        self._refuse(
            facts,
            rule="allowed_local_roots",
            detail=f"local path {str(candidate)!r} is outside every permitted root (allowed: {permitted})",
            remedy=(
                f"Move the dependency under {permitted}, or use a policy whose "
                "`allowed_local_roots` covers it."
            ),
        )

    def _check_ref(self, facts: SourceFacts, locked_ref: str | None) -> None:
        if not self.require_immutable_refs:
            return

        if locked_ref is not None:
            if is_immutable_ref(locked_ref):
                return
            self._refuse(
                facts,
                rule="require_immutable_refs",
                detail=(
                    f"lock entry pins {locked_ref!r}, which is not a full-length immutable "
                    "object id -- a lock on a moving ref pins nothing"
                ),
                remedy="Regenerate the lockfile so it records resolved commit shas.",
            )

        if is_immutable_ref(facts.requested_ref):
            return

        declared = (
            f"ref {facts.requested_ref!r} is a moving ref"
            if facts.requested_ref
            else "no ref is declared, so the default branch would be used"
        )
        self._refuse(
            facts,
            rule="require_immutable_refs",
            detail=f"{declared} and no lock entry pins it",
            remedy=(
                "Pin the source to a full commit sha (`@<40-hex>`), supply a lockfile entry "
                "for it, or run under an interactive policy."
            ),
        )

    def _refuse(self, facts: SourceFacts, *, rule: str, detail: str, remedy: str | None = None) -> NoReturn:
        raise TrustRefusedError(
            facts.raw,
            reason=f"{rule}: {detail}",
            policy=self.name,
            remedy=remedy,
        )


def _listed(values: Iterable[str]) -> str:
    return ", ".join(sorted(values))
