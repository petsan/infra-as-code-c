# Drift Detection

The `--drift` flag compares the current manifest against previously
generated output files and reports two categories of drift.

```bash
infra-gen services.yaml --drift -o output
```

## Forward Drift

Forward drift identifies resources that **would change** if the generator
is re-run.  This includes:

- **New services** -- a service exists in the manifest but its output files
  do not yet exist.
- **Structural changes** -- a service's `db_type` or `cache` has changed
  (e.g. a database was added or removed).

```
=== FORWARD DRIFT (changes to apply) ===
  [CREATE] terraform/dev/new-service: New service, Terraform file will be created
  [CREATE] kubernetes/dev/new-service: New service, Kubernetes manifest will be created
  [UPDATE] terraform/prod/auth-service: Database resources will be added
```

## Reverse Drift (Orphaned Resources)

Reverse drift identifies files that exist in the output directory but
correspond to services that are **no longer in the manifest**.  These are
orphaned resources that would be left dangling.

```
=== REVERSE DRIFT (orphaned resources) ===
  [ORPHAN] terraform/dev/removed-service: Service no longer in manifest
           File: output/terraform/dev/removed-service.tf.json
  [ORPHAN] kubernetes/dev/removed-service: Service no longer in manifest
           File: output/kubernetes/dev/removed-service.yaml
```

!!! warning
    Orphaned resources are a strong signal that cleanup is needed.  The
    orphaned Terraform files may still be managing live AWS resources.
    Remove them manually or run `terraform destroy` before deleting the
    files.

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | No orphaned resources (forward drift may still exist) |
| `1` | Orphaned resources detected |

## Workflow

A typical drift-check workflow:

```bash
# 1. Generate initial infrastructure
infra-gen services.yaml -o output

# 2. Edit the manifest (add/remove services)
vim services.yaml

# 3. Check what would change
infra-gen services.yaml --drift -o output

# 4. If satisfied, regenerate
infra-gen services.yaml -o output

# 5. Clean up any orphaned files manually
rm output/terraform/*/removed-service.tf.json
rm output/kubernetes/*/removed-service.yaml
```

## What Is Compared

| Output type | Forward detection | Reverse detection |
|-------------|-------------------|-------------------|
| Terraform | Missing `.tf.json` files; structural changes (db/cache added or removed) | `.tf.json` files whose service name is not in the manifest |
| Kubernetes | Missing `.yaml` files | `.yaml` files whose service name is not in the manifest |

Backend and provider files (`backend.tf.json`, `provider.tf.json`) are
excluded from drift detection since they are environment-level, not
service-level.
