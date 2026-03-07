"""Comprehensive tests for infra-gen."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from infra_gen.cli import main
from infra_gen.cost import estimate_costs, format_cost_report
from infra_gen.drift import detect_drift, format_drift_report
from infra_gen.graph import find_all_cycles, find_peer_pairs, topological_sort
from infra_gen.kubernetes import generate_kubernetes
from infra_gen.models import EnvOverride, Manifest, Service
from infra_gen.parser import parse_manifest
from infra_gen.terraform import generate_terraform
from infra_gen.validator import ValidationError, validate_manifest

# ---- Fixtures ----


def _svc(
    name,
    deps=None,
    db="none",
    cache="none",
    exposure="internal",
    health=None,
    dev_r=1,
    stg_r=2,
    prod_r=3,
):
    return Service(
        name=name,
        port=8080,
        dependencies=deps or [],
        db_type=db,
        cache=cache,
        exposure=exposure,
        health_check_path=health,
        env_overrides={
            "dev": EnvOverride(replicas=dev_r, cpu="250m"),
            "staging": EnvOverride(replicas=stg_r, cpu="500m"),
            "prod": EnvOverride(replicas=prod_r, cpu="750m"),
        },
    )


@pytest.fixture
def simple_manifest():
    return Manifest(
        services=[
            _svc("api", deps=["auth"], exposure="external", health="/healthz"),
            _svc("auth", db="postgres", cache="redis", health="/health"),
            _svc("worker"),
        ]
    )


@pytest.fixture
def peer_manifest():
    """Manifest with a valid peer relationship."""
    return Manifest(
        services=[
            _svc("order", deps=["inventory"]),
            _svc("inventory", deps=["order"]),
            _svc("notifier"),
        ]
    )


@pytest.fixture
def cycle_manifest():
    """Manifest with a true 3-node cycle."""
    return Manifest(
        services=[
            _svc("a", deps=["b"]),
            _svc("b", deps=["c"]),
            _svc("c", deps=["a"]),
        ]
    )


@pytest.fixture
def mixed_manifest():
    """Manifest with both a peer pair AND a true cycle."""
    return Manifest(
        services=[
            _svc("order", deps=["inventory"]),
            _svc("inventory", deps=["order"]),
            _svc("a", deps=["b"]),
            _svc("b", deps=["c"]),
            _svc("c", deps=["a"]),
        ]
    )


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield d


# ---- Parser tests ----


class TestParser:
    def test_parse_sample(self):
        manifest = parse_manifest("sample.yaml")
        assert len(manifest.services) == 6
        names = {s.name for s in manifest.services}
        assert "api-gateway" in names
        assert "auth-service" in names

    def test_parse_env_overrides(self):
        manifest = parse_manifest("sample.yaml")
        api = manifest.service_map()["api-gateway"]
        assert api.env_overrides["prod"].replicas == 4
        assert api.env_overrides["dev"].cpu == "250m"

    def test_parse_exposure(self):
        manifest = parse_manifest("sample.yaml")
        svc_map = manifest.service_map()
        assert svc_map["api-gateway"].exposure == "external"
        assert svc_map["auth-service"].exposure == "internal"


# ---- Graph tests ----


class TestGraph:
    def test_peer_detection(self, peer_manifest):
        peers = find_peer_pairs(peer_manifest)
        assert len(peers) == 1
        assert peers[0] == ("inventory", "order")

    def test_no_peers_in_simple(self, simple_manifest):
        peers = find_peer_pairs(simple_manifest)
        assert len(peers) == 0

    def test_cycle_detection_3_nodes(self, cycle_manifest):
        cycles = find_all_cycles(cycle_manifest)
        assert len(cycles) >= 1
        # The cycle should contain a, b, c
        cycle_sets = [set(c) for c in cycles]
        assert any({"a", "b", "c"} == cs for cs in cycle_sets)

    def test_peer_not_detected_as_cycle(self, peer_manifest):
        cycles = find_all_cycles(peer_manifest)
        assert len(cycles) == 0

    def test_mixed_peer_and_cycle(self, mixed_manifest):
        peers = find_peer_pairs(mixed_manifest)
        assert len(peers) == 1

        cycles = find_all_cycles(mixed_manifest)
        assert len(cycles) >= 1
        cycle_sets = [set(c) for c in cycles]
        assert any({"a", "b", "c"} == cs for cs in cycle_sets)
        # Peer pair should NOT appear as a cycle
        assert not any(cs == {"inventory", "order"} for cs in cycle_sets)

    def test_topological_sort_simple(self, simple_manifest):
        order = topological_sort(simple_manifest)
        assert len(order) == 3
        # auth and worker have no deps, api depends on auth
        assert order.index("auth") < order.index("api")

    def test_topological_sort_peer(self, peer_manifest):
        order = topological_sort(peer_manifest)
        assert len(order) == 3
        # All three should be present
        assert set(order) == {"order", "inventory", "notifier"}

    def test_find_all_cycles_finds_multiple(self):
        """Two independent 3-cycles."""
        manifest = Manifest(
            services=[
                _svc("a", deps=["b"]),
                _svc("b", deps=["c"]),
                _svc("c", deps=["a"]),
                _svc("x", deps=["y"]),
                _svc("y", deps=["z"]),
                _svc("z", deps=["x"]),
            ]
        )
        cycles = find_all_cycles(manifest)
        cycle_sets = [set(c) for c in cycles]
        assert any({"a", "b", "c"} == cs for cs in cycle_sets)
        assert any({"x", "y", "z"} == cs for cs in cycle_sets)


# ---- Validator tests ----


class TestValidator:
    def test_valid_manifest(self, simple_manifest):
        errors = validate_manifest(simple_manifest)
        real_errors = [e for e in errors if e.severity == "error"]
        assert len(real_errors) == 0

    def test_self_reference(self):
        manifest = Manifest(services=[_svc("a", deps=["a"])])
        errors = validate_manifest(manifest)
        real_errors = [e for e in errors if e.severity == "error"]
        assert any("self-reference" in e.message for e in real_errors)

    def test_missing_dependency(self):
        manifest = Manifest(services=[_svc("a", deps=["nonexistent"])])
        errors = validate_manifest(manifest)
        real_errors = [e for e in errors if e.severity == "error"]
        assert any("unknown service" in e.message for e in real_errors)

    def test_invalid_replicas(self):
        svc = _svc("a")
        svc.env_overrides["dev"] = EnvOverride(replicas=0, cpu="100m")
        manifest = Manifest(services=[svc])
        errors = validate_manifest(manifest)
        real_errors = [e for e in errors if e.severity == "error"]
        assert any("replicas must be > 0" in e.message for e in real_errors)

    def test_invalid_cpu(self):
        svc = _svc("a")
        svc.env_overrides["dev"] = EnvOverride(replicas=1, cpu="1.5cores")
        manifest = Manifest(services=[svc])
        errors = validate_manifest(manifest)
        real_errors = [e for e in errors if e.severity == "error"]
        assert any("cpu must match" in e.message for e in real_errors)

    def test_replica_ordering_violation(self):
        svc = _svc("a", dev_r=5, stg_r=2, prod_r=1)
        manifest = Manifest(services=[svc])
        errors = validate_manifest(manifest)
        real_errors = [e for e in errors if e.severity == "error"]
        assert any("replica ordering violated" in e.message for e in real_errors)

    def test_cycle_error(self, cycle_manifest):
        errors = validate_manifest(cycle_manifest)
        real_errors = [e for e in errors if e.severity == "error"]
        assert any("True cycle detected" in e.message for e in real_errors)

    def test_peer_info_not_error(self, peer_manifest):
        errors = validate_manifest(peer_manifest)
        real_errors = [e for e in errors if e.severity == "error"]
        info = [e for e in errors if e.severity == "info"]
        assert len(real_errors) == 0
        assert any("Peer relationship" in e.message for e in info)


# ---- Terraform tests ----


class TestTerraform:
    def test_generates_files(self, simple_manifest, tmpdir):
        files = generate_terraform(simple_manifest, tmpdir)
        assert len(files) > 0
        # 3 envs * (2 infra files + 3 service files) = 15
        assert len(files) == 15

    def test_backend_per_env(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        for env in ["dev", "staging", "prod"]:
            backend = json.loads((Path(tmpdir) / "terraform" / env / "backend.tf.json").read_text())
            s3 = backend["terraform"]["backend"]["s3"]
            assert s3["bucket"] == f"terraform-state-{env}"
            assert s3["dynamodb_table"] == f"terraform-locks-{env}"

    def test_external_gets_alb_ingress(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        sg = api_tf["resource"]["aws_security_group_api"]
        has_443 = any(
            r.get("from_port") == 443 and "0.0.0.0/0" in r.get("cidr_blocks", [])
            for r in sg["ingress"]
        )
        assert has_443

    def test_internal_no_public_ingress(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        sg = auth_tf["resource"]["aws_security_group_auth"]
        has_public = any("0.0.0.0/0" in r.get("cidr_blocks", []) for r in sg["ingress"])
        assert not has_public

    def test_directional_sg_rules(self, simple_manifest, tmpdir):
        """A depends on B means A can reach B only (B gets ingress from A)."""
        generate_terraform(simple_manifest, tmpdir)
        # api depends on auth, so auth gets ingress from api
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        sg = auth_tf["resource"]["aws_security_group_auth"]
        has_ingress_from_api = any(
            "api" in str(r.get("security_groups", [])) for r in sg["ingress"]
        )
        assert has_ingress_from_api

    def test_db_sg_only_from_service(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        db_sg = auth_tf["resource"]["aws_security_group_auth_db"]
        # DB SG should only have ingress from the auth service SG
        assert len(db_sg["ingress"]) == 1
        assert "auth" in str(db_sg["ingress"][0]["security_groups"])

    def test_tags_present(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        sg = api_tf["resource"]["aws_security_group_api"]
        tags = sg["tags"]
        assert tags["environment"] == "prod"
        assert tags["service-name"] == "api"
        assert "cost-center" in tags
        assert "dependency-hash" in tags
        assert "last-generated" in tags

    def test_peer_bidirectional_rules(self, peer_manifest, tmpdir):
        generate_terraform(peer_manifest, tmpdir)
        order_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "order.tf.json").read_text())
        inv_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "inventory.tf.json").read_text())

        order_sg = order_tf["resource"]["aws_security_group_order"]
        inv_sg = inv_tf["resource"]["aws_security_group_inventory"]

        # Order should have peer ingress from inventory
        assert any("inventory" in str(r) for r in order_sg["ingress"])
        # Inventory should have peer ingress from order
        assert any("order" in str(r) for r in inv_sg["ingress"])

        # Both should have peer egress to each other
        assert any("inventory" in str(r) for r in order_sg["egress"])
        assert any("order" in str(r) for r in inv_sg["egress"])

    def test_peer_group_tag(self, peer_manifest, tmpdir):
        generate_terraform(peer_manifest, tmpdir)
        order_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "order.tf.json").read_text())
        sg = order_tf["resource"]["aws_security_group_order"]
        assert sg["tags"]["peer-group"] == "inventory-order"


# ---- Kubernetes tests ----


class TestKubernetes:
    def test_generates_files(self, simple_manifest, tmpdir):
        files = generate_kubernetes(simple_manifest, tmpdir)
        # 3 envs * 3 services = 9
        assert len(files) == 9

    def test_anti_affinity_present(self, simple_manifest, tmpdir):
        generate_kubernetes(simple_manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "api.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        deployment = docs[0]
        spec = deployment["spec"]["template"]["spec"]
        assert "podAntiAffinity" in spec["affinity"]

    def test_topology_spread(self, simple_manifest, tmpdir):
        generate_kubernetes(simple_manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "api.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        deployment = docs[0]
        spec = deployment["spec"]["template"]["spec"]
        constraints = spec["topologySpreadConstraints"]
        assert len(constraints) == 1
        assert constraints[0]["maxSkew"] == 2
        assert constraints[0]["topologyKey"] == "topology.kubernetes.io/zone"

    def test_readiness_http_when_health_path(self, simple_manifest, tmpdir):
        generate_kubernetes(simple_manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "api.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        container = docs[0]["spec"]["template"]["spec"]["containers"][0]
        assert "httpGet" in container["readinessProbe"]
        assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"

    def test_readiness_tcp_fallback(self, simple_manifest, tmpdir):
        generate_kubernetes(simple_manifest, tmpdir)
        # worker has no health_check_path
        path = Path(tmpdir) / "kubernetes" / "prod" / "worker.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        container = docs[0]["spec"]["template"]["spec"]["containers"][0]
        assert "tcpSocket" in container["readinessProbe"]

    def test_liveness_always_tcp(self, simple_manifest, tmpdir):
        generate_kubernetes(simple_manifest, tmpdir)
        # Even for api which has health_check_path, liveness should be TCP
        path = Path(tmpdir) / "kubernetes" / "prod" / "api.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        container = docs[0]["spec"]["template"]["spec"]["containers"][0]
        assert "tcpSocket" in container["livenessProbe"]
        # Liveness should have longer intervals than readiness
        liveness_period = container["livenessProbe"]["periodSeconds"]
        readiness_period = container["readinessProbe"]["periodSeconds"]
        assert liveness_period > readiness_period

    def test_hpa_cpu_and_memory(self, simple_manifest, tmpdir):
        generate_kubernetes(simple_manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "api.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        hpa = docs[3]  # Deployment, Service, NetworkPolicy, HPA
        assert hpa["kind"] == "HorizontalPodAutoscaler"
        metrics = hpa["spec"]["metrics"]
        assert len(metrics) == 2
        names = {m["resource"]["name"] for m in metrics}
        assert names == {"cpu", "memory"}
        cpu_metric = next(m for m in metrics if m["resource"]["name"] == "cpu")
        mem_metric = next(m for m in metrics if m["resource"]["name"] == "memory")
        assert cpu_metric["resource"]["target"]["averageUtilization"] == 70
        assert mem_metric["resource"]["target"]["averageUtilization"] == 80

    def test_network_policy_internal_rejects_external(self, simple_manifest, tmpdir):
        generate_kubernetes(simple_manifest, tmpdir)
        # auth is internal
        path = Path(tmpdir) / "kubernetes" / "prod" / "auth.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        netpol = docs[2]
        assert netpol["kind"] == "NetworkPolicy"
        # Check that the first ingress rule rejects external-labeled pods
        ingress = netpol["spec"]["ingress"]
        first_rule = ingress[0]
        match_expr = first_rule["from"][0]["podSelector"]["matchExpressions"][0]
        assert match_expr["key"] == "exposure"
        assert match_expr["operator"] == "NotIn"
        assert "external" in match_expr["values"]


# ---- Drift tests ----


class TestDrift:
    def test_forward_all_new(self, simple_manifest, tmpdir):
        report = detect_drift(simple_manifest, tmpdir)
        # Nothing exists yet, all services should be "create"
        assert len(report["forward"]) > 0
        assert all(item["action"] == "create" for item in report["forward"])
        assert len(report["reverse"]) == 0

    def test_reverse_orphan(self, simple_manifest, tmpdir):
        # Generate first
        generate_terraform(simple_manifest, tmpdir)
        generate_kubernetes(simple_manifest, tmpdir)

        # Now use a manifest WITHOUT "worker"
        smaller = Manifest(services=[s for s in simple_manifest.services if s.name != "worker"])
        report = detect_drift(smaller, tmpdir)

        orphans = report["reverse"]
        assert len(orphans) > 0
        assert any(item["service"] == "worker" for item in orphans)

    def test_no_drift_after_generate(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        generate_kubernetes(simple_manifest, tmpdir)
        report = detect_drift(simple_manifest, tmpdir)
        assert len(report["forward"]) == 0
        assert len(report["reverse"]) == 0

    def test_forward_db_change(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        generate_kubernetes(simple_manifest, tmpdir)

        # Change worker to have a db
        modified = Manifest(
            services=[
                s if s.name != "worker" else _svc("worker", db="postgres")
                for s in simple_manifest.services
            ]
        )
        report = detect_drift(modified, tmpdir)
        forward = report["forward"]
        assert any(item["service"] == "worker" and "Database" in item["reason"] for item in forward)


# ---- Cost tests ----


class TestCost:
    def test_basic_cost(self):
        manifest = Manifest(services=[_svc("a")])
        costs = estimate_costs(manifest)
        # dev: 1 replica * $7.49 = $7.49
        assert costs["dev"]["a"] == 7.49
        assert costs["dev"]["total"] == 7.49

    def test_cost_with_db_and_cache(self):
        manifest = Manifest(services=[_svc("a", db="postgres", cache="redis")])
        costs = estimate_costs(manifest)
        # dev: 1*7.49 + 12.25 + 11.52 = 31.26
        assert costs["dev"]["a"] == 31.26

    def test_cost_scales_with_replicas(self):
        manifest = Manifest(services=[_svc("a", dev_r=1, stg_r=2, prod_r=4)])
        costs = estimate_costs(manifest)
        assert costs["dev"]["a"] == 7.49
        assert costs["staging"]["a"] == 14.98
        assert costs["prod"]["a"] == 29.96


# ---- CLI tests ----


class TestCLI:
    def test_validate_valid(self):
        rc = main(["sample.yaml", "--validate"])
        assert rc == 0

    def test_validate_cycle(self):
        rc = main(["sample_with_cycle.yaml", "--validate"])
        assert rc == 1

    def test_dry_run_valid(self):
        rc = main(["sample.yaml", "--dry-run"])
        assert rc == 0

    def test_dry_run_cycle(self):
        rc = main(["sample_with_cycle.yaml", "--dry-run"])
        assert rc == 1

    def test_generate(self, tmpdir):
        rc = main(["sample.yaml", "-o", tmpdir])
        assert rc == 0
        assert (Path(tmpdir) / "terraform" / "prod").exists()
        assert (Path(tmpdir) / "kubernetes" / "prod").exists()

    def test_drift_fresh(self, tmpdir):
        rc = main(["sample.yaml", "--drift", "-o", tmpdir])
        # forward drift means changes needed (new files), exit 0 if no orphans
        assert rc == 0

    def test_parse_error(self, tmpdir):
        """CLI returns 1 on unparseable manifest."""
        bad = Path(tmpdir) / "bad.yaml"
        bad.write_text("not: valid: yaml: [")
        rc = main([str(bad)])
        assert rc == 1

    def test_empty_services(self, tmpdir):
        """CLI returns 1 when manifest has no services."""
        empty = Path(tmpdir) / "empty.yaml"
        empty.write_text("services: []\n")
        rc = main([str(empty)])
        assert rc == 1

    def test_drift_with_orphans(self, tmpdir):
        """CLI --drift returns 1 when orphaned resources exist."""
        # Generate with full manifest
        main(["sample.yaml", "-o", tmpdir])
        # Drift-check with a smaller manifest that removes services
        small = Path(tmpdir) / "small.yaml"
        small.write_text(
            "services:\n"
            "  - name: api-gateway\n"
            "    port: 8080\n"
            "    exposure: external\n"
            "    env_overrides:\n"
            "      dev: {replicas: 1, cpu: '250m'}\n"
            "      staging: {replicas: 2, cpu: '500m'}\n"
            "      prod: {replicas: 4, cpu: '1000m'}\n"
        )
        rc = main([str(small), "--drift", "-o", tmpdir])
        assert rc == 1

    def test_generate_with_cycle(self, tmpdir):
        """CLI generate returns 1 when manifest has validation errors."""
        rc = main(["sample_with_cycle.yaml", "-o", tmpdir])
        assert rc == 1


# ---- Graph edge cases ----


class TestGraphEdgeCases:
    def test_four_node_cycle(self):
        """4-node cycle: a -> b -> c -> d -> a."""
        manifest = Manifest(
            services=[
                _svc("a", deps=["b"]),
                _svc("b", deps=["c"]),
                _svc("c", deps=["d"]),
                _svc("d", deps=["a"]),
            ]
        )
        cycles = find_all_cycles(manifest)
        cycle_sets = [set(c) for c in cycles]
        assert any({"a", "b", "c", "d"} == cs for cs in cycle_sets)

    def test_two_independent_peer_pairs(self):
        """Two separate peer pairs in one manifest."""
        manifest = Manifest(
            services=[
                _svc("alpha", deps=["beta"]),
                _svc("beta", deps=["alpha"]),
                _svc("gamma", deps=["delta"]),
                _svc("delta", deps=["gamma"]),
            ]
        )
        peers = find_peer_pairs(manifest)
        peer_sets = [set(p) for p in peers]
        assert len(peers) == 2
        assert {"alpha", "beta"} in peer_sets
        assert {"gamma", "delta"} in peer_sets
        # None should be detected as cycles
        cycles = find_all_cycles(manifest)
        assert len(cycles) == 0

    def test_topological_sort_with_remaining_cycle(self, cycle_manifest):
        """Topo sort returns fewer nodes when a true cycle exists."""
        order = topological_sort(cycle_manifest)
        assert len(order) < 3

    def test_no_peers_no_cycles_empty_deps(self):
        """Manifest with no dependencies at all."""
        manifest = Manifest(services=[_svc("a"), _svc("b"), _svc("c")])
        assert find_peer_pairs(manifest) == []
        assert find_all_cycles(manifest) == []
        order = topological_sort(manifest)
        assert set(order) == {"a", "b", "c"}


# ---- Validator edge cases ----


class TestValidatorEdgeCases:
    def test_missing_env_overrides(self):
        """Service with no env_overrides triggers errors for each env."""
        svc = Service(
            name="bare",
            port=8080,
            dependencies=[],
            db_type="none",
            cache="none",
            exposure="internal",
            env_overrides={},
        )
        manifest = Manifest(services=[svc])
        errors = validate_manifest(manifest)
        real_errors = [e for e in errors if e.severity == "error"]
        missing = [e for e in real_errors if "missing env_overrides" in e.message]
        assert len(missing) == 3  # dev, staging, prod

    def test_equal_replicas_passes(self):
        """Equal replicas across envs is valid."""
        svc = _svc("a", dev_r=2, stg_r=2, prod_r=2)
        errors = validate_manifest(Manifest(services=[svc]))
        real_errors = [e for e in errors if e.severity == "error"]
        assert len(real_errors) == 0

    def test_staging_less_than_dev_fails(self):
        """dev=2, staging=1, prod=3 fails because staging < dev."""
        svc = _svc("a", dev_r=2, stg_r=1, prod_r=3)
        errors = validate_manifest(Manifest(services=[svc]))
        real_errors = [e for e in errors if e.severity == "error"]
        assert any("replica ordering violated" in e.message for e in real_errors)

    def test_validation_error_repr(self):
        """ValidationError.__repr__ returns expected format."""
        err = ValidationError("test msg", severity="info")
        assert "test msg" in repr(err)
        assert "info" in repr(err)

    def test_validation_error_str(self):
        """ValidationError.__str__ includes severity and message."""
        err = ValidationError("some problem")
        result = str(err)
        assert "[ERROR]" in result
        assert "some problem" in result


# ---- Drift edge cases ----


class TestDriftEdgeCases:
    def test_reverse_orphan_count(self, simple_manifest, tmpdir):
        """Orphaned service shows up in all 3 tf envs + 3 k8s envs = 6 orphans."""
        generate_terraform(simple_manifest, tmpdir)
        generate_kubernetes(simple_manifest, tmpdir)
        smaller = Manifest(services=[s for s in simple_manifest.services if s.name != "worker"])
        report = detect_drift(smaller, tmpdir)
        orphans = [o for o in report["reverse"] if o["service"] == "worker"]
        assert len(orphans) == 6  # 3 terraform + 3 kubernetes

    def test_non_dir_entries_ignored(self, simple_manifest, tmpdir):
        """Files placed directly in terraform/ or kubernetes/ don't crash drift."""
        generate_terraform(simple_manifest, tmpdir)
        generate_kubernetes(simple_manifest, tmpdir)
        # Create stray files (not directories)
        (Path(tmpdir) / "terraform" / "README.md").write_text("stray file")
        (Path(tmpdir) / "kubernetes" / "notes.txt").write_text("stray file")
        # Should not crash
        report = detect_drift(simple_manifest, tmpdir)
        assert len(report["reverse"]) == 0

    def test_forward_db_removal(self, simple_manifest, tmpdir):
        """Removing a db from a service is detected as forward drift."""
        generate_terraform(simple_manifest, tmpdir)
        generate_kubernetes(simple_manifest, tmpdir)
        # auth has db="postgres"; change it to db="none"
        modified = Manifest(
            services=[
                s
                if s.name != "auth"
                else _svc("auth", deps=[], db="none", cache="redis", health="/health")
                for s in simple_manifest.services
            ]
        )
        report = detect_drift(modified, tmpdir)
        forward = report["forward"]
        assert any(
            item["service"] == "auth" and "Database resources will be removed" in item["reason"]
            for item in forward
        )

    def test_forward_cache_addition(self, simple_manifest, tmpdir):
        """Adding a cache to a service is detected as forward drift."""
        generate_terraform(simple_manifest, tmpdir)
        generate_kubernetes(simple_manifest, tmpdir)
        # worker has no cache; give it redis
        modified = Manifest(
            services=[
                s if s.name != "worker" else _svc("worker", cache="redis")
                for s in simple_manifest.services
            ]
        )
        report = detect_drift(modified, tmpdir)
        forward = report["forward"]
        assert any(
            item["service"] == "worker" and "Cache resources will be added" in item["reason"]
            for item in forward
        )

    def test_forward_cache_removal(self, simple_manifest, tmpdir):
        """Removing a cache from a service is detected as forward drift."""
        generate_terraform(simple_manifest, tmpdir)
        generate_kubernetes(simple_manifest, tmpdir)
        # auth has cache="redis"; change it to cache="none"
        modified = Manifest(
            services=[
                s
                if s.name != "auth"
                else _svc("auth", deps=[], db="postgres", cache="none", health="/health")
                for s in simple_manifest.services
            ]
        )
        report = detect_drift(modified, tmpdir)
        forward = report["forward"]
        assert any(
            item["service"] == "auth" and "Cache resources will be removed" in item["reason"]
            for item in forward
        )

    def test_format_drift_report_no_changes(self):
        """Format report when there's no drift at all."""
        report = {"forward": [], "reverse": []}
        text = format_drift_report(report)
        assert "No changes detected" in text
        assert "No orphaned resources" in text

    def test_format_drift_report_with_orphans(self):
        """Format report includes ORPHAN lines and file paths."""
        report = {
            "forward": [
                {
                    "type": "terraform",
                    "environment": "dev",
                    "service": "new-svc",
                    "action": "create",
                    "reason": "New service",
                },
            ],
            "reverse": [
                {
                    "type": "terraform",
                    "environment": "prod",
                    "file": "output/terraform/prod/old.tf.json",
                    "service": "old",
                    "reason": "Service no longer in manifest",
                },
            ],
        }
        text = format_drift_report(report)
        assert "[CREATE]" in text
        assert "[ORPHAN]" in text
        assert "old.tf.json" in text
        assert "FORWARD DRIFT (changes to apply)" in text
        assert "REVERSE DRIFT (orphaned resources)" in text


# ---- Cost edge cases ----


class TestCostEdgeCases:
    def test_format_cost_report(self):
        """format_cost_report produces human-readable output."""
        manifest = Manifest(services=[_svc("a", db="postgres")])
        costs = estimate_costs(manifest)
        text = format_cost_report(costs)
        assert "Estimated Monthly AWS Costs" in text
        assert "GRAND TOTAL" in text
        assert "DEV:" in text
        assert "STAGING:" in text
        assert "PROD:" in text
        assert "$" in text

    def test_cost_without_env_overrides(self):
        """Service with no env_overrides defaults to 1 replica."""
        svc = Service(
            name="bare",
            port=8080,
            dependencies=[],
            db_type="none",
            cache="none",
            exposure="internal",
            env_overrides={},
        )
        costs = estimate_costs(Manifest(services=[svc]))
        assert costs["dev"]["bare"] == 7.49
        assert costs["prod"]["bare"] == 7.49

    def test_multiple_services_total(self):
        """Total is sum of all service costs in an environment."""
        manifest = Manifest(services=[_svc("a"), _svc("b", db="postgres")])
        costs = estimate_costs(manifest)
        # dev: a=1*7.49, b=1*7.49+12.25=19.74, total=27.23
        assert costs["dev"]["a"] == 7.49
        assert costs["dev"]["b"] == 19.74
        assert costs["dev"]["total"] == 27.23


# ---- Model edge cases ----


class TestModels:
    def test_dependency_hash_stable(self):
        """Same deps in different order produce the same hash."""
        s1 = _svc("a", deps=["b", "c"])
        s2 = _svc("a", deps=["c", "b"])
        assert s1.dependency_hash() == s2.dependency_hash()

    def test_dependency_hash_differs(self):
        """Different deps produce different hashes."""
        s1 = _svc("a", deps=["b"])
        s2 = _svc("a", deps=["c"])
        assert s1.dependency_hash() != s2.dependency_hash()

    def test_service_map(self):
        """Manifest.service_map() returns name-keyed dict."""
        manifest = Manifest(services=[_svc("x"), _svc("y")])
        smap = manifest.service_map()
        assert set(smap.keys()) == {"x", "y"}
        assert smap["x"].name == "x"

    def test_has_db_and_cache_properties(self):
        svc = _svc("a", db="postgres", cache="redis")
        assert svc.has_db is True
        assert svc.has_cache is True
        svc2 = _svc("b")
        assert svc2.has_db is False
        assert svc2.has_cache is False


# ---- Kubernetes edge cases ----


class TestKubernetesEdgeCases:
    def test_peer_group_label(self, peer_manifest, tmpdir):
        """Peer services get a peer-group label in k8s manifests."""
        generate_kubernetes(peer_manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "order.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        labels = docs[0]["metadata"]["labels"]
        assert "peer-group" in labels
        assert labels["peer-group"] == "inventory-order"

    def test_network_policy_external_allows_any(self, simple_manifest, tmpdir):
        """External services allow ingress from any pod in namespace."""
        generate_kubernetes(simple_manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "api.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        netpol = docs[2]
        first_rule = netpol["spec"]["ingress"][0]
        # External: podSelector is empty (matches all)
        assert first_rule["from"][0]["podSelector"] == {}

    def test_network_policy_dns_egress(self, simple_manifest, tmpdir):
        """All services allow DNS egress on port 53."""
        generate_kubernetes(simple_manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "worker.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        netpol = docs[2]
        egress = netpol["spec"]["egress"]
        dns_rules = [r for r in egress if any(p.get("port") == 53 for p in r.get("ports", []))]
        assert len(dns_rules) == 1
        # Both UDP and TCP
        protocols = {p["protocol"] for p in dns_rules[0]["ports"]}
        assert protocols == {"UDP", "TCP"}

    def test_network_policy_dependency_egress(self, simple_manifest, tmpdir):
        """Service has egress rule to its dependency."""
        generate_kubernetes(simple_manifest, tmpdir)
        # api depends on auth
        path = Path(tmpdir) / "kubernetes" / "prod" / "api.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        netpol = docs[2]
        egress = netpol["spec"]["egress"]
        dep_rules = [
            r
            for r in egress
            if any(
                t.get("podSelector", {}).get("matchLabels", {}).get("app") == "auth"
                for t in r.get("to", [])
            )
        ]
        assert len(dep_rules) == 1

    def test_hpa_autoscaling_v2(self, simple_manifest, tmpdir):
        """HPA uses autoscaling/v2 API version."""
        generate_kubernetes(simple_manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "api.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        hpa = docs[3]
        assert hpa["apiVersion"] == "autoscaling/v2"

    def test_deployment_replicas_from_env(self, tmpdir):
        """Deployment replicas match env_overrides."""
        svc = _svc("web", dev_r=1, stg_r=3, prod_r=6)
        manifest = Manifest(services=[svc])
        generate_kubernetes(manifest, tmpdir)
        for env, expected in [("dev", 1), ("staging", 3), ("prod", 6)]:
            path = Path(tmpdir) / "kubernetes" / env / "web.yaml"
            docs = list(yaml.safe_load_all(path.read_text()))
            assert docs[0]["spec"]["replicas"] == expected

    def test_all_four_resource_types(self, simple_manifest, tmpdir):
        """Each service YAML contains Deployment, Service, NetworkPolicy, HPA."""
        generate_kubernetes(simple_manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "api.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        kinds = [d["kind"] for d in docs]
        assert kinds == ["Deployment", "Service", "NetworkPolicy", "HorizontalPodAutoscaler"]

    def test_liveness_readiness_all_timings_differ(self, simple_manifest, tmpdir):
        """All four timing parameters differ between liveness and readiness."""
        generate_kubernetes(simple_manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "api.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        container = docs[0]["spec"]["template"]["spec"]["containers"][0]
        readiness = container["readinessProbe"]
        liveness = container["livenessProbe"]
        for key in ["initialDelaySeconds", "periodSeconds", "timeoutSeconds", "failureThreshold"]:
            assert readiness[key] != liveness[key], f"{key} should differ"


# ---- Terraform edge cases ----


class TestTerraformEdgeCases:
    def test_cache_sg_only_from_service(self, simple_manifest, tmpdir):
        """Cache SG only allows ingress from the owning service."""
        generate_terraform(simple_manifest, tmpdir)
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        cache_sg = auth_tf["resource"]["aws_security_group_auth_cache"]
        assert len(cache_sg["ingress"]) == 1
        assert "auth" in str(cache_sg["ingress"][0]["security_groups"])

    def test_ecs_service_desired_count(self, simple_manifest, tmpdir):
        """ECS service desired_count matches env_overrides replicas."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        ecs = api_tf["resource"]["aws_ecs_service_api"]
        assert ecs["desired_count"] == 3  # prod_r=3 from _svc default

    def test_rds_engine_postgres(self, simple_manifest, tmpdir):
        """RDS instance uses the correct engine."""
        generate_terraform(simple_manifest, tmpdir)
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        rds = auth_tf["resource"]["aws_db_instance_auth"]
        assert rds["engine"] == "postgres"

    def test_rds_engine_mysql(self, tmpdir):
        """MySQL services get mysql engine."""
        manifest = Manifest(services=[_svc("db-svc", db="mysql")])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "db-svc.tf.json").read_text())
        rds = tf["resource"]["aws_db_instance_db_svc"]
        assert rds["engine"] == "mysql"

    def test_elasticache_engine(self, simple_manifest, tmpdir):
        """ElastiCache uses the correct engine (redis/memcached)."""
        generate_terraform(simple_manifest, tmpdir)
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        cache = auth_tf["resource"]["aws_elasticache_cluster_auth"]
        assert cache["engine"] == "redis"

    def test_provider_default_tags(self, simple_manifest, tmpdir):
        """Provider includes environment in default_tags."""
        generate_terraform(simple_manifest, tmpdir)
        provider = json.loads(
            (Path(tmpdir) / "terraform" / "prod" / "provider.tf.json").read_text()
        )
        tags = provider["provider"]["aws"]["default_tags"]["tags"]
        assert tags["environment"] == "prod"
