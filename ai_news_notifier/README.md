# AI 前沿科技新闻推送系统

## 📋 功能说明
每天下午 16:35 自动抓取全球主流 AI 科技新闻并推送给你

## 🚀 快速开始

### 1. 安装依赖
```bash
cd ai_news_notifier
pip install -r requirements.txt
```

### 2. 配置通知方式

#### 方式一：Telegram 推送（推荐）
1. 在 Telegram 搜索 @BotFather 创建新机器人
2. 获取 Bot Token
3. 获取你的 Chat ID（搜索 @userinfobot）
4. 修改 `ai_news_bot.py` 中的配置：
```python
TELEGRAM_BOT_TOKEN = "你的 Bot Token"
TELEGRAM_CHAT_ID = "你的 Chat ID"
NOTIFY_METHOD = "telegram"
```

#### 方式二：文件保存
默认会将新闻保存到 `/tmp/ai_news_daily.txt`，直接查看文件即可

#### 方式三：微信推送
使用 ServerChan 或类似服务，需要配置相应的 API

### 3. 设置定时任务

#### Linux/Mac 系统
```bash
# 编辑 crontab
crontab -e

# 添加以下行（修改实际路径）：
35 16 * * * /bin/bash /your/path/to/ai_news_notifier/run_daily.sh >> /var/log/ai_news_cron.log 2>&1

# 保存退出
```

#### Windows 系统
使用任务计划程序：
1. 打开"任务计划程序"
2. 创建基本任务
3. 触发器：每天 16:35
4. 操作：启动程序
   - 程序：`python.exe`
   - 参数：`"C:\path\to\ai_news_notifier\ai_news_bot.py"`
   - 起始于：`C:\path\to\ai_news_notifier`

### 4. 测试运行
```bash
python3 ai_news_notifier/ai_news_bot.py
```

## 📁 文件说明
- `ai_news_bot.py` - 主程序脚本
- `run_daily.sh` - 定时任务执行脚本
- `requirements.txt` - Python 依赖
- `crontab_config.txt` - Crontab 配置示例

## 🌐 新闻源
- OpenAI Blog
- Google AI Blog
- MIT Technology Review AI
- Hugging Face Blog
- arXiv AI

## ⚙️ 自定义
- 修改 `NEWS_SOURCES` 添加更多新闻源
- 调整抓取数量（默认每个源 5 条）
- 修改输出格式

## 📝 日志查看
```bash
# 查看执行日志
tail -f /var/log/ai_news.log

# 查看 cron 日志
tail -f /var/log/ai_news_cron.log
```

## 🆘 故障排查
1. 确保网络连接正常
2. 检查 Python 依赖是否安装
3. 确认定时任务是否生效：`crontab -l`
4. 查看日志文件排查错误

## 🎉 开始享受每日 AI 资讯！
