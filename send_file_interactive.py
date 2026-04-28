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
sys.path.insert(0, r'D:\Hermes\phoenix_agent')

from phoenix_agent.channels.dingtalk_openapi import DingTalkOpenAPI

# Configuration
CLIENT_ID = "dingolvkbhspqygzur0o"
CLIENT_SECRET = "LryXgRqxHAm6Doc6EBNpnyJ2wd64xRCa4Dz681iO0kHRTQvkVYSYfCNDlJ3Ta2St"

# File info (already uploaded)
MEDIA_ID = "@lAjPD1d1HNxO-wHOKsT-PM490lcn"
FILE_NAME = "运维常用命令.txt"

async def send_file():
    """Send file to DingTalk"""
    # Get conversation_id from user
    print("=" * 60)
    print("Send 运维常用命令.txt to DingTalk")
    print("=" * 60)
    print()
    
    # Try to get from environment variable first
    conv_id = os.environ.get("DINGTALK_CONVERSATION_ID")
    
    if not conv_id:
        print("Please enter the DingTalk conversation_id:")
        print("You can find it in your DingTalk message history or logs.")
        print("Format: Usually a long string like '1234567890@chat.dingtalk.com'")
        print()
        conv_id = input("conversation_id: ").strip()
    
    if not conv_id:
        print("❌ Error: conversation_id is required")
        return
    
    api = DingTalkOpenAPI(CLIENT_ID, CLIENT_SECRET)
    
    try:
        print(f"\nSending file: {FILE_NAME}")
        print(f"Media ID: {MEDIA_ID}")
        print(f"Conversation ID: {conv_id}")
        print(f"Robot Code: {CLIENT_ID}")
        print()
        
        # Send file to group
        await api.send_file_to_group(
            robot_code=CLIENT_ID,
            conversation_id=conv_id,
            media_id=MEDIA_ID,
            file_name=FILE_NAME,
            is_image=False
        )
        
        print()
        print("✅ File sent successfully!")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(send_file())
