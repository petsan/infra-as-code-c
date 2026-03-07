# Quick Start

This guide walks you through generating your first set of infrastructure
files from the included sample manifest.

## 1. Validate the Manifest

Start by checking the manifest for errors:

```bash
infra-gen sample.yaml --validate
```

Expected output:

```
[INFO] Peer relationship detected: inventory-service <-> order-service (bidirectional rules will be generated)

Validation PASSED
```

The `[INFO]` line confirms that the two-service mutual dependency between
`order-service` and `inventory-service` is a valid peer relationship, not a
cycle error.

## 2. Preview with Dry-Run

See what would be generated and get a cost estimate:

```bash
infra-gen sample.yaml --dry-run
```

Expected output:

```
=== Peer Relationships ===
  inventory-service <-> order-service (bidirectional rules)

=== Resource Creation Order ===
  1. auth-service [db:postgres, cache:redis]
  2. inventory-service [db:mysql] (peer: inventory-service<->order-service)
  3. notification-service
  4. order-service [db:postgres, cache:memcached] (peer: inventory-service<->order-service)
  5. product-catalog [db:postgres, cache:redis, external]
  6. api-gateway [cache:redis, external]

=== Estimated Monthly AWS Costs ===
  ...
  GRAND TOTAL                      $  554.88/mo
```

The creation order reflects the topological sort -- dependencies appear
before the services that depend on them.

## 3. Generate Infrastructure Files

```bash
infra-gen sample.yaml -o output
```

This creates:

```
output/
  terraform/
    dev/
      backend.tf.json
      provider.tf.json
      api-gateway.tf.json
      auth-service.tf.json
      ...
    staging/
      ...
    prod/
      ...
  kubernetes/
    dev/
      api-gateway.yaml
      auth-service.yaml
      ...
    staging/
      ...
    prod/
      ...
```

## 4. Inspect the Output

### Terraform

```bash
# Check the prod backend configuration
cat output/terraform/prod/backend.tf.json | python3 -m json.tool
```

### Kubernetes

```bash
# View the prod api-gateway deployment, service, network policy, and HPA
cat output/kubernetes/prod/api-gateway.yaml
```

## 5. Detect Drift

After making changes to the manifest, detect what would change:

```bash
infra-gen sample.yaml --drift -o output
```

If you remove a service from the manifest, the drift detector will flag its
existing files as orphaned resources.

## 6. Apply the Generated Files

### Terraform

```bash
cd output/terraform/dev
terraform init
terraform plan
terraform apply
```

### Kubernetes

```bash
kubectl apply -f output/kubernetes/dev/
```

## 7. Test Cycle Detection

Try validating the sample that contains a deliberate 3-service cycle:

```bash
infra-gen sample_with_cycle.yaml --validate
```

Expected output:

```
[ERROR] True cycle detected (3+ services): cycle-a -> cycle-b -> cycle-c -> cycle-a
[INFO] Peer relationship detected: inventory-service <-> order-service ...

Validation FAILED with 1 error(s)
```

The peer pair passes, but the three-service cycle is flagged as an error.

## Next Steps

- [Manifest Format](../guide/manifest.md) -- full field reference
- [Terraform Output](../guide/terraform.md) -- what gets generated
- [Kubernetes Output](../guide/kubernetes.md) -- deployment details
- [Secrets Vault](../guide/secrets.md) -- managing secrets with AWS Secrets Manager
- [Peer Relationships](../guide/peers.md) -- how mutual dependencies work
