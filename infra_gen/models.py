"""
Data models for the service manifest.

This module defines the core dataclasses used throughout the generator:

* :class:`EnvOverride` -- per-environment replica and CPU overrides.
* :class:`Service` -- a single micro-service definition.
* :class:`Manifest` -- the top-level container holding a list of services.

All models are plain dataclasses with no I/O side-effects so they can be
constructed freely in tests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


@dataclass
class EnvOverride:
    """Per-environment resource overrides for a service.

    Attributes:
        replicas: Number of desired pod / task replicas.  Must be > 0.
        cpu: CPU request string in Kubernetes millicore format (e.g. ``"250m"``).
             Validated against the regex ``^[0-9]+m$``.
    """

    replicas: int
    cpu: str


@dataclass
class Service:
    """A single micro-service defined in the YAML manifest.

    Attributes:
        name: Unique service identifier (used as resource name prefix).
        port: Primary listening port for the service container.
        dependencies: List of other service *names* this service depends on.
            Dependency edges drive security-group rules and topological ordering.
        db_type: Database engine -- ``"postgres"``, ``"mysql"``, or ``"none"``.
        cache: Cache engine -- ``"redis"``, ``"memcached"``, or ``"none"``.
        exposure: Network exposure level -- ``"internal"`` or ``"external"``.
            External services receive an ALB ingress rule on port 443.
            Internal services are guaranteed **no** ``0.0.0.0/0`` ingress.
        health_check_path: Optional HTTP path for Kubernetes readiness probes
            (e.g. ``"/healthz"``).  When *None*, a TCP socket probe is used
            instead.
        env_overrides: Mapping of environment name (``"dev"``, ``"staging"``,
            ``"prod"``) to :class:`EnvOverride` instances.
        secrets: List of secret names that the service requires at runtime
            (e.g. ``["DB_PASSWORD", "API_KEY"]``).  Names must match
            ``^[A-Z][A-Z0-9_]*$``.  Secrets are provisioned in AWS Secrets
            Manager (Terraform) and mounted as a Kubernetes ``Secret``.
    """

    name: str
    port: int
    dependencies: list[str]
    db_type: str
    cache: str
    exposure: str
    health_check_path: str | None = None
    env_overrides: dict[str, EnvOverride] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)

    @property
    def has_db(self) -> bool:
        """Return *True* if the service provisions a database (``db_type != "none"``)."""
        return self.db_type != "none"

    @property
    def has_secrets(self) -> bool:
        """Return *True* if the service declares any secrets."""
        return len(self.secrets) > 0

    @property
    def has_cache(self) -> bool:
        """Return *True* if the service provisions a cache (``cache != "none"``)."""
        return self.cache != "none"

    def dependency_hash(self) -> str:
        """Compute a stable, truncated SHA-256 hash of the sorted dependency list.

        The hash is used as a ``dependency-hash`` tag on every generated
        resource so that downstream tooling can detect when the dependency
        graph has changed without diffing the full output.

        Returns:
            A 12-character hex digest string.
        """
        data = json.dumps(sorted(self.dependencies), sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()[:12]


@dataclass
class Manifest:
    """Top-level manifest containing all service definitions.

    Attributes:
        services: Ordered list of :class:`Service` instances parsed from the
            YAML ``services`` key.
        regions: List of AWS regions to deploy to.  Defaults to
            ``["us-east-1"]``.  Each region gets its own directory subtree
            with separate state backends and provider configurations.
    """

    services: list[Service]
    regions: list[str] = field(default_factory=lambda: ["us-east-1"])

    def service_map(self) -> dict[str, Service]:
        """Build a name-keyed lookup dictionary for fast service resolution.

        Returns:
            ``{service.name: service}`` for every service in the manifest.
        """
        return {s.name: s for s in self.services}
