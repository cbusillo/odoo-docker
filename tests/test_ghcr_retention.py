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
            env={**os.environ, **job["env"], "DRY_RUN": "false"},
            check=False,
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    outcome = json.loads(result.stdout)
    return RetentionRun(deleted_ids=set(outcome["deletedIds"]), failure=outcome["failure"])


class GhcrRetentionTest(unittest.TestCase):
    INDEX = "sha256:index"
    CHILD_IMAGE = "sha256:child-image"
    CHILD_ATTESTATION = "sha256:child-attestation"
    DEVTOOLS_INDEX = "sha256:devtools-index"
    RUNTIME_CACHE = "sha256:runtime-cache"
    DEVTOOLS_CACHE = "sha256:devtools-cache"
    STALE_PR_IMAGE_ID = 7
    ORPHAN_ID = 8

    def published_package(self) -> dict[str, Any]:
        return {
            "versions": [
                package_version(1, self.INDEX, "19.0-runtime"),
                package_version(2, self.CHILD_IMAGE),
                package_version(3, self.CHILD_ATTESTATION),
                package_version(4, self.DEVTOOLS_INDEX, "19.0-devtools"),
                package_version(5, self.RUNTIME_CACHE, "buildcache-runtime"),
                package_version(6, self.DEVTOOLS_CACHE, "buildcache-devtools"),
                package_version(self.STALE_PR_IMAGE_ID, "sha256:stale-pr-image", "pr-12-runtime"),
                package_version(self.ORPHAN_ID, "sha256:orphan"),
            ],
            "rawInspections": {
                self.INDEX: manifest_inspection({"manifests": [{"digest": self.CHILD_IMAGE}, {"digest": self.CHILD_ATTESTATION}]}),
                self.DEVTOOLS_INDEX: manifest_inspection({"manifests": []}),
                self.RUNTIME_CACHE: manifest_inspection({"layers": []}),
                self.DEVTOOLS_CACHE: manifest_inspection({"layers": []}),
            },
        }

    def test_retention_keeps_retained_indexes_their_children_and_build_caches(self) -> None:
        run = run_retention(self.published_package())

        self.assertIsNone(run.failure)
        self.assertEqual(run.deleted_ids, {self.STALE_PR_IMAGE_ID, self.ORPHAN_ID})

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
