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

# Current conversation_id (from the conversation context)
# In stream mode, this would be passed in the incoming message
# For now, we'll use a placeholder - you need to replace this with the actual conversation_id
CONVERSATION_ID = "YOUR_CONVERSATION_ID_HERE"  # Replace with actual conversation_id

async def send_file():
    """Send file to DingTalk"""
    if CONVERSATION_ID == "YOUR_CONVERSATION_ID_HERE":
        print("❌ Error: Please replace CONVERSATION_ID with the actual conversation_id")
        print("   You can find it in your DingTalk message history or from the stream handler")
        return
    
    api = DingTalkOpenAPI(CLIENT_ID, CLIENT_SECRET)
    
    try:
        print(f"Sending file: {FILE_NAME}")
        print(f"Media ID: {MEDIA_ID}")
        print(f"Conversation ID: {CONVERSATION_ID}")
        print(f"Robot Code: {CLIENT_ID}")
        print()
        
        # Send file to group
        await api.send_file_to_group(
            robot_code=CLIENT_ID,
            conversation_id=CONVERSATION_ID,
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
