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

    def test_parse_empty_yaml(self, tmpdir):
        """Parser handles empty YAML files without crashing."""
        empty = Path(tmpdir) / "empty.yaml"
        empty.write_text("")
        manifest = parse_manifest(str(empty))
        assert manifest.services == []

    def test_parse_yaml_with_only_comments(self, tmpdir):
        """Parser handles YAML with only comments."""
        path = Path(tmpdir) / "comments.yaml"
        path.write_text("# just a comment\n")
        manifest = parse_manifest(str(path))
        assert manifest.services == []


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
        # 3 envs * (3 infra files + 3 service files) = 18
        assert len(files) == 18

    def test_backend_per_env(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        for env in ["dev", "staging", "prod"]:
            backend = json.loads((Path(tmpdir) / "terraform" / env / "backend.tf.json").read_text())
            s3 = backend["terraform"]["backend"]["s3"]
            assert s3["bucket"] == f"terraform-state-{env}-us-east-1"
            assert s3["dynamodb_table"] == f"terraform-locks-{env}-us-east-1"
            assert s3["region"] == "us-east-1"

    def test_external_gets_alb_ingress(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        sg = api_tf["resource"]["aws_security_group"]["api"]
        has_443 = any(
            r.get("from_port") == 443 and "0.0.0.0/0" in r.get("cidr_blocks", [])
            for r in sg["ingress"]
        )
        assert has_443

    def test_internal_no_public_ingress(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        sg = auth_tf["resource"]["aws_security_group"]["auth"]
        has_public = any("0.0.0.0/0" in r.get("cidr_blocks", []) for r in sg["ingress"])
        assert not has_public

    def test_directional_sg_rules(self, simple_manifest, tmpdir):
        """A depends on B means A can reach B only (B gets ingress from A)."""
        generate_terraform(simple_manifest, tmpdir)
        # api depends on auth, so auth gets ingress from api
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        sg = auth_tf["resource"]["aws_security_group"]["auth"]
        has_ingress_from_api = any(
            "api" in str(r.get("security_groups", [])) for r in sg["ingress"]
        )
        assert has_ingress_from_api

    def test_db_sg_only_from_service(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        db_sg = auth_tf["resource"]["aws_security_group"]["auth_db"]
        # DB SG should only have ingress from the auth service SG
        assert len(db_sg["ingress"]) == 1
        assert "auth" in str(db_sg["ingress"][0]["security_groups"])

    def test_tags_present(self, simple_manifest, tmpdir):
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        sg = api_tf["resource"]["aws_security_group"]["api"]
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

        order_sg = order_tf["resource"]["aws_security_group"]["order"]
        inv_sg = inv_tf["resource"]["aws_security_group"]["inventory"]

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
        sg = order_tf["resource"]["aws_security_group"]["order"]
        assert sg["tags"]["peer-group"] == "inventory-order"

    def test_variables_file_created(self, simple_manifest, tmpdir):
        """Each environment gets a variables.tf.json."""
        generate_terraform(simple_manifest, tmpdir)
        for env in ["dev", "staging", "prod"]:
            path = Path(tmpdir) / "terraform" / env / "variables.tf.json"
            assert path.exists()
            content = json.loads(path.read_text())
            assert "vpc_id" in content["variable"]
            assert "ecs_cluster_arn" in content["variable"]
            assert "private_subnet_ids" in content["variable"]

    def test_vpc_id_on_security_groups(self, simple_manifest, tmpdir):
        """All security groups reference var.vpc_id."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        sg = api_tf["resource"]["aws_security_group"]["api"]
        assert sg["vpc_id"] == "${var.vpc_id}"


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

    def test_state_without_drift_errors(self, tmpdir):
        """CLI --state without --drift produces an error."""
        with pytest.raises(SystemExit):
            main(["sample.yaml", "--state", "-o", tmpdir])

    def test_state_s3_without_state_errors(self, tmpdir):
        """CLI --state-s3 without --state produces an error."""
        with pytest.raises(SystemExit):
            main(["sample.yaml", "--drift", "--state-s3", "-o", tmpdir])


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

    def test_network_policy_dns_allows_any_destination(self, simple_manifest, tmpdir):
        """DNS egress uses empty selector (allow to any) not empty list (block all)."""
        generate_kubernetes(simple_manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "worker.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        netpol = docs[2]
        egress = netpol["spec"]["egress"]
        dns_rules = [r for r in egress if any(p.get("port") == 53 for p in r.get("ports", []))]
        assert len(dns_rules) == 1
        # "to" should be [{}] not []
        assert dns_rules[0]["to"] == [{}]

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
        cache_sg = auth_tf["resource"]["aws_security_group"]["auth_cache"]
        assert len(cache_sg["ingress"]) == 1
        assert "auth" in str(cache_sg["ingress"][0]["security_groups"])

    def test_ecs_service_desired_count(self, simple_manifest, tmpdir):
        """ECS service desired_count matches env_overrides replicas."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        ecs = api_tf["resource"]["aws_ecs_service"]["api"]
        assert ecs["desired_count"] == 3  # prod_r=3 from _svc default

    def test_rds_engine_postgres(self, simple_manifest, tmpdir):
        """RDS instance uses the correct engine."""
        generate_terraform(simple_manifest, tmpdir)
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        rds = auth_tf["resource"]["aws_db_instance"]["auth"]
        assert rds["engine"] == "postgres"

    def test_rds_engine_mysql(self, tmpdir):
        """MySQL services get mysql engine."""
        manifest = Manifest(services=[_svc("db-svc", db="mysql")])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "db-svc.tf.json").read_text())
        rds = tf["resource"]["aws_db_instance"]["db_svc"]
        assert rds["engine"] == "mysql"

    def test_rds_has_required_fields(self, simple_manifest, tmpdir):
        """RDS instance has username, password, and skip_final_snapshot."""
        generate_terraform(simple_manifest, tmpdir)
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        rds = auth_tf["resource"]["aws_db_instance"]["auth"]
        assert "username" in rds
        assert "password" in rds
        assert "skip_final_snapshot" in rds

    def test_rds_skip_final_snapshot_by_env(self, simple_manifest, tmpdir):
        """skip_final_snapshot is False for prod, True for dev/staging."""
        generate_terraform(simple_manifest, tmpdir)
        for env, expected in [("prod", False), ("dev", True), ("staging", True)]:
            tf = json.loads((Path(tmpdir) / "terraform" / env / "auth.tf.json").read_text())
            rds = tf["resource"]["aws_db_instance"]["auth"]
            assert rds["skip_final_snapshot"] is expected, f"{env}: skip_final_snapshot wrong"

    def test_elasticache_engine(self, simple_manifest, tmpdir):
        """ElastiCache uses the correct engine (redis/memcached)."""
        generate_terraform(simple_manifest, tmpdir)
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        cache = auth_tf["resource"]["aws_elasticache_cluster"]["auth"]
        assert cache["engine"] == "redis"

    def test_provider_default_tags(self, simple_manifest, tmpdir):
        """Provider includes environment in default_tags."""
        generate_terraform(simple_manifest, tmpdir)
        provider = json.loads(
            (Path(tmpdir) / "terraform" / "prod" / "provider.tf.json").read_text()
        )
        tags = provider["provider"]["aws"]["default_tags"]["tags"]
        assert tags["environment"] == "prod"

    def test_ecs_service_has_cluster(self, simple_manifest, tmpdir):
        """ECS service references the ECS cluster ARN."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        ecs = api_tf["resource"]["aws_ecs_service"]["api"]
        assert ecs["cluster"] == "${var.ecs_cluster_arn}"


# ---- ECS Task Definition tests ----


class TestECSTaskDefinition:
    def test_task_definition_exists(self, simple_manifest, tmpdir):
        """Each service gets an ECS task definition."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        assert "api" in api_tf["resource"]["aws_ecs_task_definition"]

    def test_task_definition_fargate(self, simple_manifest, tmpdir):
        """Task definition uses FARGATE compatibility."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        task_def = api_tf["resource"]["aws_ecs_task_definition"]["api"]
        assert task_def["requires_compatibilities"] == ["FARGATE"]
        assert task_def["network_mode"] == "awsvpc"

    def test_task_definition_cpu_is_valid_fargate(self, simple_manifest, tmpdir):
        """Task definition CPU is a valid Fargate CPU value (rounded up from millicore)."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        task_def = api_tf["resource"]["aws_ecs_task_definition"]["api"]
        # 750m -> rounds up to 1024 Fargate CPU
        assert task_def["cpu"] == "1024"

    def test_task_definition_memory_matches_fargate_cpu(self, simple_manifest, tmpdir):
        """Task definition memory matches the Fargate memory for the CPU tier."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        task_def = api_tf["resource"]["aws_ecs_task_definition"]["api"]
        # CPU 1024 -> memory 2048
        assert task_def["memory"] == "2048"

    def test_task_definition_container_definition(self, simple_manifest, tmpdir):
        """Container definition includes name, image, port, and essential flag."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        td = api_tf["resource"]["aws_ecs_task_definition"]["api"]
        containers = json.loads(td["container_definitions"])
        assert len(containers) == 1
        c = containers[0]
        assert c["name"] == "api"
        assert c["image"] == "api:latest"
        assert c["essential"] is True
        assert c["portMappings"][0]["containerPort"] == 8080

    def test_task_definition_log_configuration(self, simple_manifest, tmpdir):
        """Container has awslogs log driver pointing to the correct log group."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        td = api_tf["resource"]["aws_ecs_task_definition"]["api"]
        c = json.loads(td["container_definitions"])[0]
        log_config = c["logConfiguration"]
        assert log_config["logDriver"] == "awslogs"
        assert log_config["options"]["awslogs-group"] == "/ecs/api/prod"
        assert log_config["options"]["awslogs-region"] == "us-east-1"

    def test_task_definition_environment_vars(self, simple_manifest, tmpdir):
        """Container has ENV, SERVICE_NAME, and PORT environment variables."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        td = api_tf["resource"]["aws_ecs_task_definition"]["api"]
        c = json.loads(td["container_definitions"])[0]
        env_vars = {e["name"]: e["value"] for e in c["environment"]}
        assert env_vars["ENV"] == "prod"
        assert env_vars["SERVICE_NAME"] == "api"
        assert env_vars["PORT"] == "8080"

    def test_task_definition_health_check_with_path(self, simple_manifest, tmpdir):
        """Container health check uses HTTP path when health_check_path is set."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        td = api_tf["resource"]["aws_ecs_task_definition"]["api"]
        c = json.loads(td["container_definitions"])[0]
        assert "healthCheck" in c
        assert "/healthz" in c["healthCheck"]["command"][1]

    def test_task_definition_no_health_check_without_path(self, simple_manifest, tmpdir):
        """Container has no health check when health_check_path is None."""
        generate_terraform(simple_manifest, tmpdir)
        worker_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "worker.tf.json").read_text())
        td = worker_tf["resource"]["aws_ecs_task_definition"]["worker"]
        c = json.loads(td["container_definitions"])[0]
        assert "healthCheck" not in c

    def test_task_definition_secrets_injected(self, tmpdir):
        """Container definition includes secrets from Secrets Manager when declared."""
        svc = _svc("api")
        svc.secrets = ["DB_PASSWORD", "API_KEY"]
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        c = json.loads(tf["resource"]["aws_ecs_task_definition"]["api"]["container_definitions"])[0]
        assert "secrets" in c
        secret_names = [s["name"] for s in c["secrets"]]
        assert "DB_PASSWORD" in secret_names
        assert "API_KEY" in secret_names

    def test_execution_role_created(self, simple_manifest, tmpdir):
        """Each service gets an ECS execution role."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        exec_role = api_tf["resource"]["aws_iam_role"]["api_execution"]
        assert "ecs-tasks.amazonaws.com" in str(exec_role["assume_role_policy"])

    def test_execution_role_policy_attached(self, simple_manifest, tmpdir):
        """Execution role has the ECS task execution policy attached."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        attachment = api_tf["resource"]["aws_iam_role_policy_attachment"]["api_execution"]
        assert "AmazonECSTaskExecutionRolePolicy" in attachment["policy_arn"]

    def test_task_role_created(self, simple_manifest, tmpdir):
        """Each service gets an ECS task role."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        task_role = api_tf["resource"]["aws_iam_role"]["api_task"]
        assert "ecs-tasks.amazonaws.com" in str(task_role["assume_role_policy"])

    def test_task_role_secrets_policy_attached(self, tmpdir):
        """Task role gets secrets policy attached when service has secrets."""
        svc = _svc("api")
        svc.secrets = ["DB_PASSWORD"]
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        assert "api_secrets" in tf["resource"].get("aws_iam_role_policy_attachment", {})

    def test_task_role_no_secrets_policy_without_secrets(self, simple_manifest, tmpdir):
        """Task role has no secrets policy when service has no secrets."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        assert "api_secrets" not in api_tf["resource"].get("aws_iam_role_policy_attachment", {})

    def test_cloudwatch_log_group_created(self, simple_manifest, tmpdir):
        """Each service gets a CloudWatch log group."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        log_group = api_tf["resource"]["aws_cloudwatch_log_group"]["api"]
        assert log_group["name"] == "/ecs/api/prod"

    def test_log_group_retention_by_env(self, simple_manifest, tmpdir):
        """Log group retention varies by environment: prod=30, staging=14, dev=7."""
        generate_terraform(simple_manifest, tmpdir)
        for env, expected_days in [("prod", 30), ("staging", 14), ("dev", 7)]:
            tf = json.loads((Path(tmpdir) / "terraform" / env / "api.tf.json").read_text())
            log_group = tf["resource"]["aws_cloudwatch_log_group"]["api"]
            assert log_group["retention_in_days"] == expected_days, f"{env} retention wrong"

    def test_ecs_service_references_task_definition(self, simple_manifest, tmpdir):
        """ECS service references the task definition ARN."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        ecs = api_tf["resource"]["aws_ecs_service"]["api"]
        assert "aws_ecs_task_definition" in ecs["task_definition"]

    def test_ecs_service_launch_type_fargate(self, simple_manifest, tmpdir):
        """ECS service uses FARGATE launch type."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        ecs = api_tf["resource"]["aws_ecs_service"]["api"]
        assert ecs["launch_type"] == "FARGATE"

    def test_ecs_service_network_configuration(self, simple_manifest, tmpdir):
        """ECS service has network configuration with security groups."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        ecs = api_tf["resource"]["aws_ecs_service"]["api"]
        net = ecs["network_configuration"]
        assert "security_groups" in net
        assert "subnets" in net

    def test_ecs_service_external_public_ip(self, simple_manifest, tmpdir):
        """External services get assign_public_ip=True."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        ecs = api_tf["resource"]["aws_ecs_service"]["api"]
        assert ecs["network_configuration"]["assign_public_ip"] is True

    def test_ecs_service_internal_no_public_ip(self, simple_manifest, tmpdir):
        """Internal services get assign_public_ip=False."""
        generate_terraform(simple_manifest, tmpdir)
        auth_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        ecs = auth_tf["resource"]["aws_ecs_service"]["auth"]
        assert ecs["network_configuration"]["assign_public_ip"] is False

    def test_ecs_service_circuit_breaker(self, simple_manifest, tmpdir):
        """ECS service has deployment circuit breaker with rollback."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        ecs = api_tf["resource"]["aws_ecs_service"]["api"]
        cb = ecs["deployment_circuit_breaker"]
        assert cb["enable"] is True
        assert cb["rollback"] is True

    def test_task_definition_family_includes_env(self, simple_manifest, tmpdir):
        """Task definition family includes the environment name."""
        generate_terraform(simple_manifest, tmpdir)
        for env in ["dev", "staging", "prod"]:
            tf = json.loads((Path(tmpdir) / "terraform" / env / "api.tf.json").read_text())
            task_def = tf["resource"]["aws_ecs_task_definition"]["api"]
            assert task_def["family"] == f"api-{env}"

    def test_execution_role_arn_referenced(self, simple_manifest, tmpdir):
        """Task definition references the execution role ARN."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        task_def = api_tf["resource"]["aws_ecs_task_definition"]["api"]
        assert "aws_iam_role" in task_def["execution_role_arn"]
        assert "execution" in task_def["execution_role_arn"]

    def test_task_role_arn_referenced(self, simple_manifest, tmpdir):
        """Task definition references the task role ARN."""
        generate_terraform(simple_manifest, tmpdir)
        api_tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        task_def = api_tf["resource"]["aws_ecs_task_definition"]["api"]
        assert "aws_iam_role" in task_def["task_role_arn"]
        assert "task" in task_def["task_role_arn"]


# ---- Fargate CPU mapping tests ----


class TestFargateCPUMapping:
    def test_250m_maps_to_256(self, tmpdir):
        """250m rounds up to 256 Fargate CPU."""
        svc = _svc("api")
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "dev" / "api.tf.json").read_text())
        task_def = tf["resource"]["aws_ecs_task_definition"]["api"]
        assert task_def["cpu"] == "256"
        assert task_def["memory"] == "512"

    def test_500m_maps_to_512(self, tmpdir):
        """500m maps to 512 Fargate CPU."""
        svc = _svc("api")
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "staging" / "api.tf.json").read_text())
        task_def = tf["resource"]["aws_ecs_task_definition"]["api"]
        assert task_def["cpu"] == "512"
        assert task_def["memory"] == "1024"

    def test_750m_maps_to_1024(self, tmpdir):
        """750m rounds up to 1024 Fargate CPU."""
        svc = _svc("api")
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        task_def = tf["resource"]["aws_ecs_task_definition"]["api"]
        assert task_def["cpu"] == "1024"
        assert task_def["memory"] == "2048"

    def test_1000m_maps_to_1024(self, tmpdir):
        """1000m maps to 1024 Fargate CPU (exact match)."""
        svc = Service(
            name="api",
            port=8080,
            dependencies=[],
            db_type="none",
            cache="none",
            exposure="internal",
            env_overrides={"prod": EnvOverride(replicas=1, cpu="1000m")},
        )
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        task_def = tf["resource"]["aws_ecs_task_definition"]["api"]
        assert task_def["cpu"] == "1024"

    def test_large_cpu_caps_at_4096(self, tmpdir):
        """Very large CPU values cap at 4096."""
        svc = Service(
            name="api",
            port=8080,
            dependencies=[],
            db_type="none",
            cache="none",
            exposure="internal",
            env_overrides={"prod": EnvOverride(replicas=1, cpu="8000m")},
        )
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        task_def = tf["resource"]["aws_ecs_task_definition"]["api"]
        assert task_def["cpu"] == "4096"
        assert task_def["memory"] == "8192"


# ---- Secrets / Vault tests ----


class TestSecretsModel:
    def test_has_secrets_true(self):
        svc = _svc("a")
        svc.secrets = ["DB_PASSWORD", "API_KEY"]
        assert svc.has_secrets is True

    def test_has_secrets_false(self):
        svc = _svc("a")
        assert svc.has_secrets is False

    def test_secrets_default_empty(self):
        svc = _svc("a")
        assert svc.secrets == []


class TestSecretsParser:
    def test_parse_secrets_from_yaml(self, tmpdir):
        """Parser reads the secrets list from YAML."""
        manifest_path = Path(tmpdir) / "svc.yaml"
        manifest_path.write_text(
            "services:\n"
            "  - name: web\n"
            "    port: 8080\n"
            "    secrets:\n"
            "      - DB_PASSWORD\n"
            "      - API_KEY\n"
            "    env_overrides:\n"
            "      dev: {replicas: 1, cpu: '250m'}\n"
            "      staging: {replicas: 2, cpu: '500m'}\n"
            "      prod: {replicas: 3, cpu: '750m'}\n"
        )
        manifest = parse_manifest(str(manifest_path))
        svc = manifest.services[0]
        assert svc.secrets == ["DB_PASSWORD", "API_KEY"]

    def test_parse_no_secrets_defaults_empty(self, tmpdir):
        """Services without a secrets key default to an empty list."""
        manifest_path = Path(tmpdir) / "svc.yaml"
        manifest_path.write_text(
            "services:\n"
            "  - name: web\n"
            "    port: 8080\n"
            "    env_overrides:\n"
            "      dev: {replicas: 1, cpu: '250m'}\n"
            "      staging: {replicas: 2, cpu: '500m'}\n"
            "      prod: {replicas: 3, cpu: '750m'}\n"
        )
        manifest = parse_manifest(str(manifest_path))
        assert manifest.services[0].secrets == []


class TestSecretsValidator:
    def test_valid_secret_names(self):
        svc = _svc("a")
        svc.secrets = ["DB_PASSWORD", "API_KEY", "X"]
        errors = validate_manifest(Manifest(services=[svc]))
        real_errors = [e for e in errors if e.severity == "error"]
        assert not any("secret" in e.message.lower() for e in real_errors)

    def test_invalid_secret_name_lowercase(self):
        svc = _svc("a")
        svc.secrets = ["db_password"]
        errors = validate_manifest(Manifest(services=[svc]))
        real_errors = [e for e in errors if e.severity == "error"]
        assert any("invalid secret name" in e.message for e in real_errors)

    def test_invalid_secret_name_starts_with_digit(self):
        svc = _svc("a")
        svc.secrets = ["3SECRET"]
        errors = validate_manifest(Manifest(services=[svc]))
        real_errors = [e for e in errors if e.severity == "error"]
        assert any("invalid secret name" in e.message for e in real_errors)

    def test_invalid_secret_name_with_hyphen(self):
        svc = _svc("a")
        svc.secrets = ["DB-PASSWORD"]
        errors = validate_manifest(Manifest(services=[svc]))
        real_errors = [e for e in errors if e.severity == "error"]
        assert any("invalid secret name" in e.message for e in real_errors)

    def test_duplicate_secret_names(self):
        svc = _svc("a")
        svc.secrets = ["DB_PASSWORD", "DB_PASSWORD"]
        errors = validate_manifest(Manifest(services=[svc]))
        real_errors = [e for e in errors if e.severity == "error"]
        assert any("duplicate secret names" in e.message for e in real_errors)

    def test_empty_secrets_no_error(self):
        svc = _svc("a")
        svc.secrets = []
        errors = validate_manifest(Manifest(services=[svc]))
        real_errors = [e for e in errors if e.severity == "error"]
        assert not any("secret" in e.message.lower() for e in real_errors)


class TestSecretsTerraform:
    def test_secrets_manager_resources_created(self, tmpdir):
        """Each secret gets a SecretsManager secret + version."""
        svc = _svc("web")
        svc.secrets = ["DB_PASSWORD", "API_KEY"]
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "web.tf.json").read_text())
        resources = tf["resource"]
        # Secret resources (nested format: aws_secretsmanager_secret -> {name: config})
        sm_secrets = resources.get("aws_secretsmanager_secret", {})
        assert "web_db_password" in sm_secrets
        assert "web_api_key" in sm_secrets
        # Version resources
        sm_versions = resources.get("aws_secretsmanager_secret_version", {})
        assert "web_db_password" in sm_versions
        assert "web_api_key" in sm_versions

    def test_secrets_manager_name_includes_env(self, tmpdir):
        """Secret name follows <service>/<env>/<SECRET_NAME> pattern."""
        svc = _svc("web")
        svc.secrets = ["DB_PASSWORD"]
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "web.tf.json").read_text())
        sm = tf["resource"]["aws_secretsmanager_secret"]["web_db_password"]
        assert sm["name"] == "web/prod/DB_PASSWORD"

    def test_secrets_iam_policy_created(self, tmpdir):
        """An IAM policy for reading secrets is created."""
        svc = _svc("web")
        svc.secrets = ["DB_PASSWORD"]
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "web.tf.json").read_text())
        resources = tf["resource"]
        policy = resources["aws_iam_policy"]["web_secrets"]
        stmt = json.loads(policy["policy"])["Statement"][0]
        assert "secretsmanager:GetSecretValue" in stmt["Action"]
        assert len(stmt["Resource"]) == 1

    def test_no_secrets_no_sm_resources(self, tmpdir):
        """Services without secrets don't get SecretsManager resources."""
        manifest = Manifest(services=[_svc("web")])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "web.tf.json").read_text())
        resources = tf["resource"]
        sm_keys = [k for k in resources if "secretsmanager" in k]
        assert sm_keys == []

    def test_secrets_version_placeholder(self, tmpdir):
        """Secret versions use CHANGE_ME as placeholder."""
        svc = _svc("web")
        svc.secrets = ["TOKEN"]
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "web.tf.json").read_text())
        version = tf["resource"]["aws_secretsmanager_secret_version"]["web_token"]
        assert version["secret_string"] == "CHANGE_ME"


class TestSecretsKubernetes:
    def test_secret_resource_created(self, tmpdir):
        """K8s Secret resource is created when service has secrets."""
        svc = _svc("web")
        svc.secrets = ["DB_PASSWORD", "API_KEY"]
        manifest = Manifest(services=[svc])
        generate_kubernetes(manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "web.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        # Deployment, Service, NetworkPolicy, HPA, Secret
        assert len(docs) == 5
        secret = docs[4]
        assert secret["kind"] == "Secret"
        assert secret["type"] == "Opaque"
        assert "DB_PASSWORD" in secret["data"]
        assert "API_KEY" in secret["data"]

    def test_secret_namespace_matches_env(self, tmpdir):
        svc = _svc("web")
        svc.secrets = ["TOKEN"]
        manifest = Manifest(services=[svc])
        generate_kubernetes(manifest, tmpdir)
        for env in ["dev", "staging", "prod"]:
            path = Path(tmpdir) / "kubernetes" / env / "web.yaml"
            docs = list(yaml.safe_load_all(path.read_text()))
            secret = docs[4]
            assert secret["metadata"]["namespace"] == env

    def test_deployment_envfrom_references_secret(self, tmpdir):
        """Deployment container has envFrom referencing the secret."""
        svc = _svc("web")
        svc.secrets = ["DB_PASSWORD"]
        manifest = Manifest(services=[svc])
        generate_kubernetes(manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "web.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        container = docs[0]["spec"]["template"]["spec"]["containers"][0]
        assert "envFrom" in container
        assert container["envFrom"][0]["secretRef"]["name"] == "web-secrets"

    def test_no_secrets_no_secret_resource(self, tmpdir):
        """Services without secrets don't get a Secret resource."""
        manifest = Manifest(services=[_svc("web")])
        generate_kubernetes(manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "web.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        assert len(docs) == 4  # Deployment, Service, NetworkPolicy, HPA
        kinds = [d["kind"] for d in docs]
        assert "Secret" not in kinds

    def test_no_secrets_no_envfrom(self, tmpdir):
        """Containers without secrets don't have envFrom."""
        manifest = Manifest(services=[_svc("web")])
        generate_kubernetes(manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "web.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        container = docs[0]["spec"]["template"]["spec"]["containers"][0]
        assert "envFrom" not in container

    def test_secret_data_placeholder(self, tmpdir):
        """Secret data values are base64 CHANGE_ME placeholders."""
        svc = _svc("web")
        svc.secrets = ["TOKEN"]
        manifest = Manifest(services=[svc])
        generate_kubernetes(manifest, tmpdir)
        path = Path(tmpdir) / "kubernetes" / "prod" / "web.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        secret = docs[4]
        assert secret["data"]["TOKEN"] == "Q0hBTkdFX01F"


class TestSecretsCost:
    def test_cost_includes_secrets(self):
        """Each secret adds $0.40/mo."""
        svc = _svc("web")
        svc.secrets = ["DB_PASSWORD", "API_KEY"]
        costs = estimate_costs(Manifest(services=[svc]))
        # dev: 1*7.49 + 2*0.40 = 8.29
        assert costs["dev"]["web"] == 8.29

    def test_cost_no_secrets(self):
        """No secrets means no extra cost."""
        costs = estimate_costs(Manifest(services=[_svc("web")]))
        assert costs["dev"]["web"] == 7.49


class TestSecretsDrift:
    def test_forward_drift_secrets_added(self, tmpdir):
        """Adding secrets to a service is detected as forward drift."""
        manifest = Manifest(services=[_svc("web")])
        generate_terraform(manifest, tmpdir)
        generate_kubernetes(manifest, tmpdir)
        svc = _svc("web")
        svc.secrets = ["DB_PASSWORD"]
        modified = Manifest(services=[svc])
        report = detect_drift(modified, tmpdir)
        forward = report["forward"]
        assert any(
            item["service"] == "web" and "Secrets Manager resources will be added" in item["reason"]
            for item in forward
        )

    def test_forward_drift_secrets_removed(self, tmpdir):
        """Removing secrets from a service is detected as forward drift."""
        svc = _svc("web")
        svc.secrets = ["DB_PASSWORD"]
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        generate_kubernetes(manifest, tmpdir)
        clean = Manifest(services=[_svc("web")])
        report = detect_drift(clean, tmpdir)
        forward = report["forward"]
        assert any(
            item["service"] == "web"
            and "Secrets Manager resources will be removed" in item["reason"]
            for item in forward
        )


class TestSecretsCLI:
    def test_validate_sample_with_secrets(self):
        """sample.yaml (which now has secrets) validates cleanly."""
        rc = main(["sample.yaml", "--validate"])
        assert rc == 0

    def test_generate_with_secrets(self, tmpdir):
        """Full generation with secrets succeeds."""
        rc = main(["sample.yaml", "-o", tmpdir])
        assert rc == 0
        # Verify secrets manager resources in terraform
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api-gateway.tf.json").read_text())
        resources = tf["resource"]
        assert any("secretsmanager" in k for k in resources)
        # Verify k8s secret resource
        path = Path(tmpdir) / "kubernetes" / "prod" / "api-gateway.yaml"
        docs = list(yaml.safe_load_all(path.read_text()))
        kinds = [d["kind"] for d in docs]
        assert "Secret" in kinds


# ---- Multi-region tests ----


class TestMultiRegion:
    def test_single_region_default(self):
        """Default manifest has single region us-east-1."""
        manifest = Manifest(services=[_svc("api")])
        assert manifest.regions == ["us-east-1"]

    def test_single_region_flat_structure(self, tmpdir):
        """Single region uses flat directory structure (no region subdir)."""
        manifest = Manifest(services=[_svc("api")])
        generate_terraform(manifest, tmpdir)
        # Should be terraform/<env>/ not terraform/<region>/<env>/
        assert (Path(tmpdir) / "terraform" / "prod" / "api.tf.json").exists()
        assert not (Path(tmpdir) / "terraform" / "us-east-1").exists()

    def test_multi_region_directory_structure(self, tmpdir):
        """Multi-region creates region subdirectories."""
        manifest = Manifest(
            services=[_svc("api")],
            regions=["us-east-1", "eu-west-1"],
        )
        generate_terraform(manifest, tmpdir)
        assert (Path(tmpdir) / "terraform" / "us-east-1" / "prod" / "api.tf.json").exists()
        assert (Path(tmpdir) / "terraform" / "eu-west-1" / "prod" / "api.tf.json").exists()

    def test_multi_region_file_count(self, tmpdir):
        """Multi-region doubles the file count for 2 regions."""
        manifest = Manifest(
            services=[_svc("api")],
            regions=["us-east-1", "eu-west-1"],
        )
        tf_files = generate_terraform(manifest, tmpdir)
        # 2 regions * 3 envs * (3 infra + 1 service) = 24
        assert len(tf_files) == 24

    def test_multi_region_provider_uses_correct_region(self, tmpdir):
        """Each region directory has provider configured for that region."""
        manifest = Manifest(
            services=[_svc("api")],
            regions=["us-east-1", "eu-west-1"],
        )
        generate_terraform(manifest, tmpdir)
        for region in ["us-east-1", "eu-west-1"]:
            provider = json.loads(
                (Path(tmpdir) / "terraform" / region / "prod" / "provider.tf.json").read_text()
            )
            assert provider["provider"]["aws"]["region"] == region

    def test_multi_region_backend_uses_correct_region(self, tmpdir):
        """Each region has its own state bucket scoped to that region."""
        manifest = Manifest(
            services=[_svc("api")],
            regions=["us-east-1", "eu-west-1"],
        )
        generate_terraform(manifest, tmpdir)
        for region in ["us-east-1", "eu-west-1"]:
            backend = json.loads(
                (Path(tmpdir) / "terraform" / region / "prod" / "backend.tf.json").read_text()
            )
            s3 = backend["terraform"]["backend"]["s3"]
            assert s3["region"] == region
            assert region in s3["bucket"]

    def test_multi_region_kubernetes_structure(self, tmpdir):
        """Multi-region K8s creates region subdirectories."""
        manifest = Manifest(
            services=[_svc("api")],
            regions=["us-east-1", "eu-west-1"],
        )
        generate_kubernetes(manifest, tmpdir)
        assert (Path(tmpdir) / "kubernetes" / "us-east-1" / "prod" / "api.yaml").exists()
        assert (Path(tmpdir) / "kubernetes" / "eu-west-1" / "prod" / "api.yaml").exists()

    def test_multi_region_k8s_region_label(self, tmpdir):
        """K8s manifests include region label."""
        manifest = Manifest(
            services=[_svc("api", health="/healthz")],
            regions=["us-east-1", "eu-west-1"],
        )
        generate_kubernetes(manifest, tmpdir)
        for region in ["us-east-1", "eu-west-1"]:
            path = Path(tmpdir) / "kubernetes" / region / "prod" / "api.yaml"
            docs = list(yaml.safe_load_all(path.read_text()))
            labels = docs[0]["metadata"]["labels"]
            assert labels["region"] == region

    def test_multi_region_cost_multiplier(self):
        """Costs are multiplied by region count."""
        single = Manifest(services=[_svc("api")], regions=["us-east-1"])
        multi = Manifest(services=[_svc("api")], regions=["us-east-1", "eu-west-1"])
        single_costs = estimate_costs(single)
        multi_costs = estimate_costs(multi)
        assert multi_costs["prod"]["api"] == single_costs["prod"]["api"] * 2

    def test_multi_region_drift_detection(self, tmpdir):
        """Drift detection works with multi-region directory structure."""
        manifest = Manifest(
            services=[_svc("api")],
            regions=["us-east-1", "eu-west-1"],
        )
        generate_terraform(manifest, tmpdir)
        generate_kubernetes(manifest, tmpdir)
        report = detect_drift(manifest, tmpdir)
        assert len(report["forward"]) == 0
        assert len(report["reverse"]) == 0


class TestRegionValidation:
    def test_valid_regions(self):
        """Valid AWS region names pass validation."""
        manifest = Manifest(
            services=[_svc("api")],
            regions=["us-east-1", "eu-west-1", "ap-southeast-2"],
        )
        errors = validate_manifest(manifest)
        region_errors = [e for e in errors if "region" in e.message.lower()]
        assert len(region_errors) == 0

    def test_invalid_region_format(self):
        """Invalid region names are flagged."""
        manifest = Manifest(
            services=[_svc("api")],
            regions=["not-a-region"],
        )
        errors = validate_manifest(manifest)
        region_errors = [e for e in errors if "region" in e.message.lower()]
        assert len(region_errors) == 1

    def test_empty_regions_error(self):
        """Empty regions list is flagged."""
        manifest = Manifest(services=[_svc("api")], regions=[])
        errors = validate_manifest(manifest)
        region_errors = [e for e in errors if "region" in e.message.lower()]
        assert len(region_errors) == 1

    def test_duplicate_regions_error(self):
        """Duplicate regions are flagged."""
        manifest = Manifest(
            services=[_svc("api")],
            regions=["us-east-1", "us-east-1"],
        )
        errors = validate_manifest(manifest)
        region_errors = [e for e in errors if "duplicate" in e.message.lower()]
        assert len(region_errors) == 1


# ---- State-aware drift tests ----


class TestStateDrift:
    def test_no_state_file_all_needs_apply(self, tmpdir):
        """When no state file exists, all resources are reported as needing apply."""
        from infra_gen.state import detect_state_drift

        manifest = Manifest(services=[_svc("api")])
        generate_terraform(manifest, tmpdir)
        report = detect_state_drift(tmpdir, "prod")
        assert len(report["missing_in_state"]) > 0
        assert len(report["missing_in_manifest"]) == 0

    def test_extract_resource_addresses(self):
        """extract_resource_addresses parses Terraform state format."""
        from infra_gen.state import extract_resource_addresses

        state = {
            "resources": [
                {"type": "aws_ecs_service", "name": "my_svc", "instances": []},
                {"type": "aws_security_group", "name": "my_sg", "instances": []},
            ]
        }
        addresses = extract_resource_addresses(state)
        assert "aws_ecs_service.my_svc" in addresses
        assert "aws_security_group.my_sg" in addresses

    def test_compare_state_finds_missing(self):
        """compare_state detects resources missing from state (nested format)."""
        from infra_gen.state import compare_state

        manifest_resources = {
            "aws_ecs_service": {"api": {"name": "api-prod"}},
            "aws_security_group": {"api": {"name": "api-sg"}},
        }
        state = {
            "resources": [
                {"type": "aws_ecs_service", "name": "api", "instances": []},
            ]
        }
        result = compare_state(manifest_resources, state)
        # SG is in manifest but not in state
        missing_addrs = [r["address"] for r in result["missing_in_state"]]
        assert "aws_security_group.api" in missing_addrs

    def test_compare_state_finds_orphaned(self):
        """compare_state detects resources in state but not in manifest."""
        from infra_gen.state import compare_state

        manifest_resources = {
            "aws_ecs_service": {"api": {"name": "api-prod"}},
        }
        state = {
            "resources": [
                {"type": "aws_ecs_service", "name": "api", "instances": []},
                {"type": "aws_rds_instance", "name": "old_db", "instances": []},
            ]
        }
        result = compare_state(manifest_resources, state)
        orphaned_addrs = [r["address"] for r in result["missing_in_manifest"]]
        assert "aws_rds_instance.old_db" in orphaned_addrs

    def test_read_state_local(self, tmpdir):
        """read_state reads a local .tfstate file."""
        from infra_gen.state import read_state

        state_data = {
            "version": 4,
            "resources": [{"type": "aws_ecs_service", "name": "test", "instances": []}],
        }
        state_path = Path(tmpdir) / "terraform.tfstate"
        state_path.write_text(json.dumps(state_data))
        result = read_state(tmpdir)
        assert len(result["resources"]) == 1

    def test_read_state_missing_file(self, tmpdir):
        """read_state returns empty dict when no state file exists."""
        from infra_gen.state import read_state

        result = read_state(tmpdir)
        assert result == {}

    def test_empty_state_resources(self):
        """extract_resource_addresses handles empty state."""
        from infra_gen.state import extract_resource_addresses

        assert extract_resource_addresses({}) == set()
        assert extract_resource_addresses({"resources": []}) == set()

    def test_s3_param_validation(self):
        """read_state_from_s3 rejects unsafe bucket/key names."""
        from infra_gen.state import read_state_from_s3

        with pytest.raises(ValueError, match="Invalid bucket"):
            read_state_from_s3("bucket; rm -rf /", "key")
        with pytest.raises(ValueError, match="Invalid key"):
            read_state_from_s3("bucket", "key$(whoami)")
        with pytest.raises(ValueError, match="Invalid region"):
            read_state_from_s3("bucket", "key", region="; echo pwned")

    def test_no_state_addresses_format(self, tmpdir):
        """Addresses from no-state path use correct type.name format."""
        from infra_gen.state import detect_state_drift

        manifest = Manifest(services=[_svc("api")])
        generate_terraform(manifest, tmpdir)
        report = detect_state_drift(tmpdir, "prod")
        for item in report["missing_in_state"]:
            addr = item["address"]
            parts = addr.split(".")
            assert len(parts) == 2, f"Address {addr} should be type.name format"
            assert parts[0].startswith("aws_"), f"Type {parts[0]} should start with aws_"


# ---- Item 8: DB auto-generated secret tests ----


class TestDBAutoSecret:
    """Tests for auto-generated DB password secrets."""

    def test_db_generates_secret(self, tmpdir):
        """A service with a database gets an auto-generated DB password secret."""
        svc = _svc("auth", db="postgres")
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        resources = tf["resource"]
        assert "auth_db_password" in resources["aws_secretsmanager_secret"]
        secret = resources["aws_secretsmanager_secret"]["auth_db_password"]
        assert "DB_PASSWORD_GENERATED" in secret["name"]

    def test_db_secret_version_has_change_me(self, tmpdir):
        """Auto-generated DB password secret version has CHANGE_ME placeholder."""
        svc = _svc("auth", db="postgres")
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        version = tf["resource"]["aws_secretsmanager_secret_version"]["auth_db_password"]
        assert version["secret_string"] == "CHANGE_ME"

    def test_db_instance_references_password_secret(self, tmpdir):
        """RDS instance password references the auto-generated secret."""
        svc = _svc("auth", db="postgres")
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "auth.tf.json").read_text())
        db = tf["resource"]["aws_db_instance"]["auth"]
        assert "auth_db_password" in db["password"]


# ---- Item 9: MySQL and memcached port tests ----


class TestMySQLMemcachedPorts:
    """Tests for MySQL port 3306 and memcached port 11211."""

    def test_mysql_port_3306(self, tmpdir):
        """MySQL database security group uses port 3306."""
        svc = _svc("api", db="mysql")
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        db_sg = tf["resource"]["aws_security_group"]["api_db"]
        assert db_sg["ingress"][0]["from_port"] == 3306
        assert db_sg["ingress"][0]["to_port"] == 3306

    def test_mysql_engine(self, tmpdir):
        """MySQL database uses mysql engine and version 8.0."""
        svc = _svc("api", db="mysql")
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        db = tf["resource"]["aws_db_instance"]["api"]
        assert db["engine"] == "mysql"
        assert db["engine_version"] == "8.0"

    def test_memcached_port_11211(self, tmpdir):
        """Memcached cache security group uses port 11211."""
        svc = _svc("api", cache="memcached")
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        cache_sg = tf["resource"]["aws_security_group"]["api_cache"]
        assert cache_sg["ingress"][0]["from_port"] == 11211
        assert cache_sg["ingress"][0]["to_port"] == 11211

    def test_memcached_engine(self, tmpdir):
        """Memcached cache uses memcached engine."""
        svc = _svc("api", cache="memcached")
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        cache = tf["resource"]["aws_elasticache_cluster"]["api"]
        assert cache["engine"] == "memcached"


# ---- Item 10: Multiple secrets → single IAM policy ----


class TestMultipleSecretsPolicy:
    """Tests for multiple secrets producing a single IAM policy."""

    def test_multiple_secrets_single_policy(self, tmpdir):
        """Multiple secrets produce one IAM policy with all ARNs."""
        svc = _svc("api")
        svc.secrets = ["DB_PASSWORD", "API_KEY", "JWT_SECRET"]
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        resources = tf["resource"]
        # Should have exactly one IAM policy for secrets
        assert "api_secrets" in resources["aws_iam_policy"]
        policy_doc = json.loads(resources["aws_iam_policy"]["api_secrets"]["policy"])
        arns = policy_doc["Statement"][0]["Resource"]
        assert len(arns) == 3

    def test_each_secret_gets_sm_resource(self, tmpdir):
        """Each declared secret gets its own SecretsManager secret resource."""
        svc = _svc("api")
        svc.secrets = ["DB_PASSWORD", "API_KEY"]
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        sm = tf["resource"]["aws_secretsmanager_secret"]
        assert "api_db_password" in sm
        assert "api_api_key" in sm


# ---- Item 11: ECS service always created ----


class TestECSServiceAlwaysCreated:
    """Tests that ECS service is always created even without env_overrides."""

    def test_ecs_service_without_env_overrides(self, tmpdir):
        """ECS service is created with default replicas when env_overrides is missing."""
        svc = Service(
            name="minimal",
            port=8080,
            dependencies=[],
            db_type="none",
            cache="none",
            exposure="internal",
            env_overrides={},
        )
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "minimal.tf.json").read_text())
        assert "aws_ecs_service" in tf["resource"]
        ecs = tf["resource"]["aws_ecs_service"]["minimal"]
        assert ecs["desired_count"] == 1


# ---- Item 12: Parser with malformed env_overrides ----


class TestParserMalformedEnvOverrides:
    """Tests for parser handling of malformed env_overrides."""

    def test_parser_missing_replicas_key(self, tmpdir):
        """Parser raises error when env_overrides entry missing replicas."""
        manifest_data = {
            "services": [
                {
                    "name": "api",
                    "port": 8080,
                    "env_overrides": {
                        "dev": {"cpu": "250m"},  # missing replicas
                    },
                }
            ]
        }
        path = Path(tmpdir) / "bad.yaml"
        path.write_text(yaml.dump(manifest_data))
        with pytest.raises(KeyError):
            parse_manifest(str(path))

    def test_parser_missing_cpu_key(self, tmpdir):
        """Parser raises error when env_overrides entry missing cpu."""
        manifest_data = {
            "services": [
                {
                    "name": "api",
                    "port": 8080,
                    "env_overrides": {
                        "dev": {"replicas": 1},  # missing cpu
                    },
                }
            ]
        }
        path = Path(tmpdir) / "bad.yaml"
        path.write_text(yaml.dump(manifest_data))
        with pytest.raises(KeyError):
            parse_manifest(str(path))

    def test_parser_env_overrides_not_dict(self, tmpdir):
        """Parser handles env_overrides that is not a dict."""
        manifest_data = {
            "services": [
                {
                    "name": "api",
                    "port": 8080,
                    "env_overrides": "invalid",
                }
            ]
        }
        path = Path(tmpdir) / "bad.yaml"
        path.write_text(yaml.dump(manifest_data))
        with pytest.raises((TypeError, AttributeError)):
            parse_manifest(str(path))


# ---- Item 15: Service name validation tests ----


class TestServiceNameValidation:
    """Tests for service name format validation."""

    def test_valid_service_name(self):
        """Valid service names pass validation."""
        svc = _svc("my-api")
        manifest = Manifest(services=[svc])
        errors = validate_manifest(manifest)
        name_errors = [e for e in errors if "invalid name" in e.message]
        assert len(name_errors) == 0

    def test_invalid_name_with_dots(self):
        """Service name with dots fails validation."""
        svc = _svc("my.api")
        manifest = Manifest(services=[svc])
        errors = validate_manifest(manifest)
        name_errors = [e for e in errors if "invalid name" in e.message]
        assert len(name_errors) == 1

    def test_invalid_name_with_uppercase(self):
        """Service name with uppercase fails validation."""
        svc = _svc("MyApi")
        manifest = Manifest(services=[svc])
        errors = validate_manifest(manifest)
        name_errors = [e for e in errors if "invalid name" in e.message]
        assert len(name_errors) == 1

    def test_invalid_name_starting_with_number(self):
        """Service name starting with number fails validation."""
        svc = _svc("1api")
        manifest = Manifest(services=[svc])
        errors = validate_manifest(manifest)
        name_errors = [e for e in errors if "invalid name" in e.message]
        assert len(name_errors) == 1

    def test_invalid_name_with_spaces(self):
        """Service name with spaces fails validation."""
        svc = _svc("my api")
        manifest = Manifest(services=[svc])
        errors = validate_manifest(manifest)
        name_errors = [e for e in errors if "invalid name" in e.message]
        assert len(name_errors) == 1


# ---- Item 7: DB subnet group name tests ----


class TestDBSubnetGroupName:
    """Tests for db_subnet_group_name variable and RDS reference."""

    def test_variables_include_db_subnet_group(self, simple_manifest, tmpdir):
        """Variables file includes db_subnet_group_name."""
        generate_terraform(simple_manifest, tmpdir)
        path = Path(tmpdir) / "terraform" / "prod" / "variables.tf.json"
        content = json.loads(path.read_text())
        assert "db_subnet_group_name" in content["variable"]

    def test_rds_references_db_subnet_group(self, tmpdir):
        """RDS instance references db_subnet_group_name variable."""
        svc = _svc("api", db="postgres")
        manifest = Manifest(services=[svc])
        generate_terraform(manifest, tmpdir)
        tf = json.loads((Path(tmpdir) / "terraform" / "prod" / "api.tf.json").read_text())
        db = tf["resource"]["aws_db_instance"]["api"]
        assert db["db_subnet_group_name"] == "${var.db_subnet_group_name}"
