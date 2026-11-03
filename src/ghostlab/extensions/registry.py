"""Operator registration for reviewed local adapter packages.

The registry holds factories handed to it by reviewed local (checked-in)
code -- the operator composing the process. It never imports by name, never
loads from a path, and never executes uploaded code: there is no API that
turns a string from a proposal, capsule or request into code, and only the
``checked_in`` origin registers. Unknown worlds or adapters fail closed at
resolve time. Standard library only.
"""

from collections.abc import Callable
from dataclasses import dataclass

from .environment import EnvironmentAdapter
from .target import TargetAdapter

CHECKED_IN = "checked_in"
REGISTERED = "registered"


class RegistrationError(ValueError):
    """A factory or manifest the registry refused."""


class UnknownWorldError(LookupError):
    """No adapter package is registered for this world version."""


class UnknownTargetError(LookupError):
    """No target adapter is registered under this id for this world."""


@dataclass(frozen=True)
class AdapterManifest:
    """Where a registered factory comes from. Only reviewed local code."""

    package: str
    version: str
    origin: str
    description: str = ""

    def __post_init__(self):
        for value, label in ((self.package, "package"), (self.version, "version")):
            if type(value) is not str or not value or len(value) > 200:
                raise ValueError(f"Invalid manifest {label}")
        if self.origin not in (CHECKED_IN, REGISTERED):
            raise ValueError("Only reviewed local adapter packages may register "
                             f"(got origin {self.origin!r})")
        if type(self.description) is not str or len(self.description) > 2000:
            raise ValueError("Invalid manifest description")


def _world_key(world_version: str) -> str:
    if type(world_version) is not str or not world_version or len(world_version) > 200:
        raise ValueError("Invalid world version")
    return world_version


def check_environment(instance: object) -> None:
    """Refuse anything that does not satisfy the environment interface."""
    if not isinstance(instance, EnvironmentAdapter):
        raise RegistrationError("Not an environment adapter: "
                                f"{type(instance).__name__} misses interface methods")


def check_target(instance: object) -> None:
    """Refuse anything that does not satisfy the target interface."""
    if not isinstance(instance, TargetAdapter):
        raise RegistrationError("Not a target adapter: "
                                f"{type(instance).__name__} misses interface methods")


class AdapterRegistry:
    """Explicit operator wiring from world and adapter ids to factories.

    Factories are zero-argument callables returning one fresh adapter
    instance per run. Registration is explicit and closed: re-registering an
    id needs ``replace=True``, and stored factories are never derived from
    untrusted input.
    """

    def __init__(self):
        self._environments: dict[str, tuple[Callable, AdapterManifest]] = {}
        self._targets: dict[tuple[str, str], tuple[Callable, AdapterManifest]] = {}

    def register_environment(self, world_version: str, factory: Callable,
                             manifest: AdapterManifest, *, replace: bool = False) -> None:
        """Register the environment factory for one world version."""
        _world_key(world_version)
        if not callable(factory):
            raise RegistrationError("Environment factory is not callable")
        if not isinstance(manifest, AdapterManifest):
            raise RegistrationError("Environment manifest is not an AdapterManifest")
        if world_version in self._environments and not replace:
            raise RegistrationError(f"{world_version!r} is already registered")
        self._environments[world_version] = (factory, manifest)

    def register_target(self, world_version: str, adapter_id: str, factory: Callable,
                        manifest: AdapterManifest, *, replace: bool = False) -> None:
        """Register one target factory for one world version."""
        _world_key(world_version)
        if type(adapter_id) is not str or not adapter_id or len(adapter_id) > 200:
            raise ValueError("Invalid adapter id")
        if not callable(factory):
            raise RegistrationError("Target factory is not callable")
        if not isinstance(manifest, AdapterManifest):
            raise RegistrationError("Target manifest is not an AdapterManifest")
        key = (world_version, adapter_id)
        if key in self._targets and not replace:
            raise RegistrationError(f"{adapter_id!r} is already registered for {world_version!r}")
        self._targets[key] = (factory, manifest)

    def environment_factory(self, world_version: str) -> Callable:
        """The registered environment factory. Unknown worlds raise."""
        try:
            return self._environments[world_version][0]
        except (KeyError, TypeError):
            raise UnknownWorldError(f"No adapter package for {world_version!r}") from None

    def target_factory(self, world_version: str, adapter_id: str) -> Callable:
        """The registered target factory. Unknown worlds or ids raise."""
        try:
            return self._targets[(world_version, adapter_id)][0]
        except (KeyError, TypeError):
            try:
                missing = world_version not in self._environments
            except TypeError:  # Unhashable ids are unknown worlds, still fail-closed.
                missing = True
            if missing:
                raise UnknownWorldError(f"No adapter package for {world_version!r}") from None
            raise UnknownTargetError(
                f"No target {adapter_id!r} for {world_version!r}") from None

    def environment_manifest(self, world_version: str) -> AdapterManifest:
        """The registered environment manifest. Unknown worlds raise."""
        try:
            return self._environments[world_version][1]
        except (KeyError, TypeError):
            raise UnknownWorldError(f"No adapter package for {world_version!r}") from None

    def target_manifest(self, world_version: str, adapter_id: str) -> AdapterManifest:
        """The registered target manifest. Unknown worlds or ids raise."""
        try:
            return self._targets[(world_version, adapter_id)][1]
        except (KeyError, TypeError):
            try:
                missing = world_version not in self._environments
            except TypeError:  # Unhashable ids are unknown worlds, still fail-closed.
                missing = True
            if missing:
                raise UnknownWorldError(f"No adapter package for {world_version!r}") from None
            raise UnknownTargetError(
                f"No target {adapter_id!r} for {world_version!r}") from None

    def worlds(self) -> tuple[str, ...]:
        """Registered world versions, in order."""
        return tuple(sorted(self._environments))

    def targets(self, world_version: str) -> tuple[str, ...]:
        """Registered target ids for one world version, in order."""
        return tuple(sorted(adapter_id for world, adapter_id in self._targets
                            if world == world_version))