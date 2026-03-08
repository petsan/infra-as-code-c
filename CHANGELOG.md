# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-03-07

This release consolidates the 0.1.x series into a production-hardened baseline.
Every layer of the tool—parsing, validation, Terraform generation, cost
estimation, and graph analysis—has been strengthened with correctness fixes,
new safety checks, and expanded test coverage (273 tests).

### Added
- **ElastiCache subnet group** — `elasticache_subnet_group_name` variable and
  reference on all ElastiCache clusters for proper VPC deployment
- **DB_PASSWORD collision detection** — error when a user-declared `DB_PASSWORD`
  secret conflicts with the auto-generated database password
- **ElastiCache cluster_id length check** — info warning when
  `{name}-{env}` exceeds the 20-character AWS limit (value is truncated)
- **Non-numeric port handling** — parser raises a clear `ValueError` instead of
  an unguarded `int()` crash
- **Unknown YAML field warnings** — parser warns on unrecognised top-level and
  service-level keys to catch typos early
- **CPU cap warning** — info-level notice when millicore CPU exceeds the Fargate
  maximum of 4096m
- **Iterative cycle detection** — DFS rewritten with an explicit stack and depth
  limit, eliminating `RecursionError` on large graphs (100+ services)
- **Secrets Manager integration** — AWS Secrets Manager + Kubernetes Secrets with
  per-secret IAM least-privilege policies
- **Comprehensive validation suite** — service name format, duplicate names,
  port range (1–65535), enum checks for `db_type`/`cache`/`exposure`, type
  checks on `dependencies`/`secrets`/`regions`, replica ordering, and cycle
  detection
- **`__main__.py`** for `python -m infra_gen` support
- 273 tests covering core features, edge cases, and all new checks

### Fixed
- **Terraform JSON correctness** — `assume_role_policy`, `container_definitions`,
  and IAM `policy` fields serialised as JSON strings; proper nested resource
  syntax throughout
- **Fargate CPU mapping** — millicores round up to valid values
  (256/512/1024/2048/4096); `0m` rejected
- **Security groups** — all SGs reference `vpc_id`; inline rules documented as
  intentional for per-service file isolation
- **ECS** — service always created (defaults to 1 replica); references
  `ecs_cluster_arn`; `db_subnet_group_name` on RDS
- **RDS** — prod instances include `final_snapshot_identifier`
- **ElastiCache** — explicit `port` field; `cluster_id` truncated to 20 chars
- **Kubernetes NetworkPolicy** — DNS egress uses `[{}]` (allow any) not `[]`
  (block all)
- **Drift detection** — excludes `variables.tf.json`; distinguishes DB-generated
  secrets from user-declared; malformed `.tf.json` files no longer crash;
  multi-region path resolution corrected
- **Cost estimation** — grand total computed from raw values, not rounded
  subtotals; floating-point precision fixed
- **CLI help text** — CPU regex corrected to `^[1-9][0-9]*m$`
- **Parser** — empty YAML handled gracefully; S3 parameter validation prevents
  command injection in state reader

## [0.1.4] - 2026-03-07

### Fixed
- `assume_role_policy`, `container_definitions`, and IAM `policy` fields now
  serialized as JSON strings (required by Terraform JSON syntax)
- ECS service always created even without `env_overrides` (defaults to 1 replica)
- RDS instances in prod now include `final_snapshot_identifier`
- ElastiCache clusters now explicitly set `port` field
- Multi-region path resolution in state-aware drift detection
- Floating-point precision in cost estimation totals
- `0m` CPU value correctly rejected (must be >= 1m)
- Malformed `.tf.json` files no longer crash drift detection

### Added
- `db_subnet_group_name` variable and RDS reference
- Service name format validation (`^[a-z][a-z0-9-]*$`)
- Duplicate service name detection
- Port range validation (1-65535)
- Enum validation for `db_type`, `cache`, and `exposure` fields
- Parser type checks for `dependencies`, `secrets`, and `regions` (must be lists)
- Error handling for file write failures during generation
- `__main__.py` for `python -m infra_gen` support
- 40+ new tests covering edge cases and feature combinations

## [0.1.2] - 2026-03-07

### Fixed
- Terraform output uses proper nested JSON configuration syntax
- Fargate CPU mapping rounds up to valid values (256/512/1024/2048/4096)
- All security groups reference `vpc_id`
- ECS service references `ecs_cluster_arn`
- Kubernetes NetworkPolicy DNS egress uses empty selector (allows any destination)
- Drift detection excludes `variables.tf.json` and distinguishes DB-generated
  secrets from user-declared secrets
- Empty YAML files handled gracefully by parser
- S3 parameter validation prevents command injection in state reader

## [0.1.1] - 2026-03-07

### Added
- Secrets Manager integration (AWS Secrets Manager + Kubernetes Secrets)
- Secret name validation (`^[A-Z][A-Z0-9_]*$`)
- Per-secret IAM policies with least-privilege access

## [0.1.0] - 2026-03-07

### Added
- Initial release
- YAML manifest parser with service definitions
- Terraform JSON generation (security groups, RDS, ElastiCache, ECS Fargate)
- Kubernetes manifest generation (Deployments, Services, NetworkPolicies, HPAs)
- Dependency graph analysis with cycle detection and peer-pair handling
- Bidirectional drift detection (forward + reverse)
- State-aware drift detection (local `.tfstate` and S3 backends)
- Monthly AWS cost estimation
- Multi-region and multi-environment support
