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
        
    commands_file = Path("tools/repair_commands.json")
    if commands_file.exists():
        print(f"Reading commands from {commands_file}")
        commands = json.loads(commands_file.read_text(encoding="utf-8"))
    else:
        commands_json = os.environ.get("COMMANDS_JSON", "")
        if not commands_json:
            print("ERROR: Neither tools/repair_commands.json nor COMMANDS_JSON is provided!", file=sys.stderr)
            sys.exit(1)
        commands = json.loads(commands_json)
        
    print(f"Loaded {len(commands)} commands to execute.")
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GitHubActions/V2Repair"
    }
    
    for i, item in enumerate(commands, 1):
        cmd = item["command"]
        occurred_at = item.get("occurred_at")
        idempotency_key = item.get("idempotency_key") or f"repair-{uuid.uuid4().hex[:16]}"
        
        payload = {
            "command": cmd,
            "idempotency_key": idempotency_key
        }
        if occurred_at:
            payload["occurred_at"] = occurred_at
            
        print(f"\n[{i}/{len(commands)}] Executing: {cmd} (occurred_at={occurred_at}, idempotency_key={idempotency_key})")
        resp = requests.post(ADMIN_URL, headers=headers, json=payload, timeout=30)
        print(f"Response status: {resp.status_code}")
        print(f"Response body: {resp.text}")
        
        if not resp.ok:
            print(f"ABORTING: Command failed with status {resp.status_code}", file=sys.stderr)
            sys.exit(1)
            
        time.sleep(1)
        
    print("\nAll commands executed successfully!")

if __name__ == "__main__":
    main()
