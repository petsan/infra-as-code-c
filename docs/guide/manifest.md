# Manifest Format

The YAML manifest is the single source of truth for your service topology.
It defines every service, its dependencies, infrastructure requirements,
and per-environment resource overrides.

## Top-Level Structure

```yaml
services:
  - name: ...
    port: ...
    # ... additional fields
  - name: ...
    # ...
```

The file must contain a top-level `services` key whose value is a list of
service definitions.

## Service Fields

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique service identifier. Used as the resource name prefix in all generated files. Must be unique across the manifest. |
| `port` | integer | Primary container listening port. Used for security group rules, Kubernetes service definitions, and health check probes. |

### Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `dependencies` | list of strings | `[]` | Service names this service depends on. Drives security group rules and topological ordering. |
| `db_type` | string | `"none"` | Database engine: `postgres`, `mysql`, or `none`. When not `none`, an RDS instance and database security group are provisioned. |
| `cache` | string | `"none"` | Cache engine: `redis`, `memcached`, or `none`. When not `none`, an ElastiCache cluster and cache security group are provisioned. |
| `exposure` | string | `"internal"` | Network exposure: `internal` or `external`. External services get ALB ingress on port 443. Internal services are guaranteed no public ingress. |
| `health_check_path` | string | *null* | HTTP path for Kubernetes readiness probes (e.g. `/healthz`). When omitted, a TCP socket probe is used instead. |
| `secrets` | list of strings | `[]` | Secret names required at runtime (e.g. `DB_PASSWORD`, `API_KEY`). Names must match `^[A-Z][A-Z0-9_]*$`. See [Secrets Vault](secrets.md). |
| `env_overrides` | mapping | `{}` | Per-environment overrides (see below). |

### Environment Overrides

The `env_overrides` mapping must contain entries for all three environments:
`dev`, `staging`, and `prod`.

```yaml
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
```

| Sub-field | Type | Validation |
|-----------|------|------------|
| `replicas` | integer | Must be > 0. Must satisfy `prod >= staging >= dev`. |
| `cpu` | string | Must match regex `^[0-9]+m$` (Kubernetes millicore notation). |

## Complete Example

```yaml
services:
  # External API gateway with Redis cache and secrets
  - name: api-gateway
    port: 8080
    dependencies:
      - auth-service
      - order-service
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

  # Internal auth service with Postgres + Redis and secrets
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

  # Peer pair: order-service <-> inventory-service
  - name: order-service
    port: 8082
    dependencies:
      - inventory-service
    db_type: postgres
    cache: memcached
    exposure: internal
    env_overrides:
      dev:     { replicas: 1, cpu: "250m" }
      staging: { replicas: 2, cpu: "500m" }
      prod:    { replicas: 3, cpu: "750m" }

  - name: inventory-service
    port: 8083
    dependencies:
      - order-service
    db_type: mysql
    cache: none
    exposure: internal
    health_check_path: /ready
    env_overrides:
      dev:     { replicas: 1, cpu: "200m" }
      staging: { replicas: 2, cpu: "400m" }
      prod:    { replicas: 3, cpu: "600m" }
```

## Dependency Rules

- Every name in `dependencies` must match the `name` of another service in the manifest.
- A service must not list itself in `dependencies` (self-reference).
- Two-service mutual dependencies (A depends on B **and** B depends on A) are valid **peer relationships** -- they are not cycle errors.
- Circular dependencies involving 3 or more services are errors that block generation.

See [Peer Relationships](peers.md) for details on how peer pairs are handled.

## Secrets

Services can declare secrets they need at runtime.  Names must be uppercase
with underscores (`^[A-Z][A-Z0-9_]*$`).  See [Secrets Vault](secrets.md)
for full setup and usage instructions.
