"""
WeChat (微信) Channel Adapter
==============================

Supports three integration modes:

1. **企业微信群机器人 Webhook** (``mode: wecom_webhook``)
   - Configure a group robot in WeCom and copy its Webhook URL.
   - Supports outbound text / markdown messages only (no inbound).
   - Requires only ``webhook_url`` in config.

2. **企业微信应用** (``mode: wecom_app``)
   - A full WeCom self-built app with message send API and callback receiver.
   - Inbound messages are verified with AES encryption (work-wx protocol).
   - Requires ``corp_id``, ``corp_secret``, ``agent_id``, ``token``, ``encoding_aes_key``.

3. **微信公众号** (``mode: mp``)
   - Official Account (订阅号/服务号) server-side message handling.
   - Inbound events are verified via SHA1 signature.
   - Requires ``app_id``, ``app_secret``, ``token``.
   - Note: Only unencrypted text messages are implemented here;
     add your own media handling on top.

Configuration (config.yaml)::

    channels:
      wechat:
        enabled: true
        mode: wecom_webhook      # wecom_webhook | wecom_app | mp

        # ---- wecom_webhook ----
        webhook_url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"

        # ---- wecom_app ----
        corp_id: "ww123abc"
        corp_secret: "your_secret"
        agent_id: 1000001
        token: "your_token"
        encoding_aes_key: "your_aes_key"
        webhook_path: /wechat/webhook

        # ---- mp (公众号) ----
        app_id: "wx123"
        app_secret: "mp_secret"
        # token: (shared with wecom_app field)
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import time
import xml.etree.ElementTree as ET
from base64 import b64decode, b64encode
from typing import Any, Dict, Optional

try:
    from aiohttp import ClientSession as AiohttpSession
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

try:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, JSONResponse
    from starlette.routing import Route
    _HAS_STARLETTE = True
except ImportError:
    _HAS_STARLETTE = False

try:
    from Crypto.Cipher import AES
    _HAS_PYCRYPTODOME = True
except ImportError:
    _HAS_PYCRYPTODOME = False

from phoenix_agent.channels.base import (
    BaseChannel,
    ChannelError,
    ChannelMessage,
    ChannelReply,
    MessageType,
    SignatureVerificationError,
)
from phoenix_agent.channels.registry import ChannelRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AES helpers for WeCom message encryption (企业微信消息加解密)
# ---------------------------------------------------------------------------

class _WXBizMsgCrypt:
    """
    Minimal re-implementation of WeCom's BizMsgCrypt spec.

    Requires pycryptodome: ``pip install pycryptodome``
    """

    def __init__(self, token: str, encoding_aes_key: str, app_id: str) -> None:
        if not _HAS_PYCRYPTODOME:
            raise ImportError(
                "pycryptodome is required for WeCom AES encryption: "
                "pip install pycryptodome"
            )
        self._token   = token
        self._key     = b64decode(encoding_aes_key + "=")   # pad to 44 chars
        self._app_id  = app_id

    # ----- Signature -----

    def _make_signature(self, timestamp: str, nonce: str, encrypted_msg: str = "") -> str:
        parts = sorted([self._token, timestamp, nonce, encrypted_msg])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """Verify URL and return decrypted echostr for registration."""
        expected = self._make_signature(timestamp, nonce, echostr)
        if expected != msg_signature:
            raise SignatureVerificationError("WeCom", "URL signature mismatch")
        return self._decrypt(echostr)

    def decrypt_message(self, msg_signature: str, timestamp: str, nonce: str,
                        xml_body: str) -> str:
        """Decrypt an inbound WeCom message and return the plaintext XML."""
        root = ET.fromstring(xml_body)
        encrypted = root.findtext("Encrypt", "")
        expected = self._make_signature(timestamp, nonce, encrypted)
        if expected != msg_signature:
            raise SignatureVerificationError("WeCom", "Message signature mismatch")
        return self._decrypt(encrypted)

    def _decrypt(self, ciphertext: str) -> str:
        cipher = AES.new(self._key, AES.MODE_CBC, self._key[:16])
        plain  = cipher.decrypt(b64decode(ciphertext))

        # Remove 16-byte random prefix + 4-byte length header
        content = plain[16:]
        msg_len = struct.unpack(">I", content[:4])[0]
        return content[4: 4 + msg_len].decode("utf-8")


# ---------------------------------------------------------------------------
# WeChatChannel
# ---------------------------------------------------------------------------

class WeChatChannel(BaseChannel):
    """
    WeChat / WeCom channel adapter.

    Handles three modes: wecom_webhook (outbound only),
    wecom_app (bi-directional) and mp (Official Account).
    """

    NAME = "wechat"

    def __init__(self, config: Any = None) -> None:
        super().__init__(config)

        cfg = self._channel_cfg
        self._enabled      : bool          = cfg.get("enabled", False)
        self._mode         : str           = cfg.get("mode", "wecom_webhook")
        self._webhook_path : str           = cfg.get("webhook_path", "/wechat/webhook")

        # wecom_webhook
        self._webhook_url  : Optional[str] = cfg.get("webhook_url")

        # wecom_app
        self._corp_id      : Optional[str] = cfg.get("corp_id")
        self._corp_secret  : Optional[str] = cfg.get("corp_secret")
        self._agent_id     : Optional[int] = cfg.get("agent_id")
        self._token        : Optional[str] = cfg.get("token")
        self._aes_key      : Optional[str] = cfg.get("encoding_aes_key")

        # mp
        self._app_id       : Optional[str] = cfg.get("app_id")
        self._app_secret   : Optional[str] = cfg.get("app_secret")

        # WeCom AES crypto helper (created lazily)
        self._crypt: Optional[_WXBizMsgCrypt] = None
        # Access token cache  {token: str, expires_at: float}
        self._access_token: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_crypt(self) -> _WXBizMsgCrypt:
        """Return (and lazily create) the AES crypto helper."""
        if self._crypt is None:
            app_id = self._corp_id or self._app_id or ""
            self._crypt = _WXBizMsgCrypt(
                token=self._token or "",
                encoding_aes_key=self._aes_key or "",
                app_id=app_id,
            )
        return self._crypt

    async def _get_access_token(self) -> str:
        """
        Fetch (or return cached) WeCom / MP access_token.

        WeCom: POST https://qyapi.weixin.qq.com/cgi-bin/gettoken
        MP:    GET  https://api.weixin.qq.com/cgi-bin/token
        """
        if not _HAS_AIOHTTP:
            raise ChannelError(self.NAME, "aiohttp required: pip install aiohttp")

        # Return cached token if still valid
        now = time.time()
        if self._access_token.get("token") and self._access_token.get("expires_at", 0) > now + 60:
            return self._access_token["token"]

        if self._mode == "wecom_app":
            url = (
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
                f"?corpid={self._corp_id}&corpsecret={self._corp_secret}"
            )
        else:  # mp
            url = (
                "https://api.weixin.qq.com/cgi-bin/token"
                f"?grant_type=client_credential"
                f"&appid={self._app_id}&secret={self._app_secret}"
            )

        async with AiohttpSession() as session:
            async with session.get(url) as resp:
                data = await resp.json(content_type=None)

        if "access_token" not in data:
            raise ChannelError(
                self.NAME,
                f"Failed to get access_token: {data.get('errmsg', data)}",
            )

        self._access_token = {
            "token":      data["access_token"],
            "expires_at": now + int(data.get("expires_in", 7200)),
        }
        return self._access_token["token"]

    def _verify_mp_signature(self, signature: str, timestamp: str, nonce: str) -> bool:
        """Verify MP (Official Account) request signature using SHA1."""
        token = self._token or ""
        parts = sorted([token, timestamp, nonce])
        expected = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
        return expected == signature

    # ------------------------------------------------------------------
    # send_message
    # ------------------------------------------------------------------

    async def send_message(self, chat_id: str, reply: ChannelReply) -> None:
        """
        Send a message.

        ``chat_id`` semantics differ per mode:
          - wecom_webhook: ignored (uses configured webhook_url)
          - wecom_app:     the WeCom user open_id (touser)
          - mp:            the MP subscriber open_id (touser)

        Raises:
            ChannelError: on HTTP or API errors.
        """
        if not _HAS_AIOHTTP:
            raise ChannelError(self.NAME, "aiohttp required: pip install aiohttp")

        if self._mode == "wecom_webhook":
            await self._send_wecom_webhook(reply)
        elif self._mode == "wecom_app":
            await self._send_wecom_app(chat_id, reply)
        else:
            await self._send_mp(chat_id, reply)

    async def _send_wecom_webhook(self, reply: ChannelReply) -> None:
        """Send via WeCom group robot Webhook."""
        if not self._webhook_url:
            raise ChannelError(self.NAME, "webhook_url not configured for WeCom webhook mode")

        if reply.markdown:
            body = {
                "msgtype": "markdown",
                "markdown": {"content": reply.markdown},
            }
        else:
            body = {
                "msgtype": "text",
                "text": {
                    "content": reply.text,
                    "mentioned_list": reply.at_users or [],
                },
            }

        async with AiohttpSession() as session:
            async with session.post(self._webhook_url, json=body) as resp:
                data = await resp.json(content_type=None)

        if data.get("errcode", 0) != 0:
            raise ChannelError(
                self.NAME,
                f"WeCom webhook error {data.get('errcode')}: {data.get('errmsg')}",
            )

    async def _send_wecom_app(self, to_user: str, reply: ChannelReply) -> None:
        """Send a message via WeCom app API."""
        token = await self._get_access_token()
        url   = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"

        if reply.markdown:
            body: Dict[str, Any] = {
                "touser":   to_user,
                "msgtype":  "markdown",
                "agentid":  self._agent_id,
                "markdown": {"content": reply.markdown},
            }
        else:
            body = {
                "touser":  to_user,
                "msgtype": "text",
                "agentid": self._agent_id,
                "text":    {"content": reply.text},
            }

        async with AiohttpSession() as session:
            async with session.post(url, json=body) as resp:
                data = await resp.json(content_type=None)

        if data.get("errcode", 0) != 0:
            raise ChannelError(
                self.NAME,
                f"WeCom app error {data.get('errcode')}: {data.get('errmsg')}",
            )

    async def _send_mp(self, to_user: str, reply: ChannelReply) -> None:
        """Send a customer-service (客服) message via MP API."""
        token = await self._get_access_token()
        url   = f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}"

        body: Dict[str, Any] = {
            "touser":  to_user,
            "msgtype": "text",
            "text":    {"content": reply.text},
        }

        async with AiohttpSession() as session:
            async with session.post(url, json=body) as resp:
                data = await resp.json(content_type=None)

        if data.get("errcode", 0) != 0:
            raise ChannelError(
                self.NAME,
                f"MP API error {data.get('errcode')}: {data.get('errmsg')}",
            )

    # ------------------------------------------------------------------
    # Webhook handler (inbound)
    # ------------------------------------------------------------------

    def get_webhook_handler(self):
        """
        Return a Starlette ASGI application for inbound WeChat messages.

        Handles:
          - GET  requests for URL verification (echostr / signature check)
          - POST requests for incoming messages
        """
        channel = self

        if not _HAS_STARLETTE:
            logger.warning("[wechat] Starlette not installed; webhook handler unavailable")
            return None

        async def _handle(request: Request):
            # ---- GET: URL verification ----
            if request.method == "GET":
                return await _verify(request)

            # ---- POST: inbound message ----
            return await _receive(request)

        async def _verify(request: Request):
            ts    = request.query_params.get("timestamp", "")
            nonce = request.query_params.get("nonce", "")

            if channel._mode == "wecom_app":
                msg_sig = request.query_params.get("msg_signature", "")
                echostr = request.query_params.get("echostr", "")
                try:
                    plain = channel._get_crypt().verify_url(msg_sig, ts, nonce, echostr)
                    return PlainTextResponse(plain)
                except Exception as exc:
                    logger.warning("[wechat] URL verification failed: %s", exc)
                    return PlainTextResponse("", status_code=403)

            else:  # mp
                sig     = request.query_params.get("signature", "")
                echostr = request.query_params.get("echostr", "")
                if channel._verify_mp_signature(sig, ts, nonce):
                    return PlainTextResponse(echostr)
                return PlainTextResponse("", status_code=403)

        async def _receive(request: Request):
            ts      = request.query_params.get("timestamp", "")
            nonce   = request.query_params.get("nonce", "")
            body_bytes = await request.body()

            try:
                xml_text = body_bytes.decode("utf-8")

                # Decrypt if WeCom AES encryption is configured
                if channel._mode == "wecom_app" and channel._aes_key:
                    msg_sig = request.query_params.get("msg_signature", "")
                    xml_text = channel._get_crypt().decrypt_message(
                        msg_sig, ts, nonce, xml_text
                    )

                msg = channel._parse_xml(xml_text)
                if msg:
                    await channel._dispatch(msg)

            except SignatureVerificationError as exc:
                logger.warning("[wechat] Signature error: %s", exc)
                return PlainTextResponse("", status_code=403)
            except Exception as exc:
                logger.exception("[wechat] Error processing webhook: %s", exc)

            return PlainTextResponse("success")

        return Starlette(routes=[
            Route("/", endpoint=_handle, methods=["GET", "POST"])
        ])

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_xml(self, xml_text: str) -> Optional[ChannelMessage]:
        """Parse a WeCom/MP inbound XML message into a ChannelMessage."""
        try:
            root = ET.fromstring(xml_text)

            msg_type_raw = (root.findtext("MsgType") or "").strip().lower()
            from_user    = root.findtext("FromUserName") or ""
            to_user      = root.findtext("ToUserName")   or ""
            create_time  = float(root.findtext("CreateTime") or time.time())

            if msg_type_raw == "text":
                text     = root.findtext("Content") or ""
                msg_type = MessageType.TEXT
            elif msg_type_raw == "event":
                event = (root.findtext("Event") or "").lower()
                text  = f"[event:{event}]"
                msg_type = MessageType.EVENT
            elif msg_type_raw == "image":
                text     = f"[image:{root.findtext('PicUrl', '')}]"
                msg_type = MessageType.IMAGE
            elif msg_type_raw == "voice":
                text     = f"[voice:{root.findtext('Recognition', '')}]"
                msg_type = MessageType.VOICE
            else:
                text     = f"[{msg_type_raw}]"
                msg_type = MessageType.UNKNOWN

            return ChannelMessage(
                channel=self.NAME,
                platform_id=to_user,   # The bot's ID
                sender_id=from_user,
                sender_name=from_user,
                text=text,
                msg_type=msg_type,
                raw={"xml": xml_text},
                timestamp=create_time,
            )

        except ET.ParseError as exc:
            logger.warning("[wechat] XML parse error: %s", exc)
            return None
        except Exception as exc:
            logger.warning("[wechat] Failed to parse message: %s", exc)
            return None


# Auto-register
ChannelRegistry.get_instance().register(WeChatChannel)
