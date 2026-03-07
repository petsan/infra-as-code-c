"""
Terraform module generation.

Produces per-environment directories under ``<output>/terraform/<env>/``
containing JSON-formatted Terraform files (``.tf.json``).

Generated files per environment
-------------------------------
``backend.tf.json``
    S3 + DynamoDB state backend configuration.  Each environment gets its own
    bucket (``terraform-state-<env>``) and lock table
    (``terraform-locks-<env>``).

``provider.tf.json``
    AWS provider with ``default_tags`` that include the environment name.

``<service>.tf.json``
    Per-service resource definitions including:

    * **Security groups** with exact directional rules:

      - *A depends on B* means A can reach B (B gets ingress from A).
      - External services receive ALB ingress on **443** from ``0.0.0.0/0``.
      - Internal services **never** get ``0.0.0.0/0`` ingress.
      - Peer pairs get **bidirectional** ingress + egress rules.

    * **Database security groups** (Postgres 5432 / MySQL 3306) that only
      allow inbound from the owning service.
    * **RDS instances** (``db.t3.micro``).
    * **Cache security groups** (Redis 6379 / Memcached 11211).
    * **ElastiCache clusters** (``cache.t3.micro``).
    * **ECS services** with ``desired_count`` from ``env_overrides``.

Tags applied to every resource
------------------------------
``environment``, ``service-name``, ``cost-center``, ``dependency-hash``,
``last-generated`` (ISO-8601 UTC), and ``peer-group`` (for peer services).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .graph import find_peer_pairs
from .models import Manifest, Service

ENVIRONMENTS = ["dev", "staging", "prod"]
"""Target deployment environments."""


def generate_terraform(manifest: Manifest, output_dir: str) -> list[str]:
    """Generate Terraform modules for all environments.

    Args:
        manifest: The validated service manifest.
        output_dir: Root output directory.  Terraform files are written to
            ``<output_dir>/terraform/<env>/``.

    Returns:
        List of absolute file paths that were created or overwritten.
    """
    generated: list[str] = []
    svc_map = manifest.service_map()
    peer_pairs = find_peer_pairs(manifest)
    peer_set: set[tuple[str, str]] = set()
    for a, b in peer_pairs:
        peer_set.add((a, b))
        peer_set.add((b, a))

    # Build peer group labels
    peer_labels: dict[str, str] = {}
    for a, b in peer_pairs:
        label = "-".join(sorted([a, b]))
        peer_labels[a] = label
        peer_labels[b] = label

    timestamp = datetime.now(timezone.utc).isoformat()

    for env in ENVIRONMENTS:
        env_dir = Path(output_dir) / "terraform" / env
        env_dir.mkdir(parents=True, exist_ok=True)

        # State backend
        generated.append(_write_backend(env_dir, env))

        # Provider
        generated.append(_write_provider(env_dir, env))

        # Per-service resources
        for svc in manifest.services:
            generated.append(
                _write_service_module(env_dir, env, svc, svc_map, peer_set, peer_labels, timestamp)
            )

    return generated


def _write_backend(env_dir: Path, env: str) -> str:
    """Write the S3 + DynamoDB remote state backend configuration.

    Each environment receives a dedicated S3 bucket and DynamoDB lock table
    to ensure safe concurrent operations and full isolation between
    environments.
    """
    content = {
        "terraform": {
            "backend": {
                "s3": {
                    "bucket": f"terraform-state-{env}",
                    "key": f"infra/{env}/terraform.tfstate",
                    "region": "us-east-1",
                    "dynamodb_table": f"terraform-locks-{env}",
                    "encrypt": True,
                }
            },
            "required_providers": {
                "aws": {
                    "source": "hashicorp/aws",
                    "version": "~> 5.0",
                }
            },
        }
    }
    path = env_dir / "backend.tf.json"
    path.write_text(json.dumps(content, indent=2) + "\n")
    return str(path)


def _write_provider(env_dir: Path, env: str) -> str:
    """Write the AWS provider block with environment-level default tags."""
    content = {
        "provider": {
            "aws": {
                "region": "us-east-1",
                "default_tags": {
                    "tags": {
                        "environment": env,
                        "managed-by": "infra-gen",
                    }
                },
            }
        }
    }
    path = env_dir / "provider.tf.json"
    path.write_text(json.dumps(content, indent=2) + "\n")
    return str(path)


def _write_service_module(
    env_dir: Path,
    env: str,
    svc: Service,
    svc_map: dict[str, Service],
    peer_set: set[tuple[str, str]],
    peer_labels: dict[str, str],
    timestamp: str,
) -> str:
    """Write all Terraform resources for a single service in one environment.

    Generates a ``<service>.tf.json`` file containing:

    * The service security group (with directional / peer / ALB rules).
    * Database security group + RDS instance (if ``db_type != "none"``).
    * Cache security group + ElastiCache cluster (if ``cache != "none"``).
    * An ECS service resource with the correct replica count.
    """
    tags = {
        "environment": env,
        "service-name": svc.name,
        "cost-center": f"{env}-{svc.name}",
        "dependency-hash": svc.dependency_hash(),
        "last-generated": timestamp,
    }
    if svc.name in peer_labels:
        tags["peer-group"] = peer_labels[svc.name]

    resources: dict[str, Any] = {}

    # VPC security group for the service
    sg_ingress_rules: list[dict[str, Any]] = []
    sg_egress_rules: list[dict[str, Any]] = []

    # External services get ALB ingress on 443
    if svc.exposure == "external":
        sg_ingress_rules.append(
            {
                "description": "ALB ingress HTTPS",
                "from_port": 443,
                "to_port": 443,
                "protocol": "tcp",
                "cidr_blocks": ["0.0.0.0/0"],
            }
        )

    # Dependency-based rules: if A depends on B, A can reach B
    # So B gets ingress FROM A on B's port
    for other_svc in svc_map.values():
        if svc.name in other_svc.dependencies:
            # other_svc depends on us, so other_svc can reach us
            is_peer = (svc.name, other_svc.name) in peer_set
            if not is_peer:
                sg_ingress_rules.append(
                    {
                        "description": f"Ingress from {other_svc.name}",
                        "from_port": svc.port,
                        "to_port": svc.port,
                        "protocol": "tcp",
                        "security_groups": [
                            f"${{aws_security_group.{_tf_name(other_svc.name)}.id}}"
                        ],
                    }
                )

    # Peer relationship: bidirectional rules
    for other_name in sorted(svc_map.keys()):
        if (svc.name, other_name) in peer_set:
            other_svc = svc_map[other_name]
            # We can reach peer and peer can reach us
            sg_ingress_rules.append(
                {
                    "description": f"Peer ingress from {other_name}",
                    "from_port": svc.port,
                    "to_port": svc.port,
                    "protocol": "tcp",
                    "security_groups": [f"${{aws_security_group.{_tf_name(other_name)}.id}}"],
                }
            )
            sg_egress_rules.append(
                {
                    "description": f"Peer egress to {other_name}",
                    "from_port": other_svc.port,
                    "to_port": other_svc.port,
                    "protocol": "tcp",
                    "security_groups": [f"${{aws_security_group.{_tf_name(other_name)}.id}}"],
                }
            )

    # Egress to dependencies (non-peer)
    for dep_name in svc.dependencies:
        if dep_name in svc_map and (svc.name, dep_name) not in peer_set:
            dep_svc = svc_map[dep_name]
            sg_egress_rules.append(
                {
                    "description": f"Egress to dependency {dep_name}",
                    "from_port": dep_svc.port,
                    "to_port": dep_svc.port,
                    "protocol": "tcp",
                    "security_groups": [f"${{aws_security_group.{_tf_name(dep_name)}.id}}"],
                }
            )

    # IMPORTANT: Internal services must NOT have 0.0.0.0/0 ingress
    # even if external services depend on them. The ALB ingress is only
    # added for external services above.

    resources[f"aws_security_group_{_tf_name(svc.name)}"] = {
        "type": "aws_security_group",
        "name": f"{svc.name}-{env}-sg",
        "description": f"Security group for {svc.name} in {env}",
        "tags": tags,
        "ingress": sg_ingress_rules,
        "egress": sg_egress_rules,
    }

    # Database security group (if service has db)
    if svc.has_db:
        db_port = 5432 if svc.db_type == "postgres" else 3306
        db_ingress = [
            {
                "description": f"DB ingress from {svc.name}",
                "from_port": db_port,
                "to_port": db_port,
                "protocol": "tcp",
                "security_groups": [f"${{aws_security_group.{_tf_name(svc.name)}.id}}"],
            }
        ]

        db_tags = dict(tags)
        db_tags["service-name"] = f"{svc.name}-db"

        resources[f"aws_security_group_{_tf_name(svc.name)}_db"] = {
            "type": "aws_security_group",
            "name": f"{svc.name}-{env}-db-sg",
            "description": f"Database security group for {svc.name} in {env}",
            "tags": db_tags,
            "ingress": db_ingress,
            "egress": [],
        }

        # RDS instance
        resources[f"aws_db_instance_{_tf_name(svc.name)}"] = {
            "type": "aws_db_instance",
            "identifier": f"{svc.name}-{env}",
            "engine": "postgres" if svc.db_type == "postgres" else "mysql",
            "instance_class": "db.t3.micro",
            "allocated_storage": 20,
            "vpc_security_group_ids": [f"${{aws_security_group.{_tf_name(svc.name)}_db.id}}"],
            "tags": db_tags,
        }

    # Cache (ElastiCache)
    if svc.has_cache:
        cache_port = 6379 if svc.cache == "redis" else 11211
        cache_ingress = [
            {
                "description": f"Cache ingress from {svc.name}",
                "from_port": cache_port,
                "to_port": cache_port,
                "protocol": "tcp",
                "security_groups": [f"${{aws_security_group.{_tf_name(svc.name)}.id}}"],
            }
        ]

        cache_tags = dict(tags)
        cache_tags["service-name"] = f"{svc.name}-cache"

        resources[f"aws_security_group_{_tf_name(svc.name)}_cache"] = {
            "type": "aws_security_group",
            "name": f"{svc.name}-{env}-cache-sg",
            "description": f"Cache security group for {svc.name} in {env}",
            "tags": cache_tags,
            "ingress": cache_ingress,
            "egress": [],
        }

        resources[f"aws_elasticache_cluster_{_tf_name(svc.name)}"] = {
            "type": "aws_elasticache_cluster",
            "cluster_id": f"{svc.name}-{env}",
            "engine": svc.cache,
            "node_type": "cache.t3.micro",
            "num_cache_nodes": 1,
            "security_group_ids": [f"${{aws_security_group.{_tf_name(svc.name)}_cache.id}}"],
            "tags": cache_tags,
        }

    # Secrets Manager
    if svc.has_secrets:
        secret_arns: list[str] = []
        for secret_name in svc.secrets:
            sm_key = f"aws_secretsmanager_secret_{_tf_name(svc.name)}_{secret_name.lower()}"
            full_name = f"{svc.name}/{env}/{secret_name}"
            resources[sm_key] = {
                "type": "aws_secretsmanager_secret",
                "name": full_name,
                "description": f"Secret {secret_name} for {svc.name} in {env}",
                "tags": tags,
            }
            resources[f"{sm_key}_version"] = {
                "type": "aws_secretsmanager_secret_version",
                "secret_id": f"${{aws_secretsmanager_secret.{sm_key}.id}}",
                "secret_string": "CHANGE_ME",
            }
            secret_arns.append(f"${{aws_secretsmanager_secret.{sm_key}.arn}}")

        resources[f"aws_iam_policy_{_tf_name(svc.name)}_secrets"] = {
            "type": "aws_iam_policy",
            "name": f"{svc.name}-{env}-secrets-read",
            "description": f"Allow {svc.name} to read its secrets in {env}",
            "policy": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "secretsmanager:GetSecretValue",
                            "secretsmanager:DescribeSecret",
                        ],
                        "Resource": secret_arns,
                    }
                ],
            },
            "tags": tags,
        }

    # ECS / compute
    ov = svc.env_overrides.get(env)
    if ov:
        resources[f"aws_ecs_service_{_tf_name(svc.name)}"] = {
            "type": "aws_ecs_service",
            "name": f"{svc.name}-{env}",
            "desired_count": ov.replicas,
            "tags": tags,
        }

    content = {"resource": resources}
    path = env_dir / f"{svc.name}.tf.json"
    path.write_text(json.dumps(content, indent=2) + "\n")
    return str(path)


def _tf_name(name: str) -> str:
    """Convert a kebab-case service name to a Terraform-safe identifier.

    Terraform resource names cannot contain hyphens, so ``my-service``
    becomes ``my_service``.
    """
    return name.replace("-", "_")
