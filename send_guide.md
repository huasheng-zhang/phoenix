# -*- coding: utf-8 -*-
"""
Send 深圳五一周边游攻略.md to DingTalk
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

# Configuration
from pathlib import Path
import yaml

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
FILE_PATH = r"D:\Hermes\phoenix_agent\深圳五一周边游攻略.md"
FILE_NAME = "深圳五一周边游攻略.md"

print(f"File to send: {FILE_NAME}")
print(f"File path: {FILE_PATH}")
print(f"File exists: {os.path.exists(FILE_PATH)}")

if not os.path.exists(FILE_PATH):
    print(f"Error: File not found: {FILE_PATH}")
    sys.exit(1)

print(f"File size: {os.path.getsize(FILE_PATH)} bytes")

# DingTalk user info from logs
USER_ID = "zhanghsi"
CONVERSATION_ID = "cid5z+QGoMinU3eVnavo"

async def send_file():
    """Send the file via DingTalk."""
    api = DingTalkOpenAPI(CLIENT_ID, CLIENT_SECRET)
    
    try:
        # Upload file
        print("\n--- Uploading file to DingTalk ---")
        print(f"    File: {FILE_NAME}")
        print(f"    Size: {os.path.getsize(FILE_PATH)} bytes")
        
        media_id = await api.upload_file(
            file_path=FILE_PATH,
            file_name=FILE_NAME,
            file_type="text/markdown"
        )
        print(f"✅ File uploaded successfully!")
        print(f"   media_id: {media_id}")
        
        # Send to user
        print(f"\n--- Sending to user ---")
        print(f"    User ID: {USER_ID}")
        print(f"    Conversation ID: {CONVERSATION_ID}")
        
        await api.send_file_to_single(
            robot_code=CLIENT_ID,
            user_ids=[USER_ID],
            media_id=media_id,
            file_name=FILE_NAME
        )
        
        print(f"\n{'='*60}")
        print("✅ File sent successfully to DingTalk!")
        print(f"{'='*60}")
        print(f"File: {FILE_NAME}")
        print(f"Recipient: 张华生 (zhanghsi)")
        print(f"Media ID: {media_id}")
        
        return media_id
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return None

if __name__ == "__main__":
    media_id = asyncio.run(send_file())
    if media_id:
        print(f"\n✅ Upload complete! media_id: {media_id}")
    else:
        print(f"\n❌ Upload failed")
