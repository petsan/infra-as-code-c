"""
Dependency-graph analysis: peer detection, cycle finding, and topological sort.

Key concepts
------------
**Peer pair**
    Exactly two services *A* and *B* where *A* depends on *B* **and** *B*
    depends on *A*.  Peer pairs are a valid design pattern (e.g.
    ``order-service`` <-> ``inventory-service``) and are **not** treated as
    cycles.  They receive bidirectional security-group rules and a shared
    ``peer-group`` label.

**True cycle**
    A strongly-connected component of **3 or more** services.  True cycles
    represent unresolvable dependency loops and are reported as validation
    errors.

Algorithms
----------
* :func:`find_peer_pairs` -- O(V + E) scan for mutual 2-node edges.
* :func:`find_all_cycles` -- DFS-based enumeration of **all** elementary
  circuits in the graph (peer edges excluded), inspired by Johnson's
  algorithm.
* :func:`topological_sort` -- Kahn's algorithm with peer edges removed so
  that peer services land at the same topological level.
"""

from __future__ import annotations

from collections import defaultdict

from .models import Manifest


def find_peer_pairs(manifest: Manifest) -> list[tuple[str, str]]:
    """Identify all two-service mutual dependencies (peer relationships).

    A peer pair ``(A, B)`` exists when service *A* lists *B* as a dependency
    **and** service *B* lists *A* as a dependency.  The returned tuples are
    sorted lexicographically (``A < B``) and deduplicated.

    Args:
        manifest: The parsed service manifest.

    Returns:
        Sorted list of ``(name_a, name_b)`` tuples where ``name_a < name_b``.
    """
    svc_map = manifest.service_map()
    peers: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for svc in manifest.services:
        for dep_name in svc.dependencies:
            if dep_name in svc_map:
                dep_svc = svc_map[dep_name]
                if svc.name in dep_svc.dependencies:
                    a, b = sorted([svc.name, dep_name])
                    pair = (a, b)
                    if pair not in seen:
                        seen.add(pair)
                        peers.append(pair)

    return peers


def find_all_cycles(manifest: Manifest) -> list[list[str]]:
    """Find **all** true cycles (3+ services) in the dependency graph.

    Peer-pair edges are excluded before the search begins, so two-service
    mutual dependencies are never reported.  The algorithm performs a DFS
    from every node, recording elementary circuits where the path length
    is >= 3 and the DFS returns to the start node.  Duplicate cycles are
    avoided by only following neighbours whose name is lexicographically
    >= the start node.

    Args:
        manifest: The parsed service manifest.

    Returns:
        List of cycles, where each cycle is an ordered list of service names
        forming a closed loop (the implicit closing edge back to ``cycle[0]``
        is not repeated in the list).
    """
    peer_pairs = find_peer_pairs(manifest)
    peer_set: set[tuple[str, str]] = set()
    for a, b in peer_pairs:
        peer_set.add((a, b))
        peer_set.add((b, a))

    svc_map = manifest.service_map()

    # Build adjacency list, excluding peer edges
    adj: dict[str, list[str]] = defaultdict(list)
    for svc in manifest.services:
        for dep in svc.dependencies:
            if dep in svc_map and (svc.name, dep) not in peer_set:
                adj[svc.name].append(dep)

    nodes = sorted(svc_map.keys())
    cycles: list[list[str]] = []
    max_depth = min(len(nodes), 100)

    for start in nodes:
        # Iterative DFS using an explicit stack.
        # Each stack frame: (current_node, path, visited, neighbor_index)
        stack: list[tuple[str, list[str], set[str], int]] = [(start, [start], {start}, 0)]
        while stack:
            current, path, visited, ni = stack[-1]
            neighbors = adj.get(current, [])
            if ni >= len(neighbors):
                stack.pop()
                continue
            # Advance the neighbor index for the current frame
            stack[-1] = (current, path, visited, ni + 1)
            neighbor = neighbors[ni]

            if neighbor == start and len(path) >= 3:
                cycles.append(list(path))
            elif neighbor not in visited and neighbor >= start and len(path) < max_depth:
                new_visited = visited | {neighbor}
                new_path = [*path, neighbor]
                stack.append((neighbor, new_path, new_visited, 0))

    return cycles


def topological_sort(manifest: Manifest) -> list[str]:
    """Return services in dependency order using Kahn's algorithm.

    Peer-pair edges are removed before sorting so that mutually-dependent
    peer services are treated as being at the same topological level.  The
    result is a deterministic, alphabetically-stable ordering where every
    service appears **after** all of its non-peer dependencies.

    Args:
        manifest: The parsed service manifest.

    Returns:
        List of service names in dependency-first order.

    Note:
        If the graph (after peer-edge removal) still contains a cycle, the
        returned list will be shorter than the total number of services.
        Callers should compare lengths to detect this condition.
    """
    peer_pairs = find_peer_pairs(manifest)
    peer_set: set[tuple[str, str]] = set()
    for a, b in peer_pairs:
        peer_set.add((a, b))
        peer_set.add((b, a))

    svc_map = manifest.service_map()

    # Build adjacency and in-degree, excluding peer edges
    in_degree: dict[str, int] = {name: 0 for name in svc_map}
    adj: dict[str, list[str]] = defaultdict(list)

    for svc in manifest.services:
        for dep in svc.dependencies:
            if dep in svc_map and (svc.name, dep) not in peer_set:
                adj[dep].append(svc.name)
                in_degree[svc.name] += 1

    # Kahn's algorithm
    queue = sorted([n for n, d in in_degree.items() if d == 0])
    result: list[str] = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        for neighbor in sorted(adj.get(node, [])):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
        queue.sort()

    return result
