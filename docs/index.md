# infra-gen

**Generate production-ready, multi-environment Terraform and Kubernetes manifests from a single YAML file.**

infra-gen reads a declarative service manifest and produces:

- **Terraform modules** with per-environment state backends, strict security groups, RDS instances, ElastiCache clusters, and ECS services.
- **Kubernetes manifests** with Deployments (anti-affinity + topology spread), Services, NetworkPolicies, and HorizontalPodAutoscalers.

## Key Features

| Feature | Description |
|---------|-------------|
| **Multi-environment** | Generates separate `dev`, `staging`, and `prod` configurations from one manifest |
| **Directional security groups** | A depends on B means A can reach B -- not the reverse |
| **Peer relationships** | Two-service mutual dependencies get bidirectional rules instead of cycle errors |
| **Cycle detection** | Finds *all* circular dependencies (3+ services), not just the first |
| **Drift detection** | Bidirectional: forward (pending changes) and reverse (orphaned resources) |
| **Cost estimation** | Estimated monthly AWS costs using t3.micro / db.t3.micro / cache.t3.micro pricing |
| **Secrets vault** | AWS Secrets Manager + Kubernetes Secrets with IAM policies and envFrom injection |
| **Validation** | Dependency checks, CPU format, replica ordering, secret names, self-reference detection |

## Quick Example

```yaml
# services.yaml
services:
  - name: api-gateway
    port: 8080
    dependencies: [auth-service]
    db_type: none
    cache: redis
    exposure: external
    health_check_path: /healthz
    env_overrides:
      dev:     { replicas: 1, cpu: "250m" }
      staging: { replicas: 2, cpu: "500m" }
      prod:    { replicas: 4, cpu: "1000m" }
```

```bash
# Validate, preview, and generate
infra-gen services.yaml --validate
infra-gen services.yaml --dry-run
infra-gen services.yaml -o output
```

## Project Layout

```
infra_gen/
    __init__.py       # Package metadata and module docstring
    cli.py            # argparse CLI entry-point
    models.py         # Service, EnvOverride, Manifest dataclasses
    parser.py         # YAML manifest reader
    graph.py          # Peer detection, cycle finder, topological sort
    validator.py      # Manifest validation checks
    terraform.py      # Terraform JSON module writer
    kubernetes.py     # Kubernetes YAML manifest writer
    drift.py          # Bidirectional drift detector
    cost.py           # AWS cost estimator
tests/
    test_all.py       # 118 comprehensive tests
docs/                 # This documentation (MkDocs + Material)
man/
    infra-gen.1       # Unix man page
sample.yaml           # Example manifest (6 services, no cycles)
sample_with_cycle.yaml # Example with a deliberate 3-service cycle
```

## Next Steps

- [Installation](getting-started/installation.md) -- install from source or pip
- [Quick Start](getting-started/quickstart.md) -- generate your first infrastructure
- [Manifest Format](guide/manifest.md) -- full field reference
- [Secrets Vault](guide/secrets.md) -- setup and usage
- [CLI Reference](cli-reference.md) -- every flag and option
