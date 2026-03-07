"""
CLI entry point for the infrastructure-as-code generator.

This module defines the ``infra-gen`` command-line interface using
:mod:`argparse`.  It ties together parsing, validation, generation, drift
detection, and cost estimation into a single user-facing tool.

Exit codes
----------
``0``
    Success.
``1``
    Validation failure, parse error, orphaned-resource warning, or cycle
    detection error.

Typical usage::

    # Validate a manifest
    infra-gen manifest.yaml --validate

    # Preview what would be generated (no files written)
    infra-gen manifest.yaml --dry-run

    # Generate Terraform + Kubernetes into ./output
    infra-gen manifest.yaml -o output

    # Detect drift between manifest and existing output
    infra-gen manifest.yaml --drift -o output
"""

from __future__ import annotations

import argparse
import sys
import textwrap

from .cost import estimate_costs, format_cost_report
from .drift import detect_drift, format_drift_report
from .graph import find_peer_pairs, topological_sort
from .kubernetes import generate_kubernetes
from .models import Manifest
from .parser import parse_manifest
from .state import detect_state_drift
from .terraform import ENVIRONMENTS, generate_terraform
from .validator import validate_manifest

_EPILOG = textwrap.dedent("""\
    examples:
      # Validate manifest (check deps, cycles, replica ordering, CPU format)
      infra-gen services.yaml --validate

      # Dry-run: show resource creation order and estimated AWS costs
      infra-gen services.yaml --dry-run

      # Generate Terraform + Kubernetes manifests into ./infra
      infra-gen services.yaml -o infra

      # Detect forward + reverse drift against previously generated files
      infra-gen services.yaml --drift -o infra

    manifest format:
      The YAML manifest must contain a top-level 'services' list.  Each
      service entry supports these fields:

        name              (required) Unique service identifier
        port              (required) Primary container port
        dependencies      List of service names this service depends on
        db_type           postgres | mysql | none  (default: none)
        cache             redis | memcached | none (default: none)
        exposure          internal | external      (default: internal)
        health_check_path HTTP path for readiness probes (optional)
        secrets           List of secret names (e.g. DB_PASSWORD, API_KEY).
                          Provisioned in AWS Secrets Manager (Terraform) and
                          mounted as Kubernetes Secrets. Names must match
                          ^[A-Z][A-Z0-9_]*$.
        env_overrides     Per-environment replica and CPU settings:
                            dev:     { replicas: 1, cpu: "250m" }
                            staging: { replicas: 2, cpu: "500m" }
                            prod:    { replicas: 4, cpu: "1000m" }

    validation rules (--validate):
      - All dependency names must reference existing services
      - No self-references in dependencies
      - cpu must match ^[0-9]+m$ (Kubernetes millicore format)
      - replicas must be > 0 in every environment
      - prod replicas >= staging replicas >= dev replicas
      - True cycles (3+ services) are errors
      - 2-service mutual dependencies are valid "peer relationships"
      - Secret names must match ^[A-Z][A-Z0-9_]*$ (no duplicates)

    security group rules (generated Terraform):
      - Directional: A depends on B -> A can reach B on B's port
      - External services get ALB ingress on 443 from 0.0.0.0/0
      - Internal services NEVER get 0.0.0.0/0 ingress
      - Database SGs only allow inbound from the owning service
      - Peer pairs get bidirectional ingress + egress rules

    cost estimation (--dry-run):
      t3.micro = $7.49/mo   db.t3.micro = $12.25/mo   cache.t3.micro = $11.52/mo
      secret = $0.40/mo (per secret)

    For the full documentation visit: https://infra-gen.readthedocs.io
""")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the appropriate handler.

    Args:
        argv: Explicit argument list for testing.  When *None*,
            ``sys.argv[1:]`` is used.

    Returns:
        Exit code: ``0`` on success, ``1`` on any error.
    """
    parser = argparse.ArgumentParser(
        prog="infra-gen",
        description=(
            "Generate production-ready, multi-environment Terraform modules "
            "and Kubernetes manifests from a declarative YAML service manifest."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "manifest",
        metavar="MANIFEST",
        help=(
            "Path to the YAML manifest file defining services, dependencies, "
            "databases, caches, and per-environment overrides."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="DIR",
        default="output",
        help=(
            "Root output directory for generated files.  Terraform modules "
            "are written to DIR/terraform/<env>/ and Kubernetes manifests to "
            "DIR/kubernetes/<env>/.  (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help=(
            "Validate the manifest and exit.  Checks dependency existence, "
            "self-references, CPU format, replica counts, replica ordering "
            "(prod >= staging >= dev), and circular dependencies.  Exit code "
            "0 = valid, 1 = errors found."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview what would be generated without writing any files.  "
            "Shows: peer relationships, topological resource creation order, "
            "and estimated monthly AWS costs per environment."
        ),
    )
    parser.add_argument(
        "--drift",
        action="store_true",
        help=(
            "Detect drift between the manifest and existing output files.  "
            "Reports forward drift (changes to apply) and reverse drift "
            "(orphaned resources no longer in the manifest).  Exit code "
            "1 if orphaned resources are found."
        ),
    )
    parser.add_argument(
        "--state",
        action="store_true",
        help=(
            "Use with --drift to compare against actual Terraform state "
            "instead of just generated files.  Reads local .tfstate files "
            "or fetches from S3 with --state-s3."
        ),
    )
    parser.add_argument(
        "--state-s3",
        action="store_true",
        help=(
            "Use with --state to fetch Terraform state from S3 backends "
            "instead of local .tfstate files.  Requires AWS CLI."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.3",
    )

    args = parser.parse_args(argv)

    # Validate flag combinations
    if args.state and not args.drift:
        parser.error("--state requires --drift")
    if args.state_s3 and not args.state:
        parser.error("--state-s3 requires --state")

    # Parse manifest
    try:
        manifest = parse_manifest(args.manifest)
    except (FileNotFoundError, PermissionError) as e:
        print(f"Error parsing manifest: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error parsing manifest: {e}", file=sys.stderr)
        return 1

    if not manifest.services:
        print("Error: No services defined in manifest", file=sys.stderr)
        return 1

    # --validate
    if args.validate:
        return _handle_validate(manifest)

    # --drift
    if args.drift:
        if args.state:
            return _handle_state_drift(manifest, args.output, args.state_s3)
        return _handle_drift(manifest, args.output)

    # --dry-run
    if args.dry_run:
        return _handle_dry_run(manifest)

    # Default: generate
    return _handle_generate(manifest, args.output)


def _handle_validate(manifest: Manifest) -> int:
    """Run all validation checks and print results.

    Returns:
        ``0`` if no errors, ``1`` if at least one error-severity finding.
    """
    errors = validate_manifest(manifest)
    has_errors = False

    for err in errors:
        print(err)
        if err.severity == "error":
            has_errors = True

    if has_errors:
        n_errors = sum(1 for e in errors if e.severity == "error")
        print(f"\nValidation FAILED with {n_errors} error(s)")
        return 1

    print("\nValidation PASSED")
    return 0


def _handle_drift(manifest: Manifest, output_dir: str) -> int:
    """Run bidirectional drift detection and print the report.

    Returns:
        ``0`` if no orphaned resources, ``1`` if reverse drift is detected.
    """
    report = detect_drift(manifest, output_dir)
    print(format_drift_report(report))

    if report["reverse"]:
        print(f"\nWARNING: {len(report['reverse'])} orphaned resource(s) detected!")
        return 1
    return 0


def _handle_state_drift(manifest: Manifest, output_dir: str, use_s3: bool) -> int:
    """Run state-aware drift detection against actual Terraform state.

    Returns:
        ``0`` if no drift, ``1`` if drift is detected.
    """
    has_drift = False
    for region in manifest.regions:
        for env in ENVIRONMENTS:
            print(f"=== State Drift: {env} ({region}) ===")
            report = detect_state_drift(output_dir, env, region, use_s3=use_s3)

            if report["missing_in_state"]:
                has_drift = True
                print("  Resources not yet applied:")
                for item in report["missing_in_state"]:
                    print(f"    [NEEDS APPLY] {item['address']}")

            if report["missing_in_manifest"]:
                has_drift = True
                print("  Orphaned resources in state:")
                for item in report["missing_in_manifest"]:
                    print(f"    [ORPHANED] {item['address']}")

            if not report["missing_in_state"] and not report["missing_in_manifest"]:
                print("  No drift detected.")
            print()

    return 1 if has_drift else 0


def _handle_dry_run(manifest: Manifest) -> int:
    """Validate, then show creation order and estimated costs without writing files.

    Returns:
        ``0`` on success, ``1`` if validation fails.
    """
    # Validate first
    errors = validate_manifest(manifest)
    real_errors = [e for e in errors if e.severity == "error"]
    if real_errors:
        print("Validation errors found:")
        for err in real_errors:
            print(f"  {err}")
        return 1

    # Show peer pairs
    peers = find_peer_pairs(manifest)
    if peers:
        print("=== Peer Relationships ===")
        for a, b in peers:
            print(f"  {a} <-> {b} (bidirectional rules)")
        print()

    # Show creation order
    order = topological_sort(manifest)
    print("=== Resource Creation Order ===")
    for i, name in enumerate(order, 1):
        svc = manifest.service_map()[name]
        extras = []
        if svc.has_db:
            extras.append(f"db:{svc.db_type}")
        if svc.has_cache:
            extras.append(f"cache:{svc.cache}")
        if svc.exposure == "external":
            extras.append("external")
        extra_str = f" [{', '.join(extras)}]" if extras else ""
        peer_marker = ""
        for a, b in peers:
            if name in (a, b):
                peer_marker = f" (peer: {a}<->{b})"
                break
        print(f"  {i}. {name}{extra_str}{peer_marker}")

    print()

    # Cost estimation
    costs = estimate_costs(manifest)
    print(format_cost_report(costs))

    return 0


def _handle_generate(manifest: Manifest, output_dir: str) -> int:
    """Validate, then generate all Terraform and Kubernetes output files.

    Returns:
        ``0`` on success, ``1`` if validation fails.
    """
    # Validate first
    errors = validate_manifest(manifest)
    real_errors = [e for e in errors if e.severity == "error"]
    if real_errors:
        print("Cannot generate: validation errors found:", file=sys.stderr)
        for err in real_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    # Print info messages
    for err in errors:
        if err.severity == "info":
            print(err)

    # Generate
    tf_files = generate_terraform(manifest, output_dir)
    k8s_files = generate_kubernetes(manifest, output_dir)

    print(f"\nGenerated {len(tf_files)} Terraform files")
    print(f"Generated {len(k8s_files)} Kubernetes manifests")
    print(f"Output directory: {output_dir}/")

    return 0


if __name__ == "__main__":
    sys.exit(main())
