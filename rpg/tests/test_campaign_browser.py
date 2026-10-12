"""Real Chromium smoke tests for campaign workbench (Playwright in CI).

These tests exercise actual rendered forms, CSRF cookies, redirects and
interaction, not just Django's request test client. No external application
or real LLM service is required.
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from playwright.sync_api import sync_playwright

from rpg.models import Campaign


pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

PASSWORD = "campaign-e2e-password-12345"


def db_call(fn, *args, **kwargs):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


@pytest.fixture
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            instance.close()


def open_as(page, live_server, username):
    page.goto(live_server.url + reverse("gm_login"))
    page.locator("#id_username").fill(username)
    page.locator("#id_password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(live_server.url + reverse("campaigns"))
    assert page.get_by_role("heading", name="Campaigns", exact=True).is_visible()


@pytest.fixture
def staff():
    def create(username):
        return db_call(get_user_model().objects.create_user,
            username=username, password=PASSWORD, is_staff=True, is_active=True
        )
    return create


def test_browser_create_character_scene_clock_archive_and_restore(
    browser, live_server, staff,
):
    staff("browser-owner")
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        open_as(page, live_server, "browser-owner")

        page.get_by_role("link", name="New campaign").click()
        page.locator("#id_name").fill("Browser campaign")
        page.locator("#id_description").fill("An interactive test world")
        page.get_by_role("button", name="Create campaign").click()
        page.wait_for_url(live_server.url + reverse("campaigns"))
        assert page.get_by_text("Browser campaign").first.is_visible()

        page.get_by_role("link", name="Open").click()
        assert page.get_by_role("heading", name="Browser campaign").is_visible()

        page.get_by_role("link", name="+ Character").click()
        page.locator("#id_display_name").fill("Browser Hero")
        page.locator("#id_transport").select_option("HUMAN")
        page.get_by_role("button", name="Create character").click()
        assert page.get_by_text("Browser Hero").first.is_visible()

        page.get_by_role("link", name="+ Scene").click()
        page.locator("#id_name").fill("First scene")
        page.locator("#id_mode").select_option("MANUAL")
        page.get_by_label("Browser Hero", exact=True).check()
        page.get_by_role("button", name="Create scene").click()
        assert page.get_by_text("First scene").first.is_visible()

        page.get_by_role("link", name="Campaign timeline · GM only").click()
        assert page.get_by_role("heading", name="Campaign timeline").is_visible()
        page.locator("#id_world_date").fill("2022-10-12")
        page.locator("#id_world_time").fill("21:30")
        page.get_by_role("button", name="Set campaign time").click()
        assert page.get_by_text("21:30").first.is_visible()

        page.get_by_role("link", name="+ NPC plan").click()
        page.locator("#id_title").fill("Governor leaves the city")
        page.locator("#id_npc_name").fill("Governor")
        page.locator("#id_scheduled_date").fill("2022-10-13")
        page.locator("#id_scheduled_time").fill("10:00")
        page.locator("#id_planned_outcome").fill("Departs unless interrupted")
        page.get_by_role("button", name="Add to calendar").click()
        assert page.get_by_text("Governor leaves the city").first.is_visible()

        # Exercise the narrow viewport and switch actual calendar views.
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("link", name="Week", exact=True).click()
        assert page.locator(".timeline-calendar.week").count() == 1
        page.get_by_role("link", name="Day", exact=True).click()
        assert page.locator(".timeline-calendar.day").count() == 1

        page.goto(live_server.url + reverse("campaigns"))
        page.get_by_role("link", name="Archive").click()
        assert page.get_by_role("heading", name="Archive campaign").is_visible()
        page.locator("#confirm_name").fill("Browser campaign")
        page.get_by_role("button", name="Archive", exact=True).click()
        page.wait_for_url(live_server.url + reverse("campaigns"))
        assert page.get_by_role("heading", name="Archive").is_visible()
        page.get_by_role("link", name="Read").click()
        assert page.get_by_text("ARCHIVED / READ ONLY").is_visible()

        page.get_by_role("link", name="Restore campaign").click()
        page.get_by_role("button", name="Restore", exact=True).click()
        page.wait_for_url(live_server.url + reverse("campaigns"))
        assert page.get_by_text("Browser campaign").is_visible()
        assert db_call(lambda: Campaign.objects.get(name="Browser campaign").archived_at) is None
    finally:
        context.close()


def test_browser_owner_adds_and_revokes_existing_co_gm(
    browser, live_server, staff,
):
    owner = staff("browser-team-owner")
    staff("browser-team-cogm")
    campaign = db_call(
        Campaign.objects.create, name="Shared browser world", owner=owner,
    )

    owner_ctx = browser.new_context(viewport={"width": 1080, "height": 820})
    co_ctx = browser.new_context(viewport={"width": 390, "height": 844})
    owner_page = owner_ctx.new_page()
    co_page = co_ctx.new_page()
    try:
        open_as(owner_page, live_server, "browser-team-owner")
        owner_page.get_by_role("link", name="Open").click()
        owner_page.get_by_role("link", name="Game Masters").click()
        owner_page.locator("#id_username").fill("browser-team-cogm")
        owner_page.get_by_role("button", name="Add co-GM").click()
        assert owner_page.get_by_text("browser-team-cogm").first.is_visible()

        open_as(co_page, live_server, "browser-team-cogm")
        assert co_page.get_by_text("Shared browser world").is_visible()
        co_page.get_by_role("link", name="Open").click()
        co_page.get_by_role("link", name="Game Masters").click()
        assert co_page.get_by_text("browser-team-owner").is_visible()
        assert co_page.get_by_role("button", name="Add co-GM").count() == 0
        assert co_page.get_by_role("button", name="Remove access").count() == 0

        owner_page.get_by_role("button", name="Remove browser-team-cogm").click()
        assert owner_page.get_by_text("No additional Game Masters yet").is_visible()
        co_page.reload()
        assert co_page.get_by_text("Not found.", exact=True).is_visible()
        assert co_page.get_by_text("Shared browser world").count() == 0
    finally:
        owner_ctx.close()
        co_ctx.close()
