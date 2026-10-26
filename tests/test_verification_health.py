import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())


def fake_tool(name: str, arguments: list[str]) -> int:
    if name == "sha256sum":
        for path in arguments or ["-"]:
            content = sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()
            print(f"{hashlib.sha256(content).hexdigest()}  {path}")
        return 0
    if name == "date":
        print("20000101T000000Z" if arguments[-1] == "+%Y%m%dT%H0000Z" else "2000-01-01T00:00:00Z")
        return 0
    if name == "git":
        if arguments[0] == "show":
            print(f"  TRIVY_IMAGE: {os.environ['FAKE_BASE_IMAGE']}")
            return 0
        if arguments[0] in {"fetch", "cat-file"}:
            return 0
        if arguments[0] == "diff":
            return 1
        raise ValueError(f"Unexpected Git command: {arguments}")
    if name != "docker" or arguments[0] != "run":
        raise ValueError(f"Unexpected command: {name} {arguments}")
    if any(argument.startswith("ubuntu:") for argument in arguments):
        with Path(os.environ["FAKE_DOCKER_TRACE"]).open("a") as stream:
            stream.write(json.dumps({"packages": "resolved"}) + "\n")
        print("postgresql-client-17=17.1\npostgresql-client-common=1.0\nlibpq5=17.1")
        return 0
    image = next(argument for argument in arguments if argument.startswith("aquasec/trivy:"))
    volumes = [arguments[index + 1] for index, argument in enumerate(arguments) if argument == "--volume"]
    trace = Path(os.environ["FAKE_DOCKER_TRACE"])
    if "--version" in arguments:
        print(json.dumps({"Version": image.split(":", 1)[1].split("@", 1)[0]}))
        return 0
    if "--download-db-only" in arguments:
        cache = Path(
            next(volume.removesuffix(":/root/.cache/trivy") for volume in volumes if volume.endswith(":/root/.cache/trivy"))
        )
        (cache / "db").mkdir(parents=True, exist_ok=True)
        (cache / "db/trivy.db").write_bytes(image.encode())
        event = {"download": image}
    else:
        output = Path(next(volume.removesuffix(":/output") for volume in volumes if volume.endswith(":/output")))
        report = json.loads(os.environ["FAKE_SCAN_REPORT"])
        (output / "trivy.json").write_text(json.dumps(report))
        event = {"scan": arguments[-1], "scanner": image}
    with trace.open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    return 0


def report(*, vulnerable: bool) -> dict[str, object]:
    findings = []
    if vulnerable:
        findings.append({"VulnerabilityID": "CVE-2000-1234", "PkgName": "example", "InstalledVersion": "1", "Severity": "HIGH"})
    return {
        "Metadata": {"OS": {"Family": "ubuntu", "Name": "24.04"}},
        "Results": [{"Target": "fixture", "Class": "os-pkgs", "Type": "ubuntu", "Vulnerabilities": findings}],
    }


class VerificationHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.workspace = self.root / "workspace"
        (self.workspace / "scripts").mkdir(parents=True)
        for name in ("scan-image-dependencies.sh", "image-dependency-health.py"):
            shutil.copy2(ROOT / "scripts" / name, self.workspace / "scripts" / name)
        self.baseline = self.root / "baseline"
        (self.baseline / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/image-dependency-health.py", self.baseline / "scripts/image-dependency-health.py")
        shutil.copy2(ROOT / "Dockerfile", self.workspace / "Dockerfile")
        for name in ("docker", "git", "date", "sha256sum"):
            path = self.bin / name
            path.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(__file__)} --fake-tool {name} "$@"\n')
            path.chmod(0o755)
        (self.bin / "python3").symlink_to(sys.executable)
        self.runner = self.root / "runner"
        self.runner.mkdir()
        self.output = self.root / "outputs"
        self.output.write_text("")
        self.image = WORKFLOW["env"]["TRIVY_IMAGE"]
        self.environment = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            GITHUB_WORKSPACE=str(self.workspace),
            RUNNER_TEMP=str(self.runner),
            GITHUB_OUTPUT=str(self.output),
            PYTHON_VERSION=str(WORKFLOW["env"]["PYTHON_VERSION"]),
            TRIVY_IMAGE=self.image,
            TRAIN_CANDIDATE="true",
            BASELINE_COMMIT="1" * 40,
            SOURCE_COMMIT="2" * 40,
            DEPENDENCY_HEALTH_REPOSITORY="example/images",
            FAKE_BASE_IMAGE=self.image,
            FAKE_DOCKER_TRACE=str(self.root / "docker.jsonl"),
            FAKE_SCAN_REPORT=json.dumps(report(vulnerable=False)),
        )

    def step(self, job: str, identifier: str) -> str:
        return next(step["run"] for step in WORKFLOW["jobs"][job]["steps"] if step.get("id") == identifier)

    def run_script(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script], cwd=self.workspace, env=self.environment, capture_output=True, text=True
        )

    def resolve(self, *, different_scanner: bool = False) -> dict[str, str]:
        if different_scanner:
            self.environment["FAKE_BASE_IMAGE"] = self.image.split("@", 1)[0] + "@sha256:" + "0" * 64
        result = self.run_script(self.step("resolve_inputs", "inputs"))
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        self.environment.update(
            SCANNER_PARITY="false",
            BASE_TRIVY_IMAGE=outputs["trivy_image"],
            BASE_TRIVY_CACHE_DIR=str(self.runner / "restored"),
            CANDIDATE_CONFIGURATION_SHA256=outputs.get("candidate_scan_configuration_sha256", ""),
            IMAGE_REFERENCE="fixture-image:immutable",
            SCAN_SCOPE="image:fixture:linux/amd64",
            HEALTH_ROOT=str(self.runner / "health"),
        )
        Path(self.environment["BASE_TRIVY_CACHE_DIR"]).mkdir()
        subprocess.run(
            [
                "tar",
                "-C",
                self.environment["BASE_TRIVY_CACHE_DIR"],
                "--strip-components=1",
                "-xzf",
                str(self.runner / "dependency-health-inputs/trivy-cache.tar.gz"),
            ],
            check=True,
            capture_output=True,
        )
        return outputs

    def events(self) -> list[dict[str, str]]:
        return [json.loads(line) for line in Path(self.environment["FAKE_DOCKER_TRACE"]).read_text().splitlines()]

    def snapshot(self, *, vulnerable: bool) -> None:
        root = Path(self.environment["HEALTH_ROOT"]) / "candidate"
        root.mkdir(parents=True, exist_ok=True)
        (root / "trivy.json").write_text(json.dumps(report(vulnerable=vulnerable)))
        profile = json.loads((self.runner / "dependency-health-inputs/comparison-inputs.json").read_text())
        provenance = {
            "repository": self.environment["DEPENDENCY_HEALTH_REPOSITORY"],
            "source_commit": self.environment["SOURCE_COMMIT"],
            "baseline_commit": self.environment["BASELINE_COMMIT"],
            "producer": "trivy",
            "producer_version": profile["scanner"]["version"],
            "advisory_source": "trivy-db",
            "advisory_revision": profile["scanner"]["databases_sha256"],
            "scan_scope": self.environment["SCAN_SCOPE"],
            "scan_configuration_sha256": hashlib.sha256(
                (self.runner / "dependency-health-inputs/comparison-inputs.json").read_bytes()
            ).hexdigest(),
        }
        (root / "provenance.json").write_text(json.dumps(provenance))
        subprocess.run(
            [
                sys.executable,
                str(self.workspace / "scripts/image-dependency-health.py"),
                "snapshot",
                "--trivy-json",
                str(root / "trivy.json"),
                "--provenance",
                str(root / "provenance.json"),
                "--output",
                str(root / "snapshot.json"),
            ],
            check=True,
            capture_output=True,
        )

    def test_same_image_reuses_resolved_database_and_scans_candidate_once(self) -> None:
        self.resolve()
        result = self.run_script(self.step("verify", "candidate_absolute"))
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        self.assertEqual([event["download"] for event in events if "download" in event], [self.image])
        self.assertEqual([event["scan"] for event in events if "scan" in event], [self.environment["IMAGE_REFERENCE"]])

    def test_changed_scanner_uses_its_own_bound_database_and_same_image(self) -> None:
        self.resolve(different_scanner=True)
        result = self.run_script(self.step("verify", "candidate_absolute"))
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        self.assertEqual(
            [event["download"] for event in events if "download" in event], [self.environment["FAKE_BASE_IMAGE"], self.image]
        )
        self.assertEqual(
            [event for event in events if "scan" in event], [{"scan": self.environment["IMAGE_REFERENCE"], "scanner": self.image}]
        )

    def test_matching_tooling_enforces_absolute_without_rescanning(self) -> None:
        self.resolve()
        self.environment["SCANNER_PARITY"] = "true"
        self.snapshot(vulnerable=False)
        passed = self.run_script(self.step("verify", "candidate_absolute"))
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.snapshot(vulnerable=True)
        result = self.run_script(self.step("verify", "candidate_absolute"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("scan" in event for event in self.events()))

    def test_head_evaluator_rejects_while_base_evaluator_passes(self) -> None:
        self.resolve()
        self.snapshot(vulnerable=False)
        self.environment.update(SCANNER_AVAILABLE="true", BASELINE_ROOT=str(self.baseline))
        base = self.run_script(self.step("verify", "trusted_absolute"))
        self.assertEqual(base.returncode, 0, base.stderr)
        self.environment["FAKE_SCAN_REPORT"] = json.dumps(report(vulnerable=True))
        head = self.run_script(self.step("verify", "candidate_absolute"))
        self.assertNotEqual(head.returncode, 0)

    def test_base_evaluator_rejects_while_head_evaluator_passes(self) -> None:
        self.resolve()
        self.snapshot(vulnerable=True)
        self.environment.update(SCANNER_AVAILABLE="true", BASELINE_ROOT=str(self.baseline))
        base = self.run_script(self.step("verify", "trusted_absolute"))
        self.assertNotEqual(base.returncode, 0)
        head = self.run_script(self.step("verify", "candidate_absolute"))
        self.assertEqual(head.returncode, 0, head.stderr)

    def test_changed_profile_fails_before_scan(self) -> None:
        self.resolve()
        (self.runner / "dependency-health-inputs/candidate-inputs.json").write_text("{}")
        result = self.run_script(self.step("verify", "candidate_absolute"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("scan" in event for event in self.events()))

    def test_changed_database_fails_before_scan(self) -> None:
        self.resolve()
        (Path(self.environment["BASE_TRIVY_CACHE_DIR"]) / "db/trivy.db").write_bytes(b"changed")
        result = self.run_script(self.step("verify", "candidate_absolute"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("scan" in event for event in self.events()))

    def test_changed_second_database_fails_before_scan(self) -> None:
        self.resolve(different_scanner=True)
        (self.runner / "trivy-candidate-cache/db/trivy.db").write_bytes(b"changed")
        subprocess.run(
            [
                "tar",
                "-C",
                str(self.runner),
                "-czf",
                str(self.runner / "dependency-health-inputs/candidate-trivy-cache.tar.gz"),
                "trivy-candidate-cache",
            ],
            check=True,
            capture_output=True,
        )
        result = self.run_script(self.step("verify", "candidate_absolute"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("scan" in event for event in self.events()))

    def test_source_pr_keeps_existing_single_scanner_path(self) -> None:
        self.environment["TRAIN_CANDIDATE"] = "false"
        self.resolve(different_scanner=True)
        result = self.run_script(self.step("verify", "candidate_absolute"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("scan" in event for event in self.events()))
        self.assertEqual(len([event for event in self.events() if "download" in event]), 1)

    def test_invalid_scanner_fails_before_resolving_packages(self) -> None:
        for key in ("FAKE_BASE_IMAGE", "TRIVY_IMAGE"):
            with self.subTest(scanner=key):
                original = self.environment[key]
                self.environment[key] = "not-an-immutable-scanner"
                result = self.run_script(self.step("resolve_inputs", "inputs"))
                self.environment[key] = original
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(Path(self.environment["FAKE_DOCKER_TRACE"]).exists())


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-tool"]:
        raise SystemExit(fake_tool(sys.argv[2], sys.argv[3:]))
    unittest.main()
