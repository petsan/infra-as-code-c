# infra-gen

A Python CLI tool that generates production-ready, multi-environment **Terraform** modules and **Kubernetes** manifests from a single declarative YAML service manifest.

## Features

- **Multi-environment generation** -- separate `dev`, `staging`, and `prod` configurations from one manifest
- **Strict security groups** -- directional rules (A depends on B means A can reach B, not the reverse), no public ingress on internal services
- **Peer relationships** -- two-service mutual dependencies get bidirectional rules instead of cycle errors
- **Cycle detection** -- finds *all* circular dependencies (3+ services), not just the first
- **Bidirectional drift detection** -- forward (pending changes) and reverse (orphaned resources)
- **Cost estimation** -- estimated monthly AWS costs per environment
- **Secrets vault** -- AWS Secrets Manager + Kubernetes Secrets with scoped IAM policies
- **Comprehensive validation** -- dependency checks, CPU format, replica ordering, secret names, self-references

## Prerequisites

| Tool | Version | Required for |
|------|---------|-------------|
| **Python** | >= 3.10 | Running infra-gen |
| **pip** | any recent | Installing infra-gen |
| **Terraform** | >= 1.5 | Applying generated `.tf.json` files |
| **kubectl** | >= 1.27 | Applying generated Kubernetes YAML |
| **AWS CLI** | >= 2.0 | Creating S3 state buckets and DynamoDB lock tables |

> **Note:** Terraform, kubectl, and the AWS CLI are only needed to *apply* the generated files. infra-gen itself only requires Python and PyYAML.

## Installation

### From Source (recommended)

```bash
git clone https://github.com/your-org/infra-gen.git
cd infra-gen

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate      # Linux / macOS
# .venv\Scripts\activate       # Windows

# Install in editable mode
pip install -e .

# Verify installation
infra-gen --version
```

### From pip

```bash
pip install infra-gen
```

## Quick Start

### 1. Write a Manifest

Create a `services.yaml` file (or use the included `sample.yaml`):

```yaml
services:
  - name: api-gateway
    port: 8080
    dependencies:
      - auth-service
    db_type: none
    cache: redis
    exposure: external
    health_check_path: /healthz
    secrets:
      - JWT_SECRET
      - RATE_LIMIT_KEY
    env_overrides:
      dev:     { replicas: 1, cpu: "250m" }
      staging: { replicas: 2, cpu: "500m" }
      prod:    { replicas: 4, cpu: "1000m" }

  - name: auth-service
    port: 8081
    dependencies: []
    db_type: postgres
    cache: redis
    exposure: internal
    health_check_path: /health
    secrets:
      - DB_PASSWORD
      - OAUTH_CLIENT_SECRET
    env_overrides:
      dev:     { replicas: 1, cpu: "250m" }
      staging: { replicas: 2, cpu: "500m" }
      prod:    { replicas: 3, cpu: "750m" }
```

### 2. Validate

```bash
infra-gen services.yaml --validate
```

```
[INFO] Peer relationship detected: ...   # (if peers exist)

Validation PASSED
```

### 3. Preview (Dry-Run)

```bash
infra-gen services.yaml --dry-run
```

```
=== Resource Creation Order ===
  1. auth-service [db:postgres, cache:redis]
  2. api-gateway [cache:redis, external]

=== Estimated Monthly AWS Costs ===

  DEV:
    api-gateway                    $   19.01/mo
    auth-service                   $   31.26/mo
    ...
  GRAND TOTAL                      $  ...
```

### 4. Generate

```bash
infra-gen services.yaml -o output
```

This creates:

```
output/
  terraform/
    dev/
      backend.tf.json        # S3 + DynamoDB state backend
      provider.tf.json       # AWS provider with default tags
      api-gateway.tf.json    # SGs, ECS (task def, IAM, logs), ElastiCache
      auth-service.tf.json   # SGs, RDS, ElastiCache, ECS (task def, IAM, logs)
    staging/
      ...
    prod/
      ...
  kubernetes/
    dev/
      api-gateway.yaml       # Deployment, Service, NetworkPolicy, HPA
      auth-service.yaml
    staging/
      ...
    prod/
      ...
```

### 5. Detect Drift

After editing the manifest, check what would change:

```bash
infra-gen services.yaml --drift -o output
```

```
=== FORWARD DRIFT (changes to apply) ===
  [CREATE] terraform/dev/new-service: New service, Terraform file will be created
  ...

=== REVERSE DRIFT: No orphaned resources ===
```

## CLI Reference

```
usage: infra-gen [-h] [-o DIR] [--validate] [--dry-run] [--drift] [--version] MANIFEST
```

| Flag | Description |
|------|-------------|
| `MANIFEST` | Path to YAML manifest file (required) |
| `-o DIR` | Output directory (default: `output`) |
| `--validate` | Validate manifest and exit (exit 0 = valid, 1 = errors) |
| `--dry-run` | Show creation order + cost estimate without writing files |
| `--drift` | Detect forward/reverse drift against existing output (exit 1 if orphans found) |
| `--version` | Print version and exit |
| `-h, --help` | Show detailed help with examples, manifest format, and validation rules |

## Manifest Format

Each service in the `services` list supports:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | -- | Unique service identifier |
| `port` | integer | yes | -- | Primary container port |
| `dependencies` | list[string] | no | `[]` | Other service names this depends on |
| `db_type` | string | no | `"none"` | `postgres`, `mysql`, or `none` |
| `cache` | string | no | `"none"` | `redis`, `memcached`, or `none` |
| `exposure` | string | no | `"internal"` | `internal` or `external` |
| `health_check_path` | string | no | *null* | HTTP path for readiness probes |
| `secrets` | list[string] | no | `[]` | Secret names (e.g. `DB_PASSWORD`). Must match `^[A-Z][A-Z0-9_]*$` |
| `env_overrides` | mapping | no | `{}` | Per-env `replicas` and `cpu` |

### Environment Overrides

```yaml
env_overrides:
  dev:     { replicas: 1, cpu: "250m" }
  staging: { replicas: 2, cpu: "500m" }
  prod:    { replicas: 4, cpu: "1000m" }
```

- `replicas` must be > 0
- `cpu` must match `^[0-9]+m$` (Kubernetes millicore format)
- `prod` replicas >= `staging` replicas >= `dev` replicas

## Validation Rules

| Rule | Error Message |
|------|---------------|
| Self-reference | `Service 'x' has a self-reference in dependencies` |
| Missing dependency | `Service 'x' depends on unknown service 'y'` |
| Invalid replicas | `replicas must be > 0` |
| Invalid CPU | `cpu must match ^[0-9]+m$` |
| Replica ordering | `replica ordering violated - prod(N) >= staging(N) >= dev(N) required` |
| Invalid secret name | `invalid secret name 'x' (must match ^[A-Z][A-Z0-9_]*$)` |
| Duplicate secrets | `duplicate secret names` |
| True cycle (3+) | `True cycle detected (3+ services): a -> b -> c -> a` |

Two-service mutual dependencies are valid **peer relationships** and are reported as `[INFO]`.

## Generated Terraform

### Security Group Rules

| Condition | Rule |
|-----------|------|
| A depends on B | B gets ingress from A on B's port |
| Service is `external` | Port 443 from `0.0.0.0/0` (ALB) |
| Service is `internal` | **No** `0.0.0.0/0` ingress, even if external services depend on it |
| Peer pair (A, B) | Bidirectional ingress + egress between A and B |
| `db_type != none` | DB security group: inbound only from owning service |
| `cache != none` | Cache security group: inbound only from owning service |
| `secrets` non-empty | Secrets Manager secrets + IAM policy scoped to owning service |

### ECS Fargate Stack

Each service generates a complete ECS Fargate deployment:

| Resource | Details |
|----------|---------|
| **CloudWatch Log Group** | `/ecs/<service>/<env>`, retention: 30d (prod), 14d (staging), 7d (dev) |
| **IAM Execution Role** | Pulls images + writes logs (`AmazonECSTaskExecutionRolePolicy`) |
| **IAM Task Role** | Container runtime permissions; secrets policy attached when applicable |
| **ECS Task Definition** | Fargate/awsvpc, CPU/memory from `env_overrides`, `awslogs` driver, health check, secrets injection |
| **ECS Service** | References task def, circuit breaker with rollback, min 100% / max 200% deploy, public IP for `external` only |

### Tags on Every Resource

`environment`, `service-name`, `cost-center`, `dependency-hash`, `last-generated`, and `peer-group` (for peers).

### State Backend

Each environment gets its own S3 bucket (`terraform-state-<env>`) and DynamoDB lock table (`terraform-locks-<env>`).

## Generated Kubernetes

| Resource | Key Details |
|----------|-------------|
| **Deployment** | Pod anti-affinity (spread across nodes) + topology spread (max 2 per zone) |
| **Service** | ClusterIP on the container port |
| **NetworkPolicy** | Internal pods reject traffic from `exposure: external` pods |
| **HPA** | CPU target 70%, memory target 80% |
| **Secret** | Opaque secret with `envFrom` injection (only when `secrets` is non-empty) |

### Probes

| Probe | Type | Timing |
|-------|------|--------|
| Readiness | HTTP GET (if `health_check_path` set) or TCP socket | 5s initial, 10s period |
| Liveness | **Always** TCP socket (different from readiness) | 15s initial, 20s period |

## Cost Estimation

The `--dry-run` flag shows estimated monthly AWS costs:

| Instance Type | Price | Provisioned For |
|---------------|-------|-----------------|
| `t3.micro` | $7.49/mo | One per replica |
| `db.t3.micro` | $12.25/mo | One per service with `db_type != none` |
| `cache.t3.micro` | $11.52/mo | One per service with `cache != none` |
| secret | $0.40/mo | One per secret in `secrets` |

## Peer Relationships

When exactly two services mutually depend on each other (A depends on B **and** B depends on A):

1. They are **not** treated as a cycle error
2. They get **bidirectional** security group rules (both ingress and egress)
3. They are tagged with `peer-group: <sorted-names>` (e.g. `inventory-service-order-service`)
4. They are skipped in cycle detection
5. True cycles are 3+ services only

## Secrets Vault

Services can declare secrets they need at runtime:

```yaml
secrets:
  - DB_PASSWORD
  - API_KEY
```

This generates:

| Output | Resource | Description |
|--------|----------|-------------|
| Terraform | `aws_secretsmanager_secret` | One per secret, named `<service>/<env>/<NAME>` |
| Terraform | `aws_secretsmanager_secret_version` | Placeholder value `CHANGE_ME` |
| Terraform | `aws_iam_policy` | Grants `GetSecretValue` scoped to the service's secrets only |
| Kubernetes | `Secret` | Opaque secret with base64 placeholder values |
| Kubernetes | `envFrom` | Added to the Deployment container to inject secrets as env vars |

After applying Terraform, replace the placeholder values:

```bash
aws secretsmanager put-secret-value \
  --secret-id "auth-service/prod/DB_PASSWORD" \
  --secret-string "real-password-here" \
  --region us-east-1
```

For Kubernetes, either edit the generated YAML or use `kubectl create secret`:

```bash
kubectl create secret generic auth-service-secrets \
  --namespace prod \
  --from-literal=DB_PASSWORD="real-password-here" \
  --dry-run=client -o yaml | kubectl apply -f -
```

## Applying the Generated Files

### AWS Prerequisites

Create S3 buckets and DynamoDB tables for Terraform state:

```bash
for ENV in dev staging prod; do
  aws s3api create-bucket \
    --bucket "terraform-state-${ENV}" \
    --region us-east-1

  aws s3api put-bucket-versioning \
    --bucket "terraform-state-${ENV}" \
    --versioning-configuration Status=Enabled

  aws dynamodb create-table \
    --table-name "terraform-locks-${ENV}" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region us-east-1
done
```

### Terraform

```bash
cd output/terraform/prod
terraform init
terraform plan
terraform apply
```

### Kubernetes

Create namespaces and apply:

```bash
for ENV in dev staging prod; do
  kubectl create namespace "${ENV}" --dry-run=client -o yaml | kubectl apply -f -
done

kubectl apply -f output/kubernetes/prod/
```

## Man Page

A Unix man page is included at `man/infra-gen.1`:

```bash
# Read directly
man ./man/infra-gen.1

# Or install system-wide
sudo cp man/infra-gen.1 /usr/local/share/man/man1/
sudo mandb
man infra-gen
```

## Documentation Site

The project includes a full [MkDocs](https://www.mkdocs.org/) documentation site with the [Material](https://squidfunnel.github.io/mkdocs-material/) theme:

```bash
# Install documentation dependencies
pip install mkdocs mkdocs-material mkdocstrings[python]

# Serve locally with live reload
mkdocs serve          # Visit http://127.0.0.1:8000

# Build static HTML
mkdocs build          # Output in site/
```

The documentation includes:

- **Getting Started**: installation and quick start guide
- **User Guide**: manifest format, Terraform/Kubernetes output details, validation, drift detection, cost estimation, peer relationships
- **CLI Reference**: every flag, option, and exit code
- **API Reference**: auto-generated from docstrings for every module, class, and function
- **Architecture**: module dependency graph, data flow, design decisions, security model

## Running Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

165 tests covering: parsing, graph algorithms (peers, cycles, topological sort), validation (every error case including secret names), Terraform output (SG directionality, ALB ingress, internal protection, DB isolation, Secrets Manager, IAM policies, tags, peers), ECS Fargate stack (task definitions, IAM roles, CloudWatch log groups, container definitions, health checks, secrets injection, network config, circuit breaker), Kubernetes output (anti-affinity, topology spread, probes, HPA, NetworkPolicy, Secrets, envFrom), drift detection (forward, reverse, no-drift, structural changes including secrets), cost estimation, and CLI end-to-end.

## Project Structure

```
infra-gen/
  infra_gen/
    __init__.py           # Package metadata
    cli.py                # argparse CLI entry-point
    models.py             # Service, EnvOverride, Manifest dataclasses
    parser.py             # YAML manifest reader
    graph.py              # Peer detection, cycle finder, topological sort
    validator.py          # Manifest validation checks
    terraform.py          # Terraform JSON module writer
    kubernetes.py         # Kubernetes YAML manifest writer
    drift.py              # Bidirectional drift detector
    cost.py               # AWS cost estimator
  tests/
    test_all.py           # 118 comprehensive tests
  docs/                   # MkDocs documentation source
    index.md
    getting-started/
    guide/
    api/
    architecture.md
    cli-reference.md
  man/
    infra-gen.1           # Unix man page
  sample.yaml             # Example: 6 services, peer pair, no cycles
  sample_with_cycle.yaml  # Example: adds a 3-service cycle for testing
  pyproject.toml          # Package configuration
  mkdocs.yml              # MkDocs configuration
  README.md               # This file
```

## License

See [LICENSE](LICENSE) for details.
