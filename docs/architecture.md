# Architecture

This page describes the internal architecture of infra-gen, the data flow
through its modules, and the design decisions behind its key features.

## Module Dependency Graph

```
cli.py
  |
  +-- parser.py -------> models.py
  |
  +-- validator.py ----> graph.py ------> models.py
  |
  +-- terraform.py ----> graph.py
  |                      models.py
  |
  +-- kubernetes.py ---> graph.py
  |                      models.py
  |
  +-- drift.py --------> models.py
  |
  +-- cost.py ---------> models.py
```

All modules depend on `models.py` for the shared `Service`, `EnvOverride`,
and `Manifest` dataclasses.  The `graph.py` module provides algorithms used
by validation, Terraform generation, and Kubernetes generation.

## Data Flow

```
YAML File
    |
    v
parse_manifest()  -->  Manifest (list of Service objects)
    |
    |--- validate_manifest()  -->  list of ValidationError
    |
    |--- find_peer_pairs()    -->  list of (name_a, name_b)
    |--- find_all_cycles()    -->  list of [name, name, ...]
    |--- topological_sort()   -->  list of names
    |
    |--- generate_terraform() -->  .tf.json files on disk
    |--- generate_kubernetes() -> .yaml files on disk
    |
    |--- detect_drift()       -->  drift report dict
    |--- estimate_costs()     -->  cost breakdown dict
```

## Design Decisions

### JSON Terraform (`.tf.json`) over HCL

Terraform natively supports JSON syntax.  Generating JSON is simpler and
more reliable than generating HCL -- no template engine is needed, and
Python's `json.dumps` produces valid output deterministically.

### Multi-Document YAML for Kubernetes

Each service's Kubernetes file contains four resources (Deployment, Service,
NetworkPolicy, HPA) -- or five if the service declares secrets (plus a
Secret resource).  This keeps related resources together and allows
`kubectl apply -f <file>` to create everything in one command.

### Peer Detection Before Cycle Detection

Peer pairs (2-service mutual dependencies) are identified first and their
edges are removed from the graph before cycle detection runs.  This ensures
that valid bidirectional relationships are never reported as errors.

### Johnson-Style DFS for Cycle Detection

The cycle finder uses a DFS from every node, recording paths of length >= 3
that return to the start.  Lexicographic ordering prevents duplicate cycles.
This finds **all** elementary circuits, not just the first.

### Kahn's Algorithm for Topological Sort

Kahn's algorithm (BFS-based) naturally handles the case where peer edges
have been removed -- peer services end up at the same topological level
because they have no remaining edges between them.

### Deterministic Output

All outputs are sorted (resource keys, file names, service lists) to ensure
that re-running the generator on the same manifest produces byte-identical
output.  The only non-deterministic element is the `last-generated`
timestamp.

### Validation Before Generation

The `generate_terraform()` and `generate_kubernetes()` functions assume
valid input.  The CLI always validates first and aborts on errors, keeping
the generators simple.

## Security Model

### Security Group Directionality

The core security invariant: **A depends on B means A can reach B, but B
cannot initiate connections to A** (unless B also depends on A, making them
peers).

This is implemented by giving B an ingress rule from A's security group,
and giving A an egress rule to B.

### Internal Service Protection

Internal services **never** receive `0.0.0.0/0` ingress, even if external
services depend on them.  The ALB ingress rule on port 443 is only added to
services with `exposure: external`.

### Database and Cache Isolation

Database and cache security groups use a separate security group that only
allows inbound from the owning service's security group.  No other service
can reach the database or cache directly.

### Secrets Isolation

Each service's secrets are stored in per-environment, per-service paths in
AWS Secrets Manager (e.g. `auth-service/prod/DB_PASSWORD`).  An IAM policy
scoped to exactly that service's secret ARNs is generated -- no service can
read another service's secrets.  On the Kubernetes side, each service gets
its own namespaced `Secret` resource with `envFrom` injection, ensuring
secrets are only mounted into the correct pods.

## Testing Strategy

The test suite (118 tests) covers:

- **Parser**: field extraction, defaults, type conversion
- **Graph**: peer detection, cycle finding (single/multiple/mixed), topological sort
- **Validator**: every error condition, peer-info-not-error
- **Terraform**: file count, backend isolation, SG directionality, ALB ingress,
  internal protection, DB isolation, tags, peer rules, peer-group tag
- **Kubernetes**: file count, anti-affinity, topology spread, probe types,
  probe timing differences, HPA metrics, NetworkPolicy rules
- **Drift**: forward-all-new, reverse-orphan, no-drift-after-generate, forward-db-change
- **Cost**: basic, db+cache, replica scaling
- **Secrets**: model, parser, validator, Terraform (SM + IAM), Kubernetes
  (Secret + envFrom), cost, drift, CLI integration
- **CLI**: validate/dry-run/generate/drift end-to-end

All tests use in-memory manifests or temporary directories and have no
external dependencies.
