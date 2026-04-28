"""
DingTalk (钉钉) Channel Adapter
================================

Supports three integration modes:

1. **Outbound Webhook** (推送模式)
   - Configure a "自定义机器人" in the DingTalk group settings.
   - Set ``webhook_url`` and optionally ``secret`` in config.
   - The agent can push messages to the group chat via ``send_message()``.
   - Inbound messages are NOT supported in this mode.

2. **Enterprise Internal Bot — HTTP** (企业内部机器人, Webhook 双向模式)
   - Requires a DingTalk Open Platform app with an HTTPS callback URL.
   - Inbound messages are delivered via HTTP POST to ``/dingtalk/webhook``.
   - Outbound messages are sent to the ``sessionWebhook`` URL in each payload.

3. **Enterprise Internal Bot — Stream** (企业内部机器人, 长连接模式)
   - Uses the official ``dingtalk-stream`` SDK (WebSocket).
   - No public HTTPS URL required — Phoenix connects OUT to DingTalk.
   - Set ``mode: stream`` and provide ``client_id`` / ``client_secret``.
   - Supports both private chats and group @-mentions.
   - **Supports file transfer** (images & documents) via DingTalk OpenAPI.

Configuration (config.yaml)::

    channels:
      dingtalk:
        enabled: true
        mode: stream                   # "webhook" | "internal" | "stream"
        # ---- stream mode (长连接) ----
        client_id: "dingXXXXXXXXXXXXXXXX"    # = app_key
        client_secret: "YOUR_CLIENT_SECRET"
        # ---- internal/webhook mode ----
        # app_key: "dingXXXXXXXXXXXXXXXX"
        # app_secret: "YOUR_APP_SECRET"
        # webhook_url: "https://oapi.dingtalk.com/robot/send?access_token=xxx"
        # webhook_path: /dingtalk/webhook
        at_all: false
        # ---- file transfer settings (optional) ----
        download_dir: "~/.phoenix/downloads"   # where received files are saved
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

try:
    from aiohttp import ClientSession as AiohttpSession, ClientTimeout
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

try:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route, Mount
    _HAS_STARLETTE = True
except ImportError:
    _HAS_STARLETTE = False

try:
    import dingtalk_stream
    _HAS_DINGTALK_STREAM = True
except ImportError:
    _HAS_DINGTALK_STREAM = False

from phoenix_agent.channels.base import (
    BaseChannel,
    ChannelError,
    ChannelFile,
    ChannelMessage,
    ChannelReply,
    MessageType,
)
from phoenix_agent.channels.dingtalk_openapi import (
    ChannelAPIError,
    DingTalkOpenAPI,
)
from phoenix_agent.channels.registry import ChannelRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stream-mode handler (long connection)
# ---------------------------------------------------------------------------

class _DingTalkStreamHandler(dingtalk_stream.AsyncChatbotHandler):
    """
    Handler that bridges dingtalk-stream callbacks to Phoenix Agent.

    NOTE: AsyncChatbotHandler.process() must be a SYNC function (runs in a
    ThreadPoolExecutor).  We dispatch async work back to the event loop
    via call_soon_threadsafe() so it can be properly awaited.
    """

    def __init__(
        self,
        pool,
        channel_name: str = "dingtalk",
        openapi: Optional[DingTalkOpenAPI] = None,
        download_dir: Optional[str] = None,
        robot_code: Optional[str] = None,
        channel_ref: Optional["DingTalkChannel"] = None,  # Reference to parent channel
    ):
        super().__init__()
        self._pool = pool
        self._channel_name = channel_name
        self._logger = logger
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._openapi = openapi
        self._download_dir = download_dir or os.path.expanduser("~/.phoenix/downloads")
        self._robot_code = robot_code or ""
        self._channel_ref = channel_ref  # For updating last active session

    def process(self, callback: dingtalk_stream.CallbackMessage):
        """Must be synchronous. Dispatches async work to the event loop."""
        if self._loop is None:
            self._logger.warning("[dingtalk][stream] No event loop, dropping message")
            return
        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(self._handle_message_async(callback))
        )

    async def _handle_message_async(self, callback: dingtalk_stream.CallbackMessage):
        """Async message handler scheduled from process()."""
        try:
            incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        except Exception as exc:
            self._logger.warning("[dingtalk][stream] Failed to parse message: %s", exc)
            return

        raw_data = callback.data or {}
        # SDK stores callback "msgtype" as "message_type" attribute
        msg_type_raw = getattr(incoming, "message_type", "") or "text"

        sender_id = str(incoming.sender_staff_id or "")
        sender_name = incoming.sender_nick or sender_id

        # Extract conversation_id early — needed for both file and text handlers
        conversation_id = str(getattr(incoming, "conversation_id", "") or "")
        chat_key = f"conv:{conversation_id}" if conversation_id else f"user:{sender_id}"

        self._logger.info(
            "[dingtalk][stream] Message from %s (%s): msgtype=%s, conv=%s",
            sender_name, sender_id, msg_type_raw,
            conversation_id[:20] if conversation_id else "(none)",
        )

        # Track last active conversation for scheduled task push
        if self._channel_ref:
            if conversation_id:
                self._channel_ref._last_conversation_id = conversation_id
            if sender_id:
                self._channel_ref._last_sender_id = sender_id

        # ---- Handle file/image messages ----
        if msg_type_raw in ("picture", "file") and self._openapi:
            await self._handle_file_message(
                incoming, raw_data, msg_type_raw,
                sender_id, sender_name, conversation_id,
            )
            return

        # ---- Handle text messages (default) ----
        try:
            user_text = incoming.text.content.strip()
        except AttributeError:
            user_text = ""

        if not user_text:
            return

        self._logger.info(
            "[dingtalk][stream] Text from %s (%s): %r",
            sender_name, sender_id, user_text[:80],
        )

        # Resolve per-conversation agent from pool
        agent = self._pool.get_agent(self._channel_name, chat_key)

        try:
            loop = asyncio.get_event_loop()
            response_text = await loop.run_in_executor(
                None,
                lambda: agent.run(user_text),
            )
        except Exception as exc:
            self._logger.exception("[dingtalk][stream] Agent error: %s", exc)
            response_text = f"⚠️ 处理消息时出错：{exc}"

        reply_text = response_text or "(no response)"
        # reply_text() is sync (uses requests.post), run in executor to avoid
        # blocking the event loop — this was causing slow responses
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: self.reply_text(reply_text, incoming)
        )

        # If the agent produced file paths in its response, try to send them
        # as file attachments via OpenAPI
        if self._openapi and hasattr(incoming, "conversation_id"):
            await self._try_send_files_from_response(
                response_text, incoming
            )

    async def _try_send_files_from_response(self, response_text: str, incoming):
        """
        Parse file paths from the agent's text response and send them
        as file attachments via DingTalk OpenAPI.

        Supports two patterns in the agent's response:
        1. [file:/path/to/file.ext] — explicit file markers
        2. Lines ending with common file extensions that point to real files
        """
        import re

        conversation_id = getattr(incoming, "conversation_id", "")
        if not conversation_id:
            return

        # Pattern 1: [file:/path/to/file.ext]
        file_paths = re.findall(r'\[file:([^\]]+)\]', response_text)

        # Pattern 2: Detect lines that look like file paths
        if not file_paths:
            path_pattern = re.compile(
                r'(?:^|\s)([A-Za-z]:[/\\][^\s<>\'"|]+\.\w{1,10})(?:\s|$)',
            )
            file_paths = path_pattern.findall(response_text)

        if not file_paths:
            return

        for fpath in file_paths:
            fpath = fpath.strip()
            if not fpath or not os.path.isfile(fpath):
                continue

            fname = os.path.basename(fpath)
            try:
                media_id = await self._openapi.upload_file(
                    file_path=fpath,
                    file_name=fname,
                )
                is_image = any(
                    fname.lower().endswith(ext)
                    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
                )
                await self._openapi.send_file_to_group(
                    robot_code=self._robot_code,
                    conversation_id=conversation_id,
                    media_id=media_id,
                    file_name=fname,
                    is_image=is_image,
                )
                self._logger.info(
                    "[dingtalk][stream] Auto-sent file: %s", fname
                )
            except Exception as exc:
                self._logger.error(
                    "[dingtalk][stream] Failed to auto-send file %s: %s",
                    fname, exc,
                )

    # ------------------------------------------------------------------
    # File message handling
    # ------------------------------------------------------------------

    async def _handle_file_message(
        self,
        incoming,
        raw_data: dict,
        msg_type_raw: str,
        sender_id: str,
        sender_name: str,
        conversation_id: str = "",
    ):
        """Download file from DingTalk and pass info to the agent."""
        try:
            content = self._extract_file_content(raw_data, msg_type_raw, incoming)
            if not content:
                self._logger.warning(
                    "[dingtalk][stream] No file content in %s message", msg_type_raw
                )
                return

            download_code = content.get("downloadCode", "")
            file_name = content.get("fileName", f"dingtalk_{msg_type_raw}")

            if not download_code:
                self._logger.warning(
                    "[dingtalk][stream] No downloadCode in %s message", msg_type_raw
                )
                return

            # Download via OpenAPI
            file_bytes, actual_name = await self._openapi.download_file(
                download_code=download_code,
                file_name=file_name,
            )

            # Save locally
            os.makedirs(self._download_dir, exist_ok=True)
            safe_name = "".join(c for c in actual_name if c.isalnum() or c in "._- ")
            local_path = os.path.join(
                self._download_dir, f"{int(time.time())}_{safe_name}"
            )
            with open(local_path, "wb") as f:
                f.write(file_bytes)

            self._logger.info(
                "[dingtalk][stream] File saved: %s (%d bytes) from %s",
                local_path, len(file_bytes), sender_name,
            )

            # Describe file to agent
            is_image = msg_type_raw == "picture"
            if is_image:
                desc = (
                    f"[用户 {sender_name} 发送了一张图片: {actual_name}，"
                    f"已保存到 {local_path}，大小 {len(file_bytes)} 字节]"
                )
            else:
                desc = (
                    f"[用户 {sender_name} 发送了一个文件: {actual_name}，"
                    f"已保存到 {local_path}，大小 {len(file_bytes)} 字节]"
                )

            # Run agent (per-conversation)
            chat_key = f"conv:{conversation_id}" if conversation_id else f"user:{sender_id}"
            agent = self._pool.get_agent(self._channel_name, chat_key)

            try:
                loop = asyncio.get_event_loop()
                response_text = await loop.run_in_executor(
                    None, lambda: agent.run(desc),
                )
            except Exception as exc:
                self._logger.exception("[dingtalk][stream] Agent error on file: %s", exc)
                response_text = f"⚠️ 处理文件时出错：{exc}"

            reply_text = response_text or "文件已收到，但未生成回复。"
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self.reply_text(reply_text, incoming)
            )

        except ChannelAPIError as exc:
            self._logger.error("[dingtalk][stream] File API error: %s", exc)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self.reply_text(f"⚠️ 下载文件失败：{exc}", incoming)
            )
        except Exception as exc:
            self._logger.exception("[dingtalk][stream] File handling error: %s", exc)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self.reply_text(f"⚠️ 处理文件时出错：{exc}", incoming)
            )

    @staticmethod
    def _extract_file_content(
        raw_data: dict, msg_type: str, incoming=None,
    ) -> Optional[dict]:
        """
        Extract downloadCode from the raw DingTalk message.

        Strategy:
        1. For picture messages, try SDK-parsed ``image_content.download_code`` first.
        2. Fall back to parsing raw_data['content'] directly.

        Raw content formats::

            picture: {"downloadCode": "xxx"}
                     or {"pictureDownloadCodeList": [{"downloadCode": "xxx"}]}
            file:    {"downloadCode": "xxx", "fileName": "report.pdf"}
        """
        # Path 1: use SDK-parsed data for picture messages
        if msg_type == "picture" and incoming and hasattr(incoming, "image_content"):
            dc = getattr(incoming.image_content, "download_code", None)
            if dc:
                return {"downloadCode": dc, "fileName": "image.png"}

        # Path 2: parse raw_data content directly
        content = raw_data.get("content", {})
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                return None
        if not isinstance(content, dict):
            return None

        # Direct downloadCode (file messages & some picture formats)
        if "downloadCode" in content:
            return content

        # pictureDownloadCodeList (picture messages)
        pic_list = content.get("pictureDownloadCodeList", [])
        if isinstance(pic_list, list) and pic_list:
            first = pic_list[0]
            if isinstance(first, dict) and "downloadCode" in first:
                return {
                    "downloadCode": first["downloadCode"],
                    "fileName": content.get("fileName", "image.jpg"),
                }

        return None


# ---------------------------------------------------------------------------
# Main channel adapter
# ---------------------------------------------------------------------------

class DingTalkChannel(BaseChannel):
    """
    DingTalk channel adapter supporting webhook, internal, and stream modes.

    Stream mode supports bidirectional file transfer via DingTalk OpenAPI.
    """

    NAME = "dingtalk"

    def __init__(self, config: Any = None) -> None:
        super().__init__(config)

        cfg = self._channel_cfg
        self._enabled:        bool           = cfg.get("enabled", False)
        self._mode:           str            = cfg.get("mode", "webhook")
        self._webhook_url:    Optional[str]  = cfg.get("webhook_url")
        self._secret:         Optional[str]  = cfg.get("secret")
        self._app_key:        Optional[str]  = cfg.get("app_key")
        self._app_secret:     Optional[str]  = cfg.get("app_secret")
        self._client_id:      Optional[str]  = cfg.get("client_id") or self._app_key
        self._client_secret:  Optional[str]  = cfg.get("client_secret") or self._app_secret
        self._webhook_path:   str            = cfg.get("webhook_path", "/dingtalk/webhook")
        self._at_all:         bool           = cfg.get("at_all", False)
        self._download_dir:   str            = cfg.get(
            "download_dir", os.path.expanduser("~/.phoenix/downloads"),
        )

        self._http: Optional[Any] = None
        self._stream_client: Optional[Any] = None
        self._openapi: Optional[DingTalkOpenAPI] = None

        # Track last active conversation for scheduled task push
        self._last_conversation_id: Optional[str] = None  # 群聊会话ID (oc_xxx)
        self._last_sender_id: Optional[str] = None        # 单聊用户ID

    # ------------------------------------------------------------------
    # Signature helpers (for internal/webhook mode)
    # ------------------------------------------------------------------

    def _build_outbound_sign(self) -> Dict[str, str]:
        if not self._secret:
            return {}
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{self._secret}"
        sign = base64.b64encode(
            hmac.new(
                self._secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return {"timestamp": ts, "sign": sign}

    def verify_signature(
        self, payload: bytes, signature: str, timestamp: str = ""
    ) -> bool:
        if not self._app_secret:
            return True
        if not timestamp:
            return False
        expected = base64.b64encode(
            hmac.new(
                self._app_secret.encode("utf-8"),
                (timestamp + "\n" + self._app_secret).encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # send_message
    # ------------------------------------------------------------------

    async def send_message(self, chat_id: str, reply: ChannelReply) -> None:
        """
        Send a reply back to DingTalk.

        For ``stream`` mode with file attachments: uses OpenAPI to upload
        files and send them as separate file messages.

        For other modes: sends text/markdown via webhook URL.
        """
        if self._mode == "stream":
            # In stream mode, replies are handled by _DingTalkStreamHandler
            # inside the handler context. If we get here from _dispatch(),
            # it means the reply has files that need to be sent via OpenAPI.
            if reply.files and self._openapi:
                await self._send_files_via_openapi(chat_id, reply)
            return

        if not _HAS_AIOHTTP:
            raise ChannelError(
                self.NAME,
                "aiohttp is required for DingTalk: pip install aiohttp",
            )

        if self._mode == "internal" and str(chat_id).startswith("https://"):
            url = chat_id
        elif self._webhook_url:
            sign_params = self._build_outbound_sign()
            if sign_params:
                url = self._webhook_url + "&" + urlencode(sign_params)
            else:
                url = self._webhook_url
        else:
            raise ChannelError(self.NAME, "No webhook_url configured for DingTalk")

        body = self._build_body(reply)
        headers = {"Content-Type": "application/json"}

        async with AiohttpSession() as session:
            async with session.post(url, json=body, headers=headers) as resp:
                data = await resp.json()
                if data.get("errcode", 0) != 0:
                    raise ChannelError(
                        self.NAME,
                        f"DingTalk API error {data.get('errcode')}: "
                        f"{data.get('errmsg')}",
                    )

        logger.debug("[dingtalk] Message sent successfully")

    async def _send_files_via_openapi(
        self, conversation_id: str, reply: ChannelReply
    ) -> None:
        """
        Send file attachments via DingTalk OpenAPI (stream mode).

        Uploads each file and sends it as a separate robot message.
        """
        robot_code = self._client_id or ""

        for f in reply.files:
            try:
                # Upload file
                media_id = await self._openapi.upload_file(
                    file_path=f.path,
                    file_bytes=None,
                    file_name=f.file_name or "file",
                    file_type=f.file_type,
                )

                # Determine if image
                is_image = (
                    f.file_type and f.file_type.startswith("image/")
                ) or (
                    f.file_name and any(
                        f.file_name.lower().endswith(ext)
                        for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
                    )
                )

                # Send to conversation
                await self._openapi.send_file_to_group(
                    robot_code=robot_code,
                    conversation_id=conversation_id,
                    media_id=media_id,
                    file_name=f.file_name or "file",
                    is_image=is_image,
                )

                logger.info(
                    "[dingtalk] File sent via OpenAPI: %s (media_id=%s)",
                    f.file_name, media_id[:12] + "...",
                )

            except (ChannelAPIError, FileNotFoundError) as exc:
                logger.error("[dingtalk] Failed to send file %r: %s", f.file_name, exc)

    def _build_body(self, reply: ChannelReply) -> Dict[str, Any]:
        """Build DingTalk webhook request body from a ChannelReply."""
        at = {
            "atUserIds": reply.at_users,
            "isAtAll": self._at_all,
        }
        if reply.markdown:
            return {
                "msgtype": "markdown",
                "markdown": {
                    "title": "Phoenix Agent",
                    "text": reply.markdown,
                },
                "at": at,
            }
        return {
            "msgtype": "text",
            "text": {"content": reply.text},
            "at": at,
        }

    # ------------------------------------------------------------------
    # Stream mode — WebSocket long connection
    # ------------------------------------------------------------------

    async def start_stream(self, pool) -> None:
        """
        Start the DingTalk WebSocket long-connection client.

        This method blocks forever. Call it as ``asyncio.create_task()``
        from the server so it runs in the background.

        Args:
            pool: The :class:`~phoenix_agent.core.pool.AgentPool` instance
                  for managing per-conversation agents.
        """
        if not _HAS_DINGTALK_STREAM:
            raise ChannelError(
                self.NAME,
                "dingtalk-stream is required for stream mode.\n"
                "Install it with:  pip install dingtalk-stream",
            )

        if not self._client_id or not self._client_secret:
            raise ChannelError(
                self.NAME,
                "client_id and client_secret are required for stream mode",
            )

        # Initialize OpenAPI client for file operations
        if not _HAS_AIOHTTP:
            logger.warning(
                "[dingtalk][stream] aiohttp not installed; file transfer disabled"
            )
            self._openapi = None
        else:
            self._openapi = DingTalkOpenAPI(
                client_id=self._client_id,
                client_secret=self._client_secret,
            )
            logger.info("[dingtalk][stream] OpenAPI client initialized (file transfer enabled)")

        credential = dingtalk_stream.Credential(
            self._client_id, self._client_secret
        )
        client = dingtalk_stream.DingTalkStreamClient(credential)

        handler = _DingTalkStreamHandler(
            pool=pool,
            channel_name=self.NAME,
            openapi=self._openapi,
            download_dir=self._download_dir,
            robot_code=self._client_id,
            channel_ref=self,  # Pass self for session tracking
        )

        # Capture the event loop reference for cross-thread dispatch
        handler._loop = asyncio.get_running_loop()

        # Register chatbot message handler
        client.register_callback_handler(
            dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
            handler,
        )

        self._stream_client = client
        logger.info(
            "[dingtalk][stream] Starting WebSocket client (client_id=%s)",
            self._client_id[:8] + "...",
        )

        try:
            await client.start()
        except asyncio.CancelledError:
            logger.info("[dingtalk][stream] WebSocket client cancelled")
        except Exception as exc:
            logger.exception("[dingtalk][stream] WebSocket error: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Webhook handler (HTTP modes: internal / webhook)
    # ------------------------------------------------------------------

    def get_webhook_handler(self):
        """
        Return a Starlette ASGI application handling DingTalk inbound messages.

        For ``stream`` mode: returns None (handled by start_stream instead).
        """
        if self._mode == "stream":
            return None

        channel = self  # capture for closures

        if _HAS_STARLETTE:
            async def _handle(request: Request):
                timestamp = request.query_params.get("timestamp", "")
                sign      = request.query_params.get("sign", "")
                body_bytes = await request.body()

                if not channel.verify_signature(body_bytes, sign, timestamp):
                    return JSONResponse(
                        {"errcode": 403, "errmsg": "signature invalid"},
                        status_code=403,
                    )

                try:
                    payload: Dict = json.loads(body_bytes)
                except json.JSONDecodeError:
                    return JSONResponse(
                        {"errcode": 400, "errmsg": "invalid JSON"},
                        status_code=400,
                    )

                msg = channel._parse_payload(payload)
                if msg:
                    await channel._dispatch(msg)

                return JSONResponse({"errcode": 0, "errmsg": "ok"})

            return Starlette(routes=[Route("/", endpoint=_handle, methods=["POST"])])

        else:
            async def _asgi(scope, receive, send):
                if scope["type"] == "http":
                    event = await receive()
                    body = event.get("body", b"")
                    try:
                        payload = json.loads(body)
                        msg = channel._parse_payload(payload)
                        if msg:
                            await channel._dispatch(msg)
                    except Exception as exc:
                        logger.exception(
                            "[dingtalk] Error processing webhook: %s", exc
                        )

                    response = b'{"errcode":0,"errmsg":"ok"}'
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [[b"content-type", b"application/json"]],
                    })
                    await send({"type": "http.response.body", "body": response})

            return _asgi

    # ------------------------------------------------------------------
    # Parsing helpers (for internal/webhook payload)
    # ------------------------------------------------------------------

    def _parse_payload(self, payload: Dict[str, Any]) -> Optional[ChannelMessage]:
        """Parse a DingTalk inbound HTTP webhook payload into a ChannelMessage."""
        try:
            msg_type_raw = payload.get("msgtype", "text")
            text = ""

            if msg_type_raw == "text":
                text = payload.get("text", {}).get("content", "").strip()
                msg_type = MessageType.TEXT
            elif msg_type_raw == "richText":
                parts = payload.get("content", {}).get("richText", [])
                text = " ".join(
                    p.get("text", "") for p in parts if p.get("type") == "text"
                )
                msg_type = MessageType.TEXT
            else:
                text = f"[{msg_type_raw}]"
                msg_type = MessageType.UNKNOWN

            sender_info = (
                payload.get("senderNick", "") or payload.get("senderStaffId", "")
            )
            sender_id = (
                payload.get("senderStaffId", "") or payload.get("senderId", "")
            )
            session_webhook = payload.get("sessionWebhook", "")
            conversation_id = payload.get("conversationId", session_webhook)

            return ChannelMessage(
                channel=self.NAME,
                platform_id=session_webhook or conversation_id,
                sender_id=sender_id,
                sender_name=sender_info,
                text=text,
                msg_type=msg_type,
                raw=payload,
                timestamp=time.time(),
            )
        except Exception as exc:
            logger.warning(
                "[dingtalk] Failed to parse payload: %s — %s", exc, payload
            )
            return None


# Auto-register
ChannelRegistry.get_instance().register(DingTalkChannel)
