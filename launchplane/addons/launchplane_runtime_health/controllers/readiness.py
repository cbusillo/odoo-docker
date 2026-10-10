"""Private readiness evidence contract; preparation and browser work live outside HTTP readiness."""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlsplit


class ReadinessFailure(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def require(condition: object, code: str) -> None:
    if not condition:
        raise ReadinessFailure(code)


def local_path(value: object) -> bool:
    return (
        isinstance(value, str) and value.startswith("/") and not value.startswith("//")
        and "\\" not in value and not urlsplit(value).netloc and not urlsplit(value).fragment
        and not any(ord(char) < 32 for char in value)
    )


def resource_allowed(contract: dict, value: object) -> bool:
    if local_path(value):
        return True
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return False
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None
            and not parsed.fragment
            and f"https://{parsed.netloc}" in contract.get("asset_origins", []))


def validate_contract(contract: dict) -> dict:
    require(isinstance(contract, dict) and type(contract.get("schema_version")) is int
            and contract["schema_version"] == 1, "invalid_contract")
    for name in ("database", "database_uuid", "host"):
        require(isinstance(contract.get(name), str) and contract[name], "invalid_contract")
    require(re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", contract["host"]), "invalid_contract")
    origins = contract.get("asset_origins", [])
    require(isinstance(origins, list), "invalid_contract")
    for origin in origins:
        require(isinstance(origin, str), "invalid_contract")
        parsed = urlsplit(origin)
        require(parsed.scheme == "https" and parsed.hostname and parsed.username is None
                and not parsed.path and not parsed.query and not parsed.fragment, "invalid_contract")
    require(type(contract.get("website_id")) is int and contract["website_id"] > 0, "invalid_contract")
    modules = contract.get("modules")
    require(isinstance(modules, list) and modules and len(modules) == len(set(modules)), "invalid_contract")
    require(all(isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9_]+", name) for name in modules), "invalid_contract")
    require({"base", "web", "website"}.issubset(modules), "invalid_contract")
    identity = contract.get("runtime_identity")
    require(isinstance(identity, dict), "invalid_contract")
    for name in ("artifact_id", "image_digest", "source_revision", "slot", "release_id"):
        require(isinstance(identity.get(name), str) and identity[name], "invalid_contract")
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", identity["image_digest"]), "invalid_contract")
    require(re.fullmatch(r"[a-f0-9]{40}", identity["source_revision"]), "invalid_contract")
    require(identity.get("database_uuid") == contract["database_uuid"], "invalid_contract")
    release = contract.get("release")
    require(isinstance(release, dict) and release.get("runtime_identity") == identity, "invalid_contract")
    require(isinstance(release.get("update_id"), str) and release["update_id"], "invalid_contract")
    require(release.get("status") == "pass", "invalid_contract")
    require(isinstance(release.get("updated_modules"), list), "invalid_contract")
    require(all(name in modules for name in release["updated_modules"]), "invalid_contract")
    fences = release.get("fences")
    require(isinstance(fences, dict) and set(fences) == {
        "credentials_committed", "mail_disabled", "integrations_disabled",
    } and all(value is True for value in fences.values()), "fence_evidence_missing")
    routes = contract.get("routes")
    require(isinstance(routes, list) and routes, "invalid_contract")
    require({route.get("kind") for route in routes} >= {"homepage", "login", "public"}, "invalid_contract")
    require(len({route["path"] for route in routes}) == len(routes), "invalid_contract")
    for route in routes:
        require(local_path(route.get("path")), "invalid_contract")
        require(isinstance(route.get("contains"), str) and route["contains"], "invalid_contract")
    return contract


def validate_observations(contract: dict, observations: dict, *, now: float | None = None) -> None:
    require(isinstance(observations, dict), "probe_evidence_missing")
    require(observations.get("contract_sha256") == fingerprint(contract), "probe_identity_mismatch")
    current = time.time() if now is None else now
    checked_at = observations.get("checked_at")
    require(type(checked_at) in (int, float) and 0 <= current - checked_at <= 30, "probe_evidence_stale")
    routes = observations.get("routes")
    require(isinstance(routes, list) and len(routes) == len(contract["routes"]), "render_evidence_missing")
    for expected, observed in zip(contract["routes"], routes):
        require(observed.get("path") == expected["path"] and observed.get("status") == 200
                and observed.get("content_match") is True
                and re.fullmatch(r"[a-f0-9]{64}", observed.get("sha256", "")), "render_failed")
    assets = observations.get("assets")
    require(isinstance(assets, list) and assets, "asset_evidence_missing")
    require({asset.get("kind") for asset in assets} >= {"css", "js", "media"}, "asset_evidence_missing")
    for asset in assets:
        require(resource_allowed(contract, asset.get("path")) and asset.get("status") == 200
                and type(asset.get("bytes")) is int and asset["bytes"] > 0
                and re.fullmatch(r"[a-f0-9]{64}", asset.get("sha256", "")), "asset_failed")
    browser = observations.get("browser")
    require(isinstance(browser, dict) and browser.get("contract_sha256") == fingerprint(contract), "browser_evidence_missing")
    require(browser.get("status") == "pass" and browser.get("paths") == [route["path"] for route in contract["routes"]]
            and browser.get("errors") == [], "browser_failed")
    browser_time = browser.get("checked_at")
    require(type(browser_time) in (int, float) and 0 <= current - browser_time <= 30, "browser_evidence_stale")
