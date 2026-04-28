# -*- coding: utf-8 -*-
"""
Send the 运维常用命令.txt file to the current user via DingTalk
"""
import asyncio
import sys
import os
import io

# Reconfigure stdout/stderr to use UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors='replace')

# Add phoenix_agent to path
sys.path.insert(0, r'D:\\Hermes\\phoenix_agent')

from phoenix_agent.channels.dingtalk_openapi import DingTalkOpenAPI
from pathlib import Path
import yaml

# Configuration
config_file = Path.home() / ".phoenix" / "config.yaml"
if config_file.exists():
    with open(config_file, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    dt_cfg = config.get("channels", {}).get("dingtalk", {})
    CLIENT_ID = dt_cfg.get("client_id") or dt_cfg.get("app_key")
    CLIENT_SECRET = dt_cfg.get("client_secret") or dt_cfg.get("app_secret")
else:
    # Fallback hardcoded credentials
    CLIENT_ID = "dingolvkbhspqygzur0o"
    CLIENT_SECRET = "LryXgRqxHAm6Doc6EBNpnyJ2wd64xRCa4Dz681iO0kHRTQvkVYSYfCNDlJ3Ta2St"

# File to send
FILE_PATH = r"./运维常用命令.txt"
FILE_NAME = "运维常用命令.txt"

print(f"File to send: {FILE_NAME}")
print(f"File path: {FILE_PATH}")
print(f"File exists: {os.path.exists(FILE_PATH)}")

if not os.path.exists(FILE_PATH):
    print(f"Error: File not found: {FILE_PATH}")
    sys.exit(1)

print(f"File size: {os.path.getsize(FILE_PATH)} bytes")

# Note: In a real scenario, you would need to get the user_id from the incoming message
# For now, I'll show you what's needed
print("\n" + "="*60)
print("To send this file, you need:")
print("="*60)
print("1. Upload the file first (already done, media_id: @lAjPM3WbpL2s-8HOD7QLps5wt5NV)")
print("2. Get your DingTalk user ID from the conversation")
print("3. Use send_file_to_single(robot_code, [user_id], media_id, file_name)")
print("="*60)

print("\nFile content preview (first 500 characters):")
with open(FILE_PATH, 'r', encoding='utf-8') as f:
    content = f.read(500)
    print(content)

print("\n..." + str(os.path.getsize(FILE_PATH) - 500) + " more bytes")
