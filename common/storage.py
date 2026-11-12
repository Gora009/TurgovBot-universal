"""Локальное файловое хранилище вместо Google Cloud Storage.

Повторяет ту часть интерфейса google.cloud.storage, которой пользуется бот
(bucket.blob(name) → upload_from_string / download_as_text / exists / delete /
generate_signed_url), поэтому код синка законов и прецедентов почти не меняется.

Файлы лежат в STORAGE_DIR (по умолчанию data/storage). URI файла — local://<имя>.
Именно такой URI записывается в law_documents.gemini_file_uri / precedents.gemini_file_uri
(имя столбца историческое) и передаётся модели: адаптер common.llm подставляет вместо
URI сам текст документа.

Ссылки на скачивание (пруфы ВИП, документы уголовных дел) подписываются HMAC и
отдаются API по маршруту /files/<имя>?exp=..&sig=.. (api/routers/files.py).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional, Union
from urllib.parse import quote

URI_SCHEME = "local://"


def storage_root() -> Path:
    import config
    root = Path(os.getenv("STORAGE_DIR") or os.path.join(config.BASE_DIR, "data", "storage"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def uri_prefix() -> str:
    return URI_SCHEME


def uri_for(blob_name: str) -> str:
    return URI_SCHEME + blob_name.lstrip("/")


def blob_name_from_uri(uri: str) -> Optional[str]:
    """local://a/b.txt → a/b.txt. Старые gs://<bucket>/a/b.txt тоже понимаем (перенос базы)."""
    if not uri:
        return None
    if uri.startswith(URI_SCHEME):
        return uri[len(URI_SCHEME):]
    if uri.startswith("gs://"):
        rest = uri[len("gs://"):]
        return rest.split("/", 1)[1] if "/" in rest else None
    return None


def _safe_path(blob_name: str) -> Path:
    root = storage_root().resolve()
    path = (root / blob_name).resolve()
    if root != path and root not in path.parents:
        raise ValueError(f"Путь вне хранилища: {blob_name!r}")
    return path


# --- Подписанные ссылки --------------------------------------------------------

def _signing_key() -> bytes:
    secret = os.getenv("FILES_SIGNING_SECRET") or os.getenv("DEVICE_JWT_SECRET") or ""
    if not secret:
        raise RuntimeError("Задайте FILES_SIGNING_SECRET (или DEVICE_JWT_SECRET) для ссылок на файлы")
    return secret.encode()


def sign(blob_name: str, expires_at: int) -> str:
    msg = f"{blob_name}\n{expires_at}".encode()
    return hmac.new(_signing_key(), msg, hashlib.sha256).hexdigest()


def verify(blob_name: str, expires_at: int, signature: str) -> bool:
    if expires_at < int(time.time()):
        return False
    return hmac.compare_digest(sign(blob_name, expires_at), signature or "")


def public_base_url() -> str:
    base = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError("Задайте PUBLIC_BASE_URL (адрес API снаружи) для ссылок на файлы")
    return base


def signed_url(blob_name: str, expiration: Union[timedelta, int] = timedelta(days=7)) -> str:
    seconds = int(expiration.total_seconds()) if isinstance(expiration, timedelta) else int(expiration)
    exp = int(time.time()) + seconds
    return f"{public_base_url()}/files/{quote(blob_name)}?exp={exp}&sig={sign(blob_name, exp)}"


# --- Совместимый с google.cloud.storage интерфейс ------------------------------

class LocalBlob:
    def __init__(self, name: str):
        self.name = name
        self.path = _safe_path(name)

    def upload_from_string(self, data: Union[str, bytes], content_type: Optional[str] = None) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Атомарная запись: читатель не увидит полузаписанный закон.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def download_as_bytes(self) -> bytes:
        return self.path.read_bytes()

    def download_as_text(self, encoding: str = "utf-8") -> str:
        return self.path.read_text(encoding=encoding)

    def exists(self) -> bool:
        return self.path.is_file()

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def generate_signed_url(self, version: str = "v4", expiration=timedelta(days=7), method: str = "GET") -> str:
        return signed_url(self.name, expiration)


class LocalBucket:
    name = "local"

    def blob(self, name: str) -> LocalBlob:
        return LocalBlob(name)


_bucket = LocalBucket()


def get_bucket() -> LocalBucket:
    return _bucket
