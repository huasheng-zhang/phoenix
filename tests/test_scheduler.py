"""Tests for the scheduler module."""
import unittest
from unittest.mock import patch, MagicMock

from phoenix_agent.core.scheduler import PhoenixScheduler, SchedulerTaskConfig, SchedulerConfig


class TestSchedulerTaskConfig(unittest.TestCase):
    """Test SchedulerTaskConfig dataclass."""

    def test_default_values(self):
        cfg = SchedulerTaskConfig()
        self.assertEqual(cfg.name, "")
        self.assertEqual(cfg.cron, "0 9 * * *")
        self.assertEqual(cfg.prompt, "")
        self.assertEqual(cfg.channel, "dingtalk")
        self.assertEqual(cfg.chat_id, "")
        self.assertIsNone(cfg.skill)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.timezone, "Asia/Shanghai")

    def test_custom_values(self):
        cfg = SchedulerTaskConfig(
            name="test-task",
            cron="0 8 * * 1",
            prompt="hello",
            channel="wechat",
            chat_id="chat-123",
            skill="translator",
            enabled=False,
            timezone="UTC",
        )
        self.assertEqual(cfg.name, "test-task")
        self.assertEqual(cfg.cron, "0 8 * * 1")
        self.assertEqual(cfg.channel, "wechat")
        self.assertEqual(cfg.timezone, "UTC")


class TestSchedulerConfig(unittest.TestCase):
    """Test SchedulerConfig dataclass."""

    def test_default_values(self):
        cfg = SchedulerConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.timezone, "Asia/Shanghai")
        self.assertEqual(len(cfg.tasks), 0)


class TestPhoenixScheduler(unittest.TestCase):
    """Test PhoenixScheduler class."""

    @patch("phoenix_agent.core.scheduler.BackgroundScheduler")
    def test_init_without_tasks(self, mock_scheduler_cls):
        """Scheduler should not initialize if no tasks."""
        cfg = MagicMock()
        cfg.scheduler = SchedulerConfig(enabled=True, tasks=[])
        scheduler = PhoenixScheduler(config=cfg)
        self.assertIsNone(scheduler._scheduler)

    @patch("phoenix_agent.core.scheduler.BackgroundScheduler")
    def test_init_with_tasks(self, mock_scheduler_cls):
        """Scheduler should initialize when tasks are present."""
        task_cfg = SchedulerTaskConfig(
            name="test",
            cron="0 9 * * *",
            prompt="hello",
            channel="dingtalk",
            chat_id="chat",
        )
        cfg = MagicMock()
        cfg.scheduler = SchedulerConfig(enabled=True, tasks=[task_cfg])
        scheduler = PhoenixScheduler(config=cfg)
        self.assertIsNotNone(scheduler._scheduler)

    @patch("phoenix_agent.core.scheduler.BackgroundScheduler")
    def test_start_and_stop(self, mock_scheduler_cls):
        """Test start and stop methods."""
        mock_scheduler = MagicMock()
        mock_scheduler_cls.return_value = mock_scheduler

        task_cfg = SchedulerTaskConfig(
            name="test",
            cron="0 9 * * *",
            prompt="hello",
            channel="dingtalk",
            chat_id="chat",
        )
        cfg = MagicMock()
        cfg.scheduler = SchedulerConfig(enabled=True, tasks=[task_cfg])
        scheduler = PhoenixScheduler(config=cfg)

        scheduler.start()
        mock_scheduler.start.assert_called_once()

        scheduler.stop()
        mock_scheduler.shutdown.assert_called_once_with(wait=False)

    @patch("phoenix_agent.core.scheduler.BackgroundScheduler")
    def test_list_tasks(self, mock_scheduler_cls):
        """Test listing tasks."""
        mock_scheduler = MagicMock()
        mock_scheduler_cls.return_value = mock_scheduler

        task_cfg = SchedulerTaskConfig(
            name="task1",
            cron="0 9 * * *",
            prompt="hello",
            channel="dingtalk",
            chat_id="chat1",
        )
        cfg = MagicMock()
        cfg.scheduler = SchedulerConfig(enabled=True, tasks=[task_cfg])
        scheduler = PhoenixScheduler(config=cfg)

        tasks = scheduler.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "task1")


if __name__ == "__main__":
    unittest.main()
