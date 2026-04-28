#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 前沿科技新闻推送脚本
每天下午 16:35 自动抓取并推送当日 AI 新闻
"""

import json
import os
from datetime import datetime
import requests
from urllib.parse import quote

# 配置
NEWS_SOURCES = [
    {
        "name": "OpenAI Blog",
        "url": "https://openai.com/blog/rss.xml"
    },
    {
        "name": "Google AI Blog",
        "url": "https://ai.google/blog/rss.xml"
    },
    {
        "name": "MIT Technology Review AI",
        "url": "https://www.technologyreview.com/feed/topic/artificial-intelligence/"
    },
    {
        "name": "Hugging Face Blog",
        "url": "https://huggingface.co/blog/rss.xml"
    },
    {
        "name": "arXiv AI",
        "url": "http://export.arxiv.org/api/query?search_query=cat:cs.AI&max_results=10"
    }
]

# 通知方式配置（选择你喜欢的）
NOTIFY_METHOD = "file"  # 可选："file", "telegram", "wechat", "email"

# Telegram 配置（如果使用）
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

# 文件输出路径
OUTPUT_FILE = "/tmp/ai_news_daily.txt"


def fetch_rss_feeds():
    """抓取多个 RSS 源的新闻"""
    news_items = []
    
    for source in NEWS_SOURCES:
        try:
            response = requests.get(source["url"], timeout=10)
            if response.status_code == 200:
                from feedparser import parse
                feed = parse(response.text)
                
                for entry in feed.entries[:5]:  # 每个源取前 5 条
                    news_items.append({
                        "title": entry.get("title", "无标题"),
                        "link": entry.get("link", ""),
                        "published": entry.get("published", ""),
                        "summary": entry.get("summary", ""),
                        "source": source["name"]
                    })
        except Exception as e:
            print(f"抓取 {source['name']} 失败：{e}")
    
    # 按时间排序
    news_items.sort(key=lambda x: x.get("published", ""), reverse=True)
    return news_items


def format_news(news_items):
    """格式化新闻内容"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    content = f"""
{'='*60}
🤖 AI 前沿科技日报 - {timestamp}
{'='*60}

"""
    
    for i, item in enumerate(news_items[:15], 1):  # 只显示前 15 条
        content += f"""
【{i}. {item['source']}】
📰 {item['title']}
🔗 {item['link']}
{'─'*50}
"""
    
    content += f"""
{'='*60}
每日 AI 新闻推送 - 保持对前沿科技的关注！
{'='*60}
"""
    
    return content


def send_telegram_notification(content):
    """发送 Telegram 通知"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram 配置未设置，跳过通知")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": content[:4096],  # Telegram 消息限制 4096 字符
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=data, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Telegram 发送失败：{e}")
        return False


def send_wechat_notification(content):
    """发送微信通知（使用 ServerChan 或类似服务）"""
    # 这里需要配置你的微信推送服务
    print("微信通知功能待配置")
    return False


def send_email_notification(content):
    """发送电子邮件通知"""
    # 需要配置 SMTP 服务
    print("邮件通知功能待配置")
    return False


def save_to_file(content):
    """保存新闻到文件"""
    try:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"新闻已保存到：{OUTPUT_FILE}")
        return True
    except Exception as e:
        print(f"保存文件失败：{e}")
        return False


def main():
    """主函数"""
    print("开始抓取 AI 前沿科技新闻...")
    
    # 抓取新闻
    news_items = fetch_rss_feeds()
    
    if not news_items:
        print("未获取到新闻")
        return
    
    # 格式化内容
    content = format_news(news_items)
    
    # 根据配置发送通知
    if NOTIFY_METHOD == "telegram" and TELEGRAM_BOT_TOKEN:
        success = send_telegram_notification(content)
        print(f"Telegram 通知发送：{'成功' if success else '失败'}")
    elif NOTIFY_METHOD == "file":
        save_to_file(content)
        print("新闻已保存，请查看文件")
    
    # 打印到终端
    print("\n" + content)


if __name__ == "__main__":
    main()
