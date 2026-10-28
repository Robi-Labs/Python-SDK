from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Union

import httpx

from . import _base as b
from .errors import ConnectionError, TimeoutError, VideoGenerationError

JSON = Dict[str, Any]
FileInput = b.FileInput
_PENDING = ("queued", "in_progress")


class _Resource:
    def __init__(self, client: Any):
        self._c = client


# ======================================================================== sync resources

class _ChatCompletions(_Resource):
    def create(self, *, model: str, messages: List[JSON], stream: bool = False, **params: Any
               ) -> Union[JSON, Iterator[JSON]]:
        """Create a chat completion. With ``stream=True`` returns an iterator of chunks.

        Extra keyword args (``temperature``, ``top_p``, ``max_tokens``, ``response_format``,
        ``reasoning_effort``, ``stop``, ``seed``, ...) are forwarded as-is. ``content`` may be a
        string or a list of parts, e.g. ``{"type": "image_url", "image_url": {"url": ...}}``.
        """
        body = {"model": model, "messages": messages, **params}
        if stream:
            return self._c._stream("/chat/completions", {**body, "stream": True})
        return self._c._json("POST", "/chat/completions", json=body)

    def text(self, *, model: str, messages: List[JSON], **params: Any) -> str:
        """Stream a completion and return the concatenated text."""
        return "".join(
            (c["choices"][0]["delta"].get("content") or "") if c.get("choices") else ""
            for c in self.create(model=model, messages=messages, stream=True, **params)
        )


class _Chat(_Resource):
    def __init__(self, client: Any):
        super().__init__(client)
        self.completions = _ChatCompletions(client)


class _Images(_Resource):
    def generate(self, *, model: str, prompt: str, n: Optional[int] = None,
                 aspect_ratio: Optional[str] = None, size: Optional[str] = None,
                 quality: Optional[str] = None, style: Optional[str] = None,
                 image: Optional[str] = None, images: Optional[List[str]] = None, **extra: Any) -> JSON:
        """Generate images. Result: ``{"data": [{"b64_json": ...}]}`` (see ``save_b64``)."""
        body = b._clean({"model": model, "prompt": prompt, "n": n, "aspect_ratio": aspect_ratio,
                         "size": size, "quality": quality, "style": style, "image": image,
                         "images": images, **extra})
        return self._c._json("POST", "/images/generations", json=body)

    def edit(self, *, model: str, image: Union[FileInput, List[FileInput]], prompt: str, **extra: Any) -> JSON:
        """Edit 1-4 reference images (path, bytes or file object) via multipart.

        Extra kwargs (``aspect_ratio``, ``quality``, ``style``, ``n``, ...) become form fields.
        """
        return self._c._json("POST", "/images/edits", **b.image_edit_request(model, image, prompt, extra))


class _Videos(_Resource):
    def create(self, *, model: str, prompt: str, seconds: Union[str, int, None] = None,
               size: Optional[str] = None, aspect_ratio: Optional[str] = None,
               resolution: Optional[str] = None, negative_prompt: Optional[str] = None,
               style: Optional[str] = None, image: Optional[str] = None,
               input_reference: Optional[FileInput] = None, **extra: Any) -> JSON:
        """Start a video job (returns immediately with status ``queued``).

        Pass ``image`` (data URL/base64) for JSON, or ``input_reference`` (path/bytes/file) for multipart.
        """
        fields = {"seconds": seconds, "size": size, "aspect_ratio": aspect_ratio, "resolution": resolution,
                  "negative_prompt": negative_prompt, "style": style, "image": image}
        return self._c._json("POST", "/videos", **b.video_request(model, prompt, input_reference, fields, extra))

    def retrieve(self, video_id: str) -> JSON:
        return self._c._json("GET", f"/videos/{video_id}")

    def list(self, *, limit: Optional[int] = None, after: Optional[str] = None, order: Optional[str] = None) -> JSON:
        return self._c._json("GET", "/videos", params=b._clean({"limit": limit, "after": after, "order": order}))

    def delete(self, video_id: str) -> JSON:
        return self._c._json("DELETE", f"/videos/{video_id}")

    def download_content(self, video_id: str, path: Union[str, os.PathLike, None] = None) -> Union[bytes, Path]:
        """Download the MP4. Returns bytes, or streams to ``path`` and returns it. Clips expire after 24h (410)."""
        res = self._c._send("GET", f"/videos/{video_id}/content", stream=True, headers={"Accept": "*/*"})
        try:
            if path is None:
                return res.read()
            with open(path, "wb") as f:
                for chunk in res.iter_bytes():
                    f.write(chunk)
            return Path(path)
        finally:
            res.close()

    def create_and_poll(self, *, poll_interval: float = 5.0, timeout: float = 600.0, **params: Any) -> JSON:
        """Create a video and wait until it completes. Raises ``VideoGenerationError`` on failure."""
        video = self.create(**params)
        deadline = time.monotonic() + timeout
        while video["status"] in _PENDING:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Video {video['id']} not finished after {timeout}s")
            time.sleep(poll_interval)
            video = self.retrieve(video["id"])
        if video["status"] == "failed":
            err = video.get("error") or {}
            raise VideoGenerationError(err.get("code"), err.get("message"), video)
        return video


class _Models(_Resource):
    def list(self) -> JSON:
        return self._c._json("GET", "/models")

    def retrieve(self, model_id: str) -> JSON:
        return self._c._json("GET", f"/models/{model_id}")


class Robi:
    """Synchronous Robi Labs client."""

    def __init__(self, *, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: float = 60.0, max_retries: int = 2,
                 default_headers: Optional[Dict[str, str]] = None,
                 http_client: Optional[httpx.Client] = None):
        self._key, self._base = b._resolve(api_key, base_url)
        self._max_retries = max_retries
        self._headers = b._headers(self._key, default_headers)
        self._http = http_client or httpx.Client(timeout=timeout, follow_redirects=True)
        self.chat = _Chat(self)
        self.images = _Images(self)
        self.videos = _Videos(self)
        self.models = _Models(self)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Robi":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _send(self, method: str, path: str, *, stream: bool = False, headers: Optional[Dict[str, str]] = None,
              **kw: Any) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                req = self._http.build_request(method, self._base + path,
                                               headers={**self._headers, **(headers or {})}, **kw)
                res = self._http.send(req, stream=stream)
            except httpx.TimeoutException as e:
                if attempt < self._max_retries:
                    time.sleep(b._delay(attempt, None)); continue
                raise TimeoutError(f"Request timed out: {e}") from e
            except httpx.TransportError as e:
                if attempt < self._max_retries:
                    time.sleep(b._delay(attempt, None)); continue
                raise ConnectionError(f"Connection error: {e}") from e
            if res.is_success:
                return res
            if stream:
                res.read()
            if b._retryable(res.status_code) and attempt < self._max_retries:
                res.close()
                time.sleep(b._delay(attempt, res.headers.get("retry-after")))
                continue
            b._raise_for(res)
        raise AssertionError("unreachable")

    def _json(self, method: str, path: str, **kw: Any) -> JSON:
        return self._send(method, path, **kw).json()

    def _stream(self, path: str, body: JSON) -> Iterator[JSON]:
        res = self._send("POST", path, json=body, stream=True, headers={"Accept": "text/event-stream"})
        try:
            yield from b.sse_events(res.iter_lines())
        finally:
            res.close()


# ======================================================================= async resources

class _AChatCompletions(_Resource):
    async def create(self, *, model: str, messages: List[JSON], stream: bool = False, **params: Any
                     ) -> Union[JSON, AsyncIterator[JSON]]:
        body = {"model": model, "messages": messages, **params}
        if stream:
            return self._c._stream("/chat/completions", {**body, "stream": True})
        return await self._c._json("POST", "/chat/completions", json=body)

    async def text(self, *, model: str, messages: List[JSON], **params: Any) -> str:
        out = []
        stream = await self.create(model=model, messages=messages, stream=True, **params)
        async for c in stream:
            if c.get("choices"):
                out.append(c["choices"][0]["delta"].get("content") or "")
        return "".join(out)


class _AChat(_Resource):
    def __init__(self, client: Any):
        super().__init__(client)
        self.completions = _AChatCompletions(client)


class _AImages(_Resource):
    async def generate(self, *, model: str, prompt: str, n: Optional[int] = None,
                       aspect_ratio: Optional[str] = None, size: Optional[str] = None,
                       quality: Optional[str] = None, style: Optional[str] = None,
                       image: Optional[str] = None, images: Optional[List[str]] = None, **extra: Any) -> JSON:
        body = b._clean({"model": model, "prompt": prompt, "n": n, "aspect_ratio": aspect_ratio,
                         "size": size, "quality": quality, "style": style, "image": image,
                         "images": images, **extra})
        return await self._c._json("POST", "/images/generations", json=body)

    async def edit(self, *, model: str, image: Union[FileInput, List[FileInput]], prompt: str, **extra: Any) -> JSON:
        return await self._c._json("POST", "/images/edits", **b.image_edit_request(model, image, prompt, extra))


class _AVideos(_Resource):
    async def create(self, *, model: str, prompt: str, seconds: Union[str, int, None] = None,
                     size: Optional[str] = None, aspect_ratio: Optional[str] = None,
                     resolution: Optional[str] = None, negative_prompt: Optional[str] = None,
                     style: Optional[str] = None, image: Optional[str] = None,
                     input_reference: Optional[FileInput] = None, **extra: Any) -> JSON:
        fields = {"seconds": seconds, "size": size, "aspect_ratio": aspect_ratio, "resolution": resolution,
                  "negative_prompt": negative_prompt, "style": style, "image": image}
        return await self._c._json("POST", "/videos", **b.video_request(model, prompt, input_reference, fields, extra))

    async def retrieve(self, video_id: str) -> JSON:
        return await self._c._json("GET", f"/videos/{video_id}")

    async def list(self, *, limit: Optional[int] = None, after: Optional[str] = None,
                   order: Optional[str] = None) -> JSON:
        return await self._c._json("GET", "/videos", params=b._clean({"limit": limit, "after": after, "order": order}))

    async def delete(self, video_id: str) -> JSON:
        return await self._c._json("DELETE", f"/videos/{video_id}")

    async def download_content(self, video_id: str, path: Union[str, os.PathLike, None] = None) -> Union[bytes, Path]:
        res = await self._c._send("GET", f"/videos/{video_id}/content", stream=True, headers={"Accept": "*/*"})
        try:
            if path is None:
                return await res.aread()
            with open(path, "wb") as f:
                async for chunk in res.aiter_bytes():
                    f.write(chunk)
            return Path(path)
        finally:
            await res.aclose()

    async def create_and_poll(self, *, poll_interval: float = 5.0, timeout: float = 600.0, **params: Any) -> JSON:
        video = await self.create(**params)
        deadline = time.monotonic() + timeout
        while video["status"] in _PENDING:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Video {video['id']} not finished after {timeout}s")
            await asyncio.sleep(poll_interval)
            video = await self.retrieve(video["id"])
        if video["status"] == "failed":
            err = video.get("error") or {}
            raise VideoGenerationError(err.get("code"), err.get("message"), video)
        return video


class _AModels(_Resource):
    async def list(self) -> JSON:
        return await self._c._json("GET", "/models")

    async def retrieve(self, model_id: str) -> JSON:
        return await self._c._json("GET", f"/models/{model_id}")


class AsyncRobi:
    """Asynchronous Robi Labs client (same surface; methods are awaitable)."""

    def __init__(self, *, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: float = 60.0, max_retries: int = 2,
                 default_headers: Optional[Dict[str, str]] = None,
                 http_client: Optional[httpx.AsyncClient] = None):
        self._key, self._base = b._resolve(api_key, base_url)
        self._max_retries = max_retries
        self._headers = b._headers(self._key, default_headers)
        self._http = http_client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self.chat = _AChat(self)
        self.images = _AImages(self)
        self.videos = _AVideos(self)
        self.models = _AModels(self)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncRobi":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _send(self, method: str, path: str, *, stream: bool = False,
                    headers: Optional[Dict[str, str]] = None, **kw: Any) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                req = self._http.build_request(method, self._base + path,
                                               headers={**self._headers, **(headers or {})}, **kw)
                res = await self._http.send(req, stream=stream)
            except httpx.TimeoutException as e:
                if attempt < self._max_retries:
                    await asyncio.sleep(b._delay(attempt, None)); continue
                raise TimeoutError(f"Request timed out: {e}") from e
            except httpx.TransportError as e:
                if attempt < self._max_retries:
                    await asyncio.sleep(b._delay(attempt, None)); continue
                raise ConnectionError(f"Connection error: {e}") from e
            if res.is_success:
                return res
            if stream:
                await res.aread()
            if b._retryable(res.status_code) and attempt < self._max_retries:
                await res.aclose()
                await asyncio.sleep(b._delay(attempt, res.headers.get("retry-after")))
                continue
            b._raise_for(res)
        raise AssertionError("unreachable")

    async def _json(self, method: str, path: str, **kw: Any) -> JSON:
        return (await self._send(method, path, **kw)).json()

    async def _stream(self, path: str, body: JSON) -> AsyncIterator[JSON]:
        res = await self._send("POST", path, json=body, stream=True, headers={"Accept": "text/event-stream"})
        try:
            async for ev in b.sse_events_async(res.aiter_lines()):
                yield ev
        finally:
            await res.aclose()
