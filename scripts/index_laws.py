#!/usr/bin/env python3
"""Векторный индекс законов (law_chunks) для семантического поиска.

Нужна настроенная роль EMBED (LLM_EMBED_MODEL и при необходимости LLM_EMBED_BASE_URL /
LLM_EMBED_API_KEY), например OpenAI text-embedding-3-large (3072 измерения, ровно под
столбец law_chunks). Без индекса поиск идёт по каталогу статей (полнотекстовый).

Первый раз индекс строится этим скриптом. Дальше синхронизация законов дообновляет его
сама для изменённых документов.

  python scripts/index_laws.py --all          # все серверы со скачанными законами
  python scripts/index_laws.py blaine chiliad # выборочно

После смены модели векторов индекс нужно построить заново: векторы разных моделей
несовместимы.
"""
import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("index_laws")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Векторный индекс законов (law_chunks)")
    parser.add_argument("servers", nargs="*", help="server_id")
    parser.add_argument("--all", action="store_true", help="все серверы со скачанными законами")
    args = parser.parse_args()

    from common.llm import settings as llm_settings
    from common.rate_limiter import init_client
    from database.manager import db_manager
    from services.indexing_service import index_server_laws

    if not llm_settings.endpoint("embed").configured:
        log.error("Роль EMBED не настроена: задайте LLM_EMBED_MODEL (и LLM_EMBED_BASE_URL / _API_KEY) в .env")
        return 2

    init_client()
    await db_manager.connect()
    try:
        servers = list(args.servers)
        if args.all:
            rows = await db_manager.fetch("SELECT DISTINCT server_id FROM law_documents ORDER BY server_id")
            servers = [r["server_id"] for r in rows]
        if not servers:
            parser.error("укажите server_id или --all")
        failed = []
        for i, server_id in enumerate(servers, 1):
            t = time.monotonic()
            try:
                res = await index_server_laws(server_id)
            except Exception:
                log.exception("[%d/%d] %s: ошибка", i, len(servers), server_id)
                failed.append(server_id)
                continue
            log.info("[%d/%d] %s: %s за %.0f с", i, len(servers), server_id, res, time.monotonic() - t)
            if not res.get("chunks"):
                failed.append(server_id)
        if failed:
            log.error("Не проиндексированы: %s", ", ".join(failed))
            return 1
        log.info("ГОТОВО: серверов %d", len(servers))
        return 0
    finally:
        await db_manager.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
