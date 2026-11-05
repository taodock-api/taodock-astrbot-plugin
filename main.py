from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Video
from astrbot.api.star import Context, Star

from .taodock_api import TaoDockClient, TaoDockError, parse_parameters

VERSION = "1.0.0"
USER_AGENT = f"taodock-astrbot-plugin/{VERSION}"
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class TaoDockMediaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.output_dir = Path(tempfile.gettempdir()) / "astrbot_taodock"

    def _client(self) -> TaoDockClient:
        return TaoDockClient(self.config.get("api_key", ""), user_agent=USER_AGENT)

    def _parameters(self, field: str) -> dict:
        return parse_parameters(self.config.get(field, "{}"))

    def _message_for_error(self, error: Exception) -> str:
        if isinstance(error, (TypeError, ValueError)):
            return f"配置错误：{error}"
        if not isinstance(error, TaoDockError):
            return "插件处理请求时发生错误，请查看 AstrBot 日志。"
        if error.status == 401 or error.code == "missing_api_key":
            message = (
                "API 密钥缺失、无效或已失效。请在 https://taodock.com/keys 检查密钥。"
            )
        elif error.status == 429 or error.code == "rate_limit_exceeded":
            message = "请求过于频繁，请稍后重试。"
        elif error.code == "insufficient_user_quota":
            message = "账户额度不足，请前往 https://taodock.com/wallet 检查钱包。"
        elif error.code == "model_not_found":
            message = "当前密钥无法使用该模型。请用 /td_models 查询可用模型。"
        elif error.code == "timeout":
            message = "请求 TaoDock 超时，请稍后重试。"
        elif error.code == "network_error":
            message = "无法连接 TaoDock，请检查 AstrBot 所在主机的网络。"
        else:
            message = f"TaoDock 请求失败：{error.message}"
        if error.request_id:
            message += f"（请求 ID：{error.request_id}）"
        return message

    def _video_path(self, task_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip("-") or "video"
        return self.output_dir / f"{safe_id}.mp4"

    async def _video_result(
        self, event: AstrMessageEvent, client: TaoDockClient, task_id: str
    ):
        path = await asyncio.to_thread(
            client.download_video, task_id, self._video_path(task_id)
        )
        return event.chain_result([Video.fromFileSystem(path=str(path))])

    @filter.command("td_models")
    async def td_models(self, event: AstrMessageEvent, kind: str = "image"):
        """列出 TaoDock 媒体模型：/td_models image|video"""
        if kind not in {"image", "video"}:
            yield event.plain_result("用法：/td_models image|video")
            return
        try:
            models = await asyncio.to_thread(self._client().list_media_models, kind)
            if not models:
                yield event.plain_result(
                    f"当前密钥没有可识别的 {kind} 模型。请在 https://taodock.com/models 核对模型和接口。"
                )
                return
            yield event.plain_result("\n".join(f"- {model}" for model in models))
        except (OSError, TaoDockError, TypeError, ValueError) as exc:
            logger.warning(f"TaoDock model listing failed: {type(exc).__name__}")
            yield event.plain_result(self._message_for_error(exc))

    @filter.command("td_image")
    async def td_image(self, event: AstrMessageEvent):
        """使用 TaoDock 生成图片：/td_image 提示词"""
        prompt = event.message_str.partition(" ")[2].strip()
        model = str(self.config.get("image_model", "")).strip()
        if not prompt or not model:
            yield event.plain_result(
                "用法：/td_image 提示词。请先在插件配置中设置默认图片模型。"
            )
            return
        try:
            client = self._client()
            parameters = self._parameters("image_parameters")
            images = await asyncio.to_thread(
                client.generate_images, model, prompt, parameters
            )
            self.output_dir.mkdir(parents=True, exist_ok=True)
            for image in images:
                suffix = IMAGE_EXTENSIONS.get(image.media_type, ".png")
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix="taodock-image-",
                    suffix=suffix,
                    dir=self.output_dir,
                    delete=False,
                ) as output:
                    output.write(image.content)
                    path = output.name
                yield event.image_result(path)
        except (OSError, TaoDockError, TypeError, ValueError) as exc:
            logger.warning(f"TaoDock image generation failed: {type(exc).__name__}")
            yield event.plain_result(self._message_for_error(exc))

    @filter.command("td_video")
    async def td_video(self, event: AstrMessageEvent):
        """使用 TaoDock 生成视频：/td_video 提示词"""
        prompt = event.message_str.partition(" ")[2].strip()
        model = str(self.config.get("video_model", "")).strip()
        if not prompt or not model:
            yield event.plain_result(
                "用法：/td_video 提示词。请先在插件配置中设置默认视频模型。"
            )
            return
        try:
            client = self._client()
            parameters = self._parameters("video_parameters")
            task = await asyncio.to_thread(
                client.create_video, model, prompt, parameters
            )
            task_id = task["id"]
            yield event.plain_result(f"视频任务已创建：{task_id}，正在等待结果。")
            wait_seconds = max(
                0, min(int(self.config.get("video_wait_seconds", 300)), 1800)
            )
            status = await asyncio.to_thread(
                client.wait_for_video, task_id, wait_seconds
            )
            if status.get("status") != "completed":
                reason = status.get("error")
                if isinstance(reason, dict):
                    reason = reason.get("message")
                suffix = (
                    f"，原因：{reason}" if isinstance(reason, str) and reason else ""
                )
                yield event.plain_result(
                    f"视频任务 {task_id} 当前状态：{status.get('status', 'unknown')}{suffix}"
                )
                return
            yield await self._video_result(event, client, task_id)
        except (OSError, TaoDockError, TypeError, ValueError) as exc:
            logger.warning(f"TaoDock video generation failed: {type(exc).__name__}")
            yield event.plain_result(self._message_for_error(exc))

    @filter.command("td_video_status")
    async def td_video_status(self, event: AstrMessageEvent, task_id: str = ""):
        """查询 TaoDock 视频任务：/td_video_status <task_id>"""
        if not task_id:
            yield event.plain_result("用法：/td_video_status <task_id>")
            return
        try:
            status = await asyncio.to_thread(self._client().get_video, task_id)
            summary = {
                "id": status.get("id") or task_id,
                "status": status.get("status") or "unknown",
            }
            if isinstance(status.get("error"), dict) and status["error"].get("message"):
                summary["error"] = status["error"]["message"]
            yield event.plain_result(json.dumps(summary, ensure_ascii=False))
        except (OSError, TaoDockError, TypeError, ValueError) as exc:
            logger.warning(f"TaoDock video status failed: {type(exc).__name__}")
            yield event.plain_result(self._message_for_error(exc))

    @filter.command("td_video_download")
    async def td_video_download(self, event: AstrMessageEvent, task_id: str = ""):
        """下载 TaoDock 视频任务：/td_video_download <task_id>"""
        if not task_id:
            yield event.plain_result("用法：/td_video_download <task_id>")
            return
        try:
            client = self._client()
            status = await asyncio.to_thread(client.get_video, task_id)
            if status.get("status") != "completed":
                yield event.plain_result(
                    f"视频任务 {task_id} 当前状态：{status.get('status', 'unknown')}"
                )
                return
            yield await self._video_result(event, client, task_id)
        except (OSError, TaoDockError, TypeError, ValueError) as exc:
            logger.warning(f"TaoDock video download failed: {type(exc).__name__}")
            yield event.plain_result(self._message_for_error(exc))
