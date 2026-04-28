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

热加载支持：
    PhoenixScheduler 单例在 server 启动时创建，之后 add_task / remove_task
    操作会立即生效并同步写入 config.yaml，无需重启服务。
"""
import logging
import os
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


# ---------------------------------------------------------------------------
# Singleton instance (set by server.py; tools/builtin.py access it via getter)
# ---------------------------------------------------------------------------

_scheduler_instance: Optional["PhoenixScheduler"] = None


def get_scheduler() -> Optional["PhoenixScheduler"]:
    """Return the running scheduler singleton (set by server.py)."""
    return _scheduler_instance


# ---------------------------------------------------------------------------
# PhoenixScheduler
# ---------------------------------------------------------------------------

class PhoenixScheduler:
    """
    Phoenix Agent 定时任务调度器。

    在 Agent 启动时自动加载 config.yaml 中的 scheduled_tasks，
    使用 APScheduler 按 cron 表达式执行任务。
    任务触发时调用 Agent 生成回复，并通过对应 channel 推送结果。

    add_task / remove_task 操作会立即生效并同步写入 config.yaml，
    无需重启服务。
    """

    def __init__(self, config: Optional[Config] = None):
        global _scheduler_instance
        self.config = config or get_config()
        self._scheduler: Optional[BackgroundScheduler] = None
        self._task_configs: List[SchedulerTaskConfig] = []
        self._job_ids: Dict[str, str] = {}  # name -> job_id

        # 从 config 加载任务
        self._load_tasks_from_config()

        if self._task_configs:
            self._init_scheduler()
            self.start()

        # Always register as singleton — even when scheduler is initially
        # disabled — so Agent tools can call add/remove and hot-activate it.
        _scheduler_instance = self

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
            # Support both dict and SchedulerTaskConfig objects
            if isinstance(t, SchedulerTaskConfig):
                task_cfg = t
            elif isinstance(t, dict):
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
            else:
                continue
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
        next_run = getattr(job, "next_run_time", None)
        logger.info(
            "Scheduled task '%s': cron=%s, next_run=%s",
            task_cfg.name, task_cfg.cron, next_run,
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
        """通过钉钉推送消息（主动推送，不依赖用户消息）。"""
        from phoenix_agent.channels.registry import ChannelRegistry

        registry = ChannelRegistry.get_instance()
        channel = registry.get("dingtalk") if registry else None

        if not channel or not channel.is_enabled():
            logger.warning("DingTalk channel not enabled, cannot send scheduled message.")
            return

        # 获取 OpenAPI 客户端（用于主动推送）
        openapi = getattr(channel, "_openapi", None)
        if not openapi:
            logger.error("DingTalk OpenAPI not initialized (stream mode needs client_id/client_secret).")
            return

        # 获取 robot_code（用于 API 调用）
        robot_code = getattr(channel, "_client_id", None) or getattr(channel, "_app_key", None)
        if not robot_code:
            logger.error("DingTalk robot_code not found.")
            return

        # 确定推送目标
        target_conversation_id = getattr(channel, "_last_conversation_id", None) or ""
        target_sender_id = getattr(channel, "_last_sender_id", None) or ""

        # 如果 config.yaml 中没有配置 chat_id，自动使用最近活跃会话
        effective_chat_id = chat_id.strip() if chat_id else ""

        if not effective_chat_id:
            if target_conversation_id:
                effective_chat_id = target_conversation_id
                logger.info("[scheduler] No chat_id configured, using last conversation: %s",
                            target_conversation_id[:20])
            elif target_sender_id:
                effective_chat_id = target_sender_id
                logger.info("[scheduler] No chat_id configured, using last sender: %s",
                            target_sender_id)
            else:
                logger.warning("No chat_id configured and no recent conversation found. "
                               "Please send a message to the bot first, or set chat_id in config.yaml.")
                return

        try:
            import asyncio
            loop = asyncio.get_event_loop()
            # 判断 chat_id 格式：群会话ID通常以 "oc_" 开头
            if effective_chat_id.startswith("oc_"):
                # 群聊消息
                loop.run_until_complete(
                    openapi.send_text_to_group(
                        robot_code=robot_code,
                        conversation_id=effective_chat_id,
                        content=message,
                    )
                )
            else:
                # 单聊消息，chat_id 是用户 ID 列表（逗号分隔）或单个用户ID
                user_ids = [uid.strip() for uid in effective_chat_id.split(",") if uid.strip()]
                if not user_ids:
                    logger.warning("No valid user IDs in chat_id: %r", effective_chat_id)
                    return
                loop.run_until_complete(
                    openapi.send_text_to_user(
                        robot_code=robot_code,
                        user_ids=user_ids,
                        content=message,
                    )
                )
            logger.info("[scheduler] DingTalk push success for chat_id: %s", effective_chat_id[:20])
        except Exception as e:
            logger.error("DingTalk send error: %s", e)

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

    def _config_path(self) -> Optional[str]:
        """Return the config file path used by the running config."""
        path = getattr(self.config, "config_file", None)
        if path:
            return str(path)
        # Fallback: derive from phoenix_home
        try:
            from phoenix_agent.core.config import _get_phoenix_home
            return str(_get_phoenix_home() / "config.yaml")
        except Exception:
            return None

    def _persist_tasks(self) -> None:
        """将当前 _task_configs 写回 config.yaml 的 scheduler 段。"""
        import yaml
        path_str = self._config_path()
        if not path_str:
            logger.warning("Cannot determine config path — skipping persist.")
            return
        path = __import__("pathlib").Path(path_str)
        if not path.exists():
            logger.warning("Config file does not exist — skipping persist.")
            return
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            raw_scheduler = data.get("scheduler", {})
            raw_scheduler["enabled"] = True
            raw_scheduler["tasks"] = [
                {
                    "name": t.name,
                    "cron": t.cron,
                    "prompt": t.prompt,
                    "channel": t.channel,
                    "chat_id": t.chat_id,
                    "skill": t.skill,
                    "enabled": t.enabled,
                    "timezone": t.timezone,
                }
                for t in self._task_configs
            ]
            data["scheduler"] = raw_scheduler
            path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            logger.info("Persisted %d task(s) to config.yaml.", len(self._task_configs))
        except Exception as e:
            logger.error("Failed to persist tasks to config.yaml: %s", e)

    def reload(self) -> None:
        """
        热加载：重新从 config.yaml 读取任务，对比当前运行的 jobs，
        添加缺失的、移除多余的，但不重复添加已存在的。

        适用于 config.yaml 被外部修改后同步调度器。
        """
        logger.info("Reloading scheduler from config.yaml...")
        # 读取最新配置（不使用缓存的 config 对象，直接读文件）
        import yaml
        path_str = self._config_path()
        if path_str:
            try:
                data = yaml.safe_load(__import__("pathlib").Path(path_str).read_text(encoding="utf-8")) or {}
                raw = data.get("scheduler", {})
                if not raw.get("enabled", False):
                    logger.info("Scheduler is disabled in config.yaml.")
                    return
                task_dicts = raw.get("tasks", [])
            except Exception as e:
                logger.warning("Failed to read config for reload: %s", e)
                return
        else:
            return

        existing_names = {t.name for t in self._task_configs}

        for t in task_dicts:
            if isinstance(t, dict) and t.get("enabled", True):
                task_cfg = SchedulerTaskConfig(
                    name=t.get("name", f"task-{uuid.uuid4().hex[:8]}"),
                    cron=t.get("cron", "0 9 * * *"),
                    prompt=t.get("prompt", ""),
                    channel=t.get("channel", "dingtalk"),
                    chat_id=t.get("chat_id", ""),
                    skill=t.get("skill"),
                    enabled=t.get("enabled", True),
                    timezone=t.get("timezone", "Asia/Shanghai"),
                )
                if task_cfg.name in existing_names:
                    continue  # 已存在，跳过
                if not task_cfg.prompt:
                    continue
                self._add_job(task_cfg)
                self._task_configs.append(task_cfg)
                logger.info("Hot-added task '%s' from config.yaml.", task_cfg.name)

        # 移除 config.yaml 中已删除的任务
        latest_names = {t.get("name") for t in task_dicts if isinstance(t, dict)}
        for task_cfg in list(self._task_configs):
            if task_cfg.name not in latest_names:
                if self._scheduler and self._scheduler.running:
                    try:
                        self._scheduler.remove_job(task_cfg.name)
                    except Exception:
                        pass
                self._task_configs.remove(task_cfg)
                self._job_ids.pop(task_cfg.name, None)
                logger.info("Hot-removed task '%s' (not in config.yaml).", task_cfg.name)

        logger.info("Reload complete. Running %d task(s).", len(self._task_configs))

    def add_task(self, task_cfg: SchedulerTaskConfig) -> bool:
        """动态添加一个定时任务，立即生效并持久化到 config.yaml。"""
        if not self._scheduler:
            self._init_scheduler()
        if not self._scheduler.running:
            self.start()
        self._add_job(task_cfg)
        self._task_configs.append(task_cfg)
        self._persist_tasks()
        return True

    def remove_task(self, name: str) -> bool:
        """移除一个定时任务，立即生效并同步更新 config.yaml。"""
        if self._scheduler:
            try:
                self._scheduler.remove_job(name)
            except Exception:
                pass
        self._job_ids.pop(name, None)
        self._task_configs = [t for t in self._task_configs if t.name != name]
        self._persist_tasks()
        logger.info("Removed scheduled task: %s", name)
        return True

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
