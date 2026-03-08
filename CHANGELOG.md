# Changelog

All notable changes to this project will be documented in this file.

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
