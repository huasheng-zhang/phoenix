"""
DingTalk OpenAPI Client for Phoenix Agent
==========================================

Handles file-related DingTalk OpenAPI operations:

- Access token management (auto-refresh, both old & new API)
- Media upload via legacy oapi (images & documents)
- Robot message file download (via downloadCode, new API)
- Send file messages (images & documents) to group/single chats

DingTalk has TWO API domains:
  - ``oapi.dingtalk.com`` — legacy API (media upload/download, old token)
  - ``api.dingtalk.com``   — new API (robot messaging, new token)

File upload MUST use ``oapi.dingtalk.com/media/upload`` (legacy).
Sending file messages MUST use ``api.dingtalk.com/v1.0/robot/...`` (new).

Reference:
    https://open.dingtalk.com/document/orgapp/the-robot-uploads-a-file
    https://open.dingtalk.com/document/orgapp/download-the-file-sent-by-the-user
    https://open.dingtalk.com/document/orgapp/the-robot-sends-a-group-message
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

try:
    import aiohttp
    from aiohttp import ClientSession, ClientTimeout, FormData
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# DingTalk API base URLs — two separate domains!
_DINGTALK_API = "https://api.dingtalk.com"       # new API (robot messaging)
_DINGTALK_OLD_API = "https://oapi.dingtalk.com"   # legacy API (media upload)

# Token endpoints — each domain has its own token format
# New API token: used for robot messaging (send_file_to_group, send_file_to_single)
_TOKEN_URL_NEW = f"{_DINGTALK_API}/v1.0/oauth2/accessToken"
# Old API token: used for media upload via oapi.dingtalk.com
_TOKEN_URL_OLD = f"{_DINGTALK_OLD_API}/gettoken"

# Media upload — MUST use legacy oapi endpoint!
# Reference: https://open.dingtalk.com/document/orgapp/the-robot-uploads-a-file
_MEDIA_UPLOAD_URL = f"{_DINGTALK_OLD_API}/media/upload"

# Robot send message endpoints — new API
_ROBOT_SEND_URL = f"{_DINGTALK_API}/v1.0/robot/oToMessages/batchSend"
_ROBOT_GROUP_SEND_URL = f"{_DINGTALK_API}/v1.0/robot/groupMessages/send"

# File download endpoint — new API (robot receives user file via downloadCode)
_FILE_DOWNLOAD_URL = f"{_DINGTALK_API}/v1.0/robot/messageFiles/download"


# ---------------------------------------------------------------------------
# Access Token Manager
# ---------------------------------------------------------------------------

@dataclass
class _TokenManager:
    """
    Manages DingTalk access_tokens with auto-refresh.

    DingTalk has two token types:
    - ``new`` (api.dingtalk.com):  JSON body {appKey, appSecret} → {"accessToken": "..."}
    - ``old`` (oapi.dingtalk.com):  GET query params ?appkey=xxx&appsecret=xxx → {"access_token": "..."}
    """

    client_id: str
    client_secret: str

    # Separate cache for each token type
    _new_token: Optional[str] = field(default=None, repr=False)
    _new_expires_at: float = field(default=0.0, repr=False)
    _old_token: Optional[str] = field(default=None, repr=False)
    _old_expires_at: float = field(default=0.0, repr=False)
    _lock: Any = field(default_factory=asyncio.Lock, repr=False)

    async def get_new_token(self) -> str:
        """
        Return a valid new-style access_token (for api.dingtalk.com).

        Used by: robot messaging (send_file_to_group, send_file_to_single),
                 robot file download.
        """
        if not _HAS_AIOHTTP:
            raise RuntimeError("aiohttp is required: pip install aiohttp")

        if self._new_token and self._new_expires_at > time.time() + 60:
            return self._new_token

        async with self._lock:
            if self._new_token and self._new_expires_at > time.time() + 60:
                return self._new_token

            logger.debug("[dingtalk][api] Refreshing NEW access_token...")
            async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                async with session.post(
                    _TOKEN_URL_NEW,
                    json={
                        "appKey": self.client_id,
                        "appSecret": self.client_secret,
                    },
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    data = await resp.json()

            if resp.status != 200 or "accessToken" not in data:
                # Sanitize response to avoid leaking tokens in error messages
                safe_data = {k: "***" if k.lower() in ("accesstoken", "token") else v
                             for k, v in data.items()}
                raise ChannelAPIError(
                    f"Failed to get new access_token: HTTP {resp.status}, {safe_data}"
                )

            self._new_token = data["accessToken"]
            self._new_expires_at = time.time() + data.get("expireIn", 7200) - 200
            logger.debug("[dingtalk][api] NEW access_token refreshed, expires in %.0fs",
                         self._new_expires_at - time.time())
            return self._new_token

    async def get_old_token(self) -> str:
        """
        Return a valid old-style access_token (for oapi.dingtalk.com).

        Used by: media upload (oapi.dingtalk.com/media/upload).
        """
        if not _HAS_AIOHTTP:
            raise RuntimeError("aiohttp is required: pip install aiohttp")

        if self._old_token and self._old_expires_at > time.time() + 60:
            return self._old_token

        async with self._lock:
            if self._old_token and self._old_expires_at > time.time() + 60:
                return self._old_token

            logger.debug("[dingtalk][api] Refreshing OLD access_token...")
            async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                async with session.get(
                    _TOKEN_URL_OLD,
                    params={
                        "appkey": self.client_id,
                        "appsecret": self.client_secret,
                    },
                ) as resp:
                    data = await resp.json()

            if resp.status != 200 or "access_token" not in data:
                safe_data = {k: "***" if k.lower() in ("accesstoken", "access_token", "token") else v
                             for k, v in data.items()}
                raise ChannelAPIError(
                    f"Failed to get old access_token: HTTP {resp.status}, {safe_data}"
                )

            self._old_token = data["access_token"]
            self._old_expires_at = time.time() + 7000
            logger.debug("[dingtalk][api] OLD access_token refreshed, expires in %.0fs",
                         self._old_expires_at - time.time())
            return self._old_token


# ---------------------------------------------------------------------------
# Public API class
# ---------------------------------------------------------------------------

class DingTalkOpenAPI:
    """
    Async DingTalk OpenAPI client for file operations.

    Usage::

        api = DingTalkOpenAPI(client_id="ding...", client_secret="...")
        media_id = await api.upload_file("/path/to/report.pdf")
        await api.send_file_to_chat(
            conversation_id="cidXXX",
            media_id=media_id,
            file_name="report.pdf",
        )
    """

    def __init__(self, client_id: str, client_secret: str):
        if not _HAS_AIOHTTP:
            raise RuntimeError("aiohttp is required: pip install aiohttp")
        self._tokens = _TokenManager(client_id, client_secret)
        self._client_id = client_id
        self._client_secret = client_secret

    # ------------------------------------------------------------------
    # Media Upload
    # ------------------------------------------------------------------

    async def upload_file(
        self,
        file_path: Optional[str] = None,
        file_bytes: Optional[bytes] = None,
        file_name: str = "file",
        file_type: Optional[str] = None,
    ) -> str:
        """
        Upload a file to DingTalk media server (legacy oapi endpoint).

        **MUST use oapi.dingtalk.com/media/upload** — the new API endpoint
        ``v1.0/robot/messageFiles/upload`` does NOT exist and returns 404.

        Returns the ``media_id`` string.

        Args:
            file_path:  Local file path to upload.
            file_bytes: Raw bytes to upload (alternative to file_path).
            file_name:  File name for the upload.
            file_type:  MIME type (auto-detected if not given).

        Returns:
            media_id string from DingTalk.

        Raises:
            ChannelAPIError: On upload failure.

        Important notes:
            - The form field name MUST be ``media``, not ``file``.
            - The ``type`` field accepts strings: "file", "image", "voice", "video".
            - File name MUST be ASCII-only; Chinese characters cause errcode 40035.
            - Token goes in query param (``?access_token=xxx``), not header.
        """
        if not file_path and not file_bytes:
            raise ValueError("Either file_path or file_bytes must be provided")

        # Read file
        if file_path:
            if not os.path.isfile(file_path):
                raise FileNotFoundError(f"File not found: {file_path}")
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            # Use original filename if not specified
            if file_name == "file":
                file_name = os.path.basename(file_path)

        # Auto-detect MIME type
        if not file_type:
            file_type = self._guess_mime_type(file_name)

        # DingTalk oapi media upload requires ASCII-only filenames
        # Chinese filenames cause errcode 40035 "missing parameter media"
        safe_name = self._to_ascii_filename(file_name)

        # Determine the upload type string: "image" or "file"
        if file_type and file_type.startswith("image/"):
            upload_type = "image"
        else:
            upload_type = "file"

        logger.debug("[dingtalk][api] Uploading file %r (type=%s, size=%d bytes) via oapi",
                      safe_name, upload_type, len(file_bytes))

        # Get old-style token (for oapi.dingtalk.com)
        old_token = await self._tokens.get_old_token()

        # Build multipart form data — field name MUST be "media"
        form_data = FormData()
        form_data.add_field("type", upload_type)
        form_data.add_field(
            "media",
            file_bytes,
            filename=safe_name,
            content_type=file_type or "application/octet-stream",
        )

        async with ClientSession(timeout=ClientTimeout(total=120)) as session:
            async with session.post(
                _MEDIA_UPLOAD_URL,
                params={"access_token": old_token},  # token in query param!
                data=form_data,
                # Do NOT set Content-Type header manually; aiohttp sets boundary
            ) as resp:
                data = await resp.json()

        # oapi returns {"errcode": 0, "errmsg": "ok", "media_id": "..."}
        if data.get("errcode") != 0:
            raise ChannelAPIError(
                f"Upload failed via oapi: errcode={data.get('errcode')}, "
                f"errmsg={data.get('errmsg')}, full={data}"
            )

        media_id = data["media_id"]
        logger.info("[dingtalk][api] File uploaded successfully via oapi: %r -> media_id=%s",
                     safe_name, media_id[:16] + "...")
        return media_id

    # ------------------------------------------------------------------
    # File Download (from user message via downloadCode)
    # ------------------------------------------------------------------

    async def download_file(
        self,
        download_code: str,
        file_name: str = "downloaded_file",
    ) -> Tuple[bytes, str]:
        """
        Download a file sent by a user in DingTalk chat.

        Args:
            download_code: The ``downloadCode`` from the message payload.
            file_name:     Suggested file name (for logging & extension).

        Returns:
            Tuple of (file_bytes, actual_file_name).

        Raises:
            ChannelAPIError: On download failure.
        """
        token = await self._tokens.get_new_token()

        logger.debug("[dingtalk][api] Downloading file with downloadCode=%s",
                      download_code[:16] + "...")

        async with ClientSession(timeout=ClientTimeout(total=60)) as session:
            # Step 1: Get the download URL
            async with session.post(
                _FILE_DOWNLOAD_URL,
                headers={
                    "x-acs-dingtalk-access-token": token,
                    "Content-Type": "application/json",
                },
                json={"downloadCode": download_code},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ChannelAPIError(
                        f"File download info failed: HTTP {resp.status}, {text}"
                    )
                data = await resp.json()

            download_url = data.get("downloadUrl")
            if not download_url:
                raise ChannelAPIError(
                    f"No downloadUrl in response: {data}"
                )

            # Step 2: Download the actual file content
            async with session.get(download_url) as file_resp:
                if file_resp.status != 200:
                    raise ChannelAPIError(
                        f"File download failed: HTTP {file_resp.status}"
                    )
                file_bytes = await file_resp.read()

        # Try to get actual filename from download response
        actual_name = file_name
        content_disp = data.get("fileName") or ""
        if content_disp:
            actual_name = content_disp

        logger.info("[dingtalk][api] File downloaded: %r (%d bytes)",
                     actual_name, len(file_bytes))
        return file_bytes, actual_name

    # ------------------------------------------------------------------
    # Send File Message
    # ------------------------------------------------------------------

    async def send_file_to_single(
        self,
        robot_code: str,
        user_ids: list,
        media_id: str,
        file_name: str = "file",
        is_image: bool = False,
    ) -> None:
        """
        Send a file message to single (1-on-1) chats via robot.

        Args:
            robot_code: The robot's appKey.
            user_ids:   List of DingTalk user IDs to send to.
            media_id:   Media ID from upload_file().
            file_name:  Display file name.
            is_image:   True for images, False for documents.
        """
        token = await self._tokens.get_new_token()

        msg_key = "sampleImage" if is_image else "sampleFile"

        # Build msgParam: sampleImage uses photoURL, sampleFile uses mediaId+fileName+fileType
        if is_image:
            msg_param = json.dumps({"photoURL": f"mediaId:{media_id}"})
        else:
            msg_param = json.dumps({
                "mediaId": media_id,
                "fileName": file_name,
                "fileType": os.path.splitext(file_name)[1].lstrip(".") or "bin",
            })

        body: Dict[str, Any] = {
            "robotCode": robot_code,
            "userIds": user_ids,
            "msgKey": msg_key,
            "msgParam": msg_param,
        }

        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.post(
                _ROBOT_SEND_URL,
                headers={
                    "x-acs-dingtalk-access-token": token,
                    "Content-Type": "application/json",
                },
                json=body,
            ) as resp:
                data = await resp.json()

        if resp.status != 200:
            raise ChannelAPIError(
                f"Send file failed: HTTP {resp.status}, {data}"
            )

        logger.info("[dingtalk][api] File message sent to %d user(s): %r",
                     len(user_ids), file_name)

    async def send_file_to_group(
        self,
        robot_code: str,
        conversation_id: str,
        media_id: str,
        file_name: str = "file",
        is_image: bool = False,
        cool_app_code: Optional[str] = None,
    ) -> None:
        """
        Send a file message to a group chat via robot.

        Args:
            robot_code:       The robot's appKey.
            conversation_id:  DingTalk group conversation ID.
            media_id:         Media ID from upload_file().
            file_name:        Display file name.
            is_image:         True for images, False for documents.
            cool_app_code:    Optional coolAppCode for group messages.
        """
        token = await self._tokens.get_new_token()

        msg_key = "sampleImage" if is_image else "sampleFile"

        # Build msgParam: sampleImage uses photoURL, sampleFile uses mediaId+fileName+fileType
        if is_image:
            msg_param = json.dumps({"photoURL": f"mediaId:{media_id}"})
        else:
            msg_param = json.dumps({
                "mediaId": media_id,
                "fileName": file_name,
                "fileType": os.path.splitext(file_name)[1].lstrip(".") or "bin",
            })

        body: Dict[str, Any] = {
            "robotCode": robot_code,
            "openConversationId": conversation_id,
            "msgKey": msg_key,
            "msgParam": msg_param,
        }

        # Group messages may need coolAppCode
        if cool_app_code:
            body["coolAppCode"] = cool_app_code

        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.post(
                _ROBOT_GROUP_SEND_URL,
                headers={
                    "x-acs-dingtalk-access-token": token,
                    "Content-Type": "application/json",
                },
                json=body,
            ) as resp:
                data = await resp.json()

        if resp.status != 200:
            raise ChannelAPIError(
                f"Send group file failed: HTTP {resp.status}, {data}"
            )

        logger.info("[dingtalk][api] File message sent to group %s: %r",
                     conversation_id[:12] + "...", file_name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _guess_mime_type(file_name: str) -> str:
        """Guess MIME type from file extension."""
        ext = os.path.splitext(file_name)[1].lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            ".svg": "image/svg+xml",
            ".pdf": "application/pdf",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xls": "application/vnd.ms-excel",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".ppt": "application/vnd.ms-powerpoint",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".txt": "text/plain",
            ".csv": "text/csv",
            ".zip": "application/zip",
            ".rar": "application/x-rar-compressed",
            ".7z": "application/x-7z-compressed",
            ".mp3": "audio/mpeg",
            ".mp4": "video/mp4",
            ".wav": "audio/wav",
        }
        return mime_map.get(ext, "application/octet-stream")

    @staticmethod
    def _to_ascii_filename(file_name: str) -> str:
        """
        Convert a filename to ASCII-only.

        DingTalk oapi media upload rejects non-ASCII filenames (errcode 40035).
        Strategy: keep extension, replace non-ASCII chars with a UUID suffix.
        """
        name_part, ext = os.path.splitext(file_name)
        # Check if filename is already pure ASCII
        try:
            name_part.encode("ascii")
            return file_name
        except UnicodeEncodeError:
            pass
        # Generate a short UUID to replace non-ASCII characters
        short_id = uuid.uuid4().hex[:8]
        return f"file_{short_id}{ext}"

    # ------------------------------------------------------------------
    # Send Text Message (Proactive Push)
    # ------------------------------------------------------------------

    async def send_text_to_user(
        self,
        robot_code: str,
        user_ids: List[str],
        content: str,
    ) -> None:
        """
        Send a text message to single users via robot (proactive push).

        Args:
            robot_code:  The robot's appKey.
            user_ids:    List of DingTalk user IDs to send to.
            content:     Text message content.
        """
        if not user_ids:
            raise ValueError("user_ids cannot be empty")

        token = await self._tokens.get_new_token()

        msg_param = json.dumps({"content": content})

        body: Dict[str, Any] = {
            "robotCode": robot_code,
            "userIds": user_ids,
            "msgKey": "sampleText",
            "msgParam": msg_param,
        }

        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.post(
                _ROBOT_SEND_URL,
                headers={
                    "x-acs-dingtalk-access-token": token,
                    "Content-Type": "application/json",
                },
                json=body,
            ) as resp:
                data = await resp.json()

        if resp.status != 200:
            raise ChannelAPIError(
                f"Send text to user failed: HTTP {resp.status}, {data}"
            )

        logger.info("[dingtalk][api] Text message sent to %d user(s)", len(user_ids))

    async def send_text_to_group(
        self,
        robot_code: str,
        conversation_id: str,
        content: str,
        cool_app_code: Optional[str] = None,
    ) -> None:
        """
        Send a text message to a group chat via robot (proactive push).

        Args:
            robot_code:       The robot's appKey.
            conversation_id:  DingTalk group conversation ID (openConversationId).
            content:          Text message content.
            cool_app_code:    Optional coolAppCode for group messages.
        """
        token = await self._tokens.get_new_token()

        msg_param = json.dumps({"content": content})

        body: Dict[str, Any] = {
            "robotCode": robot_code,
            "openConversationId": conversation_id,
            "msgKey": "sampleText",
            "msgParam": msg_param,
        }

        if cool_app_code:
            body["coolAppCode"] = cool_app_code

        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.post(
                _ROBOT_GROUP_SEND_URL,
                headers={
                    "x-acs-dingtalk-access-token": token,
                    "Content-Type": "application/json",
                },
                json=body,
            ) as resp:
                data = await resp.json()

        if resp.status != 200:
            raise ChannelAPIError(
                f"Send text to group failed: HTTP {resp.status}, {data}"
            )

        logger.info("[dingtalk][api] Text message sent to group %s",
                    conversation_id[:12] + "...")


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class ChannelAPIError(Exception):
    """Raised when a DingTalk OpenAPI call fails."""
    pass
