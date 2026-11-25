"""Клиент с интерфейсом google-genai поверх любого OpenAI-совместимого API.

Весь бот обращается к модели одинаково:

    client.aio.models.generate_content(model=..., contents=..., config=types.GenerateContentConfig(...))
    client.aio.models.generate_content_stream(...)
    client.aio.models.embed_content(...)

CompatClient повторяет этот интерфейс, а внутри ходит в /chat/completions и /embeddings.
Типы google.genai используются только как структуры данных (сетевой части SDK здесь нет),
поэтому 60+ мест вызова, промпты и тесты остаются без изменений.

Что переводится:
  * Content/Part → сообщения OpenAI; роль 'model' → 'assistant';
  * Part.from_uri(local://…, gs://…) → текст документа из хранилища/БД (законы, прецеденты);
  * Part(inline_data=Blob(image/*)) → image_url (data URI); такой запрос уходит в роль vision;
  * system_instruction, temperature, top_p, max_output_tokens, stop_sequences, seed;
  * response_mime_type=application/json (+ response_schema) → response_format / инструкция;
  * tools с function_declarations → tools OpenAI; function_call / function_response — туда и обратно;
  * ошибки HTTP → google.genai.errors.ClientError/ServerError с тем же кодом, поэтому
    лимитер бота по-прежнему повторяет 429 и 5xx.
Игнорируется: top_k, candidate_count, thinking_config (если не включён LLM_SEND_REASONING_EFFORT).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Tuple

import httpx
from google.genai import errors as genai_errors
from google.genai import types

from . import settings
from .documents import resolve_documents

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)

_FINISH = {
    "stop": types.FinishReason.STOP,
    "tool_calls": types.FinishReason.STOP,
    "function_call": types.FinishReason.STOP,
    "length": types.FinishReason.MAX_TOKENS,
    "content_filter": types.FinishReason.SAFETY,
}


# ---------------------------------------------------------------------------
# Выбор роли (поставщика) по имени модели
# ---------------------------------------------------------------------------

# Уровни размышления Gemini → reasoning_effort. "minimal" у OpenAI-совместимых API нет
# (DeepSeek V4 принимает low/medium/high/max), ближайший — low.
_REASONING_EFFORT = {"minimal": "low", "thinking_level_unspecified": "medium"}


def role_for_model(model: Optional[str]) -> str:
    """Имя модели в коде бота — это роль ('fast', 'smart', 'deep', 'vision', 'embed').

    Старые имена Gemini тоже понимаем (на случай, если их задали через env):
    *lite* → fast, *embedding* → embed, остальное → smart.
    """
    name = (model or "").strip().lower()
    if name in settings.ROLES:
        return name
    if "embed" in name:
        return "embed"
    if "lite" in name:  # не "mini": это подстрока "gemini"
        return "fast"
    return "smart"


# ---------------------------------------------------------------------------
# Вспомогательные функции разбора config
# ---------------------------------------------------------------------------

def _cfg(config: Any, name: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(name, default)
    value = getattr(config, name, default)
    return default if value is None else value


def _text_of(value: Any) -> str:
    """system_instruction бывает строкой, Part, Content или их списком."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, types.Part):
        return value.text or ""
    if isinstance(value, types.Content):
        return "\n".join(p.text for p in (value.parts or []) if p.text)
    if isinstance(value, (list, tuple)):
        return "\n".join(_text_of(v) for v in value)
    return str(value)


def _normalize_contents(contents: Any) -> List[types.Content]:
    """Приводит contents к списку Content, как это делает SDK."""
    if contents is None:
        return []
    if isinstance(contents, (str, types.Part, types.Content, dict)):
        contents = [contents]
    result: List[types.Content] = []
    pending: List[types.Part] = []

    def flush():
        if pending:
            result.append(types.Content(role="user", parts=list(pending)))
            pending.clear()

    for item in contents:
        if isinstance(item, types.Content):
            flush()
            result.append(item)
        elif isinstance(item, dict) and "parts" in item:
            flush()
            result.append(types.Content.model_validate(item))
        elif isinstance(item, types.Part):
            pending.append(item)
        elif isinstance(item, str):
            pending.append(types.Part(text=item))
        else:
            pending.append(types.Part(text=str(item)))
    flush()
    return result


def _json_schema(schema: Any) -> Optional[dict]:
    """types.Schema / dict / pydantic-модель → обычная JSON Schema (типы в нижнем регистре)."""
    if schema is None:
        return None
    if isinstance(schema, types.Schema):
        raw = schema.model_dump(exclude_none=True, mode="json")
    elif isinstance(schema, dict):
        raw = schema
    elif hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    else:
        return None

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, val in node.items():
                if key == "type" and isinstance(val, str):
                    out[key] = val.lower()
                elif key in ("property_ordering", "propertyOrdering"):
                    continue
                elif key == "any_of":
                    out["anyOf"] = fix(val)
                else:
                    out[key] = fix(val)
            return out
        if isinstance(node, list):
            return [fix(v) for v in node]
        return node

    return fix(raw)


# ---------------------------------------------------------------------------
# Content → сообщения OpenAI
# ---------------------------------------------------------------------------

def _collect_uris(contents: Iterable[types.Content]) -> List[str]:
    uris = []
    for content in contents:
        for part in content.parts or []:
            fd = part.file_data
            if fd is not None and fd.file_uri and fd.file_uri not in uris:
                uris.append(fd.file_uri)
    return uris


def _blob_block(blob: types.Blob) -> Dict[str, Any]:
    mime = (blob.mime_type or "application/octet-stream").lower()
    data = blob.data or b""
    if mime.startswith("image/"):
        b64 = base64.b64encode(data).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
    if mime.startswith("text/") or mime in ("application/json",):
        return {"type": "text", "text": data.decode("utf-8", errors="replace")}
    raise ValueError(f"Тип вложения {mime} не поддерживается OpenAI-совместимым API")


def _to_messages(
    contents: List[types.Content],
    system_text: str,
    documents: Dict[str, str],
) -> Tuple[List[Dict[str, Any]], bool]:
    """Возвращает (messages, есть_картинки)."""
    messages: List[Dict[str, Any]] = []
    if system_text:
        messages.append({"role": "system", "content": system_text})
    has_images = False
    pending_call_ids: List[str] = []
    call_seq = 0

    for content in contents:
        role = (content.role or "user").lower()
        blocks: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []

        for part in content.parts or []:
            if part.thought:
                continue  # «мысли» Gemini в историю не передаём
            if part.text is not None and part.text != "":
                blocks.append({"type": "text", "text": part.text})
            elif part.file_data is not None:
                uri = part.file_data.file_uri or ""
                text = documents.get(uri)
                if text is None:
                    continue  # недоступен или не влез в бюджет — залогировано в fit_documents
                blocks.append({"type": "text", "text": f"=== ДОКУМЕНТ ===\n{text}\n=== КОНЕЦ ДОКУМЕНТА ==="})
            elif part.inline_data is not None:
                block = _blob_block(part.inline_data)
                has_images = has_images or block["type"] == "image_url"
                blocks.append(block)
            elif part.function_call is not None:
                fc = part.function_call
                call_seq += 1
                call_id = fc.id or f"call_{call_seq}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": fc.name or "", "arguments": json.dumps(fc.args or {}, ensure_ascii=False)},
                })
            elif part.function_response is not None:
                fr = part.function_response
                call_id = fr.id or (pending_call_ids.pop(0) if pending_call_ids else f"call_{fr.name}")
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(fr.response or {}, ensure_ascii=False),
                })

        if role in ("model", "assistant"):
            msg: Dict[str, Any] = {"role": "assistant",
                                   "content": "\n\n".join(b["text"] for b in blocks if b["type"] == "text") or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
                pending_call_ids = [c["id"] for c in tool_calls]
            if msg["content"] is None and not tool_calls:
                continue
            messages.append(msg)
            continue

        messages.extend(tool_results)
        if not blocks:
            continue
        if any(b["type"] != "text" for b in blocks):
            messages.append({"role": "user", "content": blocks})
        else:
            messages.append({"role": "user", "content": "\n\n".join(b["text"] for b in blocks)})

    return messages, has_images


def _tools_payload(tools: Any) -> Optional[List[Dict[str, Any]]]:
    out = []
    for tool in tools or []:
        decls = getattr(tool, "function_declarations", None) or (
            tool.get("function_declarations") if isinstance(tool, dict) else None)
        if not decls:
            logger.warning("[LLM] инструмент %r не поддерживается (только function_declarations)", tool)
            continue
        for d in decls:
            name = getattr(d, "name", None) or d.get("name")
            desc = getattr(d, "description", None) or (d.get("description") if isinstance(d, dict) else "")
            params = getattr(d, "parameters", None) if not isinstance(d, dict) else d.get("parameters")
            schema = _json_schema(params) or {"type": "object", "properties": {}}
            out.append({"type": "function", "function": {"name": name, "description": desc or "", "parameters": schema}})
    return out or None


def fit_documents(uris: List[str], documents: Dict[str, str], other_chars: int, budget: int) -> Dict[str, str]:
    """Оставляет документы в порядке запроса, пока суммарный объём влезает в бюджет.

    Gemini принимал ~1 млн токенов, у большинства других моделей окно 32–200 тыс.
    Запасной путь «роутер ничего не выбрал → все законы сервера» иначе упирался бы в
    ошибку контекста. Порядок URI = порядок, в котором их положил код (сначала законы,
    выбранные роутером, потом прецеденты), поэтому отбрасываются хвостовые документы.
    """
    missing = [u for u in uris if u not in documents]
    if missing:
        logger.error("[LLM] документы недоступны, пропущены: %s", missing)
    if budget <= 0:
        return documents
    kept: Dict[str, str] = {}
    used = other_chars
    dropped = []
    for uri in uris:
        text = documents.get(uri)
        if text is None:
            continue
        if used + len(text) > budget:
            dropped.append(uri)
            continue
        kept[uri] = text
        used += len(text)
    if dropped:
        logger.warning("[LLM] не влезли в LLM_MAX_INPUT_CHARS=%d: отброшено %d документов из %d",
                       budget, len(dropped), len(uris))
    return kept


def _plain_chars(contents: List[types.Content], system_text: str) -> int:
    n = len(system_text or "")
    for c in contents:
        for p in c.parts or []:
            if p.text:
                n += len(p.text)
    return n


def build_payload(model_name: str, contents: Any, config: Any, documents: Dict[str, str]) -> Tuple[str, Dict[str, Any]]:
    """Собирает тело /chat/completions. Возвращает (роль, payload)."""
    norm = _normalize_contents(contents)
    system_text = _text_of(_cfg(config, "system_instruction"))
    documents = fit_documents(_collect_uris(norm), documents, _plain_chars(norm, system_text),
                              settings.max_input_chars())

    wants_json = (str(_cfg(config, "response_mime_type", "")) or "").lower() == "application/json"
    schema = _json_schema(_cfg(config, "response_schema") or _cfg(config, "response_json_schema"))
    mode = settings.json_mode()
    if wants_json:
        hint = "Ответь строго валидным JSON-объектом, без пояснений и без markdown-обёртки ```."
        if schema and mode != "json_schema":
            hint += "\nJSON Schema ответа:\n" + json.dumps(schema, ensure_ascii=False)
        system_text = (system_text + "\n\n" + hint).strip()

    messages, has_images = _to_messages(norm, system_text, documents)
    role = role_for_model(model_name)
    if has_images and role != "vision":
        role = "vision"

    payload: Dict[str, Any] = {"messages": messages}
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("max_output_tokens", "max_tokens"), ("seed", "seed"),
                     ("presence_penalty", "presence_penalty"), ("frequency_penalty", "frequency_penalty")):
        value = _cfg(config, src)
        if value is not None:
            payload[dst] = value
    stop = _cfg(config, "stop_sequences")
    if stop:
        payload["stop"] = list(stop)

    if wants_json:
        if mode == "json_schema" and schema:
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "response", "schema": schema}}
        elif mode in ("json_object", "json_schema"):
            payload["response_format"] = {"type": "json_object"}

    tools = _tools_payload(_cfg(config, "tools"))
    if tools:
        payload["tools"] = tools

    thinking = _cfg(config, "thinking_config")
    if thinking is not None and settings.send_reasoning_effort(role):
        level = getattr(thinking, "thinking_level", None)
        level = getattr(level, "value", level)
        if level:
            payload["reasoning_effort"] = _REASONING_EFFORT.get(str(level).lower(), str(level).lower())

    return role, payload


# ---------------------------------------------------------------------------
# Ответ OpenAI → GenerateContentResponse
# ---------------------------------------------------------------------------

def _clean_text(text: str, wants_json: bool) -> str:
    text = _THINK_RE.sub("", text or "")
    if wants_json:
        m = _FENCE_RE.match(text)
        if m:
            text = m.group(1)
    return text


def _usage(raw: Optional[dict]) -> Optional[types.GenerateContentResponseUsageMetadata]:
    if not raw:
        return None
    details = raw.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens", raw.get("prompt_cache_hit_tokens"))
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=raw.get("prompt_tokens"),
        candidates_token_count=raw.get("completion_tokens"),
        total_token_count=raw.get("total_tokens"),
        cached_content_token_count=cached,
        thoughts_token_count=(raw.get("completion_tokens_details") or {}).get("reasoning_tokens"),
    )


def to_response(data: dict, wants_json: bool = False) -> types.GenerateContentResponse:
    choices = data.get("choices") or []
    candidates = []
    for choice in choices[:1]:
        message = choice.get("message") or {}
        parts: List[types.Part] = []
        text = _clean_text(message.get("content") or "", wants_json)
        if text:
            parts.append(types.Part(text=text))
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            parts.append(types.Part(function_call=types.FunctionCall(
                id=call.get("id"), name=fn.get("name"), args=args)))
        candidates.append(types.Candidate(
            content=types.Content(role="model", parts=parts),
            finish_reason=_FINISH.get(choice.get("finish_reason") or "", types.FinishReason.OTHER),
            index=0,
        ))
    return types.GenerateContentResponse(
        candidates=candidates,
        usage_metadata=_usage(data.get("usage")),
        model_version=data.get("model"),
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _raise_for_status(status: int, body: Any) -> None:
    if status < 400:
        return
    if isinstance(body, dict) and "error" in body:
        err = body["error"] if isinstance(body["error"], dict) else {"message": str(body["error"])}
    else:
        err = {"message": str(body)[:500]}
    err.setdefault("code", status)
    if status == 429:
        err["status"] = "RESOURCE_EXHAUSTED"
    payload = {"error": err}
    if status >= 500:
        raise genai_errors.ServerError(status, payload)
    raise genai_errors.ClientError(status, payload)


class _HttpPool:
    """Один httpx.AsyncClient на event loop: переиспользует соединения (быстрее ответ)."""

    def __init__(self):
        self._clients: Dict[int, httpx.AsyncClient] = {}

    def get(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        client = self._clients.get(id(loop))
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout(), connect=20.0))
            self._clients[id(loop)] = client
        return client


_pool = _HttpPool()
_transport_override: Optional[httpx.AsyncBaseTransport] = None  # для тестов


def _http() -> httpx.AsyncClient:
    if _transport_override is not None:
        return httpx.AsyncClient(transport=_transport_override, timeout=settings.request_timeout())
    return _pool.get()


def _headers(ep: settings.Endpoint) -> Dict[str, str]:
    headers = {"Content-Type": "application/json", **settings.extra_headers()}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    return headers


def _endpoint_for(role: str) -> settings.Endpoint:
    ep = settings.endpoint(role)
    if not ep.configured:
        raise genai_errors.ClientError(400, {"error": {
            "code": 400,
            "message": f"Модель для роли '{role}' не настроена: задайте LLM_{role.upper()}_MODEL "
                       f"(и при необходимости LLM_{role.upper()}_BASE_URL / _API_KEY) или общие LLM_*",
        }})
    return ep


async def _post_json(ep: settings.Endpoint, path: str, payload: dict) -> dict:
    client = _http()
    try:
        resp = await client.post(f"{ep.base_url}{path}", json=payload, headers=_headers(ep))
    except httpx.TimeoutException as e:
        raise TimeoutError(f"LLM {ep.role}: таймаут запроса") from e
    except httpx.TransportError as e:
        raise genai_errors.ServerError(503, {"error": {"code": 503, "message": f"сеть: {e}"}}) from e
    finally:
        if _transport_override is not None:
            await client.aclose()
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    _raise_for_status(resp.status_code, body)
    if not isinstance(body, dict):
        raise genai_errors.ServerError(502, {"error": {"code": 502, "message": "ответ не JSON"}})
    return body


# ---------------------------------------------------------------------------
# Публичный клиент
# ---------------------------------------------------------------------------

class _AsyncModels:
    async def generate_content(self, *, model: str, contents: Any, config: Any = None) -> types.GenerateContentResponse:
        documents = await resolve_documents(_collect_uris(_normalize_contents(contents)))
        role, payload = build_payload(model, contents, config, documents)
        ep = _endpoint_for(role)
        payload["model"] = ep.model
        data = await _post_json(ep, "/chat/completions", payload)
        wants_json = (str(_cfg(config, "response_mime_type", "")) or "").lower() == "application/json"
        return to_response(data, wants_json)

    async def generate_content_stream(self, *, model: str, contents: Any, config: Any = None) -> AsyncIterator[types.GenerateContentResponse]:
        documents = await resolve_documents(_collect_uris(_normalize_contents(contents)))
        role, payload = build_payload(model, contents, config, documents)
        ep = _endpoint_for(role)
        payload["model"] = ep.model
        payload["stream"] = True
        return self._stream(ep, payload)

    async def _stream(self, ep: settings.Endpoint, payload: dict) -> AsyncIterator[types.GenerateContentResponse]:
        client = _http()
        in_think = False
        try:
            async with client.stream("POST", f"{ep.base_url}/chat/completions",
                                     json=payload, headers=_headers(ep)) as resp:
                if resp.status_code >= 400:
                    raw = await resp.aread()
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = raw.decode("utf-8", errors="replace")
                    _raise_for_status(resp.status_code, body)
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
                    text = delta.get("content") or ""
                    # Локальные reasoning-модели шлют <think>…</think> прямо в тексте.
                    if "<think>" in text:
                        in_think = True
                        text = text.split("<think>", 1)[0]
                    if in_think:
                        if "</think>" in text:
                            in_think = False
                            text = text.split("</think>", 1)[1]
                        else:
                            continue
                    if text:
                        yield types.GenerateContentResponse(candidates=[types.Candidate(
                            content=types.Content(role="model", parts=[types.Part(text=text)]), index=0)])
        except httpx.TimeoutException as e:
            raise TimeoutError(f"LLM {ep.role}: таймаут потока") from e
        except httpx.TransportError as e:
            raise genai_errors.ServerError(503, {"error": {"code": 503, "message": f"сеть: {e}"}}) from e
        finally:
            if _transport_override is not None:
                await client.aclose()

    async def embed_content(self, *, model: str, contents: Any, config: Any = None) -> types.EmbedContentResponse:
        ep = _endpoint_for("embed")
        texts = [contents] if isinstance(contents, str) else [
            c if isinstance(c, str) else _text_of(c) for c in contents]
        target = _cfg(config, "output_dimensionality")
        payload: Dict[str, Any] = {"model": ep.model, "input": texts}
        if target and settings.embed_send_dimensions():
            payload["dimensions"] = int(target)
        data = await _post_json(ep, "/embeddings", payload)
        rows = sorted(data.get("data") or [], key=lambda r: r.get("index", 0))
        embeddings = []
        for row in rows:
            values = [float(v) for v in row.get("embedding") or []]
            if target:
                values = fit_dimensions(values, int(target))
            embeddings.append(types.ContentEmbedding(values=values))
        return types.EmbedContentResponse(embeddings=embeddings)


def fit_dimensions(values: List[float], target: int) -> List[float]:
    """Подгоняет вектор под размерность столбца pgvector.

    Короче — дополняем нулями: косинусная близость между векторами одной модели не
    меняется. Длиннее — ошибка: обрезать вектор произвольной модели нельзя (только
    модели с MRL, напр. text-embedding-3-*, через LLM_EMBED_SEND_DIMENSIONS=true).
    """
    if len(values) == target:
        return values
    if len(values) < target:
        return values + [0.0] * (target - len(values))
    raise ValueError(
        f"Модель эмбеддингов вернула {len(values)} измерений, а столбец рассчитан на {target}. "
        "Включите LLM_EMBED_SEND_DIMENSIONS=true (если модель умеет) или выберите модель "
        f"с размерностью ≤ {target}."
    )


class _Aio:
    def __init__(self):
        self.models = _AsyncModels()


class CompatClient:
    """Замена google.genai.Client: client.aio.models.generate_content(...) и т.д."""

    def __init__(self):
        self.aio = _Aio()

    def __repr__(self) -> str:
        ep = settings.endpoint("smart")
        return f"<CompatClient smart={ep.model} @ {ep.base_url}>"
