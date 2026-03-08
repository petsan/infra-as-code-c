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
    cpu_regex = re.compile(r"^[1-9][0-9]*m$")
    secret_regex = re.compile(r"^[A-Z][A-Z0-9_]*$")
    name_regex = re.compile(r"^[a-z][a-z0-9-]*$")
    region_regex = re.compile(r"^[a-z]{2}(-[a-z]+-\d+)$")

    valid_db_types = {"postgres", "mysql", "none"}
    valid_caches = {"redis", "memcached", "none"}
    valid_exposures = {"internal", "external"}

    # Duplicate service names
    seen_names: list[str] = [s.name for s in manifest.services]
    if len(seen_names) != len(set(seen_names)):
        dupes = {n for n in seen_names if seen_names.count(n) > 1}
        for d in sorted(dupes):
            errors.append(ValidationError(f"Duplicate service name: '{d}'"))

    # Validate regions
    if not manifest.regions:
        errors.append(ValidationError("At least one region must be specified"))
    for region in manifest.regions:
        if not region_regex.match(region):
            errors.append(
                ValidationError(
                    f"Invalid region '{region}' (must match AWS region format, e.g. 'us-east-1')"
                )
            )
    if len(manifest.regions) != len(set(manifest.regions)):
        errors.append(ValidationError("Duplicate regions specified"))

    for svc in manifest.services:
        # Service name format
        if not name_regex.match(svc.name):
            errors.append(
                ValidationError(
                    f"Service '{svc.name}': invalid name "
                    f"(must match ^[a-z][a-z0-9-]*$ for valid Terraform identifiers)"
                )
            )

        # Port validation
        if not isinstance(svc.port, int) or isinstance(svc.port, bool):
            errors.append(ValidationError(f"Service '{svc.name}': port must be an integer"))
        elif not (1 <= svc.port <= 65535):
            errors.append(
                ValidationError(f"Service '{svc.name}': port must be 1-65535, got {svc.port}")
            )

        # db_type validation
        if svc.db_type not in valid_db_types:
            errors.append(
                ValidationError(
                    f"Service '{svc.name}': invalid db_type '{svc.db_type}' "
                    f"(must be one of: {', '.join(sorted(valid_db_types))})"
                )
            )

        # cache validation
        if svc.cache not in valid_caches:
            errors.append(
                ValidationError(
                    f"Service '{svc.name}': invalid cache '{svc.cache}' "
                    f"(must be one of: {', '.join(sorted(valid_caches))})"
                )
            )

        # exposure validation
        if svc.exposure not in valid_exposures:
            errors.append(
                ValidationError(
                    f"Service '{svc.name}': invalid exposure '{svc.exposure}' "
                    f"(must be one of: {', '.join(sorted(valid_exposures))})"
                )
            )

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
                        f"cpu must match ^[1-9][0-9]*m$, got '{ov.cpu}'"
                    )
                )

        # Secret name validation
        for secret_name in svc.secrets:
            if not secret_regex.match(secret_name):
                errors.append(
                    ValidationError(
                        f"Service '{svc.name}': invalid secret name '{secret_name}' "
                        f"(must match ^[A-Z][A-Z0-9_]*$)"
                    )
                )
        if len(svc.secrets) != len(set(svc.secrets)):
            errors.append(ValidationError(f"Service '{svc.name}': duplicate secret names"))

        # DB_PASSWORD collision: user-declared secret collides with auto-generated DB secret
        if svc.has_db and "DB_PASSWORD" in svc.secrets:
            errors.append(
                ValidationError(
                    f"Service '{svc.name}': secret 'DB_PASSWORD' collides with "
                    f"auto-generated database password (db_type='{svc.db_type}')"
                )
            )

        # ElastiCache cluster_id max 20 characters (will be truncated)
        if svc.has_cache:
            for env_name in required_envs:
                cluster_id = f"{svc.name}-{env_name}"
                if len(cluster_id) > 20:
                    errors.append(
                        ValidationError(
                            f"Service '{svc.name}': ElastiCache cluster_id "
                            f"'{cluster_id}' exceeds 20-character limit "
                            f"({len(cluster_id)} chars, will be truncated)",
                            severity="info",
                        )
                    )
                    break  # One warning is enough

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

    # CPU cap warning: millicore values > 4096 silently cap at Fargate max
    for svc in manifest.services:
        for env_name, ov in svc.env_overrides.items():
            if cpu_regex.match(ov.cpu):
                millicore = int(ov.cpu.rstrip("m"))
                if millicore > 4096:
                    errors.append(
                        ValidationError(
                            f"Service '{svc.name}' env '{env_name}': "
                            f"cpu '{ov.cpu}' exceeds Fargate maximum (4096m), "
                            f"will be capped at 4096",
                            severity="info",
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
