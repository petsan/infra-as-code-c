# Terraform Output

infra-gen produces Terraform files in JSON format (`.tf.json`) organised by
environment under `<output>/terraform/<env>/`.

## Directory Structure

```
output/terraform/
  dev/
    backend.tf.json         # S3 + DynamoDB state backend
    provider.tf.json        # AWS provider with default tags
    api-gateway.tf.json     # Per-service resources
    auth-service.tf.json
    ...
  staging/
    ...
  prod/
    ...
```

## State Backend (backend.tf.json)

Each environment gets its own S3 bucket and DynamoDB lock table for safe,
isolated state management:

```json
{
  "terraform": {
    "backend": {
      "s3": {
        "bucket": "terraform-state-prod",
        "key": "infra/prod/terraform.tfstate",
        "region": "us-east-1",
        "dynamodb_table": "terraform-locks-prod",
        "encrypt": true
      }
    },
    "required_providers": {
      "aws": { "source": "hashicorp/aws", "version": "~> 5.0" }
    }
  }
}
```

## Provider (provider.tf.json)

Default tags are applied to every resource created in the environment:

```json
{
  "provider": {
    "aws": {
      "region": "us-east-1",
      "default_tags": {
        "tags": {
          "environment": "prod",
          "managed-by": "infra-gen"
        }
      }
    }
  }
}
```

## Per-Service Resources

Each `<service>.tf.json` contains a `resource` block with:

### Security Group

Every service gets a VPC security group with exact directional rules.

**Ingress rules:**

| Condition | Rule |
|-----------|------|
| Service is `external` | Port 443 from `0.0.0.0/0` (ALB ingress) |
| Service B is depended on by service A (non-peer) | B's port from A's security group |
| Service has a peer relationship | Peer's port from peer's security group |

**Egress rules:**

| Condition | Rule |
|-----------|------|
| Service depends on another (non-peer) | Dependency's port to dependency's SG |
| Service has a peer relationship | Peer's port to peer's security group |

!!! warning "Internal services never get public ingress"
    Even if an external service depends on an internal service, the internal
    service will **not** receive a `0.0.0.0/0` ingress rule.  Traffic from
    external services reaches internal services through security-group
    references, not CIDR blocks.

### Database Resources (when `db_type != "none"`)

- **Database security group**: allows inbound only from the owning service's
  security group on port 5432 (Postgres) or 3306 (MySQL).
- **RDS instance** (`db.t3.micro`): 20 GB allocated storage.

### Cache Resources (when `cache != "none"`)

- **Cache security group**: allows inbound only from the owning service's
  security group on port 6379 (Redis) or 11211 (Memcached).
- **ElastiCache cluster** (`cache.t3.micro`): single-node cluster.

### Secrets Manager Resources (when `secrets` is non-empty)

For each secret declared on a service:

- **`aws_secretsmanager_secret`**: named `<service>/<env>/<SECRET_NAME>`.
- **`aws_secretsmanager_secret_version`**: initial version with placeholder
  value `CHANGE_ME`.

Plus one IAM policy per service:

- **`aws_iam_policy`**: grants `secretsmanager:GetSecretValue` and
  `secretsmanager:DescribeSecret`, scoped to exactly that service's secret ARNs.

!!! warning "Replace placeholders before applying"
    Secret versions are created with the value `CHANGE_ME`.  Use the AWS CLI
    or console to set real values before deploying workloads:

    ```bash
    aws secretsmanager put-secret-value \
      --secret-id "auth-service/prod/DB_PASSWORD" \
      --secret-string "real-password-here" \
      --region us-east-1
    ```

See [Secrets Vault](secrets.md) for the full setup workflow.

### ECS Service

An ECS service with `desired_count` from the environment's `replicas` override.

## Tags

Every resource is tagged with:

| Tag | Example Value |
|-----|---------------|
| `environment` | `prod` |
| `service-name` | `api-gateway` |
| `cost-center` | `prod-api-gateway` |
| `dependency-hash` | `a1b2c3d4e5f6` (SHA-256 of sorted dependencies) |
| `last-generated` | `2026-03-07T12:00:00+00:00` |
| `peer-group` | `inventory-service-order-service` (only for peer services) |

## Applying

```bash
cd output/terraform/prod
terraform init      # Download providers and configure backend
terraform plan      # Preview changes
terraform apply     # Apply changes
```
