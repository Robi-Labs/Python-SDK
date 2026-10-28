from __future__ import annotations

import base64
import json
import mimetypes
import os
import random
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Tuple, Union

import httpx

from .errors import RobiError, error_for

DEFAULT_BASE_URL = "https://api.robiai.com/v1"
VERSION = "0.1.0"

FileInput = Union[str, os.PathLike, bytes, Any]  # path, bytes, or binary file-like


def _resolve(api_key: Optional[str], base_url: Optional[str]) -> Tuple[str, str]:
    key = api_key or os.environ.get("ROBI_API_KEY")
    if not key:
        raise RobiError("Missing API key. Pass api_key= or set ROBI_API_KEY.")
    url = base_url or os.environ.get("ROBI_BASE_URL") or DEFAULT_BASE_URL
    return key, url.rstrip("/")


def _headers(key: str, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": f"robiai-python/{VERSION}",
        **(extra or {}),
    }


def _delay(attempt: int, retry_after: Optional[str]) -> float:
    try:
        if retry_after is not None:
            return min(float(retry_after), 30.0)
    except ValueError:
        pass
    return min(0.5 * 2**attempt, 8.0) * (0.75 + random.random() * 0.25)


def _retryable(status: int) -> bool:
    """Only 429 and 5xx are worth retrying; 4xx describe conditions that won't change."""
    return status == 429 or status >= 500


def _raise_for(res: httpx.Response) -> None:
    try:
        body = res.json()
    except Exception:
        body = None
    raise error_for(res.status_code, body, res.headers.get("x-request-id"))


# ---- request body helpers shared by sync/async clients -------------------------------------

def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _file_part(name: str, f: FileInput) -> Tuple[str, Tuple[str, bytes, str]]:
    if isinstance(f, (str, os.PathLike)):
        p = Path(f)
        data, fname = p.read_bytes(), p.name
    elif isinstance(f, (bytes, bytearray)):
        data, fname = bytes(f), "upload"
    else:  # file-like
        data = f.read()
        fname = os.path.basename(getattr(f, "name", "") or "upload")
    ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    return name, (fname, data, ctype)


def _form(fields: Dict[str, Any]) -> Dict[str, str]:
    return {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in _clean(fields).items()}


def image_edit_request(model: str, image: Union[FileInput, List[FileInput]], prompt: str,
                       extra: Dict[str, Any]) -> Dict[str, Any]:
    images = image if isinstance(image, list) else [image]
    if not 1 <= len(images) <= 4:
        raise RobiError("images.edit takes one to four reference images")
    return {
        "data": _form({"model": model, "prompt": prompt, **extra}),
        "files": [_file_part("image", i) for i in images],
    }


def video_request(model: str, prompt: str, input_reference: Optional[FileInput],
                  fields: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    body = {"model": model, "prompt": prompt, **fields, **extra}
    if input_reference is not None:
        return {"data": _form(body), "files": [_file_part("input_reference", input_reference)]}
    return {"json": _clean(body)}


def save_b64(item: Dict[str, Any], path: Union[str, os.PathLike]) -> Path:
    """Decode an image result item (``result["data"][0]``) and write it to ``path``.

    Write the bytes untouched: re-encoding strips the C2PA Content Credential.
    """
    p = Path(path)
    p.write_bytes(base64.b64decode(item["b64_json"]))
    return p


# ---- SSE ------------------------------------------------------------------------------------

def _parse_sse_line(line: str, data: list) -> Optional[Any]:
    """Feed one SSE line; return an event (or ``Ellipsis`` for [DONE]) at a blank line."""
    if line == "":
        if not data:
            return None
        payload = "\n".join(data)
        data.clear()
        if payload == "[DONE]":
            return Ellipsis
        ev = json.loads(payload)
        if isinstance(ev, dict) and isinstance(ev.get("error"), dict):
            err = ev["error"]
            code = err.get("code")
            status = code if isinstance(code, int) else 500
            raise error_for(status, {"detail": err}, None)
        return ev
    if line.startswith("data:"):
        v = line[5:]
        data.append(v[1:] if v.startswith(" ") else v)
    return None


def sse_events(lines: Iterator[str]) -> Iterator[Dict[str, Any]]:
    data: list = []
    for line in lines:
        ev = _parse_sse_line(line, data)
        if ev is Ellipsis:
            return
        if ev is not None:
            yield ev


async def sse_events_async(lines: AsyncIterator[str]) -> AsyncIterator[Dict[str, Any]]:
    data: list = []
    async for line in lines:
        ev = _parse_sse_line(line, data)
        if ev is Ellipsis:
            return
        if ev is not None:
            yield ev
