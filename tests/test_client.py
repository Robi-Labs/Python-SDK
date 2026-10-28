import base64
import json

import httpx
import pytest

from robiai import (
    AsyncRobi, AuthenticationError, BadRequestError, PaymentRequiredError, APIError,
    Robi, VideoGenerationError, save_b64,
)

CHUNK = 'data: {"choices":[{"index":0,"delta":{"content":"%s"},"finish_reason":null}]}\n\n'
USAGE = 'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
SSE = (CHUNK % "a") + (CHUNK % "b") + USAGE + "data: [DONE]\n\n"
SSE_ERR = (CHUNK % "a") + 'data: {"error":{"message":"Monthly spend limit reached","type":"upstream_error","code":402}}\n\ndata: [DONE]\n\n'


def sync(h):
    return Robi(api_key="k", base_url="http://x/v1", max_retries=1, http_client=httpx.Client(transport=httpx.MockTransport(h)))


def handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer k"
    if request.url.path.endswith("/models"):
        return httpx.Response(401, json={"detail": "Invalid or revoked API key"})
    body = request.read()
    if b'"stream":true' in body.replace(b" ", b""):
        return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]})


def test_chat_stream_and_detail_error():
    c = sync(handler)
    assert c.chat.completions.create(model="lime", messages=[])["choices"][0]["message"]["content"] == "hi"
    assert c.chat.completions.text(model="lime", messages=[]) == "ab"
    with pytest.raises(AuthenticationError) as ei:
        c.models.list()
    assert ei.value.message == "Invalid or revoked API key"


def test_object_detail_exposes_code_type():
    c = sync(lambda r: httpx.Response(400, json={"detail": {"message": "deprecated", "type": "model_deprecated", "code": "model_deprecated"}}))
    with pytest.raises(BadRequestError) as ei:
        c.chat.completions.create(model="vera", messages=[])
    assert ei.value.code == "model_deprecated" and ei.value.type == "model_deprecated"


def test_402_not_retried_429_retried():
    n = {"i": 0}

    def h402(req):
        n["i"] += 1
        return httpx.Response(402, json={"detail": "Insufficient credits"})

    with pytest.raises(PaymentRequiredError):
        sync(h402).models.list()
    assert n["i"] == 1

    m = {"i": 0}

    def h429(req):
        m["i"] += 1
        return httpx.Response(429, headers={"retry-after": "0"}) if m["i"] == 1 else httpx.Response(200, json={"data": []})

    assert sync(h429).models.list() == {"data": []} and m["i"] == 2


def test_mid_stream_error():
    c = sync(lambda r: httpx.Response(200, text=SSE_ERR))
    got = []
    with pytest.raises(PaymentRequiredError) as ei:
        for ch in c.chat.completions.create(model="m", messages=[], stream=True):
            got.append(ch)
    assert len(got) == 1 and ei.value.status == 402 and ei.value.message.startswith("Monthly")


def test_images_generate_and_save(tmp_path):
    png = base64.b64encode(b"\x89PNGdata").decode()
    seen = {}

    def h(req):
        seen["path"], seen["body"] = req.url.path, json.loads(req.read())
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": png}]})

    r = sync(h).images.generate(model="prism", prompt="bike", n=1, aspect_ratio="4:5", style=None)
    assert seen["path"] == "/v1/images/generations"
    assert seen["body"] == {"model": "prism", "prompt": "bike", "n": 1, "aspect_ratio": "4:5"}
    out = save_b64(r["data"][0], tmp_path / "o.png")
    assert out.read_bytes() == b"\x89PNGdata"


def test_image_edit_multipart(tmp_path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"JPEGDATA")
    seen = {}

    def h(req):
        seen["ct"], seen["body"] = req.headers["content-type"], req.read()
        return httpx.Response(200, json={"data": [{"b64_json": "AA=="}]})

    sync(h).images.edit(model="prism", image=src, prompt="studio", aspect_ratio="4:5")
    assert seen["ct"].startswith("multipart/form-data")
    for needle in (b'name="image"', b'filename="photo.jpg"', b"JPEGDATA", b'name="aspect_ratio"', b"4:5", b'name="model"'):
        assert needle in seen["body"]


def test_video_poll_download_and_failure(tmp_path):
    states = iter(["queued", "in_progress", "completed"])

    def h(req):
        p = req.url.path
        if req.method == "POST":
            assert json.loads(req.read())["seconds"] == "8"
            return httpx.Response(200, json={"id": "video_1", "status": "queued"})
        if p.endswith("/content"):
            return httpx.Response(200, content=b"MP4BYTES")
        return httpx.Response(200, json={"id": "video_1", "status": next(states)})

    c = sync(h)
    v = c.videos.create_and_poll(model="movi", prompt="p", seconds="8", poll_interval=0)
    assert v["status"] == "completed"
    assert c.videos.download_content("video_1") == b"MP4BYTES"
    dest = tmp_path / "o.mp4"
    c.videos.download_content("video_1", dest)
    assert dest.read_bytes() == b"MP4BYTES"

    def hf(req):
        if req.method == "POST":
            return httpx.Response(200, json={"id": "v", "status": "queued"})
        return httpx.Response(200, json={"id": "v", "status": "failed", "error": {"code": "audio_filtered", "message": "declined"}})

    with pytest.raises(VideoGenerationError) as ei:
        sync(hf).videos.create_and_poll(model="movi", prompt="p", poll_interval=0)
    assert ei.value.code == "audio_filtered"


def test_video_multipart_and_list_delete(tmp_path):
    img = tmp_path / "a.jpg"
    img.write_bytes(b"IMG")
    seen = []

    def h(req):
        seen.append((req.method, str(req.url.path), req.url.query, req.headers.get("content-type", "")))
        return httpx.Response(200, json={"id": "v", "status": "queued", "data": []})

    c = sync(h)
    c.videos.create(model="movi", prompt="p", input_reference=img, seconds=6)
    c.videos.list(limit=20, order="desc")
    c.videos.delete("v")
    assert seen[0][3].startswith("multipart/form-data")
    assert seen[1][2] == b"limit=20&order=desc"
    assert seen[2][0] == "DELETE"


async def test_async():
    c = AsyncRobi(api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await c.chat.completions.text(model="m", messages=[]) == "ab"
    with pytest.raises(AuthenticationError):
        await c.models.list()

    e = AsyncRobi(api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SSE_ERR))))
    with pytest.raises(APIError) as ei:
        await e.chat.completions.text(model="m", messages=[])
    assert ei.value.status == 402


async def test_async_video_poll():
    states = iter(["in_progress", "completed"])

    def h(req):
        if req.method == "POST":
            return httpx.Response(200, json={"id": "v", "status": "queued"})
        return httpx.Response(200, json={"id": "v", "status": next(states)})

    c = AsyncRobi(api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    assert (await c.videos.create_and_poll(model="movi", prompt="p", poll_interval=0))["status"] == "completed"
