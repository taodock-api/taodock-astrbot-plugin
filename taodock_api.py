from __future__ import annotations

import base64
import binascii
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

API_BASE_URL = "https://taodock.com/v1"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024


class TaoDockError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.request_id = request_id


@dataclass(frozen=True)
class ImageArtifact:
    content: bytes
    media_type: str


def parse_parameters(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        raise TypeError("request parameters must be a JSON object")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(parsed, dict):
        raise TypeError("request parameters must be a JSON object")
    return parsed


def _read_limited(stream: BinaryIO, limit: int) -> bytes:
    declared = (
        stream.headers.get("Content-Length")
        if getattr(stream, "headers", None)
        else None
    )
    if declared:
        try:
            if int(declared) > limit:
                raise TaoDockError("response is larger than the allowed limit")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(1024 * 1024, limit - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise TaoDockError("response is larger than the allowed limit")
        chunks.append(chunk)


def _error_details(error: urllib.error.HTTPError) -> tuple[str, str | None, str | None]:
    request_id = error.headers.get("x-request-id") if error.headers else None
    try:
        raw = error.read(MAX_JSON_BYTES + 1)
    except OSError:
        raw = b""
    if len(raw) > MAX_JSON_BYTES:
        return f"HTTP {error.code}", None, request_id
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = None
    message = error.reason or f"HTTP {error.code}"
    code = None
    if isinstance(body, dict):
        payload = body.get("error")
        if isinstance(payload, dict):
            if isinstance(payload.get("message"), str) and payload["message"]:
                message = payload["message"]
            if isinstance(payload.get("code"), str) and payload["code"]:
                code = payload["code"]
        elif isinstance(body.get("message"), str) and body["message"]:
            message = body["message"]
    return str(message), code, request_id


class TaoDockClient:
    def __init__(
        self,
        api_key: str,
        *,
        user_agent: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise TaoDockError(
                "a TaoDock API key is required", status=401, code="missing_api_key"
            )
        self._api_key = key
        self._user_agent = user_agent
        self._open = opener

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 120,
    ) -> Any:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": self._user_agent,
        }
        if payload is not None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = f"{API_BASE_URL}/{path.lstrip('/')}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            return self._open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                message, code, request_id = _error_details(exc)
            finally:
                exc.close()
            raise TaoDockError(
                message, status=exc.code, code=code, request_id=request_id
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError):
                raise TaoDockError(
                    "the TaoDock request timed out", code="timeout"
                ) from exc
            raise TaoDockError(
                "could not connect to TaoDock", code="network_error"
            ) from exc

    def _json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 120,
    ) -> dict[str, Any]:
        response = self._request(method, path, payload, timeout)
        try:
            raw = _read_limited(response, MAX_JSON_BYTES)
        finally:
            response.close()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TaoDockError("TaoDock returned an invalid JSON response") from exc
        if not isinstance(parsed, dict):
            raise TaoDockError("TaoDock returned an unexpected JSON response")
        return parsed

    def list_media_models(self, kind: str) -> list[str]:
        if kind not in {"image", "video"}:
            raise ValueError("kind must be image or video")
        payload = self._json("GET", "models", timeout=30)
        models = payload.get("data")
        if not isinstance(models, list):
            return []
        required_endpoint = "image-generation" if kind == "image" else "openai-video"
        result: list[str] = []
        for item in models:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            raw_endpoints = item.get("supported_endpoint_types")
            if isinstance(raw_endpoints, dict):
                endpoints = set(raw_endpoints)
            elif isinstance(raw_endpoints, list):
                endpoints = {value for value in raw_endpoints if isinstance(value, str)}
            else:
                endpoints = set()
            if item.get("kind") == kind or required_endpoint in endpoints:
                result.append(item["id"])
        return sorted(set(result))

    def generate_images(
        self, model: str, prompt: str, parameters: dict[str, Any]
    ) -> list[ImageArtifact]:
        payload = dict(parameters)
        payload["model"] = model
        payload["prompt"] = prompt
        response = self._json("POST", "images/generations", payload, timeout=300)
        items = response.get("data")
        if not isinstance(items, list):
            raise TaoDockError("image response contains no results")
        artifacts: list[ImageArtifact] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            encoded = item.get("b64_json")
            if isinstance(encoded, str) and encoded:
                if len(encoded) > (MAX_IMAGE_BYTES * 4 // 3) + 8:
                    raise TaoDockError(
                        "generated image is larger than the allowed limit"
                    )
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise TaoDockError(
                        "image response contains invalid base64 data"
                    ) from exc
                if len(content) > MAX_IMAGE_BYTES:
                    raise TaoDockError(
                        "generated image is larger than the allowed limit"
                    )
                artifacts.append(ImageArtifact(content, "image/png"))
                continue
            url = item.get("url")
            if isinstance(url, str) and url:
                artifacts.append(self._download_image(url))
        if not artifacts:
            raise TaoDockError("image response contains no downloadable results")
        return artifacts

    def _download_image(self, url: str) -> ImageArtifact:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise TaoDockError("image result URL is not a valid HTTPS URL")
        request = urllib.request.Request(
            url, headers={"Accept": "image/*", "User-Agent": self._user_agent}
        )
        try:
            response = self._open(request, timeout=120)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise TaoDockError(
                f"image download failed with HTTP {exc.code}", status=exc.code
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TaoDockError(
                "could not download the generated image", code="network_error"
            ) from exc
        try:
            content = _read_limited(response, MAX_IMAGE_BYTES)
            media_type = (
                response.headers.get_content_type() if response.headers else "image/png"
            )
        finally:
            response.close()
        if not media_type.startswith("image/"):
            raise TaoDockError("image result has an unexpected content type")
        return ImageArtifact(content, media_type)

    def create_video(
        self, model: str, prompt: str, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        payload = dict(parameters)
        payload["model"] = model
        payload["prompt"] = prompt
        response = self._json("POST", "videos", payload, timeout=120)
        task_id = response.get("id") or response.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise TaoDockError("video response contains no task ID")
        return {"id": task_id, "status": response.get("status") or "queued"}

    def get_video(self, task_id: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(task_id, safe="")
        response = self._json("GET", f"videos/{encoded}", timeout=30)
        response["id"] = response.get("id") or response.get("task_id") or task_id
        return response

    def wait_for_video(
        self, task_id: str, timeout: float, interval: float = 5
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0, timeout)
        while True:
            task = self.get_video(task_id)
            if task.get("status") in {"completed", "failed", "cancelled"}:
                return task
            if time.monotonic() >= deadline:
                return task
            time.sleep(min(interval, max(0, deadline - time.monotonic())))

    def download_video(self, task_id: str, destination: Path) -> Path:
        encoded = urllib.parse.quote(task_id, safe="")
        response = self._request("GET", f"videos/{encoded}/content", timeout=600)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        total = 0
        try:
            declared = (
                response.headers.get("Content-Length") if response.headers else None
            )
            if declared:
                try:
                    if int(declared) > MAX_VIDEO_BYTES:
                        raise TaoDockError(
                            "generated video is larger than the allowed limit"
                        )
                except ValueError:
                    pass
            with partial.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_VIDEO_BYTES:
                        raise TaoDockError(
                            "generated video is larger than the allowed limit"
                        )
                    output.write(chunk)
            os.replace(partial, destination)
            return destination
        finally:
            response.close()
            if partial.exists():
                partial.unlink()
