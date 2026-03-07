"""
infra_gen -- Infrastructure-as-Code Generator
==============================================

A Python CLI tool that reads a declarative YAML service manifest and produces
production-ready, multi-environment **Terraform** modules and **Kubernetes**
manifests.  The generator enforces security-group directionality, peer
relationship handling, circular-dependency detection, drift analysis, cost
estimation, and comprehensive validation.

Modules
-------
cli
    Command-line interface (argparse entry-point).
models
    Dataclasses for Service, EnvOverride, and Manifest.
parser
    YAML manifest reader that hydrates the model layer.
graph
    Dependency-graph algorithms -- peer-pair detection, cycle finding
    (Johnson-style DFS), and topological sorting (Kahn's algorithm).
validator
    Manifest validation: dependency existence, self-references, CPU format,
    replica ordering, and cycle errors.
terraform
    Terraform JSON module writer -- backends, providers, security groups,
    RDS instances, ElastiCache clusters, and ECS services.
kubernetes
    Kubernetes YAML writer -- Deployments (anti-affinity + topology spread),
    Services, NetworkPolicies, and HorizontalPodAutoscalers.
drift
    Bidirectional drift detector -- forward (pending changes) and reverse
    (orphaned resources).
cost
    AWS monthly cost estimator using fixed per-instance-type pricing.

Quick start
-----------
>>> from infra_gen.parser import parse_manifest
>>> manifest = parse_manifest("sample.yaml")
>>> from infra_gen.terraform import generate_terraform
>>> generate_terraform(manifest, "output")
"""

__version__ = "0.1.0"
