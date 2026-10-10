"""Prove explicit external-asset routing without making any external request."""
import runpy
from types import SimpleNamespace

helpers = runpy.run_path("/usr/local/bin/launchplane-readiness")
contract = {"host": "fixture.invalid:8069", "database": "private_fixture",
            "asset_origins": ["https://cdn.example.invalid"]}


class Response:
    status = 200
    headers = SimpleNamespace(get_content_type=lambda: "text/css")

    @staticmethod
    def read(_limit: int) -> bytes:
        return b"body { color: black; }"

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args) -> None:
        pass


class Transport:
    def __init__(self) -> None:
        self.requests = []

    def open(self, req, timeout: int) -> Response:
        assert timeout == 10
        self.requests.append(req)
        return Response()


transport = Transport()
path = helpers["resource_path"]("../font.woff2", "https://cdn.example.invalid/css/style.css", contract)
assert path == "https://cdn.example.invalid/font.woff2"
helpers["fetch"](transport, "http://127.0.0.1:8069", contract, path)
assert not transport.requests[-1].header_items(), transport.requests[-1].header_items()
helpers["fetch"](transport, "http://127.0.0.1:8069", contract, "/web/assets/style.css")
assert transport.requests[-1].get_header("Host") == contract["host"]
assert transport.requests[-1].get_header("X-odoo-database") == contract["database"]
for url in ["https://other.example.invalid/font.woff2", "http://cdn.example.invalid/font.woff2",
            "https://user:inert@cdn.example.invalid/font.woff2"]:
    try:
        helpers["fetch"](transport, "http://127.0.0.1:8069", contract, url)
    except ValueError:
        pass
    else:
        raise AssertionError("Unapproved asset origin was fetched")
assert len(transport.requests) == 2
print("explicit external-asset origins and private-header isolation passed")
