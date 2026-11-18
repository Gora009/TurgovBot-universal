"""Онбординг проекта NOVA RP (forum.nova-rp.com).

Форум — XenForo 2 БЕЗ ЧПУ-ссылок: темы /threads/412046/, посты /threads/412046/post-1822904.
Все прежние регулярки бота ждали /threads/slug.ID/ и молча возвращали None, поэтому
кроме конфига проверяем, что ID тем из таких ссылок извлекаются везде, где на них
опирается синк законов (discovery, дедупликация, excluded_urls) и судебный парсер.
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(_ROOT), str(_ROOT / "parser_worker")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from knowledge import sources  # noqa: E402
from knowledge import base_manager as kb  # noqa: E402
import worker  # noqa: E402
from api import url_validator  # noqa: E402

PROJECT_ID = "nova_rp"
HOST = "forum.nova-rp.com"
EXPECTED_SERVERS = {
    "nova_new_era": "https://forum.nova-rp.com/forums/1320/",
    "nova_horizon": "https://forum.nova-rp.com/forums/1478/",
}


# --- Конфиг проекта ---------------------------------------------------------

def test_project_registered_with_two_law_bases():
    project = sources.PROJECTS_DATA[PROJECT_ID]
    assert project["display_name"] == "NOVA RP"
    assert project["forum_base_url"] == f"https://{HOST}"
    assert {sid: s["forum_section_urls"] for sid, s in project["servers"].items()} == {
        sid: [url] for sid, url in EXPECTED_SERVERS.items()
    }


@pytest.mark.parametrize("server_id", sorted(EXPECTED_SERVERS))
def test_only_first_post_is_parsed(server_id):
    server = sources.PROJECTS_DATA[PROJECT_ID]["servers"][server_id]
    assert server["parse_only_author_posts"] is True
    assert server["default_thread_post_override"] == {"first_n": 1}


def test_server_ids_are_unique_across_all_projects():
    seen = {}
    for project_id, project in sources.PROJECTS_DATA.items():
        for server_id in project["servers"]:
            assert server_id not in seen, f"{server_id}: {seen.get(server_id)} и {project_id}"
            seen[server_id] = project_id
    flat = sources.get_flat_servers()
    for server_id in EXPECTED_SERVERS:
        assert server_id in flat
        assert sources.get_project_for_server(server_id) == PROJECT_ID


def test_new_era_excludes_superseded_codes_but_keeps_new_ones():
    """На New Era в одном разделе лежат старые (2025) и NEW (2026-10-31) редакции.
    Старый УК обязан быть исключён, новый — нет, иначе в базе два противоречащих УК."""
    excluded = sources.PROJECTS_DATA[PROJECT_ID]["servers"]["nova_new_era"]["excluded_urls"]
    discovered = [
        "https://forum.nova-rp.com/threads/404073/",   # старый УК
        "https://forum.nova-rp.com/threads/412046/",   # NEW УК
        "https://forum.nova-rp.com/threads/405387/",   # старый закон без NEW-аналога
    ]
    kept, n_excluded = kb.filter_discovered_thread_urls(discovered, excluded)
    assert n_excluded == 1
    assert set(kept) == {412046, 405387}


# --- Ссылки без slug ---------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://forum.nova-rp.com/threads/412046/", 412046),
    ("https://forum.nova-rp.com/threads/412046", 412046),
    ("https://forum.nova-rp.com/threads/412046/post-1822904", 412046),
    ("https://forum.nova-rp.com/threads/412046/page-2?x=1#post-5", 412046),
    ("https://forum.nova-rp.com/threads/412046/unread", 412046),
    # прежний формат со slug не сломан
    ("https://forum.majestic-rp.ru/threads/zakon.12345/", 12345),
    ("https://forum.gta5rp.com/threads/a.b.c.777/latest", 777),
])
def test_thread_id_extracted_with_and_without_slug(url, expected):
    assert kb.extract_thread_id_from_url(url) == expected
    assert worker.extract_thread_id_from_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://forum.nova-rp.com/forums/1320/",
    "https://forum.nova-rp.com/threads/",
    "https://forum.nova-rp.com/threads/abc/",
    "https://forum.nova-rp.com/members/412046/",
])
def test_non_thread_urls_are_not_mistaken_for_threads(url):
    """Ложный thread_id опасен для destructive cleanup — не-темы не распознаём."""
    assert kb.extract_thread_id_from_url(url) is None


def test_canonical_thread_url_without_slug():
    assert kb.canonical_thread_url(
        "https://forum.nova-rp.com/threads/412046/post-1822904?x=1"
    ) == "https://forum.nova-rp.com/threads/412046/"
    assert kb.canonical_thread_url(
        "https://forum.majestic-rp.ru/threads/zakon.12345/page-3"
    ) == "https://forum.majestic-rp.ru/threads/zakon.12345/"


def test_worker_discovery_accepts_slugless_threads():
    urls = worker.validated_discovery_thread_urls(
        ["/threads/412046/", "/threads/412046/post-1822904", "/threads/411159/latest", "/forums/1479/"],
        page_url="https://forum.nova-rp.com/forums/1478/",
        section_url="https://forum.nova-rp.com/forums/1478/",
    )
    assert urls == {
        "https://forum.nova-rp.com/threads/412046/",
        "https://forum.nova-rp.com/threads/411159/",
    }


# --- Вайтлисты хоста --------------------------------------------------------

def test_url_validator_allows_nova_rp():
    ok, _ = url_validator.validate_forum_url("https://forum.nova-rp.com/threads/412046/")
    assert ok is True


def test_court_parser_supports_nova_rp():
    from parsers import court_case

    assert court_case.is_supported_forum_url("https://forum.nova-rp.com/threads/412046/")
    # форум открыт гостю — логин не нужен
    assert court_case._login_host("https://forum.nova-rp.com/threads/412046/") is None


def test_case_refresh_regex_accepts_nova_rp():
    import re

    source = (_ROOT / "user_handlers" / "assistant_flow.py").read_text(encoding="utf-8")
    match = re.search(r"r'\((https\?://forum\\\.[^']+)\)'", source)
    assert match
    assert re.compile(match.group(1)).search("тема: https://forum.nova-rp.com/threads/412046/")
