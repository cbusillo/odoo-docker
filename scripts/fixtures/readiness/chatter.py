import os
import re
import sys
from typing import NamedTuple

from playwright.sync_api import Browser, Page, expect, sync_playwright

LATEST_NOTE = "Chatter fixture note 15"


class FormGeometry(NamedTuple):
    sheet_width: float
    sheet_height: float
    title_visible: bool
    title_width: float
    rendered_words: int
    split_words: int
    scroll_width: int
    client_width: int
    chatter_top: float
    sheet_bottom: float


def open_record(page: Page, origin: str, user: str) -> None:
    page.goto(f"{origin}/web/login?login=fixture-{user}")
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
    panel.get_by_text(LATEST_NOTE, exact=True).wait_for(state="attached")
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
            const x = titleBox.left + Math.min(10, titleBox.width / 2);
            const y = titleBox.top + titleBox.height / 2;
            const titleVisible = x >= sheetBox.left && x < sheetBox.right && y >= sheetBox.top &&
                y < sheetBox.bottom && title.contains(document.elementFromPoint(x, y));
            const control = title.querySelector("textarea, input");
            let text = title;
            let mirror;
            let textWidth = title.clientWidth;
            if (control) {
                const style = getComputedStyle(control);
                textWidth = control.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
                mirror = document.createElement("div");
                Object.assign(mirror.style, {
                    position: "fixed",
                    top: "0",
                    left: "-10000px",
                    visibility: "hidden",
                    boxSizing: "content-box",
                    width: textWidth + "px",
                    font: style.font,
                    letterSpacing: style.letterSpacing,
                    wordSpacing: style.wordSpacing,
                    whiteSpace: control.tagName === "INPUT" ? "pre" : style.whiteSpace,
                    overflowWrap: style.overflowWrap,
                    wordBreak: style.wordBreak,
                });
                mirror.textContent = control.value;
                document.body.append(mirror);
                text = mirror;
            }
            const walker = document.createTreeWalker(text, NodeFilter.SHOW_TEXT);
            const range = document.createRange();
            let renderedWords = 0;
            let splitWords = 0;
            for (let node; (node = walker.nextNode());) {
                for (const word of node.textContent.matchAll(/[A-Za-z0-9]+/g)) {
                    range.setStart(node, word.index);
                    range.setEnd(node, word.index + word[0].length);
                    const rects = [...range.getClientRects()].filter(rect => rect.width > 0);
                    if (rects.length) renderedWords++;
                    if (new Set(rects.map(rect => Math.round(rect.top))).size > 1 ||
                        rects.some(rect => rect.width > textWidth)) splitWords++;
                }
            }
            mirror?.remove();
            return [sheetBox.width, sheetBox.height, titleVisible, textWidth, renderedWords, splitWords,
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
        layout.sheet_width <= 0
        or layout.sheet_height <= 0
        or not layout.title_visible
        or not layout.rendered_words
        or layout.split_words
        or layout.scroll_width > layout.client_width
        or (not aside and layout.chatter_top < layout.sheet_bottom)
    ):
        failures.append(f"{mode} {transition} at {width}: {layout}")
    if not aside:
        note = page.locator(".o-mail-Form-chatter").get_by_text(LATEST_NOTE, exact=True)
        note.hover(trial=True)
        expect(note).to_be_in_viewport()
        page.locator("main.o_content").evaluate("content => { content.scrollTop = 0; }")


def check_bottom(browser: Browser, origin: str, failures: list[str]) -> None:
    with browser.new_context(viewport={"width": 900, "height": 1000}) as context:
        page = context.new_page()
        open_record(page, origin, "bottom")
        for width in (900, 1440, 1920, 3440, 1920, 900):
            page.set_viewport_size({"width": width, "height": 1000})
            inspect_form(page, "Bottom", width, "resize", aside=False, failures=failures)
            page.reload(wait_until="domcontentloaded")
            inspect_form(page, "Bottom", width, "reload", aside=False, failures=failures)


def drag_side(page: Page, delta: int) -> int:
    wait_for_chatter(page, aside=True)
    handle = page.locator(".o-mail-Form-chatter.o-aside .mk_chatter_resize").bounding_box()
    assert handle is not None
    position = handle["y"] + 30
    start = handle["x"] + 2
    page.mouse.move(start, position)
    page.mouse.down()
    page.mouse.move(max(4, start + delta), position, steps=20)
    page.mouse.up()
    stored = page.evaluate("localStorage.getItem('muk_web_chatter.width')")
    assert isinstance(stored, str) and stored.isdecimal(), stored
    return int(stored)


def check_side(browser: Browser, origin: str, failures: list[str]) -> None:
    with browser.new_context(viewport={"width": 3440, "height": 1000}) as context:
        page = context.new_page()
        open_record(page, origin, "side")
        page.set_viewport_size({"width": 1920, "height": 1000})
        inspect_form(page, "Side", 1920, "fresh", aside=True, failures=failures)
        page.set_viewport_size({"width": 3440, "height": 1000})
        stored = drag_side(page, -3440)
        assert stored > 1920, stored
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
            assert page.evaluate("localStorage.getItem('muk_web_chatter.width')") == str(stored)

        page.set_viewport_size({"width": 1920, "height": 1000})
        panel = page.locator(".o-mail-Form-chatter.o-aside")
        before = panel.bounding_box()
        assert before is not None
        narrow_width = drag_side(page, 140)
        assert 0 < narrow_width < before["width"], (narrow_width, before["width"])
        page.reload(wait_until="domcontentloaded")
        inspect_form(page, "Side", 1920, "narrower drag reload", aside=True, failures=failures)
        assert page.evaluate("localStorage.getItem('muk_web_chatter.width')") == str(narrow_width)
        restored = panel.bounding_box()
        assert restored is not None and restored["width"] == narrow_width, restored


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
