import os
import re

from chatter import open_record
from playwright.sync_api import Page, Route, expect, sync_playwright

ORIGIN = "http://fixture.invalid:8069"


def check_toggle(page: Page) -> None:
    panel = page.locator(".mk_apps_sidebar_panel")
    contacts = panel.get_by_role("menuitem", name="Contacts", exact=True)
    contacts_label = contacts.locator(".mk_apps_sidebar_name")
    collapse = page.get_by_role("button", name="Collapse AppsBar", exact=True)
    expand = page.get_by_role("button", name="Expand AppsBar", exact=True)
    open_record(page, ORIGIN, "bottom")
    record = page.locator(".o_form_view")
    expect(record).to_be_visible()
    record_url = page.url
    expect(contacts_label).to_be_visible()
    large_width = panel.evaluate("panel => panel.offsetWidth")
    expect(collapse).to_have_attribute("aria-expanded", "true")
    collapse.click()
    expect(expand).to_have_attribute("aria-expanded", "false")
    expect(contacts).to_be_visible()
    expect(contacts_label).to_be_hidden()
    assert 0 < panel.evaluate("panel => panel.offsetWidth") < large_width
    expect(contacts.locator("xpath=..")).to_have_class(re.compile(r"\bactive\b"))
    expect(record).to_be_visible()
    expect(page).to_have_url(record_url)

    expect(expand).to_have_attribute("aria-disabled", "false")
    page.reload()
    expect(expand).to_be_visible()
    expect(contacts_label).to_be_hidden()
    expect(record).to_be_visible()
    expect(page).to_have_url(record_url)

    expand.focus()
    page.keyboard.press("Space")
    expect(collapse).to_have_attribute("aria-expanded", "true")
    expect(collapse).to_be_focused()
    expect(contacts_label).to_be_visible()
    expect(record).to_be_visible()
    expect(page).to_have_url(record_url)
    expect(collapse).to_have_attribute("aria-disabled", "false")

    page.set_viewport_size({"width": 991, "height": 1000})
    expect(collapse).to_be_hidden()
    expect(panel).to_have_css("width", "0px")
    page.set_viewport_size({"width": 992, "height": 1000})
    expect(collapse).to_be_visible()
    expect(contacts_label).to_be_visible()
    page.get_by_title("Home Menu", exact=True).click()
    expect(page.locator(".o_app").filter(has_text="Contacts")).to_be_visible()
    page.keyboard.press("Escape")

    collapse.click()
    expect(expand).to_have_attribute("aria-disabled", "false")
    panel.get_by_role("menuitem", name="Discuss", exact=True).click()
    expect(panel.get_by_role("menuitem", name="Discuss", exact=True).locator("xpath=..")).to_have_class(re.compile(r"\bactive\b"))
    expect(expand).to_be_visible()
    page.set_viewport_size({"width": 700, "height": 1000})
    expect(expand).to_be_hidden()
    page.locator(".o_menu_toggle").click()
    expect(page.get_by_text("All Apps", exact=True)).to_be_visible()


def check_rejected_write(page: Page) -> None:
    open_record(page, ORIGIN, "read-only")
    expand = page.get_by_role("button", name="Expand AppsBar", exact=True)
    collapse = page.get_by_role("button", name="Collapse AppsBar", exact=True)
    notice = page.locator(".o_notification")
    error_dialog = page.get_by_role("dialog")
    calls = []

    def reject_write(route: Route) -> None:
        request = route.request.post_data_json
        calls.append(request["params"]["args"])
        route.fulfill(
            json={
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {
                    "code": 200,
                    "message": "Odoo Server Error",
                    "data": {
                        "name": "odoo.exceptions.AccessError",
                        "message": "Fixture denies this write",
                    },
                },
            },
        )

    write_url = "**/web/dataset/call_kw/res.users/write"
    page.route(write_url, reject_write)
    expand.click()
    expect(notice).to_have_count(1)
    expect(notice).to_be_visible()
    expect(error_dialog).to_have_count(0)
    expect(expand).to_have_attribute("aria-expanded", "false")
    expect(expand).to_have_attribute("aria-disabled", "false")
    expect(expand).to_be_focused()
    assert len(calls) == 1 and calls[0][1] == {"sidebar_type": "large"}, calls

    page.unroute(write_url, reject_write)
    expand.click()
    expect(collapse).to_have_attribute("aria-disabled", "false")
    page.reload()
    expect(collapse).to_be_visible()


def check_invisible(page: Page) -> None:
    open_record(page, ORIGIN, "invisible")
    expect(page.locator(".o_form_view")).to_be_visible()
    expect(page.locator(".mk_apps_sidebar_panel")).to_have_css("width", "0px")
    expect(page.locator(".mk_apps_sidebar_toggle")).to_be_hidden()
    page.get_by_title("Home Menu", exact=True).click()
    expect(page.locator(".o_app").filter(has_text="Contacts")).to_be_visible()


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ["CHROME_BIN"],
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        viewport = {"width": 1280, "height": 1000}
        with browser.new_context(viewport=viewport) as context:
            check_toggle(context.new_page())
        with browser.new_context(viewport=viewport) as context:
            check_rejected_write(context.new_page())
        with browser.new_context(viewport=viewport) as context:
            check_invisible(context.new_page())
        browser.close()
    print("AppsBar desktop, persistence, navigation, responsive and Invisible checks passed")


if __name__ == "__main__":
    main()
