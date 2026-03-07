# Validation

The `--validate` flag runs a comprehensive suite of checks against the
manifest.  Generation is blocked if any `ERROR`-level findings are present.

```bash
infra-gen services.yaml --validate
```

## Checks Performed

### 1. Self-References

A service must not list its own name in `dependencies`.

```
[ERROR] Service 'api' has a self-reference in dependencies
```

### 2. Missing Dependencies

Every name in `dependencies` must match the `name` of another service in
the manifest.

```
[ERROR] Service 'api' depends on unknown service 'nonexistent'
```

### 3. Required Environments

Each service must have `env_overrides` for all three environments: `dev`,
`staging`, and `prod`.

```
[ERROR] Service 'api' missing env_overrides for 'staging'
```

### 4. Replica Count

`replicas` must be greater than 0 in every environment.

```
[ERROR] Service 'api' env 'dev': replicas must be > 0, got 0
```

### 5. CPU Format

`cpu` must match the regex `^[0-9]+m$` (Kubernetes millicore notation).
Valid examples: `"100m"`, `"1000m"`.  Invalid: `"1.5cores"`, `"2"`.

```
[ERROR] Service 'api' env 'dev': cpu must match ^[0-9]+m$, got '1.5cores'
```

### 6. Replica Ordering

Replicas must satisfy: **prod >= staging >= dev**.  This ensures that
higher environments always have at least as much capacity as lower ones.

```
[ERROR] Service 'api': replica ordering violated - prod(1) >= staging(5) >= dev(3) required
```

### 7. Circular Dependencies (3+ services)

True cycles involving three or more services are errors.  The validator
finds **all** cycles in the graph, not just the first one.

```
[ERROR] True cycle detected (3+ services): a -> b -> c -> a
```

### 8. Peer Relationships (informational)

Two-service mutual dependencies are reported as `INFO` messages and do
**not** block generation.

```
[INFO] Peer relationship detected: order-service <-> inventory-service (bidirectional rules will be generated)
```

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Validation passed (no `ERROR`-level findings) |
| `1` | Validation failed (at least one `ERROR`) |

## Example: Valid Manifest

```bash
$ infra-gen sample.yaml --validate
[INFO] Peer relationship detected: inventory-service <-> order-service (bidirectional rules will be generated)

Validation PASSED
```

## Example: Invalid Manifest (cycle)

```bash
$ infra-gen sample_with_cycle.yaml --validate
[ERROR] True cycle detected (3+ services): cycle-a -> cycle-b -> cycle-c -> cycle-a
[INFO] Peer relationship detected: inventory-service <-> order-service (bidirectional rules will be generated)

Validation FAILED with 1 error(s)
```
