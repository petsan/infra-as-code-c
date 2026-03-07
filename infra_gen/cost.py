"""
AWS monthly cost estimator.

Provides a simple, deterministic cost estimate for the ``--dry-run`` CLI
flag.  Costs are calculated from fixed per-instance-type monthly prices
(on-demand, ``us-east-1``):

=================  ===========
Instance type      Monthly cost
=================  ===========
``t3.micro``       $7.49
``db.t3.micro``    $12.25
``cache.t3.micro`` $11.52
=================  ===========

The estimator assumes:

* One ``t3.micro`` compute instance **per replica** (derived from
  ``env_overrides``).
* One ``db.t3.micro`` RDS instance per service that has ``db_type != "none"``.
* One ``cache.t3.micro`` ElastiCache node per service that has
  ``cache != "none"``.

These prices are illustrative for planning purposes and do not account for
data transfer, storage IOPS, reserved-instance discounts, or Savings Plans.
"""

from __future__ import annotations

from .models import Manifest

COSTS: dict[str, float] = {
    "t3.micro": 7.49,
    "db.t3.micro": 12.25,
    "cache.t3.micro": 11.52,
    "secret": 0.40,
}
"""Monthly on-demand prices (USD) for each instance type."""

ENVIRONMENTS = ["dev", "staging", "prod"]
"""Target deployment environments."""


def estimate_costs(manifest: Manifest) -> dict[str, dict[str, float]]:
    """Estimate monthly AWS costs per environment.

    Args:
        manifest: The parsed service manifest.

    Returns:
        A nested dictionary keyed by environment name, where each value is a
        dict mapping service names to their estimated monthly cost, plus a
        ``"total"`` key for the environment subtotal.  Example::

            {
                "dev":     {"api": 7.49, "auth": 31.26, "total": 38.75},
                "staging": { ... },
                "prod":    { ... },
            }
    """
    result: dict[str, dict[str, float]] = {}

    for env in ENVIRONMENTS:
        env_costs: dict[str, float] = {}

        for svc in manifest.services:
            cost = 0.0
            ov = svc.env_overrides.get(env)
            replicas = ov.replicas if ov else 1

            # Compute instances (t3.micro per replica)
            cost += replicas * COSTS["t3.micro"]

            # Database
            if svc.has_db:
                cost += COSTS["db.t3.micro"]

            # Cache
            if svc.has_cache:
                cost += COSTS["cache.t3.micro"]

            # Secrets Manager (per secret)
            if svc.has_secrets:
                cost += len(svc.secrets) * COSTS["secret"]

            env_costs[svc.name] = round(cost, 2)

        env_costs["total"] = round(sum(env_costs.values()), 2)
        result[env] = env_costs

    return result


def format_cost_report(costs: dict[str, dict[str, float]]) -> str:
    """Format cost estimates into a human-readable table.

    Args:
        costs: The dict returned by :func:`estimate_costs`.

    Returns:
        A multi-line string suitable for printing to the terminal, showing
        per-service costs within each environment plus subtotals and a grand
        total.
    """
    lines = ["=== Estimated Monthly AWS Costs ===", ""]

    grand_total = 0.0
    for env in ENVIRONMENTS:
        env_costs = costs[env]
        total = env_costs["total"]
        grand_total += total
        lines.append(f"  {env.upper()}:")

        for name, cost in sorted(env_costs.items()):
            if name == "total":
                continue
            lines.append(f"    {name:30s} ${cost:>8.2f}/mo")

        lines.append(f"    {'':30s} {'--------':>10s}")
        lines.append(f"    {'Subtotal':30s} ${total:>8.2f}/mo")
        lines.append("")

    lines.append(f"  {'GRAND TOTAL':32s} ${grand_total:>8.2f}/mo")
    return "\n".join(lines)
