"""Адаптер common.llm: интерфейс google-genai поверх OpenAI-совместимого API."""
import json

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from common import storage
from common.llm import compat, documents, settings


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for key in list(__import__("os").environ):
        if key.startswith("LLM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_SMART_MODEL", "big-model")
    monkeypatch.setenv("LLM_FAST_MODEL", "small-model")
    monkeypatch.setenv("LLM_VISION_BASE_URL", "https://vision.example.com/v1")
    monkeypatch.setenv("LLM_VISION_MODEL", "eye-model")
    monkeypatch.setenv("LLM_EMBED_MODEL", "emb-model")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    settings.reset_cache()
    documents.clear_cache()
    yield
    settings.reset_cache()
    compat._transport_override = None


class Recorder:
    def __init__(self, responder):
        self.requests = []
        self.responder = responder

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((str(request.url), dict(request.headers), body))
        return self.responder(request, body)


def install(responder):
    rec = Recorder(responder)
    compat._transport_override = httpx.MockTransport(rec)
    return rec


def chat_reply(text="", tool_calls=None, finish="stop", usage=None):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"model": "m", "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                     "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


client = compat.CompatClient()


async def test_plain_text_request_and_response():
    rec = install(lambda req, body: chat_reply("Ответ по ст. 12 УК"))
    resp = await client.aio.models.generate_content(
        model="smart",
        contents="Что грозит за угон?",
        config=types.GenerateContentConfig(system_instruction="Ты юрист", temperature=0.3,
                                           max_output_tokens=500, top_k=40),
    )
    assert resp.text == "Ответ по ст. 12 УК"
    assert resp.usage_metadata.total_token_count == 15
    url, headers, body = rec.requests[0]
    assert url == "https://api.example.com/v1/chat/completions"
    assert headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "big-model"
    assert body["messages"] == [{"role": "system", "content": "Ты юрист"},
                                {"role": "user", "content": "Что грозит за угон?"}]
    assert body["temperature"] == 0.3 and body["max_tokens"] == 500
    assert "top_k" not in body


@pytest.mark.parametrize("model, expected", [
    ("fast", "small-model"), ("smart", "big-model"),
    ("gemini-3.1-flash-lite", "small-model"), ("gemini-3-flash-preview", "big-model"),
])
async def test_role_routing(model, expected):
    rec = install(lambda req, body: chat_reply("ok"))
    await client.aio.models.generate_content(model=model, contents="x")
    assert rec.requests[0][2]["model"] == expected


async def test_fast_falls_back_to_smart(monkeypatch):
    monkeypatch.delenv("LLM_FAST_MODEL")
    settings.reset_cache()
    rec = install(lambda req, body: chat_reply("ok"))
    await client.aio.models.generate_content(model="fast", contents="x")
    assert rec.requests[0][2]["model"] == "big-model"


async def test_history_roles_and_documents_from_storage():
    storage.get_bucket().blob("srv/law1.txt").upload_from_string("НАЗВАНИЕ ДОКУМЕНТА: УК\n\nСтатья 1. ...")
    rec = install(lambda req, body: chat_reply("ok"))
    contents = [
        types.Content(role="user", parts=[types.Part(text="Привет")]),
        types.Content(role="model", parts=[types.Part(text="Здравствуйте")]),
        types.Content(role="user", parts=[
            types.Part.from_uri(file_uri=storage.uri_for("srv/law1.txt"), mime_type="text/plain"),
            types.Part(text="Вопрос"),
        ]),
    ]
    await client.aio.models.generate_content(model="smart", contents=contents)
    msgs = rec.requests[0][2]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert "Статья 1." in msgs[2]["content"] and msgs[2]["content"].endswith("Вопрос")


async def test_image_goes_to_vision_endpoint():
    rec = install(lambda req, body: chat_reply('{"name": "John"}'))
    part = types.Part(inline_data=types.Blob(data=b"\x89PNG", mime_type="image/png"))
    resp = await client.aio.models.generate_content(
        model="fast", contents=[part, "Распознай паспорт"],
        config=types.GenerateContentConfig(response_mime_type="application/json"))
    url, _, body = rec.requests[0]
    assert url.startswith("https://vision.example.com/v1/")
    assert body["model"] == "eye-model"
    blocks = body["messages"][-1]["content"]
    assert blocks[0]["type"] == "image_url" and blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert body["response_format"] == {"type": "json_object"}
    assert json.loads(resp.text) == {"name": "John"}


async def test_json_schema_hint_and_fence_stripping():
    rec = install(lambda req, body: chat_reply('```json\n{"ok": true}\n```'))
    schema = types.Schema(type=types.Type.OBJECT, properties={"ok": types.Schema(type=types.Type.BOOLEAN)})
    resp = await client.aio.models.generate_content(
        model="smart", contents="x",
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema))
    system = rec.requests[0][2]["messages"][0]["content"]
    assert '"type": "object"' in system and '"boolean"' in system
    assert resp.text == '{"ok": true}'


async def test_function_calling_round_trip():
    """Цикл Q&A v2: модель зовёт search_laws, бот возвращает результат, модель отвечает."""
    calls = iter([
        chat_reply("", tool_calls=[{"id": "call_a", "type": "function",
                                    "function": {"name": "search_laws", "arguments": '{"query": "угон"}'}}],
                   finish="tool_calls"),
        chat_reply("Итоговый ответ"),
    ])
    rec = install(lambda req, body: next(calls))
    tool = types.Tool(function_declarations=[types.FunctionDeclaration(
        name="search_laws", description="поиск",
        parameters=types.Schema(type=types.Type.OBJECT,
                                properties={"query": types.Schema(type=types.Type.STRING)}, required=["query"]))])
    cfg = types.GenerateContentConfig(tools=[tool])
    contents = [types.Content(role="user", parts=[types.Part(text="Вопрос")])]

    first = await client.aio.models.generate_content(model="smart", contents=contents, config=cfg)
    assert first.function_calls[0].name == "search_laws"
    assert first.function_calls[0].args == {"query": "угон"}
    contents.append(first.candidates[0].content)
    contents.append(types.Content(role="user", parts=[
        types.Part.from_function_response(name="search_laws", response={"result": "ст. 5"})]))
    second = await client.aio.models.generate_content(model="smart", contents=contents)
    assert second.text == "Итоговый ответ"

    sent_tools = rec.requests[0][2]["tools"][0]["function"]
    assert sent_tools["parameters"]["type"] == "object"
    msgs = rec.requests[1][2]["messages"]
    assert msgs[1]["role"] == "assistant" and msgs[1]["tool_calls"][0]["id"] == "call_a"
    assert msgs[2] == {"role": "tool", "tool_call_id": "call_a", "content": '{"result": "ст. 5"}'}


@pytest.mark.parametrize("status, exc", [(429, genai_errors.ClientError), (400, genai_errors.ClientError),
                                         (503, genai_errors.ServerError)])
async def test_http_errors_map_to_genai_errors(status, exc):
    install(lambda req, body: httpx.Response(status, json={"error": {"message": "boom"}}))
    with pytest.raises(exc) as info:
        await client.aio.models.generate_content(model="smart", contents="x")
    assert info.value.code == status
    if status == 429:
        assert "RESOURCE_EXHAUSTED" in str(info.value)


async def test_rate_limiter_classifies_errors():
    from common.rate_limiter import is_retryable_vertex_server_error
    assert is_retryable_vertex_server_error(genai_errors.ServerError(503, {"error": {"code": 503}}))


async def test_streaming():
    sse = ('data: {"choices":[{"delta":{"content":"Пер"}}]}\n\n'
           'data: {"choices":[{"delta":{"content":"вая строка\\n"}}]}\n\n'
           'data: [DONE]\n\n')
    rec = install(lambda req, body: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
    stream = await client.aio.models.generate_content_stream(model="smart", contents="x")
    chunks = [c.text async for c in stream]
    assert "".join(chunks) == "Первая строка\n"
    assert rec.requests[0][2]["stream"] is True


async def test_think_tags_are_stripped():
    install(lambda req, body: chat_reply("<think>рассуждаю</think>\nОтвет"))
    resp = await client.aio.models.generate_content(model="smart", contents="x")
    assert resp.text == "Ответ"


async def test_embeddings_padded_to_column_size():
    rec = install(lambda req, body: httpx.Response(200, json={"data": [
        {"index": 1, "embedding": [0.3, 0.4]}, {"index": 0, "embedding": [0.1, 0.2]}]}))
    resp = await client.aio.models.embed_content(
        model="gemini-embedding-001", contents=["a", "b"],
        config=types.EmbedContentConfig(output_dimensionality=4))
    assert [e.values for e in resp.embeddings] == [[0.1, 0.2, 0.0, 0.0], [0.3, 0.4, 0.0, 0.0]]
    assert rec.requests[0][0].endswith("/embeddings")
    assert "dimensions" not in rec.requests[0][2]


def test_embeddings_longer_than_column_rejected():
    with pytest.raises(ValueError):
        compat.fit_dimensions([0.0] * 5, 4)


async def test_unconfigured_role_gives_clear_error(monkeypatch):
    monkeypatch.delenv("LLM_EMBED_MODEL")
    settings.reset_cache()
    with pytest.raises(genai_errors.ClientError) as info:
        await client.aio.models.embed_content(model="embed", contents=["a"])
    assert "LLM_EMBED_MODEL" in str(info.value)


def test_storage_signed_urls(monkeypatch):
    monkeypatch.setenv("FILES_SIGNING_SECRET", "s3cret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://bot.example.com/")
    url = storage.signed_url("case_documents/a.docx")
    assert url.startswith("https://bot.example.com/files/case_documents/a.docx?exp=")
    exp = int(url.split("exp=")[1].split("&")[0])
    sig = url.split("sig=")[1]
    assert storage.verify("case_documents/a.docx", exp, sig)
    assert not storage.verify("case_documents/b.docx", exp, sig)
    with pytest.raises(ValueError):
        storage.get_bucket().blob("../../etc/passwd")


async def test_documents_over_budget_are_dropped_from_the_tail(monkeypatch):
    monkeypatch.setenv("LLM_MAX_INPUT_CHARS", "2500")
    bucket = storage.get_bucket()
    for name in ("a", "b", "c"):
        bucket.blob(f"srv/{name}.txt").upload_from_string(name.upper() * 1000)
    rec = install(lambda req, body: chat_reply("ok"))
    parts = [types.Part.from_uri(file_uri=storage.uri_for(f"srv/{n}.txt"), mime_type="text/plain") for n in "abc"]
    await client.aio.models.generate_content(model="smart", contents=parts + [types.Part(text="Вопрос")])
    sent = rec.requests[0][2]["messages"][-1]["content"]
    assert "A" * 1000 in sent and "B" * 1000 in sent and "C" * 1000 not in sent
    assert sent.endswith("Вопрос")


async def test_budget_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("LLM_MAX_INPUT_CHARS", "0")
    storage.get_bucket().blob("srv/big.txt").upload_from_string("X" * 500_000)
    rec = install(lambda req, body: chat_reply("ok"))
    await client.aio.models.generate_content(
        model="smart", contents=[types.Part.from_uri(file_uri=storage.uri_for("srv/big.txt"), mime_type="text/plain")])
    assert "X" * 500_000 in rec.requests[0][2]["messages"][-1]["content"]


async def test_real_service_runs_through_adapter():
    """Сквозная проверка: настоящая функция бота (извлечение фактов жалобы) через
    лимитер и адаптер получает JSON от «поставщика» и разбирает его своим кодом."""
    from common import rate_limiter
    from services.complaint_analyzer_service.analysis import extract_complaint_facts

    rate_limiter.init_client()
    reply = '{"facts": ["Игрок угнал машину"], "requirements": ["Наказать"]}'
    rec = install(lambda req, body: chat_reply(reply))
    result = await extract_complaint_facts("nova_new_era", "Жалоба: игрок угнал мою машину, прошу наказать.")
    assert result["facts"] == ["Игрок угнал машину"]
    url, _, body = rec.requests[0]
    assert url == "https://api.example.com/v1/chat/completions"
    assert body["model"] == "big-model"
    assert "угнал мою машину" in json.dumps(body, ensure_ascii=False)
