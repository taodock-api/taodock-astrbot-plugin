import base64
import importlib.util
import io
import json
import sys
import types
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

from taodock_api import API_BASE_URL, TaoDockClient, TaoDockError, parse_parameters


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json"):
        self.body = io.BytesIO(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.body.read(size)

    def close(self) -> None:
        self.closed = True


class RecordingOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class TaoDockClientTests(unittest.TestCase):
    def test_plugin_module_loads_with_astrbot_contract(self):
        root = Path(__file__).parents[1]
        package_name = "astrbot_plugin_taodock"
        package = types.ModuleType(package_name)
        package.__path__ = [str(root)]

        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        event = types.ModuleType("astrbot.api.event")
        components = types.ModuleType("astrbot.api.message_components")
        star = types.ModuleType("astrbot.api.star")

        class Config(dict):
            pass

        class Star:
            def __init__(self, context):
                self.context = context

        class Video:
            @classmethod
            def fromFileSystem(cls, *, path):
                return path

        api.AstrBotConfig = Config
        api.logger = types.SimpleNamespace(warning=lambda message: None)
        event.AstrMessageEvent = type("AstrMessageEvent", (), {})
        event.filter = types.SimpleNamespace(
            command=lambda name: lambda function: function
        )
        components.Video = Video
        star.Context = type("Context", (), {})
        star.Star = Star

        modules = {
            package_name: package,
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.message_components": components,
            "astrbot.api.star": star,
        }
        previous = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)
        module_name = f"{package_name}.main"
        spec = importlib.util.spec_from_file_location(module_name, root / "main.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            plugin = module.TaoDockMediaPlugin(star.Context(), Config())
            self.assertEqual(plugin.config, {})
            metadata_version = next(
                line.split(":", 1)[1].strip()
                for line in (root / "metadata.yaml")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.startswith("version:")
            )
            self.assertEqual(module.VERSION, metadata_version)
        finally:
            sys.modules.pop(module_name, None)
            sys.modules.pop(f"{package_name}.taodock_api", None)
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

    def test_parameters_must_be_json_object(self):
        self.assertEqual(
            parse_parameters('{"n": 0, "flag": false}'), {"n": 0, "flag": False}
        )
        with self.assertRaisesRegex(TypeError, "JSON object"):
            parse_parameters("[1, 2]")

    def test_model_listing_uses_official_endpoint_and_media_metadata(self):
        body = {
            "data": [
                {"id": "image-a", "supported_endpoint_types": ["image-generation"]},
                {"id": "video-b", "kind": "video"},
                {"id": "chat-c", "supported_endpoint_types": ["openai"]},
            ]
        }
        opener = RecordingOpener([FakeResponse(json.dumps(body).encode())])
        client = TaoDockClient("test-key", user_agent="test/1", opener=opener)

        self.assertEqual(client.list_media_models("image"), ["image-a"])
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, f"{API_BASE_URL}/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(timeout, 30)

    def test_image_request_preserves_zero_values_and_protects_model_and_prompt(self):
        encoded = base64.b64encode(b"png-bytes").decode()
        opener = RecordingOpener(
            [FakeResponse(json.dumps({"data": [{"b64_json": encoded}]}).encode())]
        )
        client = TaoDockClient("test-key", user_agent="test/1", opener=opener)

        images = client.generate_images(
            "configured-model",
            "command prompt",
            {"n": 0, "stream": False, "model": "ignored", "prompt": "ignored"},
        )

        self.assertEqual(images[0].content, b"png-bytes")
        request, _ = opener.requests[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["n"], 0)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["model"], "configured-model")
        self.assertEqual(payload["prompt"], "command prompt")

    def test_video_task_id_is_path_encoded(self):
        opener = RecordingOpener([FakeResponse(b'{"status":"completed"}')])
        client = TaoDockClient("test-key", user_agent="test/1", opener=opener)

        result = client.get_video("task/with space")

        self.assertEqual(result["id"], "task/with space")
        request, _ = opener.requests[0]
        self.assertEqual(request.full_url, f"{API_BASE_URL}/videos/task%2Fwith%20space")

    def test_http_error_exposes_request_id_without_key(self):
        headers = Message()
        headers["x-request-id"] = "req-123"
        error = urllib.error.HTTPError(
            f"{API_BASE_URL}/models",
            401,
            "Unauthorized",
            headers,
            io.BytesIO(b'{"error":{"message":"bad key","code":"invalid_api_key"}}'),
        )
        opener = RecordingOpener([error])
        client = TaoDockClient("secret-value", user_agent="test/1", opener=opener)

        with self.assertRaises(TaoDockError) as raised:
            client.list_media_models("image")

        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(raised.exception.code, "invalid_api_key")
        self.assertEqual(raised.exception.request_id, "req-123")
        self.assertNotIn("secret-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
