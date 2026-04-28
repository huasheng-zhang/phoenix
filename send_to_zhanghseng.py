# -*- coding: utf-8 -*-
"""Send 运维常用命令.txt to Zhang Huasheng via DingTalk"""
import asyncio
import sys
import os

sys.path.insert(0, r'D:\Hermes\phoenix_agent')

from phoenix_agent.channels.dingtalk_openapi import DingTalkOpenAPI

async def send_file():
    # 配置信息
    CLIENT_ID = 'dingolvkbhspqygzur0o'
    CLIENT_SECRET = 'LryXgRqxHAm6Doc6EBNpnyJ2wd64xRCa4Dz681iO0kHRTQvkVYSYfCNDlJ3Ta2St'
    
    # 用户信息（从日志中提取）
    USER_ID = 'zhanghsi'
    CONVERSATION_ID = 'cid5z+QGoMinU3eVnavo'
    
    # 文件信息
    FILE_PATH = r'./运维常用命令.txt'
    FILE_NAME = '运维常用命令.txt'
    MEDIA_ID = '@lAjPM3WbpL2s-8HOD7QLps5wt5NV'
    
    print('='*60)
    print('准备发送文件到钉钉')
    print('='*60)
    print(f'文件名：{FILE_NAME}')
    print(f'用户 ID: {USER_ID}')
    print(f'对话 ID: {CONVERSATION_ID}')
    print(f'Media ID: {MEDIA_ID}')
    print('='*60)
    
    api = DingTalkOpenAPI(CLIENT_ID, CLIENT_SECRET)
    
    try:
        print('\n正在发送文件...')
        await api.send_file_to_single(
            robot_code=CLIENT_ID,
            user_ids=[USER_ID],
            media_id=MEDIA_ID,
            file_name=FILE_NAME
        )
        print('\n文件发送成功！')
        print(f'已发送给：张华生 (zhanghsi)')
        print('='*60)
        return True
    except Exception as e:
        print(f'\n❌ 发送失败：{e}')
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    result = asyncio.run(send_file())
    sys.exit(0 if result else 1)
