import os
import sys
import json
import uuid
import time
from pathlib import Path
import requests

ADMIN_URL = "https://mstr-challenge-v2-production.nevets-steven.workers.dev/admin/v2/command"

def main():
    token = os.environ.get("OWNER_ADMIN_TOKEN")
    if not token:
        print("ERROR: OWNER_ADMIN_TOKEN is missing!", file=sys.stderr)
        sys.exit(1)
        
    print(f"Token length: {len(token)}, prefix: {token[:3]}..., suffix: ...{token[-3:]}")
    
    commands_file = Path("tools/repair_commands.json")
    commands = json.loads(commands_file.read_text(encoding="utf-8"))
    
    cmd = commands[0]["command"]
    
    # Try different auth methods:
    methods = [
        ("Bearer Header", {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, {}),
        ("Raw Header", {"Authorization": token, "Content-Type": "application/json"}, {}),
        ("X-Admin-Token Header", {"X-Admin-Token": token, "Content-Type": "application/json"}, {}),
        ("X-Owner-Token Header", {"X-Owner-Token": token, "Content-Type": "application/json"}, {}),
        ("Secret-Token Header", {"X-Telegram-Bot-Api-Secret-Token": token, "Content-Type": "application/json"}, {}),
        ("Payload token", {"Content-Type": "application/json"}, {"token": token}),
        ("Payload admin_token", {"Content-Type": "application/json"}, {"admin_token": token}),
        ("URL query token", {"Content-Type": "application/json"}, {}),
    ]
    
    for name, headers, extra_payload in methods:
        headers["User-Agent"] = "Mozilla/5.0"
        payload = {
            "command": cmd,
            "idempotency_key": f"diag-{uuid.uuid4().hex[:12]}"
        }
        payload.update(extra_payload)
        url = ADMIN_URL
        if name == "URL query token":
            url += f"?token={token}"
            
        print(f"\n--- Testing auth method: {name} ---")
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            print(f"Status: {resp.status_code}, Body: {resp.text}")
            if resp.ok:
                print(f"SUCCESS with {name}!")
                return
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()
