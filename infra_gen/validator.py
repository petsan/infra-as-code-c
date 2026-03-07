"""
Manifest validation logic.

Runs a comprehensive suite of checks against a parsed
:class:`~infra_gen.models.Manifest` and returns a list of
:class:`ValidationError` objects.  Errors with ``severity="error"`` prevent
code generation; those with ``severity="info"`` are advisory.

Checks performed
----------------
1. **Self-references** -- a service must not list itself in ``dependencies``.
2. **Missing dependencies** -- every name in ``dependencies`` must correspond
   to an existing service.
3. **Environment overrides** -- ``dev``, ``staging``, and ``prod`` must all be
   present for each service.
4. **Replica count** -- ``replicas`` must be > 0 in every environment.
5. **CPU format** -- ``cpu`` must match the regex ``^[0-9]+m$``
   (Kubernetes millicore notation).
6. **Replica ordering** -- ``prod >= staging >= dev`` replicas.
7. **True cycles** -- any cycle involving 3+ services is an error.
8. **Peer relationships** (info) -- 2-service mutual dependencies are reported
   for visibility but do **not** block generation.
"""

from __future__ import annotations

import re

from .graph import find_all_cycles, find_peer_pairs
from .models import Manifest


class ValidationError:
    """A single validation finding.

    Attributes:
        message: Human-readable description of the issue.
        severity: ``"error"`` (blocks generation) or ``"info"`` (advisory).
    """

    def __init__(self, message: str, severity: str = "error") -> None:
        self.message = message
        self.severity = severity

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.message}"

    def __repr__(self) -> str:
        return f"ValidationError({self.message!r}, severity={self.severity!r})"


def validate_manifest(manifest: Manifest) -> list[ValidationError]:
    """Run all validation checks against *manifest*.

    Args:
        manifest: A :class:`~infra_gen.models.Manifest` instance (typically
            obtained from :func:`~infra_gen.parser.parse_manifest`).

    Returns:
        A list of :class:`ValidationError` objects.  An empty list (or a list
        containing only ``severity="info"`` items) means the manifest is valid
        for generation.
    """
    errors: list[ValidationError] = []
    svc_map = manifest.service_map()
    required_envs = ["dev", "staging", "prod"]
    cpu_regex = re.compile(r"^[0-9]+m$")

    for svc in manifest.services:
        # Self-references
        if svc.name in svc.dependencies:
            errors.append(
                ValidationError(f"Service '{svc.name}' has a self-reference in dependencies")
            )

        # Missing dependencies
        for dep in svc.dependencies:
            if dep not in svc_map:
                errors.append(
                    ValidationError(f"Service '{svc.name}' depends on unknown service '{dep}'")
                )

        # env_overrides validation
        for env_name in required_envs:
            if env_name not in svc.env_overrides:
                errors.append(
                    ValidationError(f"Service '{svc.name}' missing env_overrides for '{env_name}'")
                )
                continue

            ov = svc.env_overrides[env_name]

            if ov.replicas <= 0:
                errors.append(
                    ValidationError(
                        f"Service '{svc.name}' env '{env_name}': "
                        f"replicas must be > 0, got {ov.replicas}"
                    )
                )

            if not cpu_regex.match(ov.cpu):
                errors.append(
                    ValidationError(
                        f"Service '{svc.name}' env '{env_name}': "
                        f"cpu must match ^[0-9]+m$, got '{ov.cpu}'"
                    )
                )

        # Replica ordering: prod >= staging >= dev
        if all(env in svc.env_overrides for env in required_envs):
            dev_r = svc.env_overrides["dev"].replicas
            staging_r = svc.env_overrides["staging"].replicas
            prod_r = svc.env_overrides["prod"].replicas

            if not (prod_r >= staging_r >= dev_r):
                errors.append(
                    ValidationError(
                        f"Service '{svc.name}': replica ordering violated - "
                        f"prod({prod_r}) >= staging({staging_r}) >= dev({dev_r}) required"
                    )
                )

    # Cycle detection (3+ services only, peer pairs excluded)
    cycles = find_all_cycles(manifest)
    for cycle in cycles:
        cycle_str = " -> ".join([*cycle, cycle[0]])
        errors.append(ValidationError(f"True cycle detected (3+ services): {cycle_str}"))

    # Report peer pairs as info
    peers = find_peer_pairs(manifest)
    for a, b in peers:
        errors.append(
            ValidationError(
                f"Peer relationship detected: {a} <-> {b} (bidirectional rules will be generated)",
                severity="info",
            )
        )

    return errors
