# CLI Reference

## Synopsis

```
infra-gen [-h] [-o DIR] [--validate] [--dry-run] [--drift] [--version] MANIFEST
```

## Arguments

### `MANIFEST` (positional, required)

Path to the YAML manifest file defining services, dependencies, databases,
caches, and per-environment resource overrides.

## Options

### `-o DIR`, `--output DIR`

Root output directory for generated files.

- Terraform modules are written to `DIR/terraform/<env>/`
- Kubernetes manifests are written to `DIR/kubernetes/<env>/`
- Default: `output`

### `--validate`

Validate the manifest and exit without generating any files.

**Checks performed:**

1. All dependency names reference existing services
2. No self-references in dependencies
3. `cpu` matches regex `^[0-9]+m$`
4. `replicas` > 0 in every environment
5. `prod` replicas >= `staging` replicas >= `dev` replicas
6. Secret names match `^[A-Z][A-Z0-9_]*$` (no duplicates)
7. No circular dependencies (3+ services)

**Exit codes:** `0` = valid, `1` = errors found.

Two-service mutual dependencies (peer relationships) are reported as `[INFO]`
and do not cause failure.

### `--dry-run`

Preview what would be generated without writing any files.

**Displays:**

1. Detected peer relationships
2. Topological resource creation order (dependencies first)
3. Estimated monthly AWS costs per environment

Validation is run first; if it fails, errors are printed and the command
exits with code `1`.

### `--drift`

Detect drift between the manifest and existing output files.

**Reports:**

- **Forward drift**: resources that would be created or updated
- **Reverse drift**: orphaned files from services no longer in the manifest

**Exit codes:** `0` = no orphaned resources, `1` = orphans detected.

### `--version`

Print the version string and exit.

### `-h`, `--help`

Show the help message (including examples and manifest format reference)
and exit.

## Default Behavior (no flags)

When no flags are given, infra-gen validates the manifest and then generates
all Terraform and Kubernetes files into the output directory.

```bash
infra-gen services.yaml -o output
```

## Exit Codes

| Code | Condition |
|------|-----------|
| `0` | Success |
| `1` | Parse error, validation failure, cycle detected, or orphaned resources |

## Examples

```bash
# Validate a manifest
infra-gen services.yaml --validate

# Preview generation + cost estimate
infra-gen services.yaml --dry-run

# Generate into a custom directory
infra-gen services.yaml -o infra

# Check for drift after editing the manifest
infra-gen services.yaml --drift -o infra

# Show version
infra-gen --version
```

## Man Page

A Unix man page is included at `man/infra-gen.1`.  Read it with:

```bash
man ./man/infra-gen.1
```

Or install it system-wide:

```bash
sudo cp man/infra-gen.1 /usr/local/share/man/man1/
sudo mandb
man infra-gen
```
