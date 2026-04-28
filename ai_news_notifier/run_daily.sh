#!/bin/bash
# AI 新闻推送定时任务脚本
# 每天下午 16:35 执行

cd /path/to/ai_news_notifier

# 激活虚拟环境（如果使用）
# source venv/bin/activate

# 运行新闻抓取脚本
python3 ai_news_bot.py

# 记录执行日志
echo "$(date '+%Y-%m-%d %H:%M:%S') - AI 新闻推送任务执行完成" >> /var/log/ai_news.log
