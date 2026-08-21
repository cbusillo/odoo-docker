#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


SCHEMA_VERSION = 1
SEVERITY_RANK = {"low": 1, "moderate": 2, "high": 3, "critical": 4}
BLOCKING_SEVERITIES = {"high", "critical"}
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ADVISORY_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._:+-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
NON_COMPARISON_PROVENANCE_FIELDS = {"source_commit", "baseline_commit"}


class ContractError(ValueError):
    pass


def read_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read valid JSON from {path}: {error}") from error


def write_json(path: str, payload: Any) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path == "-":
        sys.stdout.write(encoded)
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded)


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_advisory(value: Any) -> str:
    advisory = str(value).strip().upper()
    if not ADVISORY_PATTERN.fullmatch(advisory):
        raise ContractError(f"invalid advisory identifier: {value!r}")
    return advisory


def normalize_severity(value: Any) -> str:
    severity = str(value).strip().lower()
    if severity == "medium":
        severity = "moderate"
    if severity not in SEVERITY_RANK:
        raise ContractError(f"unsupported vulnerability severity: {value!r}")
    return severity


def normalize_ecosystem(value: Any) -> str:
    ecosystem = re.sub(r"[^a-z0-9._+-]+", "-", str(value).strip().lower()).strip("-")
    if not ecosystem:
        raise ContractError("Trivy result is missing a package ecosystem")
    return ecosystem


def normalize_manifest_path(result: dict[str, Any], ecosystem: str) -> str:
    if str(result.get("Class") or "").strip().lower() == "os-pkgs":
        return f"os/{ecosystem}"
    target = str(result.get("Target") or ecosystem).strip().lstrip("/")
    normalized = re.sub(r"[^A-Za-z0-9._/+-]+", "-", target).strip("-./")
    if not normalized:
        raise ContractError("Trivy result has no stable manifest path")
    return normalized


def require_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return value.strip()


def validate_provenance(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ContractError("provenance must be an object")
    expected_fields = {
        "repository",
        "source_commit",
        "baseline_commit",
        "producer",
        "producer_version",
        "advisory_source",
        "advisory_revision",
        "scan_scope",
        "scan_configuration_sha256",
    }
    if set(payload) != expected_fields:
        missing = sorted(expected_fields - set(payload))
        extra = sorted(set(payload) - expected_fields)
        raise ContractError(f"invalid provenance fields; missing={missing}, extra={extra}")
    normalized = {
        field: require_string(payload, field)
        for field in expected_fields
        if field != "baseline_commit"
    }
    baseline_commit = payload.get("baseline_commit")
    if not isinstance(baseline_commit, str):
        raise ContractError("baseline_commit must be a string")
    normalized["baseline_commit"] = baseline_commit.strip()
    if not COMMIT_PATTERN.fullmatch(normalized["source_commit"]):
        raise ContractError("source_commit must be a lowercase 40-character Git commit")
    if normalized["baseline_commit"] and not COMMIT_PATTERN.fullmatch(
        normalized["baseline_commit"]
    ):
        raise ContractError("baseline_commit must be empty or a lowercase 40-character Git commit")
    if not SHA256_PATTERN.fullmatch(normalized["scan_configuration_sha256"]):
        raise ContractError("scan_configuration_sha256 must be a lowercase SHA-256")
    return normalized


def normalize_snapshot(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("dependency snapshot must use schema_version 1")
    provenance = validate_provenance(payload.get("provenance"))
    generated_at = require_string(payload, "generated_at")
    try:
        parsed_generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError("generated_at must be an ISO-8601 timestamp") from error
    if parsed_generated_at.utcoffset() != timezone.utc.utcoffset(parsed_generated_at):
        raise ContractError("generated_at must use UTC")
    findings_payload = payload.get("findings")
    if not isinstance(findings_payload, list):
        raise ContractError("findings must be an array")
    findings: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str, str]] = set()
    for raw_finding in findings_payload:
        if not isinstance(raw_finding, dict):
            raise ContractError("each finding must be an object")
        advisory_id = normalize_advisory(raw_finding.get("advisory_id"))
        aliases_payload = raw_finding.get("aliases", [])
        if not isinstance(aliases_payload, list):
            raise ContractError("finding aliases must be an array")
        aliases = sorted({normalize_advisory(alias) for alias in aliases_payload})
        if advisory_id in aliases:
            raise ContractError("finding aliases cannot repeat advisory_id")
        ecosystem = require_string(raw_finding, "ecosystem").lower()
        package = require_string(raw_finding, "package")
        manifest_path = require_string(raw_finding, "manifest_path")
        severity = normalize_severity(raw_finding.get("severity"))
        versions_payload = raw_finding.get("versions")
        if not isinstance(versions_payload, list) or not versions_payload:
            raise ContractError("finding versions must be a non-empty array")
        versions = sorted({str(version).strip() for version in versions_payload if str(version).strip()})
        if not versions:
            raise ContractError("finding versions cannot contain only empty values")
        occurrence_count = raw_finding.get("occurrence_count", 1)
        if type(occurrence_count) is not int or occurrence_count < 1:
            raise ContractError("finding occurrence_count must be a positive integer")
        identity = (advisory_id, ecosystem, package, manifest_path)
        if identity in identities:
            raise ContractError(f"duplicate finding identity: {identity}")
        identities.add(identity)
        findings.append(
            {
                "advisory_id": advisory_id,
                "aliases": aliases,
                "ecosystem": ecosystem,
                "package": package,
                "versions": versions,
                "occurrence_count": occurrence_count,
                "manifest_path": manifest_path,
                "severity": severity,
            }
        )
    findings.sort(key=finding_identity)
    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": provenance,
        "generated_at": parsed_generated_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "findings": findings,
    }


def finding_identity(finding: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        finding["advisory_id"],
        finding["ecosystem"],
        finding["package"],
        finding["manifest_path"],
    )


def build_snapshot(trivy_payload: Any, provenance_payload: Any) -> dict[str, Any]:
    provenance = validate_provenance(provenance_payload)
    if not isinstance(trivy_payload, dict):
        raise ContractError("Trivy report must be an object")
    metadata = trivy_payload.get("Metadata")
    operating_system = metadata.get("OS") if isinstance(metadata, dict) else None
    if not isinstance(operating_system, dict) or not operating_system.get("Family"):
        raise ContractError("Trivy report must identify the scanned operating system")
    results = trivy_payload.get("Results", [])
    if not isinstance(results, list):
        raise ContractError("Trivy Results must be an array")
    if not any(isinstance(result, dict) and result.get("Class") == "os-pkgs" for result in results):
        raise ContractError("Trivy report must contain operating-system package coverage")
    aggregated: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for result in results:
        if not isinstance(result, dict):
            raise ContractError("Trivy result must be an object")
        vulnerabilities = result.get("Vulnerabilities") or []
        if not isinstance(vulnerabilities, list):
            raise ContractError("Trivy Vulnerabilities must be an array")
        ecosystem = normalize_ecosystem(result.get("Type") or result.get("Class") or "unknown")
        target = normalize_manifest_path(result, ecosystem)
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise ContractError("Trivy vulnerability must be an object")
            advisory_id = normalize_advisory(vulnerability.get("VulnerabilityID"))
            package = str(vulnerability.get("PkgName") or "").strip()
            installed_version = str(vulnerability.get("InstalledVersion") or "").strip()
            if not package or not installed_version:
                raise ContractError(f"Trivy finding {advisory_id} lacks package or installed version")
            severity = normalize_severity(vulnerability.get("Severity"))
            identity = (advisory_id, ecosystem, package, target)
            existing = aggregated.get(identity)
            if existing is None:
                aggregated[identity] = {
                    "advisory_id": advisory_id,
                    "aliases": [],
                    "ecosystem": ecosystem,
                    "package": package,
                    "versions": {installed_version},
                    "occurrence_count": 1,
                    "manifest_path": target,
                    "severity": severity,
                }
                continue
            existing["versions"].add(installed_version)
            existing["occurrence_count"] += 1
            if SEVERITY_RANK[severity] > SEVERITY_RANK[existing["severity"]]:
                existing["severity"] = severity
    findings = []
    for finding in aggregated.values():
        finding["versions"] = sorted(finding["versions"])
        findings.append(finding)
    generated_at = str(trivy_payload.get("CreatedAt") or "").strip()
    if not generated_at:
        generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return normalize_snapshot(
        {
            "schema_version": SCHEMA_VERSION,
            "provenance": provenance,
            "generated_at": generated_at,
            "findings": findings,
        }
    )


def require_compatible_provenance(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> None:
    baseline_provenance = baseline["provenance"]
    candidate_provenance = candidate["provenance"]
    mismatched = []
    for field in sorted(set(baseline_provenance) - NON_COMPARISON_PROVENANCE_FIELDS):
        if baseline_provenance[field] != candidate_provenance[field]:
            mismatched.append(field)
    if candidate_provenance["baseline_commit"] != baseline_provenance["source_commit"]:
        mismatched.append("candidate_baseline_commit")
    if mismatched:
        raise ContractError(
            "dependency snapshots have incompatible provenance fields: " + ", ".join(mismatched)
        )


def finding_worsened(baseline: dict[str, Any], candidate: dict[str, Any]) -> bool:
    return any(
        (
            SEVERITY_RANK[candidate["severity"]] > SEVERITY_RANK[baseline["severity"]],
            len(candidate["versions"]) > len(baseline["versions"]),
            candidate["occurrence_count"] > baseline["occurrence_count"],
        )
    )


def advisory_component(findings: list[dict[str, Any]], advisory_id: str) -> set[str]:
    component = {normalize_advisory(advisory_id)}
    while True:
        expanded = set(component)
        for finding in findings:
            finding_ids = {finding["advisory_id"], *finding["aliases"]}
            if finding_ids & component:
                expanded.update(finding_ids)
        if expanded == component:
            return component
        component = expanded


def compare_snapshots(
    baseline: dict[str, Any], candidate: dict[str, Any], target_advisories: list[str]
) -> dict[str, Any]:
    require_compatible_provenance(baseline, candidate)
    baseline_findings = {finding_identity(finding): finding for finding in baseline["findings"]}
    candidate_findings = {finding_identity(finding): finding for finding in candidate["findings"]}
    introduced = []
    worsened = []
    resolved = []
    unchanged = []
    for identity in sorted(set(baseline_findings) | set(candidate_findings)):
        baseline_finding = baseline_findings.get(identity)
        candidate_finding = candidate_findings.get(identity)
        if baseline_finding is None:
            if candidate_finding is None:
                raise AssertionError("comparison identity has no finding")
            introduced.append(candidate_finding)
        elif candidate_finding is None:
            resolved.append(baseline_finding)
        elif finding_worsened(baseline_finding, candidate_finding):
            worsened.append({"baseline": baseline_finding, "candidate": candidate_finding})
        else:
            unchanged.append({"baseline": baseline_finding, "candidate": candidate_finding})

    reason_codes: set[str] = set()
    if any(finding["severity"] in BLOCKING_SEVERITIES for finding in introduced):
        reason_codes.add("introduced_high_or_critical")
    if any(
        finding["candidate"]["severity"] in BLOCKING_SEVERITIES for finding in worsened
    ):
        reason_codes.add("worsened_to_high_or_critical")

    target_evaluations = []
    for raw_advisory in sorted(set(target_advisories)):
        advisory_id = normalize_advisory(raw_advisory)
        equivalent = advisory_component(baseline["findings"], advisory_id)
        baseline_matches = [
            finding
            for finding in baseline["findings"]
            if {finding["advisory_id"], *finding["aliases"]} & equivalent
        ]
        candidate_matches = [
            finding
            for finding in candidate["findings"]
            if {finding["advisory_id"], *finding["aliases"]} & equivalent
        ]
        if not baseline_matches:
            status = "missing_from_baseline"
            reason_codes.add("target_advisory_missing_from_baseline")
        elif candidate_matches:
            status = "unresolved"
            reason_codes.add("target_advisory_unresolved")
        else:
            status = "resolved"
        target_evaluations.append(
            {
                "advisory_id": advisory_id,
                "baseline_matches": len(baseline_matches),
                "candidate_matches": len(candidate_matches),
                "status": status,
            }
        )

    status = "fail" if reason_codes else "pass"
    return {
        "schema_version": SCHEMA_VERSION,
        "comparison": {
            "schema_version": SCHEMA_VERSION,
            "baseline_provenance": baseline["provenance"],
            "candidate_provenance": candidate["provenance"],
            "introduced": introduced,
            "worsened": worsened,
            "resolved": resolved,
            "unchanged": unchanged,
        },
        "policy": {
            "schema_version": SCHEMA_VERSION,
            "mode": "no_high_or_critical_regressions",
            "target_advisory_ids": sorted(set(target_advisories)),
        },
        "policy_evaluation": {
            "status": status,
            "reason_codes": sorted(reason_codes),
            "target_advisories": target_evaluations,
            "summary": f"dependency health regression policy {status}ed",
        },
    }


def absolute_evaluation(snapshot: dict[str, Any]) -> dict[str, Any]:
    blocking = [
        finding for finding in snapshot["findings"] if finding["severity"] in BLOCKING_SEVERITIES
    ]
    status = "fail" if blocking else "pass"
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_provenance": snapshot["provenance"],
        "generated_at": snapshot["generated_at"],
        "status": status,
        "finding_count": len(snapshot["findings"]),
        "blocking_findings": blocking,
        "summary": f"dependency health absolute policy {status}ed",
    }


def manifest_platforms(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ContractError("manifest index must be an object")
    manifests = payload.get("manifests")
    if not isinstance(manifests, list):
        raise ContractError("manifest index does not contain manifests")
    platforms: dict[str, str] = {}
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        platform = manifest.get("platform")
        digest = manifest.get("digest")
        if not isinstance(platform, dict) or not isinstance(digest, str):
            continue
        os_name = platform.get("os")
        architecture = platform.get("architecture")
        if os_name != "linux" or not isinstance(architecture, str):
            continue
        if architecture not in {"amd64", "arm64"}:
            continue
        key = f"{os_name}/{architecture}"
        if not DIGEST_PATTERN.fullmatch(digest):
            raise ContractError(f"manifest has invalid digest for {key}: {digest}")
        if key in platforms:
            raise ContractError(f"manifest has duplicate platform: {key}")
        platforms[key] = digest
    return platforms


def build_artifact_evidence(args: argparse.Namespace) -> dict[str, Any]:
    if not DIGEST_PATTERN.fullmatch(args.parent_digest):
        raise ContractError("parent digest must be sha256:<64 lowercase hex characters>")
    manifest = read_json(args.manifest_json)
    manifest_sha256 = file_sha256(args.manifest_json)
    if args.parent_digest != f"sha256:{manifest_sha256}":
        raise ContractError("parent digest does not match the exact manifest index bytes")
    platforms = manifest_platforms(manifest)
    expected_platforms = {"linux/amd64", "linux/arm64"}
    if set(platforms) != expected_platforms:
        raise ContractError(
            f"published manifest platforms must be exactly {sorted(expected_platforms)}; "
            f"found {sorted(platforms)}"
        )
    snapshots: dict[str, dict[str, Any]] = {}
    for assignment in args.platform_snapshot:
        if "=" not in assignment:
            raise ContractError("platform snapshot must use PLATFORM=PATH")
        platform, path = assignment.split("=", 1)
        if platform in snapshots:
            raise ContractError(f"duplicate platform snapshot: {platform}")
        snapshot = normalize_snapshot(read_json(path))
        child_digest = platforms.get(platform)
        if child_digest is None:
            raise ContractError(f"snapshot platform is not present in manifest: {platform}")
        scope = snapshot["provenance"]["scan_scope"]
        if child_digest not in scope or platform not in scope:
            raise ContractError(
                f"snapshot scope does not bind {platform} to child digest {child_digest}: {scope}"
            )
        snapshots[platform] = {"path": path, "snapshot": snapshot}
    if set(snapshots) != expected_platforms:
        raise ContractError("artifact evidence requires amd64 and arm64 snapshots")
    platform_evidence = []
    status = "pass"
    for platform in sorted(expected_platforms):
        snapshot_entry = snapshots[platform]
        evaluation = absolute_evaluation(snapshot_entry["snapshot"])
        if evaluation["status"] != "pass":
            status = "fail"
        platform_evidence.append(
            {
                "platform": platform,
                "digest": platforms[platform],
                "snapshot_sha256": file_sha256(snapshot_entry["path"]),
                "absolute_evaluation": evaluation,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": args.repository,
        "image": args.image,
        "parent_digest": args.parent_digest,
        "manifest_sha256": manifest_sha256,
        "build_provenance_sha256": file_sha256(args.build_provenance),
        "platforms": platform_evidence,
        "status": status,
    }


def command_snapshot(args: argparse.Namespace) -> int:
    snapshot = build_snapshot(read_json(args.trivy_json), read_json(args.provenance))
    write_json(args.output, snapshot)
    return 0


def command_compare(args: argparse.Namespace) -> int:
    baseline = normalize_snapshot(read_json(args.baseline))
    candidate = normalize_snapshot(read_json(args.candidate))
    evaluation = compare_snapshots(baseline, candidate, args.target_advisory)
    write_json(args.output, evaluation)
    return 0 if evaluation["policy_evaluation"]["status"] == "pass" else 1


def command_absolute(args: argparse.Namespace) -> int:
    snapshot = normalize_snapshot(read_json(args.snapshot))
    evaluation = absolute_evaluation(snapshot)
    write_json(args.output, evaluation)
    return 0 if evaluation["status"] == "pass" else 1


def command_artifact_evidence(args: argparse.Namespace) -> int:
    evidence = build_artifact_evidence(args)
    write_json(args.output, evidence)
    return 0 if evidence["status"] == "pass" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and evaluate Odoo image dependency health evidence")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="Normalize one Trivy JSON report")
    snapshot_parser.add_argument("--trivy-json", required=True)
    snapshot_parser.add_argument("--provenance", required=True)
    snapshot_parser.add_argument("--output", required=True)
    snapshot_parser.set_defaults(handler=command_snapshot)

    compare_parser = subparsers.add_parser("compare", help="Evaluate a causal base/head comparison")
    compare_parser.add_argument("--baseline", required=True)
    compare_parser.add_argument("--candidate", required=True)
    compare_parser.add_argument("--target-advisory", action="append", default=[])
    compare_parser.add_argument("--output", required=True)
    compare_parser.set_defaults(handler=command_compare)

    absolute_parser = subparsers.add_parser("absolute", help="Evaluate one absolute snapshot")
    absolute_parser.add_argument("--snapshot", required=True)
    absolute_parser.add_argument("--output", required=True)
    absolute_parser.set_defaults(handler=command_absolute)

    evidence_parser = subparsers.add_parser(
        "artifact-evidence", help="Bind absolute platform scans to one manifest-list digest"
    )
    evidence_parser.add_argument("--repository", required=True)
    evidence_parser.add_argument("--image", required=True)
    evidence_parser.add_argument("--parent-digest", required=True)
    evidence_parser.add_argument("--manifest-json", required=True)
    evidence_parser.add_argument("--build-provenance", required=True)
    evidence_parser.add_argument("--platform-snapshot", action="append", required=True)
    evidence_parser.add_argument("--output", required=True)
    evidence_parser.set_defaults(handler=command_artifact_evidence)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except ContractError as error:
        print(f"dependency health contract error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
