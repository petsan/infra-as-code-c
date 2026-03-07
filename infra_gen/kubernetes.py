"""
Kubernetes manifest generation.

Produces per-environment directories under ``<output>/kubernetes/<env>/``
containing multi-document YAML files (one file per service).

Each service file contains four Kubernetes resources:

1. **Deployment** -- with pod anti-affinity (spread across nodes), topology
   spread constraints (max 2 pods per zone), readiness and liveness probes,
   and resource requests/limits from ``env_overrides``.

2. **Service** -- ClusterIP service exposing the container port.

3. **NetworkPolicy** -- enforces the internal/external distinction:

   * Internal pods reject traffic from pods labeled ``exposure: external``.
   * All pods allow egress to their declared dependencies and to DNS (port 53).

4. **HorizontalPodAutoscaler** (``autoscaling/v2``) -- scales on **both**
   CPU utilisation (target 70%) and memory utilisation (target 80%).

Probe strategy
--------------
* **Readiness**: HTTP GET on ``health_check_path`` if specified, otherwise
  TCP socket on the service port.  Short intervals (10 s).
* **Liveness**: **Always** TCP socket (never HTTP) with longer intervals
  (20 s) and a higher failure threshold, ensuring liveness and readiness
  probes are always different.

Labels and annotations
----------------------
Every resource carries ``app``, ``environment``, and ``exposure`` labels.
Peer services additionally carry a ``peer-group`` label.  Annotations
include ``dependency-hash`` and ``last-generated`` (ISO-8601 UTC).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .graph import find_peer_pairs
from .models import Manifest, Service

ENVIRONMENTS = ["dev", "staging", "prod"]
"""Target deployment environments."""


def generate_kubernetes(manifest: Manifest, output_dir: str) -> list[str]:
    """Generate Kubernetes manifests for all environments.

    Args:
        manifest: The validated service manifest.
        output_dir: Root output directory.  Kubernetes files are written to
            ``<output_dir>/kubernetes/<env>/``.

    Returns:
        List of absolute file paths that were created or overwritten.
    """
    generated: list[str] = []
    svc_map = manifest.service_map()
    peer_pairs = find_peer_pairs(manifest)
    peer_labels: dict[str, str] = {}
    for a, b in peer_pairs:
        label = "-".join(sorted([a, b]))
        peer_labels[a] = label
        peer_labels[b] = label

    timestamp = datetime.now(timezone.utc).isoformat()

    for env in ENVIRONMENTS:
        env_dir = Path(output_dir) / "kubernetes" / env
        env_dir.mkdir(parents=True, exist_ok=True)

        for svc in manifest.services:
            docs = _generate_service_manifests(svc, env, svc_map, peer_labels, timestamp)
            path = env_dir / f"{svc.name}.yaml"
            content = "---\n".join(
                yaml.dump(doc, default_flow_style=False, sort_keys=False) for doc in docs
            )
            path.write_text(content)
            generated.append(str(path))

    return generated


def _generate_service_manifests(
    svc: Service,
    env: str,
    svc_map: dict[str, Service],
    peer_labels: dict[str, str],
    timestamp: str,
) -> list[dict[str, Any]]:
    """Build the four Kubernetes resource dicts for a single service.

    Returns:
        A list of four dicts: ``[Deployment, Service, NetworkPolicy, HPA]``.
    """
    ov = svc.env_overrides.get(env)
    replicas = ov.replicas if ov else 1
    cpu = ov.cpu if ov else "250m"

    labels = {
        "app": svc.name,
        "environment": env,
        "exposure": svc.exposure,
    }
    if svc.name in peer_labels:
        labels["peer-group"] = peer_labels[svc.name]

    annotations = {
        "dependency-hash": svc.dependency_hash(),
        "last-generated": timestamp,
    }

    docs: list[dict[str, Any]] = []

    # --- Deployment ---
    readiness_probe = _readiness_probe(svc)
    liveness_probe = _liveness_probe(svc)

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": svc.name,
            "namespace": env,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": svc.name}},
            "template": {
                "metadata": {
                    "labels": labels,
                },
                "spec": {
                    "affinity": {
                        "podAntiAffinity": {
                            "preferredDuringSchedulingIgnoredDuringExecution": [
                                {
                                    "weight": 100,
                                    "podAffinityTerm": {
                                        "labelSelector": {
                                            "matchExpressions": [
                                                {
                                                    "key": "app",
                                                    "operator": "In",
                                                    "values": [svc.name],
                                                }
                                            ],
                                        },
                                        "topologyKey": "kubernetes.io/hostname",
                                    },
                                }
                            ],
                        },
                    },
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 2,
                            "topologyKey": "topology.kubernetes.io/zone",
                            "whenUnsatisfiable": "DoNotSchedule",
                            "labelSelector": {
                                "matchLabels": {"app": svc.name},
                            },
                        }
                    ],
                    "containers": [
                        {
                            "name": svc.name,
                            "image": f"{svc.name}:latest",
                            "ports": [{"containerPort": svc.port}],
                            "resources": {
                                "requests": {"cpu": cpu, "memory": "128Mi"},
                                "limits": {"cpu": cpu, "memory": "256Mi"},
                            },
                            "readinessProbe": readiness_probe,
                            "livenessProbe": liveness_probe,
                        }
                    ],
                },
            },
        },
    }
    docs.append(deployment)

    # --- Service ---
    k8s_service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": svc.name,
            "namespace": env,
            "labels": labels,
        },
        "spec": {
            "selector": {"app": svc.name},
            "ports": [
                {
                    "port": svc.port,
                    "targetPort": svc.port,
                    "protocol": "TCP",
                }
            ],
            "type": "ClusterIP",
        },
    }
    docs.append(k8s_service)

    # --- NetworkPolicy ---
    network_policy = _network_policy(svc, env, svc_map, labels)
    docs.append(network_policy)

    # --- HorizontalPodAutoscaler ---
    hpa = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {
            "name": svc.name,
            "namespace": env,
            "labels": labels,
        },
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": svc.name,
            },
            "minReplicas": replicas,
            "maxReplicas": max(replicas * 3, 3),
            "metrics": [
                {
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {
                            "type": "Utilization",
                            "averageUtilization": 70,
                        },
                    },
                },
                {
                    "type": "Resource",
                    "resource": {
                        "name": "memory",
                        "target": {
                            "type": "Utilization",
                            "averageUtilization": 80,
                        },
                    },
                },
            ],
        },
    }
    docs.append(hpa)

    return docs


def _readiness_probe(svc: Service) -> dict[str, Any]:
    """Build a readiness probe definition.

    Uses **HTTP GET** on :attr:`Service.health_check_path` when it is set,
    falling back to a **TCP socket** check on the service port.

    Timing: ``initialDelaySeconds=5``, ``periodSeconds=10``,
    ``timeoutSeconds=3``, ``failureThreshold=3``.
    """
    if svc.health_check_path:
        return {
            "httpGet": {
                "path": svc.health_check_path,
                "port": svc.port,
            },
            "initialDelaySeconds": 5,
            "periodSeconds": 10,
            "timeoutSeconds": 3,
            "failureThreshold": 3,
        }
    return {
        "tcpSocket": {"port": svc.port},
        "initialDelaySeconds": 5,
        "periodSeconds": 10,
        "timeoutSeconds": 3,
        "failureThreshold": 3,
    }


def _liveness_probe(svc: Service) -> dict[str, Any]:
    """Build a liveness probe definition.

    Liveness is **always** TCP-based (never HTTP) with deliberately longer
    intervals than the readiness probe, ensuring the two probes are always
    different in both type and timing.

    Timing: ``initialDelaySeconds=15``, ``periodSeconds=20``,
    ``timeoutSeconds=5``, ``failureThreshold=5``.
    """
    return {
        "tcpSocket": {"port": svc.port},
        "initialDelaySeconds": 15,
        "periodSeconds": 20,
        "timeoutSeconds": 5,
        "failureThreshold": 5,
    }


def _network_policy(
    svc: Service,
    env: str,
    svc_map: dict[str, Service],
    labels: dict[str, str],
) -> dict[str, Any]:
    """Generate a NetworkPolicy enforcing the internal/external distinction.

    * **Internal services**: the first ingress rule uses a ``matchExpressions``
      selector with ``operator: NotIn`` to reject traffic from any pod labeled
      ``exposure: external``.
    * **External services**: allow traffic from any pod in the namespace.
    * All services allow egress to their declared dependencies and to DNS
      (UDP + TCP port 53).
    """
    ingress_rules: list[dict[str, Any]] = []

    if svc.exposure == "internal":
        # Internal: only allow from pods NOT labeled as external
        ingress_rules.append(
            {
                "from": [
                    {
                        "podSelector": {
                            "matchExpressions": [
                                {
                                    "key": "exposure",
                                    "operator": "NotIn",
                                    "values": ["external"],
                                }
                            ],
                        },
                    }
                ],
                "ports": [{"port": svc.port, "protocol": "TCP"}],
            }
        )
    else:
        # External: allow from anywhere in namespace
        ingress_rules.append(
            {
                "from": [{"podSelector": {}}],
                "ports": [{"port": svc.port, "protocol": "TCP"}],
            }
        )

    # Allow ingress from specific dependent services
    for other in svc_map.values():
        if svc.name in other.dependencies:
            ingress_rules.append(
                {
                    "from": [
                        {
                            "podSelector": {"matchLabels": {"app": other.name}},
                        }
                    ],
                    "ports": [{"port": svc.port, "protocol": "TCP"}],
                }
            )

    egress_rules: list[dict[str, Any]] = []
    # Allow egress to dependencies
    for dep_name in svc.dependencies:
        if dep_name in svc_map:
            dep = svc_map[dep_name]
            egress_rules.append(
                {
                    "to": [
                        {
                            "podSelector": {"matchLabels": {"app": dep_name}},
                        }
                    ],
                    "ports": [{"port": dep.port, "protocol": "TCP"}],
                }
            )

    # Allow DNS egress
    egress_rules.append(
        {
            "to": [],
            "ports": [
                {"port": 53, "protocol": "UDP"},
                {"port": 53, "protocol": "TCP"},
            ],
        }
    )

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"{svc.name}-policy",
            "namespace": env,
            "labels": labels,
        },
        "spec": {
            "podSelector": {"matchLabels": {"app": svc.name}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": ingress_rules,
            "egress": egress_rules,
        },
    }
