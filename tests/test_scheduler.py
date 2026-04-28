"""Tests for the scheduler module."""
import unittest
from unittest.mock import MagicMock, patch


class TestPhoenixScheduler(unittest.TestCase):
    """Test PhoenixScheduler class."""

    def _make_scheduler_config(self, enabled=True, tasks=None):
        """Build a real SchedulerConfig, then wrap it in a MagicMock with spec."""
        from phoenix_agent.core.scheduler import SchedulerConfig, SchedulerTaskConfig
        cfg = MagicMock(spec=SchedulerConfig)
        cfg.scheduler = SchedulerConfig(enabled=enabled, tasks=tasks or [])
        cfg.provider = MagicMock()
        cfg.agent = MagicMock()
        cfg.tools = MagicMock()
        return cfg

    def test_init_without_tasks(self):
        """Scheduler._scheduler is None when there are no tasks."""
        from phoenix_agent.core.scheduler import PhoenixScheduler
        scheduler = PhoenixScheduler(
            config=self._make_scheduler_config(enabled=True, tasks=[])
        )
        self.assertIsNone(scheduler._scheduler)

    def test_init_disabled(self):
        """Scheduler._scheduler is None when scheduler is disabled."""
        from phoenix_agent.core.scheduler import PhoenixScheduler
        scheduler = PhoenixScheduler(
            config=self._make_scheduler_config(enabled=False, tasks=[])
        )
        self.assertIsNone(scheduler._scheduler)

    def test_init_with_tasks(self):
        """Scheduler._scheduler is set when tasks are present."""
        from phoenix_agent.core.scheduler import PhoenixScheduler, SchedulerTaskConfig
        scheduler = PhoenixScheduler(
            config=self._make_scheduler_config(enabled=True, tasks=[
                SchedulerTaskConfig(
                    name="test", cron="0 9 * * *",
                    prompt="hello", channel="dingtalk", chat_id="chat"
                )
            ])
        )
        self.assertIsNotNone(scheduler._scheduler)

    def test_list_tasks(self):
        """list_tasks() returns the configured tasks."""
        from phoenix_agent.core.scheduler import PhoenixScheduler, SchedulerTaskConfig
        scheduler = PhoenixScheduler(
            config=self._make_scheduler_config(enabled=True, tasks=[
                SchedulerTaskConfig(
                    name="task1", cron="0 9 * * *",
                    prompt="hello", channel="dingtalk", chat_id="chat1"
                ),
                SchedulerTaskConfig(
                    name="task2", cron="0 10 * * *",
                    prompt="world", channel="wechat", chat_id="chat2"
                ),
            ])
        )
        tasks = scheduler.list_tasks()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["name"], "task1")
        self.assertEqual(tasks[1]["name"], "task2")

    def test_start_and_stop(self):
        """start() and stop() work without raising exceptions."""
        from phoenix_agent.core.scheduler import PhoenixScheduler, SchedulerTaskConfig
        scheduler = PhoenixScheduler(
            config=self._make_scheduler_config(enabled=True, tasks=[
                SchedulerTaskConfig(
                    name="test", cron="0 9 * * *",
                    prompt="hello", channel="dingtalk", chat_id="chat"
                )
            ])
        )
        # _scheduler is created at __init__ time when tasks are present
        self.assertIsNotNone(scheduler._scheduler)
        # start() and stop() should not raise
        scheduler.start()
        scheduler.stop()


if __name__ == "__main__":
    unittest.main()
