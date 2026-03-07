"""
Terraform state reader for state-aware drift detection.

Reads Terraform state files (``.tfstate``) from local disk or S3 backends
and extracts the list of managed resource addresses.  This allows drift
detection to compare the *desired* manifest against *actually deployed*
infrastructure, not just previously generated files.

Supported state sources
-----------------------
``local``
    Reads ``terraform.tfstate`` directly from the environment directory.

``s3``
    Fetches state from the S3 backend using ``aws s3 cp``.  Requires the
    AWS CLI to be installed and configured.

Usage::

    from infra_gen.state import read_state, compare_state

    resources = read_state("output/terraform/prod")
    drift = compare_state(manifest, resources, "prod")
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_SAFE_S3_NAME = re.compile(r"^[a-zA-Z0-9._/-]+$")


def _validate_s3_param(value: str, label: str) -> None:
    """Validate an S3 parameter to prevent command injection."""
    if not _SAFE_S3_NAME.match(value):
        raise ValueError(f"Invalid {label}: {value!r}")


def read_state(env_dir: str | Path) -> dict[str, Any]:
    """Read Terraform state from a local ``.tfstate`` file.

    Args:
        env_dir: Path to the environment directory containing
            ``terraform.tfstate``.

    Returns:
        Parsed JSON state dict, or an empty dict if no state file exists.
    """
    state_path = Path(env_dir) / "terraform.tfstate"
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text())  # type: ignore[no-any-return]


def read_state_from_s3(
    bucket: str,
    key: str,
    region: str = "us-east-1",
) -> dict[str, Any]:
    """Fetch Terraform state from an S3 backend.

    Shells out to ``aws s3 cp`` to download the state file.  Requires the
    AWS CLI to be installed and configured with appropriate credentials.

    Args:
        bucket: S3 bucket name (e.g. ``terraform-state-prod-us-east-1``).
        key: Object key (e.g. ``infra/prod/terraform.tfstate``).
        region: AWS region for the S3 bucket.

    Returns:
        Parsed JSON state dict, or an empty dict if the fetch fails.

    Raises:
        ValueError: If *bucket*, *key*, or *region* contain unsafe characters.
    """
    _validate_s3_param(bucket, "bucket")
    _validate_s3_param(key, "key")
    _validate_s3_param(region, "region")

    with tempfile.NamedTemporaryFile(suffix=".tfstate", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [
                "aws",
                "s3",
                "cp",
                f"s3://{bucket}/{key}",
                tmp_path,
                "--region",
                region,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {}
        return json.loads(Path(tmp_path).read_text())  # type: ignore[no-any-return]
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return {}
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def extract_resource_addresses(state: dict[str, Any]) -> set[str]:
    """Extract all managed resource addresses from a Terraform state dict.

    Handles both Terraform state v3 (``resources`` list with ``instances``)
    and v4 format.

    Args:
        state: Parsed Terraform state (from :func:`read_state` or
            :func:`read_state_from_s3`).

    Returns:
        Set of resource addresses like ``aws_ecs_service.my_service``.
    """
    addresses: set[str] = set()
    for resource in state.get("resources", []):
        res_type = resource.get("type", "")
        res_name = resource.get("name", "")
        if res_type and res_name:
            addresses.add(f"{res_type}.{res_name}")
    return addresses


def compare_state(
    manifest_resources: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, list[dict[str, str]]]:
    """Compare generated Terraform resource keys against actual state.

    Accepts the nested ``{type: {name: config}}`` format produced by the
    Terraform generator.

    Args:
        manifest_resources: The ``resource`` dict from a generated
            ``.tf.json`` file in nested Terraform JSON format.
        state: Parsed Terraform state dict.

    Returns:
        A dict with two keys:

        ``"missing_in_state"``
            Resources defined in the manifest but not found in state
            (never applied or deleted out-of-band).

        ``"missing_in_manifest"``
            Resources in state but not in the generated manifest
            (created out-of-band or from a previous manifest version).
    """
    state_addresses = extract_resource_addresses(state)
    manifest_addresses: set[str] = set()

    for res_type, names_dict in manifest_resources.items():
        if isinstance(names_dict, dict):
            for name in names_dict:
                manifest_addresses.add(f"{res_type}.{name}")

    missing_in_state = manifest_addresses - state_addresses
    missing_in_manifest = state_addresses - manifest_addresses

    return {
        "missing_in_state": [
            {"address": addr, "action": "needs apply"} for addr in sorted(missing_in_state)
        ],
        "missing_in_manifest": [
            {"address": addr, "action": "orphaned in state"} for addr in sorted(missing_in_manifest)
        ],
    }


def detect_state_drift(
    output_dir: str,
    env: str,
    region: str = "us-east-1",
    use_s3: bool = False,
) -> dict[str, list[dict[str, str]]]:
    """Run state-aware drift detection for a single environment.

    Reads both the generated ``.tf.json`` files and the Terraform state,
    then compares them to find resources that need applying or are orphaned.

    Args:
        output_dir: Root output directory.
        env: Environment name (``dev``, ``staging``, ``prod``).
        region: AWS region.
        use_s3: If True, fetch state from S3 instead of local files.

    Returns:
        Combined drift report with ``missing_in_state`` and
        ``missing_in_manifest`` lists.
    """
    # Check for multi-region layout first, fall back to single-region
    multi_region_dir = Path(output_dir) / "terraform" / region / env
    single_region_dir = Path(output_dir) / "terraform" / env
    env_dir = multi_region_dir if multi_region_dir.exists() else single_region_dir

    # Read state
    if use_s3:
        bucket = f"terraform-state-{env}-{region}"
        key = f"infra/{env}/terraform.tfstate"
        state = read_state_from_s3(bucket, key, region)
    else:
        state = read_state(env_dir)

    # Collect all manifest resources across service files (nested format)
    all_manifest_resources: dict[str, dict[str, Any]] = {}
    if env_dir.exists():
        for tf_file in sorted(env_dir.glob("*.tf.json")):
            if tf_file.name in ("backend.tf.json", "provider.tf.json", "variables.tf.json"):
                continue
            data = json.loads(tf_file.read_text())
            for res_type, names_dict in data.get("resource", {}).items():
                if isinstance(names_dict, dict):
                    all_manifest_resources.setdefault(res_type, {}).update(names_dict)

    if not state:
        # No state = everything is new
        return {
            "missing_in_state": [
                {"address": f"{res_type}.{name}", "action": "needs apply"}
                for res_type, names_dict in sorted(all_manifest_resources.items())
                for name in sorted(names_dict)
            ],
            "missing_in_manifest": [],
        }

    return compare_state(all_manifest_resources, state)
