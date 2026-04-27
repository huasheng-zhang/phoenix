"""
Tests for Phoenix Agent channel adapters.

These tests are fully offline — no real HTTP calls are made.
Platform API calls are either monkeypatched or simply not triggered because
the tests exercise only the *parsing* and *config* paths.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phoenix_agent.channels.base import (
    BaseChannel,
    ChannelMessage,
    ChannelReply,
    MessageType,
)
from phoenix_agent.channels.registry import ChannelRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine synchronously (helper for non-async test functions)."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# ChannelConfig integration tests
# ---------------------------------------------------------------------------

class TestChannelsConfig:
    """Verify that config.py correctly parses the 'channels:' YAML section."""

    def test_default_channels_config(self, tmp_path: Path):
        """A config with no 'channels:' section returns sensible defaults."""
        from phoenix_agent.core.config import Config

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "provider:\n  api_key: test\n",
            encoding="utf-8",
        )

        cfg = Config(path=str(cfg_file))

        assert cfg.channels.host == "0.0.0.0"
        assert cfg.channels.port == 8080
        # All four known channels should be present but disabled
        for ch_name in ("dingtalk", "wechat", "qq", "telegram"):
            assert ch_name in cfg.channels.channels
            assert cfg.channels.channels[ch_name].enabled is False

    def test_channels_config_from_yaml(self, tmp_path: Path):
        """Channel settings are loaded from the YAML file correctly."""
        from phoenix_agent.core.config import Config

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            """
provider:
  api_key: test
channels:
  server:
    host: "127.0.0.1"
    port: 9000
  telegram:
    enabled: true
    bot_token: "123:ABC"
    mode: polling
  dingtalk:
    enabled: true
    webhook_url: "https://example.com/robot"
""",
            encoding="utf-8",
        )

        cfg = Config(path=str(cfg_file))

        assert cfg.channels.host == "127.0.0.1"
        assert cfg.channels.port == 9000

        tg = cfg.channels.channels["telegram"]
        assert tg.enabled is True
        assert tg.settings["bot_token"] == "123:ABC"
        assert tg.settings["mode"] == "polling"

        dt = cfg.channels.channels["dingtalk"]
        assert dt.enabled is True
        assert dt.settings["webhook_url"] == "https://example.com/robot"


# ---------------------------------------------------------------------------
# ChannelRegistry
# ---------------------------------------------------------------------------

class TestChannelRegistry:
    """Verify the channel registry correctly stores and retrieves adapters."""

    def test_register_and_get(self):
        """Registering a channel class allows retrieval by name."""

        class _DummyChannel(BaseChannel):
            NAME = "_test_dummy"

            async def send_message(self, chat_id, reply):
                pass

            def get_webhook_handler(self):
                return None

        reg = ChannelRegistry()
        reg.register(_DummyChannel)

        assert reg.get("_test_dummy") is _DummyChannel
        assert "_test_dummy" in reg.list_channels()

    def test_get_unknown_channel_returns_none(self):
        reg = ChannelRegistry()
        assert reg.get("nonexistent_channel_xyz") is None

    def test_create_returns_instance(self):
        """create() returns an initialised instance."""

        class _DummyChannel2(BaseChannel):
            NAME = "_test_dummy2"

            async def send_message(self, chat_id, reply):
                pass

            def get_webhook_handler(self):
                return None

        reg = ChannelRegistry()
        reg.register(_DummyChannel2)
        instance = reg.create("_test_dummy2", config={})
        assert isinstance(instance, _DummyChannel2)


# ---------------------------------------------------------------------------
# DingTalk channel
# ---------------------------------------------------------------------------

class TestDingTalkChannel:
    """Test DingTalk webhook mode parsing and message building."""

    def _make_channel(self, settings=None):
        from phoenix_agent.channels.dingtalk import DingTalkChannel

        cfg = {
            "enabled": True,
            "mode": "webhook",
            "webhook_url": "https://oapi.example.com/robot",
            "secret": "",
            **(settings or {}),
        }
        return DingTalkChannel(config=cfg)

    def test_parse_text_message(self):
        """Standard text message is parsed into a ChannelMessage."""
        ch = self._make_channel()

        payload = {
            "msgtype": "text",
            "text": {"content": "hello phoenix"},
            "senderNick": "Alice",
            "senderId": "alice123",
            "sessionWebhook": "https://oapi.example.com/robot/session",
            "createAt": int(time.time() * 1000),
        }
        msg = ch._parse_payload(payload)

        assert msg is not None
        assert msg.text == "hello phoenix"
        assert msg.msg_type == MessageType.TEXT
        assert msg.sender_name == "Alice"
        assert msg.platform_id == "https://oapi.example.com/robot/session"

    def test_parse_at_message(self):
        """@-mention messages are parsed correctly."""
        ch = self._make_channel(settings={"app_key": "myapp"})

        payload = {
            "msgtype": "text",
            "text": {"content": "@PhoenixBot what time is it?"},
            "senderNick": "Bob",
            "senderId": "bob456",
            "sessionWebhook": "https://oapi.example.com/robot/session",
            "atUsers": [{"dingtalkId": "bot123"}],
            "createAt": int(time.time() * 1000),
        }
        msg = ch._parse_payload(payload)

        assert msg is not None
        assert "what time is it" in msg.text

    def test_build_text_body(self):
        """Outbound text reply is serialised to correct DingTalk payload."""
        ch = self._make_channel()
        reply = ChannelReply(text="Hello, world!")
        body = ch._build_body(reply)

        assert body["msgtype"] == "text"
        assert body["text"]["content"] == "Hello, world!"

    def test_build_markdown_body(self):
        """Markdown reply uses the markdown msgtype."""
        ch = self._make_channel()
        reply = ChannelReply(text="plain", markdown="# Hello")
        body = ch._build_body(reply)

        assert body["msgtype"] == "markdown"
        assert body["markdown"]["text"] == "# Hello"


# ---------------------------------------------------------------------------
# QQ channel
# ---------------------------------------------------------------------------

class TestQQChannel:
    """Test QQ OneBot v11 event parsing."""

    def _make_channel(self, self_id=111):
        from phoenix_agent.channels.qq import QQChannel

        return QQChannel(config={
            "enabled": True,
            "api_url": "http://127.0.0.1:5700",
            "self_id": self_id,
            "superusers": [999],
        })

    def test_parse_private_message(self):
        """Private chat messages are parsed and routed correctly."""
        ch = self._make_channel()

        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 123456,
            "raw_message": "hi there",
            "sender": {"nickname": "Tom"},
            "time": int(time.time()),
        }
        msg = ch._parse_event(event)

        assert msg is not None
        assert msg.text == "hi there"
        assert msg.platform_id == "private:123456"
        assert msg.extra["is_superuser"] is False

    def test_parse_group_message(self):
        """Group messages include group_id in platform_id."""
        ch = self._make_channel()

        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 123456,
            "group_id": 789,
            "raw_message": "[CQ:at,qq=111] hello bot",
            "sender": {"nickname": "Jerry"},
            "time": int(time.time()),
        }
        msg = ch._parse_event(event)

        assert msg is not None
        assert msg.platform_id == "group:789"
        # Bot's @-mention CQ code should be stripped
        assert "[CQ:at,qq=111]" not in msg.text
        assert "hello bot" in msg.text

    def test_parse_superuser(self):
        """Messages from superusers set is_superuser=True."""
        ch = self._make_channel()

        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 999,    # This is in superusers
            "raw_message": "sudo command",
            "sender": {"nickname": "Admin"},
            "time": int(time.time()),
        }
        msg = ch._parse_event(event)
        assert msg is not None
        assert msg.extra["is_superuser"] is True

    def test_non_message_events_ignored(self):
        """Non-message events (meta, notice) return None."""
        ch = self._make_channel()

        event = {"post_type": "meta_event", "meta_event_type": "heartbeat"}
        assert ch._parse_event(event) is None


# ---------------------------------------------------------------------------
# Telegram channel
# ---------------------------------------------------------------------------

class TestTelegramChannel:
    """Test Telegram update parsing and message splitting."""

    def _make_channel(self):
        from phoenix_agent.channels.telegram import TelegramChannel

        return TelegramChannel(config={
            "enabled": True,
            "bot_token": "123:ABC",
            "mode": "webhook",
            "superusers": [777],
        })

    def test_parse_text_update(self):
        """Standard text message is parsed correctly."""
        ch = self._make_channel()

        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 42, "first_name": "Alice", "last_name": "W"},
                "text": "Hello Phoenix!",
                "date": int(time.time()),
            },
        }
        msg = ch._parse_update(update)

        assert msg is not None
        assert msg.text == "Hello Phoenix!"
        assert msg.channel == "telegram"
        assert msg.platform_id == "100"
        assert msg.sender_name == "Alice W"

    def test_parse_command(self):
        """Command messages get the COMMAND type."""
        ch = self._make_channel()

        update = {
            "update_id": 2,
            "message": {
                "chat": {"id": 200, "type": "private"},
                "from": {"id": 42, "first_name": "Bob"},
                "text": "/start",
                "date": int(time.time()),
            },
        }
        msg = ch._parse_update(update)
        assert msg is not None
        assert msg.msg_type == MessageType.COMMAND

    def test_parse_superuser(self):
        ch = self._make_channel()

        update = {
            "update_id": 3,
            "message": {
                "chat": {"id": 777, "type": "private"},
                "from": {"id": 777, "first_name": "Admin"},
                "text": "admin cmd",
                "date": int(time.time()),
            },
        }
        msg = ch._parse_update(update)
        assert msg is not None
        assert msg.extra["is_superuser"] is True

    def test_non_message_update_ignored(self):
        """Updates without 'message' key return None."""
        ch = self._make_channel()
        assert ch._parse_update({"update_id": 4, "callback_query": {}}) is None

    def test_split_long_text(self):
        """Text longer than 4096 chars is split into multiple chunks."""
        ch = self._make_channel()
        long_text = "x" * 9000
        chunks = ch._split_text(long_text)
        assert len(chunks) == 3
        assert all(len(c) <= 4096 for c in chunks)
        assert "".join(chunks) == long_text

    def test_no_split_for_short_text(self):
        ch = self._make_channel()
        short = "Hello!"
        chunks = ch._split_text(short)
        assert chunks == [short]


# ---------------------------------------------------------------------------
# WeChat channel
# ---------------------------------------------------------------------------

class TestWeChatChannel:
    """Test WeChat XML message parsing."""

    def _make_channel(self, mode="wecom_webhook"):
        from phoenix_agent.channels.wechat import WeChatChannel

        return WeChatChannel(config={
            "enabled": True,
            "mode": mode,
            "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
        })

    def test_parse_text_xml(self):
        """Plain-text XML messages are parsed correctly."""
        ch = self._make_channel(mode="mp")

        xml = """<xml>
            <ToUserName><![CDATA[gh_abc]]></ToUserName>
            <FromUserName><![CDATA[openid123]]></FromUserName>
            <CreateTime>1700000000</CreateTime>
            <MsgType><![CDATA[text]]></MsgType>
            <Content><![CDATA[Hello bot!]]></Content>
        </xml>"""

        msg = ch._parse_xml(xml)

        assert msg is not None
        assert msg.text == "Hello bot!"
        assert msg.msg_type == MessageType.TEXT
        assert msg.sender_id == "openid123"

    def test_parse_event_xml(self):
        """Event messages are parsed with EVENT type."""
        ch = self._make_channel(mode="mp")

        xml = """<xml>
            <ToUserName><![CDATA[gh_abc]]></ToUserName>
            <FromUserName><![CDATA[user123]]></FromUserName>
            <CreateTime>1700000000</CreateTime>
            <MsgType><![CDATA[event]]></MsgType>
            <Event><![CDATA[subscribe]]></Event>
        </xml>"""

        msg = ch._parse_xml(xml)

        assert msg is not None
        assert msg.msg_type == MessageType.EVENT
        assert "subscribe" in msg.text

    def test_parse_invalid_xml(self):
        """Malformed XML returns None gracefully."""
        ch = self._make_channel()
        assert ch._parse_xml("not xml at all!!!") is None

    def test_mp_signature_verification(self):
        """SHA1 signature verification returns True for correct signature."""
        import hashlib

        ch = self._make_channel()
        ch._token = "my_secret_token"

        ts, nonce = "1700000000", "abc123"
        parts = sorted(["my_secret_token", ts, nonce])
        expected = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

        assert ch._verify_mp_signature(expected, ts, nonce) is True
        assert ch._verify_mp_signature("wrong_sig", ts, nonce) is False
