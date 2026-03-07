# Secrets Vault

infra-gen provisions a secrets vault for services that declare sensitive
configuration.  Secrets are managed through **AWS Secrets Manager** on the
Terraform side and **Kubernetes Secrets** on the Kubernetes side.  Secret
values are never written in plaintext -- generated files contain placeholder
values that you replace before applying.

## Declaring Secrets

Add a `secrets` list to any service in your manifest:

```yaml
services:
  - name: auth-service
    port: 8081
    db_type: postgres
    cache: redis
    exposure: internal
    secrets:
      - DB_PASSWORD
      - OAUTH_CLIENT_SECRET
    env_overrides:
      dev:     { replicas: 1, cpu: "250m" }
      staging: { replicas: 2, cpu: "500m" }
      prod:    { replicas: 3, cpu: "750m" }
```

### Naming Rules

Secret names must match the regex `^[A-Z][A-Z0-9_]*$`:

| Valid | Invalid | Reason |
|-------|---------|--------|
| `DB_PASSWORD` | `db_password` | Must be uppercase |
| `API_KEY` | `API-KEY` | Hyphens not allowed |
| `X509_CERT` | `509_CERT` | Must start with a letter |
| `TOKEN` | `token` | Must be uppercase |

Duplicate secret names within a service are rejected.

## What Gets Generated

### Terraform (AWS Secrets Manager)

For each secret declared on a service, three resources are created per
environment:

1. **`aws_secretsmanager_secret`** -- the secret itself, named
   `<service>/<env>/<SECRET_NAME>` (e.g. `auth-service/prod/DB_PASSWORD`).

2. **`aws_secretsmanager_secret_version`** -- an initial version with the
   placeholder value `CHANGE_ME`.

3. **`aws_iam_policy`** (one per service) -- grants `GetSecretValue` and
   `DescribeSecret` scoped to exactly that service's secret ARNs.

```json
{
  "resource": {
    "aws_secretsmanager_secret_auth_service_db_password": {
      "type": "aws_secretsmanager_secret",
      "name": "auth-service/prod/DB_PASSWORD",
      "description": "Secret DB_PASSWORD for auth-service in prod",
      "tags": { "..." }
    },
    "aws_secretsmanager_secret_auth_service_db_password_version": {
      "type": "aws_secretsmanager_secret_version",
      "secret_id": "${aws_secretsmanager_secret.auth_service_db_password.id}",
      "secret_string": "CHANGE_ME"
    },
    "aws_iam_policy_auth_service_secrets": {
      "type": "aws_iam_policy",
      "name": "auth-service-prod-secrets-read",
      "policy": {
        "Version": "2012-10-17",
        "Statement": [{
          "Effect": "Allow",
          "Action": [
            "secretsmanager:GetSecretValue",
            "secretsmanager:DescribeSecret"
          ],
          "Resource": [
            "${aws_secretsmanager_secret.auth_service_db_password.arn}"
          ]
        }]
      }
    }
  }
}
```

### Kubernetes (Secret + envFrom)

A **Secret** resource is appended to the service's multi-document YAML file:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: auth-service-secrets
  namespace: prod
  labels:
    app: auth-service
    environment: prod
    exposure: internal
type: Opaque
data:
  DB_PASSWORD: Q0hBTkdFX01F          # base64("CHANGE_ME")
  OAUTH_CLIENT_SECRET: Q0hBTkdFX01F
```

The Deployment container gets an `envFrom` entry that injects all secret keys
as environment variables:

```yaml
containers:
  - name: auth-service
    envFrom:
      - secretRef:
          name: auth-service-secrets
```

Services **without** secrets get neither a Secret resource nor an `envFrom`
entry -- existing behaviour is unchanged.

## Setting Up the Vault

### Step 1: Generate the infrastructure

```bash
infra-gen services.yaml -o output
```

### Step 2: Apply Terraform to create the secrets

```bash
cd output/terraform/prod
terraform init
terraform plan    # Review the Secrets Manager resources
terraform apply
```

This creates the secrets in AWS Secrets Manager with placeholder values.

### Step 3: Replace placeholder values

Use the AWS CLI to set real secret values:

```bash
# Set a secret for a specific environment
aws secretsmanager put-secret-value \
  --secret-id "auth-service/prod/DB_PASSWORD" \
  --secret-string "my-actual-database-password" \
  --region us-east-1

aws secretsmanager put-secret-value \
  --secret-id "auth-service/prod/OAUTH_CLIENT_SECRET" \
  --secret-string "real-oauth-secret" \
  --region us-east-1
```

!!! warning "Never commit real secrets"
    The generated files contain `CHANGE_ME` placeholders.  Always set real
    values through the AWS CLI or console -- never edit the generated
    `.tf.json` files with actual secrets.

### Step 4: Attach the IAM policy to your ECS task role

```bash
aws iam attach-role-policy \
  --role-name "auth-service-prod-task-role" \
  --policy-arn "$(terraform output -raw auth_service_secrets_policy_arn)"
```

### Step 5: Apply Kubernetes secrets

Before applying the Kubernetes manifests, replace the placeholder base64
values with real ones:

```bash
# Encode your real secret
echo -n "my-actual-database-password" | base64
# -> bXktYWN0dWFsLWRhdGFiYXNlLXBhc3N3b3Jk

# Edit the secret in the generated YAML
# Replace Q0hBTkdFX01F with the real base64 value
```

Or use `kubectl create secret` directly:

```bash
kubectl create secret generic auth-service-secrets \
  --namespace prod \
  --from-literal=DB_PASSWORD="my-actual-database-password" \
  --from-literal=OAUTH_CLIENT_SECRET="real-oauth-secret" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Then apply the rest of the service manifests:

```bash
kubectl apply -f output/kubernetes/prod/
```

The Deployment's `envFrom` automatically picks up the secret values as
environment variables (`DB_PASSWORD`, `OAUTH_CLIENT_SECRET`) inside the
container.

## Cost

Each secret adds **$0.40/month** to the cost estimate:

```
cost = (replicas x $7.49)
     + ($12.25 if db_type != none)
     + ($11.52 if cache != none)
     + (num_secrets x $0.40)
```

The `--dry-run` output includes secrets in the per-service breakdown.

## Drift Detection

The drift detector tracks secrets-related structural changes:

| Change | Drift Type | Reason |
|--------|-----------|--------|
| Secrets added to a service | Forward | `Secrets Manager resources will be added` |
| Secrets removed from a service | Forward | `Secrets Manager resources will be removed` |

## Validation

The `--validate` flag checks:

- Secret names match `^[A-Z][A-Z0-9_]*$`
- No duplicate secret names within a service

```bash
$ infra-gen bad-manifest.yaml --validate
[ERROR] Service 'api': invalid secret name 'db_password' (must match ^[A-Z][A-Z0-9_]*$)

Validation FAILED with 1 error(s)
```

## Rotating Secrets

To rotate a secret after deployment:

```bash
# Update in AWS Secrets Manager
aws secretsmanager put-secret-value \
  --secret-id "auth-service/prod/DB_PASSWORD" \
  --secret-string "new-rotated-password" \
  --region us-east-1

# Update in Kubernetes
kubectl create secret generic auth-service-secrets \
  --namespace prod \
  --from-literal=DB_PASSWORD="new-rotated-password" \
  --from-literal=OAUTH_CLIENT_SECRET="existing-oauth-secret" \
  --dry-run=client -o yaml | kubectl apply -f -

# Restart pods to pick up the new values
kubectl rollout restart deployment/auth-service -n prod
```
