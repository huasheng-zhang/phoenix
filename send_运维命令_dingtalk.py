# -*- coding: utf-8 -*-
"""
Send the 运维常用命令.txt file to DingTalk
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
FILE_PATH = r"./运维常用命令.txt"
FILE_NAME = "运维常用命令.txt"

print(f"File to send: {FILE_NAME}")
print(f"File path: {FILE_PATH}")
print(f"File exists: {os.path.exists(FILE_PATH)}")

if not os.path.exists(FILE_PATH):
    print(f"Error: File not found: {FILE_PATH}")
    sys.exit(1)

print(f"File size: {os.path.getsize(FILE_PATH)} bytes")

async def send_file():
    """Send the file via DingTalk."""
    api = DingTalkOpenAPI(CLIENT_ID, CLIENT_SECRET)
    
    try:
        # Get token
        print("\n--- Getting access token ---")
        token = await api._tokens.get_new_token()
        print(f"✅ Token obtained: {token[:16]}...")
        
        # Upload file
        print(f"\n--- Uploading file to DingTalk ---")
        print(f"    File: {FILE_NAME}")
        print(f"    Size: {os.path.getsize(FILE_PATH)} bytes")
        
        media_id = await api.upload_file(
            file_path=FILE_PATH,
            file_name=FILE_NAME,
            file_type="text/plain"
        )
        print(f"✅ File uploaded successfully!")
        print(f"   media_id: {media_id}")
        
        # For now, just show the media_id
        # To send to a group or user, you need:
        # - conversation_id (for groups) or user_ids (for individuals)
        # - robot_code (appKey)
        
        print(f"\n{'='*60}")
        print("✅ File uploaded successfully!")
        print(f"{'='*60}")
        print(f"media_id: {media_id}")
        print(f"\nTo send this file, use:")
        print(f"  - send_file_to_group(robot_code, conversation_id, '{media_id}', '{FILE_NAME}')")
        print(f"  - send_file_to_single(robot_code, user_ids, '{media_id}', '{FILE_NAME}')")
        
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
