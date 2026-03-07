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

from pathlib import Path

import yaml

from .models import EnvOverride, Manifest, Service


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

    services = []
    for svc in data.get("services", []):
        env_overrides = {}
        for env_name, ov in svc.get("env_overrides", {}).items():
            env_overrides[env_name] = EnvOverride(
                replicas=ov["replicas"],
                cpu=ov["cpu"],
            )

        services.append(
            Service(
                name=svc["name"],
                port=svc["port"],
                dependencies=svc.get("dependencies", []),
                db_type=svc.get("db_type", "none"),
                cache=svc.get("cache", "none"),
                exposure=svc.get("exposure", "internal"),
                health_check_path=svc.get("health_check_path"),
                env_overrides=env_overrides,
            )
        )

    return Manifest(services=services)
