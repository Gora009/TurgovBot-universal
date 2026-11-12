"""Скачивание файлов из локального хранилища по подписанной ссылке.

Ссылки выдаёт common.storage.signed_url(): /files/<имя>?exp=<unix-time>&sig=<hmac>.
Так отдаются документы уголовных дел (.docx) и пруфы заявок на ВИП — то, что раньше
лежало в Google Docs / Cloud Storage. Без верной подписи или после exp — 404.
"""
import mimetypes
import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from common import storage

router = APIRouter(tags=["Files"])

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.get("/files/{blob_name:path}", include_in_schema=False)
async def download_file(blob_name: str, exp: int = Query(...), sig: str = Query(...)):
    try:
        if not storage.verify(blob_name, exp, sig):
            raise HTTPException(status_code=404)
        blob = storage.get_bucket().blob(blob_name)
    except (ValueError, RuntimeError):
        raise HTTPException(status_code=404)
    if not blob.exists():
        raise HTTPException(status_code=404)
    filename = os.path.basename(blob_name)
    media = _DOCX if filename.endswith(".docx") else (mimetypes.guess_type(filename)[0] or "application/octet-stream")
    return FileResponse(blob.path, media_type=media, filename=filename)
