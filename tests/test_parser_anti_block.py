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


def _record(**overrides):
    record = {
        "source_thread_id": 150,  # spread = 0.75 + 0.5 * 0.5 = 1.0 → ровно recheck_days
        "content_hash": "abc",
        "derived_sync_pending": False,
        "source_last_post_at": 1000,
        "source_checked_at": NOW - timedelta(days=1),
    }
    record.update(overrides)
    return record


def _skip(record, last_post_at=1000, force=False, days=7):
    return kb.can_skip_unchanged_topic(record, last_post_at, force, now=NOW, recheck_days=days)


def test_unchanged_recent_topic_is_skipped():
    assert _skip(_record()) is True


@pytest.mark.parametrize("record, last_post_at, force, days", [
    (_record(), 1001, False, 7),                                   # новое сообщение в теме
    (_record(), None, False, 7),                                   # воркер не прислал время
    (_record(), 1000, True, 7),                                    # принудительное обновление
    (_record(), 1000, False, 0),                                   # пропуск выключен
    (_record(source_checked_at=NOW - timedelta(days=8)), 1000, False, 7),  # пора перепроверить
    (_record(source_checked_at=None), 1000, False, 7),             # ни разу не проверялась
    (_record(source_last_post_at=None), 1000, False, 7),           # старая запись без времени
    (_record(content_hash=None), 1000, False, 7),                  # текста нет
    (_record(derived_sync_pending=True), 1000, False, 7),          # недоделанный синк
    ({}, 1000, False, 7),                                          # новой темы нет в БД
])
def test_topic_is_downloaded_when_needed(record, last_post_at, force, days):
    assert _skip(record, last_post_at, force, days) is False
