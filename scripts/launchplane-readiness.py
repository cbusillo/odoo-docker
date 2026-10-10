#!/venv/bin/python
"""Controlled render/asset preparation and direct, observational readiness probing."""
from __future__ import annotations

import argparse
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, ProxyHandler, Request, build_opener

sys.path[:0] = ["/odoo", "/opt"]
from launchplane.addons.launchplane_runtime_health.controllers.readiness import fingerprint, resource_allowed, validate_contract, validate_observations


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl) -> None:
        return None


class References(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.references.append((values["src"], "js"))
        if tag == "link" and values.get("href") and "stylesheet" in values.get("rel", "").split():
            self.references.append((values["href"], "css"))
        if tag in {"img", "video", "audio", "source", "iframe"} and values.get("src"):
            self.references.append((values["src"], "media"))
        if values.get("poster"):
            self.references.append((values["poster"], "media"))
        if values.get("srcset"):
            self.references.extend((part.strip().split()[0], "media") for part in values["srcset"].split(",") if part.strip())


def resource_path(reference: str, page: str, contract: dict) -> str | None:
    if reference.startswith(("data:", "#")):
        return None
    base = f"https://{contract['host']}{page}" if page.startswith("/") else page
    url = urlsplit(urljoin(base, reference))
    if url.netloc == contract["host"] and url.scheme in {"http", "https"}:
        path = url.path + ("?" + url.query if url.query else "")
    else:
        path = urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))
    if not resource_allowed(contract, path):
        raise ValueError("Resource origin is not in the explicit contract")
    return path


def fetch(opener: OpenerDirector, origin: str, contract: dict, path: str, data: bytes | None = None) -> tuple[bytes, str]:
    if not resource_allowed(contract, path):
        raise ValueError("Resource origin is not in the explicit contract")
    internal = path.startswith("/")
    headers = {"Host": contract["host"], "X-Odoo-Database": contract["database"]} if internal else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with opener.open(Request(origin + path if internal else path, data=data, headers=headers), timeout=10) as response:
        body = response.read(8 * 1024 * 1024 + 1)
        if response.status != 200 or not body or len(body) > 8 * 1024 * 1024:
            raise ValueError("Unsuccessful or oversized response")
        return body, response.headers.get_content_type()


def prepare(opener: OpenerDirector, origin: str, contract: dict, browser: dict) -> dict:
    checked_at = time.time()
    # This rejection occurs only after all current database/fence checks pass.
    # It permits preparation without pretending the candidate is traffic-ready.
    try:
        fetch(opener, origin, contract, "/launchplane/readiness",
              json.dumps({"contract": contract, "observations": None}).encode())
    except HTTPError as error:
        if error.code != 503 or json.loads(error.read()) != {
            "status": "fail", "reason": "probe_evidence_missing",
        }:
            raise ValueError("Candidate preparation preflight failed") from None
    else:
        raise ValueError("Candidate preparation preflight did not verify bound state")
    routes = []
    pending: list[tuple[str, str]] = []
    for route in contract["routes"]:
        body, content_type = fetch(opener, origin, contract, route["path"])
        text = body.decode()
        if content_type != "text/html" or route["contains"] not in text:
            raise ValueError("Render or expected content failed")
        routes.append({"path": route["path"], "status": 200, "content_match": True,
                       "sha256": hashlib.sha256(body).hexdigest()})
        parser = References()
        parser.feed(text)
        for reference, kind in parser.references:
            path = resource_path(reference, route["path"], contract)
            if path:
                pending.append((path, kind))
    assets = []
    seen = set()
    while pending:
        path, kind = pending.pop(0)
        if path in seen:
            continue
        seen.add(path)
        if len(seen) > 1000:
            raise ValueError("Too many resource references")
        body, content_type = fetch(opener, origin, contract, path)
        if kind == "css" and content_type != "text/css":
            raise ValueError("Stylesheet returned the wrong content type")
        if kind == "js" and content_type not in {"application/javascript", "text/javascript"}:
            raise ValueError("Script returned the wrong content type")
        if kind == "media" and content_type == "text/html":
            raise ValueError("Media returned an HTML error page")
        assets.append({"path": path, "kind": kind, "status": 200, "bytes": len(body),
                       "sha256": hashlib.sha256(body).hexdigest()})
        if kind == "css":
            text = body.decode()
            references = re.findall(r"url[(]\s*['\"]?([^'\")]+)['\"]?\s*[)]", text)
            references += re.findall(r"@import\s+['\"]([^'\"]+)['\"]", text)
            for reference in references:
                nested = resource_path(reference.strip(), path, contract)
                if nested:
                    pending.append((nested, "css" if urlsplit(nested).path.endswith(".css") else "media"))
    observations = {"contract_sha256": fingerprint(contract), "checked_at": checked_at,
                    "routes": routes, "assets": assets, "browser": browser}
    validate_observations(contract, observations)
    return observations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "check"))
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True,
                        help="Browser receipt for prepare; preparation observations for check")
    parser.add_argument("--origin", default="http://127.0.0.1:8069")
    args = parser.parse_args()
    origin = urlsplit(args.origin)
    if origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "::1"} or origin.path or origin.query or origin.fragment or origin.username is not None:
        parser.error("Use a direct loopback HTTP origin inside the candidate")
    contract = validate_contract(json.loads(args.contract.read_text()))
    evidence = json.loads(args.evidence.read_text())
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        if args.mode == "prepare":
            result = prepare(opener, args.origin, contract, evidence)
        else:
            validate_observations(contract, evidence)
            body, _ = fetch(opener, args.origin, contract, "/launchplane/readiness",
                            json.dumps({"contract": contract, "observations": evidence}).encode())
            result = json.loads(body)
            if result != {"status": "pass"}:
                raise ValueError("Readiness failed")
    except (HTTPError, ValueError, OSError):
        print("Readiness preparation/check failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
