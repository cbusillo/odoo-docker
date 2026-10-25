import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, NamedTuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RETENTION_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ghcr-retention.yml"
RETENTION_HARNESS = REPO_ROOT / "tests" / "ghcr_retention_harness.js"
OUTSIDE_RETENTION_WINDOW = "2020-01-01T00:00:00Z"


def load_workflow(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as workflow_file:
        return yaml.safe_load(workflow_file)


class RetentionRun(NamedTuple):
    deleted_ids: set[int]
    failure: str | None


def package_version(version_id: int, digest: str, *tags: str) -> dict[str, Any]:
    return {
        "id": version_id,
        "name": digest,
        "created_at": OUTSIDE_RETENTION_WINDOW,
        "metadata": {"container": {"tags": list(tags)}},
    }


def manifest_inspection(manifest: dict[str, Any]) -> dict[str, Any]:
    return {"exitCode": 0, "stdout": json.dumps(manifest)}


def run_retention(scenario: dict[str, Any]) -> RetentionRun:
    job = load_workflow(RETENTION_WORKFLOW)["jobs"]["cleanup"]
    script_step = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/github-script@"))
    with tempfile.TemporaryDirectory() as temporary_directory:
        script_path = Path(temporary_directory) / "retention.js"
        script_path.write_text(script_step["with"]["script"], encoding="utf-8")
        scenario_path = Path(temporary_directory) / "scenario.json"
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        result = subprocess.run(
            ["node", str(RETENTION_HARNESS), str(script_path), str(scenario_path)],
            env={"PATH": os.environ["PATH"], **job["env"], "DRY_RUN": "false"},
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    outcome = json.loads(result.stdout)
    return RetentionRun(deleted_ids=set(outcome["deletedIds"]), failure=outcome["failure"])


class GhcrRetentionTest(unittest.TestCase):
    INDEX = "sha256:index"
    CHILD_IMAGE = "sha256:child-image"
    CHILD_ATTESTATION = "sha256:child-attestation"
    RUNTIME_CACHE = "sha256:runtime-cache"
    ORPHAN_ID = 8

    def published_package(self) -> dict[str, Any]:
        return {
            "versions": [
                {**package_version(1, self.INDEX), "created_at": "2999-01-01T00:00:00Z"},
                package_version(2, self.CHILD_IMAGE),
                package_version(3, self.CHILD_ATTESTATION),
                {**package_version(5, self.RUNTIME_CACHE), "created_at": "2999-01-01T00:00:00Z"},
                package_version(self.ORPHAN_ID, "sha256:orphan"),
            ],
            "rawInspections": {
                self.INDEX: manifest_inspection(
                    {
                        "manifests": [
                            {"digest": self.CHILD_IMAGE, "mediaType": "application/vnd.oci.image.manifest.v1+json"},
                            {"digest": self.CHILD_ATTESTATION, "mediaType": "application/vnd.oci.image.manifest.v1+json"},
                        ]
                    }
                ),
                self.RUNTIME_CACHE: manifest_inspection({"layers": []}),
            },
        }

    def test_retention_keeps_retained_indexes_their_children_and_build_caches(self) -> None:
        run = run_retention(self.published_package())

        self.assertIsNone(run.failure)
        self.assertEqual(run.deleted_ids, {self.ORPHAN_ID})

    def test_retention_protects_tagged_children_without_applying_root_tag_policy(self) -> None:
        scenario = self.published_package()
        scenario["versions"][1]["metadata"]["container"]["tags"] = ["fixture-label"]
        run = run_retention(scenario)
        self.assertIsNone(run.failure)
        self.assertEqual(run.deleted_ids, {self.ORPHAN_ID})

    def test_retention_follows_nested_indexes_but_never_cache_layer_blobs(self) -> None:
        scenario = self.published_package()
        nested = "sha256:nested"
        scenario["versions"].append(package_version(9, nested))
        scenario["rawInspections"][self.INDEX] = manifest_inspection(
            {"manifests": [{"digest": nested, "mediaType": "application/vnd.oci.image.index.v1+json"}]}
        )
        scenario["rawInspections"][nested] = self.published_package()["rawInspections"][self.INDEX]
        scenario["rawInspections"][self.RUNTIME_CACHE] = manifest_inspection(
            {
                "manifests": [
                    {"digest": "sha256:blob", "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"},
                    {"digest": "sha256:config", "mediaType": "application/vnd.buildkit.cacheconfig.v0"},
                ]
            }
        )
        run = run_retention(scenario)
        self.assertIsNone(run.failure)
        self.assertEqual(run.deleted_ids, {self.ORPHAN_ID})

    def test_retention_reports_already_missing_children_and_protects_recoverable_children(self) -> None:
        scenario = self.published_package()
        scenario["versions"] = [version for version in scenario["versions"] if version["name"] != self.CHILD_IMAGE]
        scenario["childInspections"] = {self.CHILD_IMAGE: {"exitCode": 1, "stderr": "manifest unknown"}}
        run = run_retention(scenario)
        self.assertIsNone(run.failure)
        self.assertEqual(run.deleted_ids, {self.ORPHAN_ID})

    def test_retention_deletes_nothing_when_a_retained_index_cannot_be_inspected(self) -> None:
        for inspection in ({"exitCode": 1, "stderr": "unauthorized"}, {"exitCode": 0, "stdout": "not a manifest"}):
            with self.subTest(inspection=inspection):
                scenario = self.published_package()
                scenario["rawInspections"][self.INDEX] = inspection

                run = run_retention(scenario)

                self.assertIsNotNone(run.failure)
                self.assertEqual(run.deleted_ids, set())

    def test_retention_deletes_nothing_when_a_retained_child_is_missing_from_inventory(self) -> None:
        for inspection in ({"exitCode": 0}, {"exitCode": 1, "stderr": "unauthorized"}):
            with self.subTest(inspection=inspection):
                scenario = self.published_package()
                scenario["versions"] = [version for version in scenario["versions"] if version["name"] != self.CHILD_IMAGE]
                scenario["childInspections"] = {self.CHILD_IMAGE: inspection}

                run = run_retention(scenario)

                self.assertIsNotNone(run.failure)
                self.assertEqual(run.deleted_ids, set())


if __name__ == "__main__":
    unittest.main()
