"""
Scheduled Task Scheduler Module

Phoenix Agent 定时任务调度器，基于 APScheduler 实现。
支持 cron 表达式，任务触发后调用 Agent 并将结果推送到指定渠道。

配置示例 (config.yaml):
    scheduler:
      enabled: true
      tasks:
        - name: "每日天气摘要"
          cron: "0 9 * * *"
          prompt: "查询深圳今天的天气，生成简短摘要"
          channel: dingtalk
          chat_id: "xxx@chat"
          skill: null
"""
import logging
import uuid
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

from phoenix_agent.core.config import Config, get_config

logger = logging.getLogger(__name__)


@dataclass
class SchedulerTaskConfig:
    """单个定时任务的配置。"""
    name: str = ""
    cron: str = "0 9 * * *"   # 默认每天 9:00
    prompt: str = ""
    channel: str = "dingtalk"   # dingtalk / wechat / qq / telegram
    chat_id: str = ""
    skill: Optional[str] = None  # 可选：激活指定 skill
    enabled: bool = True
    timezone: str = "Asia/Shanghai"


@dataclass
class SchedulerConfig:
    """调度器总配置。"""
    enabled: bool = False
    timezone: str = "Asia/Shanghai"
    tasks: List[SchedulerTaskConfig] = field(default_factory=list)


class PhoenixScheduler:
    """
    Phoenix Agent 定时任务调度器。

    在 Agent 启动时自动加载 config.yaml 中的 scheduled_tasks，
    使用 APScheduler 按 cron 表达式执行任务。
    任务触发时调用 Agent 生成回复，并通过对应 channel 推送结果。
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self._scheduler: Optional[BackgroundScheduler] = None
        self._task_configs: List[SchedulerTaskConfig] = []
        self._job_ids: Dict[str, str] = {}  # name -> job_id

        # 从 config 加载任务
        self._load_tasks_from_config()

        if self._task_configs:
            self._init_scheduler()

    def _load_tasks_from_config(self) -> None:
        """从 config.yaml 的 scheduler: 段加载任务配置。"""
        raw = getattr(self.config, "scheduler", None)
        if not raw:
            return

        # 兼容 dict 和 SchedulerConfig 对象
        if isinstance(raw, dict):
            enabled = raw.get("enabled", False)
            timezone = raw.get("timezone", "Asia/Shanghai")
            task_dicts = raw.get("tasks", [])
        elif hasattr(raw, "enabled"):
            enabled = raw.enabled
            timezone = raw.timezone
            task_dicts = raw.tasks if isinstance(raw.tasks, list) else []
        else:
            return

        if not enabled:
            logger.info("Scheduler is disabled in config.")
            return

        for t in task_dicts:
            if not isinstance(t, dict):
                continue
            task_cfg = SchedulerTaskConfig(
                name=t.get("name", f"task-{uuid.uuid4().hex[:8]}"),
                cron=t.get("cron", "0 9 * * *"),
                prompt=t.get("prompt", ""),
                channel=t.get("channel", "dingtalk"),
                chat_id=t.get("chat_id", ""),
                skill=t.get("skill"),
                enabled=t.get("enabled", True),
                timezone=t.get("timezone", timezone),
            )
            if task_cfg.enabled and task_cfg.prompt:
                self._task_configs.append(task_cfg)

        logger.info(
            "Loaded %d scheduled task(s) from config (enabled=%s)",
            len(self._task_configs), enabled,
        )

    def _init_scheduler(self) -> None:
        """初始化 APScheduler。"""
        self._scheduler = BackgroundScheduler(timezone=self._task_configs[0].timezone if self._task_configs else "Asia/Shanghai")
        self._scheduler.add_listener(self._on_job_executed, EVENT_JOB_EXECUTED)
        self._scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)

    def start(self) -> None:
        """启动调度器，注册所有任务。"""
        if not self._scheduler:
            if not self._task_configs:
                logger.info("No scheduled tasks to start.")
                return
            self._init_scheduler()

        for task_cfg in self._task_configs:
            self._add_job(task_cfg)

        self._scheduler.start()
        logger.info("Scheduler started with %d job(s).", len(self._job_ids))

    def stop(self) -> None:
        """停止调度器。"""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped.")

    def _add_job(self, task_cfg: SchedulerTaskConfig) -> None:
        """为一个任务注册 APScheduler job。"""
        try:
            trigger = CronTrigger.from_crontab(task_cfg.cron, timezone=task_cfg.timezone)
        except Exception as e:
            logger.error("Invalid cron expression for task '%s': %s", task_cfg.name, e)
            return

        job = self._scheduler.add_job(
            func=self._execute_task,
            trigger=trigger,
            args=[task_cfg],
            id=task_cfg.name,
            replace_existing=True,
            misfire_grace_time=60,
        )
        self._job_ids[task_cfg.name] = job.id
        logger.info(
            "Scheduled task '%s': cron=%s, next_run=%s",
            task_cfg.name, task_cfg.cron, job.next_run_time,
        )

    def _execute_task(self, task_cfg: SchedulerTaskConfig) -> None:
        """
        执行定时任务：
        1. 创建 Agent 实例
        2. 如果配置了 skill，先激活
        3. 执行 prompt
        4. 将结果通过指定 channel 推送
        """
        logger.info("Executing scheduled task: %s", task_cfg.name)
        try:
            from phoenix_agent.core.agent import Agent

            agent = Agent(config=self.config)

            # 激活 skill（如配置）
            if task_cfg.skill:
                try:
                    from phoenix_agent.skills.registry import SkillRegistry
                    registry = SkillRegistry.get_instance()
                    registry.discover()
                    skill = registry.get(task_cfg.skill)
                    if skill:
                        agent.use_skill(skill)
                        logger.info("Activated skill '%s' for task '%s'", task_cfg.skill, task_cfg.name)
                except Exception as e:
                    logger.warning("Failed to activate skill '%s': %s", task_cfg.skill, e)

            # 执行 Agent
            response = agent.run(task_cfg.prompt)
            logger.info("Task '%s' completed, response length=%d", task_cfg.name, len(response))

            # 推送结果
            self._send_result(task_cfg, response)

        except Exception as e:
            logger.exception("Error executing scheduled task '%s': %s", task_cfg.name, e)

    def _send_result(self, task_cfg: SchedulerTaskConfig, response: str) -> None:
        """将 Agent 执行结果推送到指定 channel。"""
        channel_name = task_cfg.channel.lower()
        chat_id = task_cfg.chat_id
        max_preview = 500

        message = (
            f"**[定时任务] {task_cfg.name}**\n\n"
            f"{response[:max_preview]}{'…' if len(response) > max_preview else ''}"
        )

        try:
            if channel_name == "dingtalk":
                self._send_dingtalk(chat_id, message)
            elif channel_name == "wechat":
                self._send_wechat(chat_id, message)
            elif channel_name == "telegram":
                self._send_telegram(chat_id, message)
            elif channel_name == "qq":
                self._send_qq(chat_id, message)
            else:
                logger.warning("Unknown channel '%s', skipping push.", channel_name)
        except Exception as e:
            logger.error("Failed to send result for task '%s': %s", task_cfg.name, e)

    def _send_dingtalk(self, chat_id: str, message: str) -> None:
        """通过钉钉推送消息。"""
        from phoenix_agent.channels.dingtalk import DingTalkChannel
        from phoenix_agent.channels.registry import ChannelRegistry

        registry = ChannelRegistry.get_instance()
        channel = registry.get("dingtalk") if registry else None

        if channel and channel.is_enabled():
            try:
                # 优先使用 channel 的 send_text 方法
                if hasattr(channel, "send_text"):
                    channel.send_text(chat_id=chat_id, content=message)
                else:
                    logger.warning("DingTalk channel does not support send_text.")
            except Exception as e:
                logger.error("DingTalk send error: %s", e)
        else:
            logger.warning("DingTalk channel not enabled, cannot send scheduled message.")

    def _send_wechat(self, chat_id: str, message: str) -> None:
        """通过企业微信推送消息。"""
        from phoenix_agent.channels.registry import ChannelRegistry
        registry = ChannelRegistry.get_instance()
        channel = registry.get("wechat") if registry else None

        if channel and channel.is_enabled() and hasattr(channel, "send_text"):
            try:
                channel.send_text(chat_id=chat_id, content=message)
            except Exception as e:
                logger.error("WeChat send error: %s", e)
        else:
            logger.warning("WeChat channel not enabled.")

    def _send_telegram(self, chat_id: str, message: str) -> None:
        """通过 Telegram 推送消息。"""
        from phoenix_agent.channels.registry import ChannelRegistry
        registry = ChannelRegistry.get_instance()
        channel = registry.get("telegram") if registry else None

        if channel and channel.is_enabled() and hasattr(channel, "send_message"):
            try:
                channel.send_message(chat_id=chat_id, text=message)
            except Exception as e:
                logger.error("Telegram send error: %s", e)
        else:
            logger.warning("Telegram channel not enabled.")

    def _send_qq(self, chat_id: str, message: str) -> None:
        """通过 QQ (OneBot) 推送消息。"""
        from phoenix_agent.channels.registry import ChannelRegistry
        registry = ChannelRegistry.get_instance()
        channel = registry.get("qq") if registry else None

        if channel and channel.is_enabled() and hasattr(channel, "send_message"):
            try:
                channel.send_message(user_id=chat_id, message=message)
            except Exception as e:
                logger.error("QQ send error: %s", e)
        else:
            logger.warning("QQ channel not enabled.")

    def _on_job_executed(self, event) -> None:
        """Job 执行成功回调。"""
        logger.debug("Job %s executed successfully.", event.job_id)

    def _on_job_error(self, event) -> None:
        """Job 执行失败回调。"""
        logger.error("Job %s failed: %s", event.job_id, event.exception)

    def add_task(self, task_cfg: SchedulerTaskConfig) -> bool:
        """动态添加一个定时任务。"""
        if not self._scheduler:
            self._init_scheduler()
        self._add_job(task_cfg)
        self._task_configs.append(task_cfg)
        if not self._scheduler.running:
            self.start()
        return True

    def remove_task(self, name: str) -> bool:
        """移除一个定时任务。"""
        if self._scheduler:
            try:
                self._scheduler.remove_job(name)
                self._job_ids.pop(name, None)
                self._task_configs = [t for t in self._task_configs if t.name != name]
                logger.info("Removed scheduled task: %s", name)
                return True
            except Exception as e:
                logger.error("Failed to remove task '%s': %s", name, e)
        return False

    def list_tasks(self) -> List[Dict[str, Any]]:
        """列出所有已注册的任务。"""
        result = []
        for task_cfg in self._task_configs:
            next_run = None
            if self._scheduler and self._scheduler.running:
                job = self._scheduler.get_job(task_cfg.name)
                if job:
                    next_run = job.next_run_time.isoformat() if job.next_run_time else None
            result.append({
                "name": task_cfg.name,
                "cron": task_cfg.cron,
                "channel": task_cfg.channel,
                "chat_id": task_cfg.chat_id,
                "skill": task_cfg.skill,
                "next_run": next_run,
            })
        return result
