# Peer Relationships

A **peer relationship** occurs when exactly two services mutually depend on
each other: service A lists B in its `dependencies` **and** service B lists
A in its `dependencies`.

This is a common pattern in microservice architectures -- for example, an
`order-service` and `inventory-service` that need to communicate
bidirectionally.

## How Peers Differ from Cycles

| Scenario | Treatment |
|----------|-----------|
| A depends on B **and** B depends on A (2 services) | **Peer pair** -- valid, bidirectional rules generated |
| A -> B -> C -> A (3+ services) | **True cycle** -- error, blocks generation |

The key distinction: peer pairs are exactly two services.  Any closed loop
involving three or more services is a true circular dependency that cannot
be resolved and is flagged as an error.

## Detection

Peer pairs are detected by scanning all dependency edges for mutual
references:

```yaml
# This creates a peer pair:
- name: order-service
  dependencies: [inventory-service]

- name: inventory-service
  dependencies: [order-service]
```

The `--validate` and `--dry-run` flags both report detected peer pairs:

```
[INFO] Peer relationship detected: inventory-service <-> order-service (bidirectional rules will be generated)
```

## Generated Security Group Rules

For a peer pair (A, B), each service gets **both** ingress and egress rules
to the other:

### Service A's Security Group

```json
{
  "ingress": [
    {
      "description": "Peer ingress from B",
      "from_port": "<A's port>",
      "to_port": "<A's port>",
      "protocol": "tcp",
      "security_groups": ["<B's SG ID>"]
    }
  ],
  "egress": [
    {
      "description": "Peer egress to B",
      "from_port": "<B's port>",
      "to_port": "<B's port>",
      "protocol": "tcp",
      "security_groups": ["<B's SG ID>"]
    }
  ]
}
```

Service B gets the mirror-image rules.  This is different from normal
directional dependencies where only the dependency target gets an ingress
rule.

## Peer Group Label

Both services in a peer pair receive a `peer-group` tag/label with the
sorted, hyphenated names:

```
peer-group: inventory-service-order-service
```

This label appears on:

- Terraform security groups and all associated resources
- Kubernetes Deployments, Services, NetworkPolicies, and HPAs

The consistent label makes it easy to query for all resources belonging to
a peer group.

## Topological Sort

Peer edges are removed before topological sorting.  This means peer services
are treated as being at the **same level** in the dependency graph and may
appear in either order in the creation sequence.

## Kubernetes NetworkPolicy

Peer services can communicate with each other through the standard
dependency-based NetworkPolicy egress rules.  Since each service lists the
other as a dependency, both directions are permitted.

## Example Manifest

```yaml
services:
  - name: order-service
    port: 8082
    dependencies:
      - inventory-service      # mutual dependency -> peer
      - notification-service   # one-way dependency -> directional
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
      - order-service          # mutual dependency -> peer
    db_type: mysql
    cache: none
    exposure: internal
    env_overrides:
      dev:     { replicas: 1, cpu: "200m" }
      staging: { replicas: 2, cpu: "400m" }
      prod:    { replicas: 3, cpu: "600m" }
```
