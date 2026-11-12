import os
import sys
import json
import time
import requests

WEBHOOK_URL = "https://mstr-challenge-v2-production.nevets-steven.workers.dev/telegram/webhook"

def main():
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "7061999032")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    owner_token = os.environ.get("OWNER_ADMIN_TOKEN", "")
    web_sync_token = os.environ.get("MSTR_WEB_SYNC_TOKEN", "")
    
    tokens = {
        "OWNER_ADMIN_TOKEN": owner_token,
        "MSTR_WEB_SYNC_TOKEN": web_sync_token,
        "BOT_TOKEN": bot_token,
    }
    
    update = {
        "update_id": 99999902,
        "message": {
            "message_id": 99999902,
            "date": int(time.time()),
            "chat": {
                "id": int(chat_id),
                "type": "private"
            },
            "from": {
                "id": int(chat_id),
                "is_bot": False,
                "first_name": "Steven"
            },
            "text": "/portfolio"
        }
    }
    
    for name, tok in tokens.items():
        if not tok: continue
        headers = {
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": tok,
            "User-Agent": "TelegramBot"
        }
        print(f"Testing X-Telegram-Bot-Api-Secret-Token with {name}...")
        resp = requests.post(WEBHOOK_URL, headers=headers, json=update, timeout=10)
        print(f"Status: {resp.status_code}, Body: {resp.text}")
        if resp.ok:
            print(f"SUCCESS with {name}!")
            return

if __name__ == "__main__":
    main()
