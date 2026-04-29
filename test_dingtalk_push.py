"""Quick test: send a text message to DingTalk via OpenAPI.

Usage:
    python test_dingtalk_push.py [conversation_id_or_user_id]

If no argument given, sends to the chat_id from config.yaml.
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from phoenix_agent.channels.dingtalk_openapi import DingTalkOpenAPI


CLIENT_ID = "dingolvkbhspqygzur0o"
CLIENT_SECRET = "LryXgRqxHAm6Doc6EBNpnyJ2wd64xRCa4Dz681iO0kHRTQvkVYSYfCNDlJ3Ta2St"


async def send_test(target: str):
    api = DingTalkOpenAPI(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

    message = "[Phoenix Agent Test]\n\nIf you see this message, DingTalk proactive push is working!"

    if target.startswith("oc_") or target.startswith("cid"):
        print(f"Sending to GROUP: {target}")
        await api.send_text_to_group(
            robot_code=CLIENT_ID,
            conversation_id=target,
            content=message,
        )
    else:
        print(f"Sending to USER: {target}")
        await api.send_text_to_user(
            robot_code=CLIENT_ID,
            user_ids=[target],
            content=message,
        )
    print("[OK] API call succeeded (HTTP 200). Check DingTalk for the message.")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "zhanghsi"
    print(f"Target: {target}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(send_test(target))
    finally:
        loop.close()
