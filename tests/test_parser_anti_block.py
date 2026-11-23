"""Защита форумов от перегрузки: паузы, автостоп при блокировке, пропуск неизменённых тем.

Без этого синк шёл до 16 параллельными запросами на форум, повторял запросы к уже
заблокировавшему форуму и каждый раз скачивал все темы, и форумы блокировали IP.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(_ROOT), str(_ROOT / "parser_worker")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import worker  # noqa: E402
from knowledge import base_manager as kb  # noqa: E402
from knowledge.remote_parser import _parse_activity  # noqa: E402

FORUMS = (
    "forum.rmrp.ru", "forum.majestic-rp.ru", "forum.gta5rp.com",
    "forum.russia.online", "forum.nova-rp.com",
)


# --- Паузы -------------------------------------------------------------------

@pytest.mark.parametrize("host", FORUMS)
def test_every_forum_is_paced_by_default(host):
    assert worker._host_delay_for(f"https://{host}/threads/1/") > 0
    assert worker._host_gate(f"https://{host}/threads/1/") is not None


# --- Время последнего сообщения в списке тем ---------------------------------

SECTION_HTML = """
<div class="structItem structItem--thread js-threadListItem-58232">
  <div class="structItem-title"><a href="/threads/law.58232/">Закон</a></div>
  <div class="structItem-cell structItem-cell--latest"><time data-time="1778668431"></time></div>
</div>
<div class="structItem structItem--thread js-threadListItem-777">
  <div class="structItem-title"><a href="/threads/777/">Без времени</a></div>
</div>
"""


def test_thread_activity_is_read_from_section_rows():
    soup = BeautifulSoup(SECTION_HTML, "html.parser")
    assert worker.thread_activity_from_soup(soup) == {58232: 1778668431}


def test_thread_activity_reads_gta5rp_timestamp_format():
    # GTA5RP: data-time — время суток, unix-время в data-timestamp
    html = """
    <div class="structItem structItem--thread js-threadListItem-2879500">
      <div class="structItem-cell structItem-cell--latest">
        <time data-time="18:15" data-timestamp="1791126902" datetime="2026-10-04T18:15:02+0300"></time>
      </div>
    </div>
    <div class="structItem structItem--thread js-threadListItem-12">
      <div class="structItem-cell structItem-cell--latest">
        <time datetime="2026-10-04T18:15:02+0300"></time>
      </div>
    </div>"""
    assert worker.thread_activity_from_soup(BeautifulSoup(html, "html.parser")) == {
        2879500: 1791126902, 12: 1791126902,
    }


def test_client_parses_activity_from_json():
    assert _parse_activity({"58232": 1778668431, "x": 1, "5": "bad"}) == {58232: 1778668431}
    assert _parse_activity(None) == {}


# --- Автостоп ----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_breakers():
    worker._host_breakers.clear()
    yield
    worker._host_breakers.clear()


def test_breaker_opens_after_consecutive_failures_and_isolates_host():
    url = "https://forum.majestic-rp.ru/threads/1/"
    for _ in range(worker.HOST_BREAKER_FAILS - 1):
        worker._breaker_failure(url, "timeout")
    assert worker.host_cooldown_left(url) == 0
    worker._breaker_failure(url, "timeout")
    assert worker.host_cooldown_left(url) > 0
    with pytest.raises(worker.HostCooldownError):
        worker._breaker_check(url)
    # Другой форум не затронут
    worker._breaker_check("https://forum.nova-rp.com/threads/1/")


def test_success_resets_failure_streak():
    url = "https://forum.gta5rp.com/threads/1/"
    for _ in range(worker.HOST_BREAKER_FAILS - 1):
        worker._breaker_failure(url, "HTTP 403")
    worker._breaker_success(url)
    worker._breaker_failure(url, "HTTP 403")
    assert worker.host_cooldown_left(url) == 0


def test_failure_right_after_cooldown_doubles_pause():
    url = "https://forum.russia.online/threads/1/"
    for _ in range(worker.HOST_BREAKER_FAILS):
        worker._breaker_failure(url, "timeout")
    state = worker._host_breakers["forum.russia.online"]
    first = state["cooldown"]
    state["open_until"] = 0.0  # пауза истекла
    worker._breaker_failure(url, "timeout")  # первый же запрос снова упал
    assert state["cooldown"] == min(first * 2, worker.HOST_BREAKER_MAX_COOLDOWN_SECONDS)
    assert worker.host_cooldown_left(url) > 0


@pytest.mark.asyncio
async def test_no_retries_while_host_is_in_cooldown():
    url = "https://forum.nova-rp.com/threads/412046/"
    for _ in range(worker.HOST_BREAKER_FAILS):
        worker._breaker_failure(url, "timeout")

    class Page:
        calls = 0

        async def goto(self, *_args, **_kwargs):
            Page.calls += 1

    with pytest.raises(worker.HostCooldownError):
        await worker._thread_page_html(url, Page())
    assert Page.calls == 0


# --- Пропуск неизменённых тем ------------------------------------------------

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
CHECKED = NOW - timedelta(days=1)
BEFORE_CHECK = int((CHECKED - timedelta(hours=5)).timestamp())  # последнее сообщение до загрузки
AFTER_CHECK = int((CHECKED + timedelta(hours=5)).timestamp())   # новое сообщение после загрузки


def _record(**overrides):
    record = {
        "source_thread_id": 150,  # spread = 0.75 + 0.5 * 0.5 = 1.0 → ровно recheck_days
        "content_hash": "abc",
        "derived_sync_pending": False,
        "source_checked_at": CHECKED,
        "last_updated": None,
    }
    record.update(overrides)
    return record


def _skip(record, last_post_at=BEFORE_CHECK, force=False, days=7):
    return kb.can_skip_unchanged_topic(record, last_post_at, force, now=NOW, recheck_days=days)


def test_topic_older_than_last_download_is_skipped():
    assert _skip(_record()) is True


def test_record_downloaded_before_migration_uses_last_updated():
    assert _skip(_record(source_checked_at=None, last_updated=CHECKED)) is True
    assert _skip(_record(source_checked_at=None, last_updated=CHECKED), AFTER_CHECK) is False


@pytest.mark.parametrize("record, last_post_at, force, days", [
    (_record(), AFTER_CHECK, False, 7),                                    # новое сообщение в теме
    (_record(), None, False, 7),                                           # воркер не прислал время
    (_record(), BEFORE_CHECK, True, 7),                                    # принудительное обновление
    (_record(), BEFORE_CHECK, False, 0),                                   # пропуск выключен
    (_record(source_checked_at=NOW - timedelta(days=8)), BEFORE_CHECK, False, 7),  # пора перепроверить
    (_record(source_checked_at=None), BEFORE_CHECK, False, 7),             # не знаем, когда скачивали
    (_record(content_hash=None), BEFORE_CHECK, False, 7),                  # текста нет
    (_record(derived_sync_pending=True), BEFORE_CHECK, False, 7),          # недоделанный синк
    ({}, BEFORE_CHECK, False, 7),                                          # новой темы нет в БД
])
def test_topic_is_downloaded_when_needed(record, last_post_at, force, days):
    assert _skip(record, last_post_at, force, days) is False


# --- Загрузка обычным HTTP ---------------------------------------------------

class _Response:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class _Client:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def get(self, _url):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def http(monkeypatch):
    async def no_throttle(url):
        worker._breaker_check(url)

    monkeypatch.setattr(worker, "_host_throttle", no_throttle)
    monkeypatch.setattr(worker, "FLARESOLVERR_URL", None)
    worker._http_disabled_hosts.clear()
    yield
    worker._http_disabled_hosts.clear()


def _use_client(monkeypatch, response):
    client = _Client(response)
    monkeypatch.setattr(worker, "_get_http_client", lambda: client)
    return client


@pytest.mark.parametrize("host, expected", [
    ("forum.gta5rp.com", True), ("forum.russia.online", True), ("forum.rmrp.ru", True),
    ("forum.majestic-rp.ru", False), ("forum.nova-rp.com", False),  # проверка браузера
])
def test_http_is_used_only_where_forum_serves_plain_html(http, host, expected):
    assert worker._use_http(f"https://{host}/threads/1/") is expected


@pytest.mark.asyncio
async def test_http_returns_forum_page(http, monkeypatch):
    _use_client(monkeypatch, _Response(200, '<h1 class="p-title-value">Закон</h1>'))
    html = await worker._http_get("https://forum.gta5rp.com/threads/1/")
    assert "Закон" in html


@pytest.mark.asyncio
async def test_challenge_page_switches_host_to_browser(http, monkeypatch):
    _use_client(monkeypatch, _Response(200, "<title>Проверка браузера</title>"))
    url = "https://forum.russia.online/threads/1/"
    assert await worker._http_get(url) is None
    assert worker._use_http(url) is False

    async def browser(u):
        return "<html>browser</html>"

    monkeypatch.setattr(worker, "_browser_get_html", browser)
    assert await worker._html_get(url) == "<html>browser</html>"


@pytest.mark.asyncio
async def test_block_status_feeds_breaker_and_is_not_retried_in_browser(http, monkeypatch):
    _use_client(monkeypatch, _Response(429, ""))
    url = "https://forum.gta5rp.com/threads/1/"

    async def browser(_u):
        raise AssertionError("браузер не должен добивать заблокировавший форум")

    monkeypatch.setattr(worker, "_browser_get_html", browser)
    for _ in range(worker.HOST_BREAKER_FAILS):
        with pytest.raises(RuntimeError):
            await worker._html_get(url)
    assert worker.host_cooldown_left(url) > 0
    with pytest.raises(worker.HostCooldownError):
        await worker._html_get(url)


# --- Обновление: форумы параллельно, серверы одного форума по очереди ----------

def test_servers_are_grouped_by_forum_in_order():
    groups = kb.group_servers_by_forum(["downtown", "new_york", "eclipse", "rmrp", "detroit"])
    assert groups["forum.gta5rp.com"] == ["downtown", "eclipse"]
    assert groups["forum.majestic-rp.ru"] == ["new_york", "detroit"]
    assert groups["forum.rmrp.ru"] == ["rmrp"]


@pytest.mark.asyncio
async def test_forums_run_in_parallel_servers_of_one_forum_sequentially(monkeypatch):
    import asyncio
    running: dict[str, int] = {}
    overlap = {"cross_forum": False, "same_forum": False}

    async def fake_update(server_id, force_update=False):
        host = kb.server_forum_host(server_id)
        if running.get(host):
            overlap["same_forum"] = True
        if any(v for h, v in running.items() if h != host):
            overlap["cross_forum"] = True
        running[host] = running.get(host, 0) + 1
        await asyncio.sleep(0.01)
        running[host] -= 1
        return server_id != "eclipse"

    monkeypatch.setattr(kb, "update_knowledge_base_for_server", fake_update)
    ok, failed = await kb.update_servers_by_forum(["downtown", "eclipse", "new_york", "detroit"])
    assert sorted(ok) == ["detroit", "downtown", "new_york"]
    assert failed == ["eclipse"]
    assert overlap == {"cross_forum": True, "same_forum": False}
