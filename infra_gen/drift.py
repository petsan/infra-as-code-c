"""
Bidirectional drift detection.

Compares a parsed manifest against previously generated output files and
reports two categories of drift:

**Forward drift** -- changes that *would* be applied if the generator is
re-run.  This includes brand-new services (files that do not yet exist) and
structural changes to existing services (e.g. a database was added or
removed).

**Reverse drift (orphaned resources)** -- files that exist in the output
directory but correspond to services that are **no longer** present in the
manifest.  These are resources that would be left dangling if the generator
runs without cleanup.

Usage::

    from infra_gen.drift import detect_drift, format_drift_report

    report = detect_drift(manifest, "output")
    print(format_drift_report(report))
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Manifest


def detect_drift(manifest: Manifest, output_dir: str) -> dict[str, list[dict[str, str]]]:
    """Compare a manifest against existing generated files and return a drift report.

    Args:
        manifest: The current (desired) service manifest.
        output_dir: Root output directory that may contain previously generated
            ``terraform/`` and ``kubernetes/`` sub-trees.

    Returns:
        A dictionary with two keys:

        ``"forward"``
            List of dicts describing resources that would be created or
            updated.  Each dict contains ``type``, ``environment``,
            ``service``, ``action`` (``"create"`` or ``"update"``), and
            ``reason``.

        ``"reverse"``
            List of dicts describing orphaned files.  Each dict contains
            ``type``, ``environment``, ``file``, ``service``, and ``reason``.
    """
    svc_names = {s.name for s in manifest.services}
    report: dict[str, list[dict[str, str]]] = {
        "forward": [],
        "reverse": [],
    }

    # Check Terraform files
    tf_dir = Path(output_dir) / "terraform"
    if tf_dir.exists():
        for env_dir in sorted(tf_dir.iterdir()):
            if not env_dir.is_dir():
                continue
            env = env_dir.name
            for tf_file in sorted(env_dir.glob("*.tf.json")):
                # Skip backend and provider files
                if tf_file.name in ("backend.tf.json", "provider.tf.json"):
                    continue
                svc_name = tf_file.stem.replace(".tf", "")
                if svc_name not in svc_names:
                    report["reverse"].append(
                        {
                            "type": "terraform",
                            "environment": env,
                            "file": str(tf_file),
                            "service": svc_name,
                            "reason": "Service no longer in manifest",
                        }
                    )

    # Check for services that need new Terraform files (forward)
    for env in ["dev", "staging", "prod"]:
        tf_env_dir = (tf_dir / env) if tf_dir.exists() else None
        for svc_name in sorted(svc_names):
            if tf_env_dir is None or not (tf_env_dir / f"{svc_name}.tf.json").exists():
                report["forward"].append(
                    {
                        "type": "terraform",
                        "environment": env,
                        "service": svc_name,
                        "action": "create",
                        "reason": "New service, Terraform file will be created",
                    }
                )
            else:
                # Compare content for structural changes
                assert tf_env_dir is not None  # guarded by if above
                existing: dict[str, Any] = json.loads(
                    (tf_env_dir / f"{svc_name}.tf.json").read_text()
                )
                svc = next(s for s in manifest.services if s.name == svc_name)
                existing_resources = set(existing.get("resource", {}).keys())
                has_db_resource = any("db_instance" in k for k in existing_resources)
                has_cache_resource = any("elasticache" in k for k in existing_resources)

                if svc.has_db and not has_db_resource:
                    report["forward"].append(
                        {
                            "type": "terraform",
                            "environment": env,
                            "service": svc_name,
                            "action": "update",
                            "reason": "Database resources will be added",
                        }
                    )
                elif not svc.has_db and has_db_resource:
                    report["forward"].append(
                        {
                            "type": "terraform",
                            "environment": env,
                            "service": svc_name,
                            "action": "update",
                            "reason": "Database resources will be removed",
                        }
                    )

                if svc.has_cache and not has_cache_resource:
                    report["forward"].append(
                        {
                            "type": "terraform",
                            "environment": env,
                            "service": svc_name,
                            "action": "update",
                            "reason": "Cache resources will be added",
                        }
                    )
                elif not svc.has_cache and has_cache_resource:
                    report["forward"].append(
                        {
                            "type": "terraform",
                            "environment": env,
                            "service": svc_name,
                            "action": "update",
                            "reason": "Cache resources will be removed",
                        }
                    )

    # Check Kubernetes files
    k8s_dir = Path(output_dir) / "kubernetes"
    if k8s_dir.exists():
        for env_dir in sorted(k8s_dir.iterdir()):
            if not env_dir.is_dir():
                continue
            env = env_dir.name
            for k8s_file in sorted(env_dir.glob("*.yaml")):
                svc_name = k8s_file.stem
                if svc_name not in svc_names:
                    report["reverse"].append(
                        {
                            "type": "kubernetes",
                            "environment": env,
                            "file": str(k8s_file),
                            "service": svc_name,
                            "reason": "Service no longer in manifest",
                        }
                    )

    for env in ["dev", "staging", "prod"]:
        k8s_env_dir = (k8s_dir / env) if k8s_dir.exists() else None
        for svc_name in sorted(svc_names):
            if k8s_env_dir is None or not (k8s_env_dir / f"{svc_name}.yaml").exists():
                report["forward"].append(
                    {
                        "type": "kubernetes",
                        "environment": env,
                        "service": svc_name,
                        "action": "create",
                        "reason": "New service, Kubernetes manifest will be created",
                    }
                )

    return report


def format_drift_report(report: dict[str, list[dict[str, str]]]) -> str:
    """Format a drift report dictionary into a human-readable string.

    Args:
        report: The dict returned by :func:`detect_drift`.

    Returns:
        A multi-line string suitable for printing to the terminal.
    """
    lines: list[str] = []

    if report["forward"]:
        lines.append("=== FORWARD DRIFT (changes to apply) ===")
        for item in report["forward"]:
            lines.append(
                f"  [{item['action'].upper()}] {item['type']}/{item['environment']}"
                f"/{item['service']}: {item['reason']}"
            )
    else:
        lines.append("=== FORWARD DRIFT: No changes detected ===")

    lines.append("")

    if report["reverse"]:
        lines.append("=== REVERSE DRIFT (orphaned resources) ===")
        for item in report["reverse"]:
            lines.append(
                f"  [ORPHAN] {item['type']}/{item['environment']}"
                f"/{item['service']}: {item['reason']}"
            )
            lines.append(f"           File: {item['file']}")
    else:
        lines.append("=== REVERSE DRIFT: No orphaned resources ===")

    return "\n".join(lines)
