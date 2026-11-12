import os
import sys
import json
import time
import requests

WEBHOOK_URL = "https://mstr-challenge-v2-production.nevets-steven.workers.dev/telegram/webhook"

def main():
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "7061999032")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    
    print(f"Chat ID: {chat_id}, Bot token length: {len(bot_token)}")
    
    # Try sending a Telegram update to the webhook
    update = {
        "update_id": 99999901,
        "message": {
            "message_id": 99999901,
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
    
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "TelegramBot"
    }
    
    print("\n--- Sending /portfolio test update to webhook ---")
    resp = requests.post(WEBHOOK_URL, headers=headers, json=update, timeout=10)
    print(f"Status: {resp.status_code}, Body: {resp.text}")

if __name__ == "__main__":
    main()
