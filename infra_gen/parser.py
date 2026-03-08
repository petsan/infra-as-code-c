"""
YAML manifest parser.

Reads a YAML file that follows the ``infra-gen`` manifest schema and
hydrates it into :class:`~infra_gen.models.Manifest` /
:class:`~infra_gen.models.Service` model objects.

The expected top-level YAML structure is::

    services:
      - name: my-service
        port: 8080
        dependencies: [other-service]
        db_type: postgres        # postgres | mysql | none
        cache: redis             # redis | memcached | none
        exposure: external       # internal | external
        health_check_path: /healthz   # optional
        env_overrides:
          dev:
            replicas: 1
            cpu: "250m"
          staging:
            replicas: 2
            cpu: "500m"
          prod:
            replicas: 4
            cpu: "1000m"

Missing optional keys fall back to safe defaults (``"none"`` for db/cache,
``"internal"`` for exposure, empty list for dependencies).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from .models import EnvOverride, Manifest, Service

_KNOWN_SERVICE_KEYS = {
    "name",
    "port",
    "dependencies",
    "db_type",
    "cache",
    "exposure",
    "health_check_path",
    "env_overrides",
    "secrets",
}
_KNOWN_TOP_LEVEL_KEYS = {"services", "regions"}


def parse_manifest(path: str | Path) -> Manifest:
    """Parse a YAML manifest file into a :class:`~infra_gen.models.Manifest`.

    Args:
        path: Filesystem path (string or :class:`~pathlib.Path`) to the YAML
            manifest.

    Returns:
        A fully populated :class:`~infra_gen.models.Manifest` instance.

    Raises:
        FileNotFoundError: If *path* does not exist.
        yaml.YAMLError: If the file contains invalid YAML.
        KeyError: If a service entry is missing required fields (``name``,
            ``port``).
    """
    with open(path) as f:
        data = yaml.safe_load(f)

    if data is None:
        data = {}

    # Warn on unknown top-level keys
    for key in data:
        if key not in _KNOWN_TOP_LEVEL_KEYS:
            print(f"Warning: unknown top-level key '{key}' in manifest", file=sys.stderr)

    services = []
    for svc in data.get("services", []):
        # Warn on unknown service-level keys
        for key in svc:
            if key not in _KNOWN_SERVICE_KEYS:
                print(
                    f"Warning: unknown key '{key}' in service '{svc.get('name', '?')}'",
                    file=sys.stderr,
                )

        env_overrides = {}
        for env_name, ov in svc.get("env_overrides", {}).items():
            env_overrides[env_name] = EnvOverride(
                replicas=ov["replicas"],
                cpu=ov["cpu"],
            )

        deps = svc.get("dependencies", [])
        if not isinstance(deps, list):
            raise TypeError(
                f"Service '{svc['name']}': dependencies must be a list, got {type(deps).__name__}"
            )

        secrets = svc.get("secrets", [])
        if not isinstance(secrets, list):
            raise TypeError(
                f"Service '{svc['name']}': secrets must be a list, got {type(secrets).__name__}"
            )

        try:
            port = int(svc["port"])
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Service '{svc['name']}': port must be an integer, got '{svc['port']}'"
            ) from e

        services.append(
            Service(
                name=svc["name"],
                port=port,
                dependencies=deps,
                db_type=svc.get("db_type", "none"),
                cache=svc.get("cache", "none"),
                exposure=svc.get("exposure", "internal"),
                health_check_path=svc.get("health_check_path"),
                env_overrides=env_overrides,
                secrets=secrets,
            )
        )

    regions = data.get("regions", ["us-east-1"])
    if not isinstance(regions, list):
        raise TypeError(f"regions must be a list, got {type(regions).__name__}")
    return Manifest(services=services, regions=regions)
