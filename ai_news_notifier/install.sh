#!/bin/bash
# AI 新闻推送系统 - 快速安装脚本

echo "🤖 AI 前沿科技新闻推送系统 - 快速安装"
echo "========================================"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 未找到 Python3，请先安装 Python3"
    exit 1
fi

# 创建虚拟环境
echo "📦 创建虚拟环境..."
python3 -m venv venv
source venv/bin/activate

# 安装依赖
echo "📚 安装依赖..."
pip install -r requirements.txt

# 设置执行权限
chmod +x ai_news_bot.py
chmod +x run_daily.sh

# 创建日志目录
mkdir -p /var/log
touch /var/log/ai_news.log
touch /var/log/ai_news_cron.log

# 测试运行
echo "🧪 测试运行..."
python3 ai_news_bot.py

echo ""
echo "✅ 安装完成！"
echo ""
echo "📋 下一步："
echo "1. 编辑 ai_news_bot.py 配置通知方式（Telegram/文件/微信）"
echo "2. 设置定时任务：crontab -e"
echo "3. 添加：35 16 * * * /bin/bash /your/path/to/ai_news_notifier/run_daily.sh"
echo ""
echo "📖 查看 README.md 获取详细说明"
