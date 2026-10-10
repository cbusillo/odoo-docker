"""Representative Chromium smoke; Launchplane supplies this receipt in a release."""
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlunsplit

from playwright.sync_api import sync_playwright


def main() -> None:
    root = Path("/fixture")
    contract = json.loads((root / "contract.json").read_text())
    errors = []
    checked_at = time.time()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ["CHROME_BIN"], headless=True, args=["--no-sandbox"])
        context = browser.new_context()

        # The fixture server admits exactly its one database via -d/dbfilter.
        # No database or authorization header is attached to browser resources.
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append("script failed"))
        page.on("requestfailed", lambda req: errors.append(f"{req.url}: {req.failure}"))
        page.on("response", lambda observed_response: errors.append(f"HTTP {observed_response.status}") if observed_response.status >= 400 else None)
        origin = urlunsplit(("http", contract["host"], "", "", ""))
        for route in contract["routes"]:
            response = page.goto(origin + route["path"], wait_until="networkidle")
            assert response.status == 200
            assert route["contains"] in page.content()
            if route["kind"] == "login":
                page.get_by_label("Email").fill("fixture@example.invalid")
                assert page.get_by_label("Email").input_value() == "fixture@example.invalid"
            assert not errors, errors
            # Controlled preparation also warms offscreen/lazy media.
            page.locator("img").evaluate_all("images => images.forEach(image => { image.loading = 'eager'; })")
            page.wait_for_function("Array.from(document.images).filter(image => image.getAttribute('src') && image.offsetWidth > 0 && image.offsetHeight > 0).every(image => image.naturalWidth > 0)", timeout=10000)
        browser.close()
    receipt = {
        "contract_sha256": hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        "checked_at": checked_at, "status": "pass", "errors": errors,
        "paths": [route["path"] for route in contract["routes"]],
    }
    (root / "browser.json").write_text(json.dumps(receipt))


if __name__ == "__main__":
    main()
