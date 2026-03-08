"""
Terraform module generation.

Produces per-environment directories under ``<output>/terraform/<env>/``
containing JSON-formatted Terraform files (``.tf.json``) using the official
`Terraform JSON Configuration Syntax`_.

.. _Terraform JSON Configuration Syntax:
    https://developer.hashicorp.com/terraform/language/syntax/json

Generated files per environment
-------------------------------
``backend.tf.json``
    S3 + DynamoDB state backend configuration.

``provider.tf.json``
    AWS provider with ``default_tags``.

``variables.tf.json``
    Input variable declarations (``vpc_id``, ``ecs_cluster_arn``,
    ``private_subnet_ids``).

``<service>.tf.json``
    Per-service resource definitions in proper Terraform JSON format::

        {"resource": {"aws_type": {"logical_name": {fields...}}}}
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

# Valid ECS Fargate CPU values (in CPU units).
FARGATE_CPU_VALUES = [256, 512, 1024, 2048, 4096]

# Smallest valid memory (MB) for each Fargate CPU value.
FARGATE_MEMORY: dict[int, int] = {
    256: 512,
    512: 1024,
    1024: 2048,
    2048: 4096,
    4096: 8192,
}

LOG_RETENTION: dict[str, int] = {"prod": 30, "staging": 14, "dev": 7}


def _millicore_to_fargate_cpu(millicore: int) -> int:
    """Round up a Kubernetes millicore value to the nearest valid Fargate CPU."""
    for valid in FARGATE_CPU_VALUES:
        if millicore <= valid:
            return valid
    return FARGATE_CPU_VALUES[-1]


def _fargate_memory(cpu: int) -> int:
    """Return the smallest valid Fargate memory (MB) for *cpu* units."""
    return FARGATE_MEMORY.get(cpu, 512)


def _add(
    resources: dict[str, dict[str, dict[str, Any]]],
    res_type: str,
    name: str,
    config: dict[str, Any],
) -> None:
    """Insert a resource into the nested ``{type: {name: config}}`` structure."""
    resources.setdefault(res_type, {})[name] = config


def generate_terraform(manifest: Manifest, output_dir: str) -> list[str]:
    """Generate Terraform modules for all environments and regions.

    Args:
        manifest: The validated service manifest.
        output_dir: Root output directory.

    Returns:
        List of file paths that were created or overwritten.
    """
    generated: list[str] = []
    svc_map = manifest.service_map()
    peer_pairs = find_peer_pairs(manifest)
    peer_set: set[tuple[str, str]] = set()
    for a, b in peer_pairs:
        peer_set.add((a, b))
        peer_set.add((b, a))

    peer_labels: dict[str, str] = {}
    for a, b in peer_pairs:
        label = "-".join(sorted([a, b]))
        peer_labels[a] = label
        peer_labels[b] = label

    timestamp = datetime.now(timezone.utc).isoformat()
    multi_region = len(manifest.regions) > 1

    for region in manifest.regions:
        for env in ENVIRONMENTS:
            if multi_region:
                env_dir = Path(output_dir) / "terraform" / region / env
            else:
                env_dir = Path(output_dir) / "terraform" / env
            env_dir.mkdir(parents=True, exist_ok=True)

            generated.append(_write_backend(env_dir, env, region))
            generated.append(_write_provider(env_dir, env, region))
            generated.append(_write_variables(env_dir))

            for svc in manifest.services:
                generated.append(
                    _write_service(
                        env_dir, env, svc, svc_map, peer_set, peer_labels, timestamp, region
                    )
                )

    return generated


# ---------------------------------------------------------------------------
# Infrastructure files
# ---------------------------------------------------------------------------


def _write_backend(env_dir: Path, env: str, region: str = "us-east-1") -> str:
    """Write the S3 + DynamoDB remote state backend."""
    content = {
        "terraform": {
            "backend": {
                "s3": {
                    "bucket": f"terraform-state-{env}-{region}",
                    "key": f"infra/{env}/terraform.tfstate",
                    "region": region,
                    "dynamodb_table": f"terraform-locks-{env}-{region}",
                    "encrypt": True,
                }
            },
            "required_providers": {
                "aws": {"source": "hashicorp/aws", "version": "~> 5.0"},
            },
        }
    }
    return _write_json(env_dir / "backend.tf.json", content)


def _write_provider(env_dir: Path, env: str, region: str = "us-east-1") -> str:
    """Write the AWS provider block with default tags."""
    content = {
        "provider": {
            "aws": {
                "region": region,
                "default_tags": {
                    "tags": {
                        "environment": env,
                        "region": region,
                        "managed-by": "infra-gen",
                    }
                },
            }
        }
    }
    return _write_json(env_dir / "provider.tf.json", content)


def _write_variables(env_dir: Path) -> str:
    """Write input variable declarations needed by service modules."""
    content = {
        "variable": {
            "vpc_id": {
                "description": "VPC ID for security groups",
                "type": "string",
            },
            "ecs_cluster_arn": {
                "description": "ARN of the ECS cluster",
                "type": "string",
            },
            "private_subnet_ids": {
                "description": "List of private subnet IDs for ECS services",
                "type": "list(string)",
            },
            "db_subnet_group_name": {
                "description": "Name of the DB subnet group for RDS instances",
                "type": "string",
            },
        }
    }
    return _write_json(env_dir / "variables.tf.json", content)


# ---------------------------------------------------------------------------
# Per-service resource file
# ---------------------------------------------------------------------------


def _write_service(
    env_dir: Path,
    env: str,
    svc: Service,
    svc_map: dict[str, Service],
    peer_set: set[tuple[str, str]],
    peer_labels: dict[str, str],
    timestamp: str,
    region: str = "us-east-1",
) -> str:
    """Write all Terraform resources for one service in one environment."""
    tf = _tf_name(svc.name)
    tags = _tags(svc, env, peer_labels, timestamp)
    resources: dict[str, dict[str, dict[str, Any]]] = {}

    _build_security_groups(resources, svc, env, tf, tags, svc_map, peer_set)
    _build_database(resources, svc, env, tf, tags)
    _build_cache(resources, svc, env, tf, tags)
    _build_secrets(resources, svc, env, tf, tags)
    _build_ecs(resources, svc, env, tf, tags, region)

    return _write_json(env_dir / f"{svc.name}.tf.json", {"resource": resources})


# ---------------------------------------------------------------------------
# Resource builders
# ---------------------------------------------------------------------------


def _build_security_groups(
    resources: dict[str, dict[str, dict[str, Any]]],
    svc: Service,
    env: str,
    tf: str,
    tags: dict[str, str],
    svc_map: dict[str, Service],
    peer_set: set[tuple[str, str]],
) -> None:
    """Build the service VPC security group with directional rules."""
    ingress: list[dict[str, Any]] = []
    egress: list[dict[str, Any]] = []

    if svc.exposure == "external":
        ingress.append(
            {
                "description": "ALB ingress HTTPS",
                "from_port": 443,
                "to_port": 443,
                "protocol": "tcp",
                "cidr_blocks": ["0.0.0.0/0"],
            }
        )

    # Dependency-based: if other_svc depends on us, it can reach us
    for other_svc in svc_map.values():
        if svc.name in other_svc.dependencies and (svc.name, other_svc.name) not in peer_set:
            ingress.append(
                {
                    "description": f"Ingress from {other_svc.name}",
                    "from_port": svc.port,
                    "to_port": svc.port,
                    "protocol": "tcp",
                    "security_groups": [f"${{aws_security_group.{_tf_name(other_svc.name)}.id}}"],
                }
            )

    # Peer bidirectional rules
    for other_name in sorted(svc_map.keys()):
        if (svc.name, other_name) in peer_set:
            other = svc_map[other_name]
            ingress.append(
                {
                    "description": f"Peer ingress from {other_name}",
                    "from_port": svc.port,
                    "to_port": svc.port,
                    "protocol": "tcp",
                    "security_groups": [f"${{aws_security_group.{_tf_name(other_name)}.id}}"],
                }
            )
            egress.append(
                {
                    "description": f"Peer egress to {other_name}",
                    "from_port": other.port,
                    "to_port": other.port,
                    "protocol": "tcp",
                    "security_groups": [f"${{aws_security_group.{_tf_name(other_name)}.id}}"],
                }
            )

    # Egress to non-peer dependencies
    for dep_name in svc.dependencies:
        if dep_name in svc_map and (svc.name, dep_name) not in peer_set:
            dep = svc_map[dep_name]
            egress.append(
                {
                    "description": f"Egress to dependency {dep_name}",
                    "from_port": dep.port,
                    "to_port": dep.port,
                    "protocol": "tcp",
                    "security_groups": [f"${{aws_security_group.{_tf_name(dep_name)}.id}}"],
                }
            )

    _add(
        resources,
        "aws_security_group",
        tf,
        {
            "name": f"{svc.name}-{env}-sg",
            "description": f"Security group for {svc.name} in {env}",
            "vpc_id": "${var.vpc_id}",
            "tags": tags,
            "ingress": ingress,
            "egress": egress,
        },
    )


def _build_database(
    resources: dict[str, dict[str, dict[str, Any]]],
    svc: Service,
    env: str,
    tf: str,
    tags: dict[str, str],
) -> None:
    """Build DB security group + RDS instance."""
    if not svc.has_db:
        return

    db_port = 5432 if svc.db_type == "postgres" else 3306
    db_tags = {**tags, "service-name": f"{svc.name}-db"}

    _add(
        resources,
        "aws_security_group",
        f"{tf}_db",
        {
            "name": f"{svc.name}-{env}-db-sg",
            "description": f"Database security group for {svc.name} in {env}",
            "vpc_id": "${var.vpc_id}",
            "tags": db_tags,
            "ingress": [
                {
                    "description": f"DB ingress from {svc.name}",
                    "from_port": db_port,
                    "to_port": db_port,
                    "protocol": "tcp",
                    "security_groups": [f"${{aws_security_group.{tf}.id}}"],
                }
            ],
            "egress": [],
        },
    )

    _add(
        resources,
        "aws_db_instance",
        tf,
        {
            "identifier": f"{svc.name}-{env}",
            "engine": "postgres" if svc.db_type == "postgres" else "mysql",
            "engine_version": "15.4" if svc.db_type == "postgres" else "8.0",
            "instance_class": "db.t3.micro",
            "allocated_storage": 20,
            "username": "admin",
            "password": f"${{aws_secretsmanager_secret_version.{tf}_db_password.secret_string}}",
            "skip_final_snapshot": env != "prod",
            **({"final_snapshot_identifier": f"{svc.name}-{env}-final"} if env == "prod" else {}),
            "db_subnet_group_name": "${var.db_subnet_group_name}",
            "vpc_security_group_ids": [f"${{aws_security_group.{tf}_db.id}}"],
            "tags": db_tags,
        },
    )

    # Auto-generate a Secrets Manager secret for the DB password
    _add(
        resources,
        "aws_secretsmanager_secret",
        f"{tf}_db_password",
        {
            "name": f"{svc.name}/{env}/DB_PASSWORD_GENERATED",
            "description": f"Auto-generated DB password for {svc.name} in {env}",
            "tags": db_tags,
        },
    )
    _add(
        resources,
        "aws_secretsmanager_secret_version",
        f"{tf}_db_password",
        {
            "secret_id": f"${{aws_secretsmanager_secret.{tf}_db_password.id}}",
            "secret_string": "CHANGE_ME",
        },
    )


def _build_cache(
    resources: dict[str, dict[str, dict[str, Any]]],
    svc: Service,
    env: str,
    tf: str,
    tags: dict[str, str],
) -> None:
    """Build cache security group + ElastiCache cluster."""
    if not svc.has_cache:
        return

    cache_port = 6379 if svc.cache == "redis" else 11211
    cache_tags = {**tags, "service-name": f"{svc.name}-cache"}

    _add(
        resources,
        "aws_security_group",
        f"{tf}_cache",
        {
            "name": f"{svc.name}-{env}-cache-sg",
            "description": f"Cache security group for {svc.name} in {env}",
            "vpc_id": "${var.vpc_id}",
            "tags": cache_tags,
            "ingress": [
                {
                    "description": f"Cache ingress from {svc.name}",
                    "from_port": cache_port,
                    "to_port": cache_port,
                    "protocol": "tcp",
                    "security_groups": [f"${{aws_security_group.{tf}.id}}"],
                }
            ],
            "egress": [],
        },
    )

    _add(
        resources,
        "aws_elasticache_cluster",
        tf,
        {
            "cluster_id": f"{svc.name}-{env}",
            "engine": svc.cache,
            "node_type": "cache.t3.micro",
            "port": cache_port,
            "num_cache_nodes": 1,
            "security_group_ids": [f"${{aws_security_group.{tf}_cache.id}}"],
            "tags": cache_tags,
        },
    )


def _build_secrets(
    resources: dict[str, dict[str, dict[str, Any]]],
    svc: Service,
    env: str,
    tf: str,
    tags: dict[str, str],
) -> None:
    """Build Secrets Manager secrets + IAM policy."""
    if not svc.has_secrets:
        return

    secret_arns: list[str] = []
    for secret_name in svc.secrets:
        sm_name = f"{tf}_{secret_name.lower()}"
        _add(
            resources,
            "aws_secretsmanager_secret",
            sm_name,
            {
                "name": f"{svc.name}/{env}/{secret_name}",
                "description": f"Secret {secret_name} for {svc.name} in {env}",
                "tags": tags,
            },
        )
        _add(
            resources,
            "aws_secretsmanager_secret_version",
            sm_name,
            {
                "secret_id": f"${{aws_secretsmanager_secret.{sm_name}.id}}",
                "secret_string": "CHANGE_ME",
            },
        )
        secret_arns.append(f"${{aws_secretsmanager_secret.{sm_name}.arn}}")

    _add(
        resources,
        "aws_iam_policy",
        f"{tf}_secrets",
        {
            "name": f"{svc.name}-{env}-secrets-read",
            "description": f"Allow {svc.name} to read its secrets in {env}",
            "policy": json.dumps(
                {
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
                }
            ),
            "tags": tags,
        },
    )


def _build_ecs(
    resources: dict[str, dict[str, dict[str, Any]]],
    svc: Service,
    env: str,
    tf: str,
    tags: dict[str, str],
    region: str,
) -> None:
    """Build CloudWatch log group, IAM roles, ECS task definition + service."""
    log_group_name = f"/ecs/{svc.name}/{env}"

    # CloudWatch Log Group
    _add(
        resources,
        "aws_cloudwatch_log_group",
        tf,
        {
            "name": log_group_name,
            "retention_in_days": LOG_RETENTION.get(env, 7),
            "tags": tags,
        },
    )

    # IAM Execution Role
    assume_ecs = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    _add(
        resources,
        "aws_iam_role",
        f"{tf}_execution",
        {
            "name": f"{svc.name}-{env}-ecs-execution",
            "assume_role_policy": json.dumps(assume_ecs),
            "tags": tags,
        },
    )
    _add(
        resources,
        "aws_iam_role_policy_attachment",
        f"{tf}_execution",
        {
            "role": f"${{aws_iam_role.{tf}_execution.name}}",
            "policy_arn": "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
        },
    )

    # IAM Task Role
    _add(
        resources,
        "aws_iam_role",
        f"{tf}_task",
        {
            "name": f"{svc.name}-{env}-ecs-task",
            "assume_role_policy": json.dumps(assume_ecs),
            "tags": tags,
        },
    )
    if svc.has_secrets:
        _add(
            resources,
            "aws_iam_role_policy_attachment",
            f"{tf}_secrets",
            {
                "role": f"${{aws_iam_role.{tf}_task.name}}",
                "policy_arn": f"${{aws_iam_policy.{tf}_secrets.arn}}",
            },
        )

    # ECS Task Definition
    ov = svc.env_overrides.get(env)
    millicore = int((ov.cpu if ov else "256m").rstrip("m"))
    cpu = _millicore_to_fargate_cpu(millicore)
    memory = _fargate_memory(cpu)

    container: dict[str, Any] = {
        "name": svc.name,
        "image": f"{svc.name}:latest",
        "essential": True,
        "portMappings": [{"containerPort": svc.port, "protocol": "tcp"}],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": log_group_name,
                "awslogs-region": region,
                "awslogs-stream-prefix": svc.name,
            },
        },
        "environment": [
            {"name": "ENV", "value": env},
            {"name": "SERVICE_NAME", "value": svc.name},
            {"name": "PORT", "value": str(svc.port)},
        ],
    }

    if svc.health_check_path:
        container["healthCheck"] = {
            "command": [
                "CMD-SHELL",
                f"curl -f http://localhost:{svc.port}{svc.health_check_path} || exit 1",
            ],
            "interval": 30,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 60,
        }

    if svc.has_secrets:
        container["secrets"] = [
            {
                "name": s,
                "valueFrom": f"${{aws_secretsmanager_secret.{tf}_{s.lower()}.arn}}",
            }
            for s in svc.secrets
        ]

    _add(
        resources,
        "aws_ecs_task_definition",
        tf,
        {
            "family": f"{svc.name}-{env}",
            "requires_compatibilities": ["FARGATE"],
            "network_mode": "awsvpc",
            "cpu": str(cpu),
            "memory": str(memory),
            "execution_role_arn": f"${{aws_iam_role.{tf}_execution.arn}}",
            "task_role_arn": f"${{aws_iam_role.{tf}_task.arn}}",
            "container_definitions": json.dumps([container]),
            "tags": tags,
        },
    )

    # ECS Service
    replicas = ov.replicas if ov else 1
    _add(
        resources,
        "aws_ecs_service",
        tf,
        {
            "name": f"{svc.name}-{env}",
            "cluster": "${var.ecs_cluster_arn}",
            "task_definition": f"${{aws_ecs_task_definition.{tf}.arn}}",
            "desired_count": replicas,
            "launch_type": "FARGATE",
            "network_configuration": {
                "subnets": "${var.private_subnet_ids}",
                "security_groups": [f"${{aws_security_group.{tf}.id}}"],
                "assign_public_ip": svc.exposure == "external",
            },
            "deployment_circuit_breaker": {"enable": True, "rollback": True},
            "deployment_minimum_healthy_percent": 100,
            "deployment_maximum_percent": 200,
            "tags": tags,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tags(svc: Service, env: str, peer_labels: dict[str, str], timestamp: str) -> dict[str, str]:
    """Build the standard tag set for a service."""
    t = {
        "environment": env,
        "service-name": svc.name,
        "cost-center": f"{env}-{svc.name}",
        "dependency-hash": svc.dependency_hash(),
        "last-generated": timestamp,
    }
    if svc.name in peer_labels:
        t["peer-group"] = peer_labels[svc.name]
    return t


def _write_json(path: Path, content: dict[str, Any]) -> str:
    """Write *content* as indented JSON to *path* and return the path string."""
    path.write_text(json.dumps(content, indent=2) + "\n")
    return str(path)


def _tf_name(name: str) -> str:
    """Convert a kebab-case service name to a Terraform-safe identifier."""
    return name.replace("-", "_")
