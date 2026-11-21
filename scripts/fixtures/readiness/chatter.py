import os
import re
import sys
from typing import NamedTuple

from playwright.sync_api import Browser, Page, expect, sync_playwright


class FormGeometry(NamedTuple):
    sheet_width: float
    sheet_height: float
    title_visible: bool
    scroll_width: int
    client_width: int
    chatter_top: float
    sheet_bottom: float


def open_record(page: Page, origin: str, position: str) -> None:
    page.goto(f"{origin}/web/login?login=fixture-{position}")
    page.locator('input[name="password"]').fill("isolated-chatter-fixture")
    page.get_by_role("button", name="Log in", exact=True).click()
    page.wait_for_url(lambda url: "/web/login" not in url)
    response = page.request.post(
        f"{origin}/web/dataset/call_kw/res.partner/search_read",
        data={
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "model": "res.partner",
                "method": "search_read",
                "args": [[["name", "=", "Chatter geometry fixture"]]],
                "kwargs": {"fields": ["id"], "limit": 2},
            },
        },
    )
    records = response.json()["result"]
    assert len(records) == 1, records
    page.goto(
        f"{origin}/odoo/action-contacts.action_contacts/{records[0]['id']}",
        wait_until="domcontentloaded",
    )


def wait_for_chatter(page: Page, *, aside: bool) -> None:
    panel = page.locator(".o-mail-Form-chatter")
    if aside:
        expect(panel).to_have_class(re.compile(r"\bo-aside\b"), timeout=15000)
    else:
        expect(panel).not_to_have_class(re.compile(r"\bo-aside\b"), timeout=15000)
    panel.get_by_text("Chatter fixture note 15", exact=True).wait_for(state="attached")
    panel.get_by_text("Completed fixture activity", exact=False).wait_for(state="attached")
    panel.get_by_text("Pending fixture activity", exact=False).wait_for(state="attached")


def geometry(page: Page) -> FormGeometry:
    values = page.locator(".o_form_renderer").evaluate(
        """renderer => {
            const sheet = renderer.querySelector(".o_form_sheet_bg");
            const title = sheet.querySelector(".o_field_widget[name='name']");
            const chatter = renderer.querySelector(".o-mail-Form-chatter");
            const content = renderer.closest(".o_content");
            const sheetBox = sheet.getBoundingClientRect();
            const titleBox = title.getBoundingClientRect();
            const hit = document.elementFromPoint(titleBox.left + Math.min(10, titleBox.width / 2), titleBox.top + titleBox.height / 2);
            return [sheetBox.width, sheetBox.height, title.contains(hit),
                content.scrollWidth, content.clientWidth, chatter.getBoundingClientRect().top, sheetBox.bottom];
        }""",
    )
    return FormGeometry(*values)


def inspect_form(
    page: Page,
    mode: str,
    width: int,
    transition: str,
    *,
    aside: bool,
    failures: list[str],
) -> None:
    wait_for_chatter(page, aside=aside)
    layout = geometry(page)
    print(mode, transition, width, layout, flush=True)
    if (
        not layout.title_visible
        or layout.scroll_width > layout.client_width
        or (not aside and layout.chatter_top < layout.sheet_bottom)
    ):
        failures.append(f"{mode} {transition} at {width}: {layout}")


def check_bottom(browser: Browser, origin: str, failures: list[str]) -> None:
    with browser.new_context(viewport={"width": 900, "height": 1000}) as context:
        page = context.new_page()
        open_record(page, origin, "bottom")
        for width in (900, 1440, 1920, 3440, 1920, 900):
            page.set_viewport_size({"width": width, "height": 1000})
            inspect_form(page, "Bottom", width, "resize", aside=False, failures=failures)
            page.reload(wait_until="domcontentloaded")
            inspect_form(page, "Bottom", width, "reload", aside=False, failures=failures)


def check_side(browser: Browser, origin: str, failures: list[str]) -> None:
    with browser.new_context(viewport={"width": 3440, "height": 1000}) as context:
        page = context.new_page()
        open_record(page, origin, "side")
        wait_for_chatter(page, aside=True)
        handle = page.locator(".o-mail-Form-chatter.o-aside .mk_chatter_resize").bounding_box()
        assert handle is not None
        position = handle["y"] + 30
        page.mouse.move(handle["x"] + 2, position)
        page.mouse.down()
        page.mouse.move(4, position, steps=20)
        page.mouse.up()
        stored = page.evaluate("localStorage.getItem('muk_web_chatter.width')")
        assert int(stored) > 1920, stored
        for width, aside in (
            (3440, True),
            (1920, True),
            (1440, True),
            (900, False),
            (3440, True),
        ):
            page.set_viewport_size({"width": width, "height": 1000})
            inspect_form(page, "Side", width, "resize", aside=aside, failures=failures)
            page.reload(wait_until="domcontentloaded")
            inspect_form(page, "Side", width, "reload", aside=aside, failures=failures)
            assert page.evaluate("localStorage.getItem('muk_web_chatter.width')") == stored


def main() -> None:
    failures: list[str] = []
    origins = sys.argv[1:] or ["http://fixture.invalid:8069"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ["CHROME_BIN"],
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        for origin in origins:
            check_bottom(browser, origin, failures)
            check_side(browser, origin, failures)
        browser.close()
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    main()
