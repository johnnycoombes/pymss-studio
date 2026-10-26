from playwright.sync_api import expect


def expand_catalog_groups(panel):
    expect(panel.locator('.plugin-manager__body')).to_have_attribute('aria-busy', 'false')
    headings = panel.locator('.plugin-manager__group-heading')
    expect(headings).not_to_have_count(0)
    for heading in headings.all():
        if heading.get_attribute('aria-expanded') == 'false':
            heading.click()
