#!/usr/bin/env python3
"""
koi_interactive_sync.py
-----------------------
A single, interactive CLI utility that:
1. Pulls all inventory items (packages) from Koi v2
2. Pulls all currently allowlisted items from Koi v2
3. Performs an in-memory diff to isolate inventory items missing from the allowlist
4. Displays a numbered list of missing items (with publisher and usage metadata)
5. Prompts the user to select which items to allowlist (supporting single IDs, lists, ranges, or 'all')
6. Executes a safe, chunked batch upload of the selected items

Features:
- Full rate-limiting backoff (HTTP 429)
- Robust parsing of CLI ranges (e.g. "1, 3, 5-10, all")
- Preserves the item version with the largest endpoint footprint during deduplication
"""

import time
import requests
import sys

# Developer Config
BASE_URL = "https://api.prod.koi.security/api/external/v2"
PAGE_SIZE = 500
CHUNK_SIZE = 100
SYNC_USER = "Kris Gabor" # Enter user name, no brackets. Will be displayed in the Notes column of the allowlist page with the text "Added by [user] via API"

# Marketplace filtering (set to empty list [] to pull all items)
MARKETPLACE_FILTER = ["npm", "pypi"]

def make_request_with_retry(method, url, headers, params=None, json_data=None, timeout=60):
    retries = 5
    backoff = 2
    while retries > 0:
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, params=params, timeout=timeout)
            elif method == "POST":
                response = requests.post(url, headers=headers, json=json_data, timeout=timeout)
            
            # Rate limit backoff (30 req/min)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", backoff))
                print(f"    [!] Rate limited (429). Sleeping for {retry_after}s before retry...")
                time.sleep(retry_after)
                backoff *= 2
                retries -= 1
                continue
                
            return response
        except requests.exceptions.RequestException as e:
            print(f"    [!] Connection error (Retries left: {retries}): {e}")
            time.sleep(backoff)
            backoff *= 2
            retries -= 1
            
    print("[X] Max retries exceeded. Exiting.")
    sys.exit(1)

def pull_inventory(headers):
    print("[*] Phase 1: Pulling all inventory items...")
    inventory_items = []
    page = 1
    has_more = True
    target_marketplaces = [m.lower() for m in MARKETPLACE_FILTER]
    
    while has_more:
        url = f"{BASE_URL}/inventory"
        params = {"page": page, "page_size": PAGE_SIZE}
        
        response = make_request_with_retry("GET", url, headers, params=params)
        response.raise_for_status()
        data = response.json()
        
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("items", data.get("data", data.get("results", [])))
            
        if not items:
            break
            
        filtered_count = 0
        for item in items:
            item_market = item.get("marketplace", "")
            item_market_str = item_market.lower() if item_market else ""
            if not target_marketplaces or item_market_str in target_marketplaces:
                inventory_items.append(item)
                filtered_count += 1
                
        print(f"    [+] Page {page}: Fetched {len(items)} items ({filtered_count} matching filter)")
        
        if len(items) < PAGE_SIZE:
            has_more = False
        else:
            if isinstance(data, dict):
                total_pages = data.get("total_pages", data.get("pages", data.get("totalPages")))
                if total_pages is not None and page >= int(total_pages):
                    has_more = False
            if has_more:
                page += 1
                time.sleep(2)
                
    print(f"[+] Total inventory items loaded: {len(inventory_items)}")
    return inventory_items

def pull_allowlist(headers):
    print("[*] Phase 2: Pulling all currently allowlisted items...")
    url = f"{BASE_URL}/policies/allowlist"
    
    response = make_request_with_retry("GET", url, headers)
    response.raise_for_status()
    data = response.json()
    
    allowlist_items = []
    if isinstance(data, list):
        allowlist_items = data
    elif isinstance(data, dict):
        allowlist_items = data.get("items", data.get("data", data.get("results", [])))
        
    print(f"    [+] Successfully loaded {len(allowlist_items)} currently allowlisted items.")
    return allowlist_items

def parse_selection_input(user_input, max_index):
    selected_indices = set()
    user_input = user_input.strip().lower()
    
    if user_input == "all":
        return set(range(1, max_index + 1))
    if user_input in ["none", "", "q", "quit"]:
        return set()
        
    parts = user_input.split(",")
    for part in parts:
        part = part.strip()
        if "-" in part:
            try:
                start_str, end_str = part.split("-")
                start = int(start_str.strip())
                end = int(end_str.strip())
                if 1 <= start <= max_index and 1 <= end <= max_index:
                    # Handle both ascending (1-5) and descending (5-1)
                    step = 1 if start <= end else -1
                    selected_indices.update(range(start, end + step, step))
            except ValueError:
                print(f"[!] Warning: Skipping invalid range segment '{part}'")
        else:
            try:
                val = int(part)
                if 1 <= val <= max_index:
                    selected_indices.add(val)
                else:
                    print(f"[!] Warning: Index {val} out of bounds (Skipped)")
            except ValueError:
                print(f"[!] Warning: Skipping invalid non-integer segment '{part}'")
                
    return selected_indices

def upload_batch(headers, chunk, attempt_batch=True):
    url = f"{BASE_URL}/policies/allowlist"
    payload = {
        "items": [{"item_id": item["item_id"], "marketplace": item["marketplace"], "notes": f"Added by {SYNC_USER} via API"} for item in chunk]
    }
    
    if attempt_batch:
        response = make_request_with_retry("POST", url, headers, json_data=payload)
        
        # 409 Conflict: One or more items in the batch are already allowlisted.
        if response.status_code == 409:
            print("    [!] Conflict (409) detected in batch. Retrying sequentially...")
            return False
        
        if response.status_code in [400, 404, 405]:
            print(f"    [!] Batch upload returned {response.status_code}. Retrying sequentially...")
            return False
            
        response.raise_for_status()
        print(f"    [+] Successfully allowlisted batch of {len(chunk)} items.")
        return True
    else:
        # Sequential Fallback
        success_count = 0
        already_exist_count = 0
        for item in chunk:
            item_payload = {
                "items": [{
                    "item_id": item["item_id"],
                    "marketplace": item["marketplace"],
                    "notes": f"Added by {SYNC_USER} via API"
                }]
            }
            response = make_request_with_retry("POST", url, headers, json_data=item_payload)
            
            if response.status_code == 409:
                print(f"        [~] Item already allowlisted: {item['item_id']}")
                already_exist_count += 1
            else:
                response.raise_for_status()
                success_count += 1
                print(f"        [+] Successfully allowlisted: {item['item_id']}")
                
            time.sleep(2)  # Respect rate limit (30 requests/minute)
            
        print(f"    [+] Segment Summary: {success_count} newly allowlisted, {already_exist_count} already existed.")
        return True

def main():
    api_key = input("Enter your Koi API Key: ").strip()
    if not api_key:
        print("[X] API Key is required.")
        sys.exit(1)
        
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    # 1. Pull Data
    inventory = pull_inventory(headers)
    allowlist = pull_allowlist(headers)
    print("-" * 60)
    
    # 2. Map Allowlist items to lowercase set for fast diffs
    allowlist_keys = set()
    for item in allowlist:
        item_id = item.get("item_id")
        marketplace = item.get("marketplace")
        if item_id and marketplace:
            allowlist_keys.add((item_id.strip().lower(), marketplace.strip().lower()))
            
    # 3. Deduplicate Inventory by (item_id, marketplace)
    # Sort by endpoint_count descending first to preserve the most widely-used version
    try:
        inventory.sort(key=lambda x: int(x.get("endpoint_count", 0)), reverse=True)
    except (ValueError, TypeError):
        pass
        
    unique_inventory = {}
    for item in inventory:
        item_id = item.get("item_id")
        marketplace = item.get("marketplace")
        if item_id and marketplace:
            key = (item_id.strip().lower(), marketplace.strip().lower())
            if key not in unique_inventory:
                unique_inventory[key] = item
                
    # 4. Perform Diff
    missing_items = []
    for key, item in unique_inventory.items():
        if key not in allowlist_keys:
            missing_items.append(item)
            
    # Sort missing alphabetically for clean CLI listing
    missing_items.sort(key=lambda x: x.get("item_id", "").lower())
    
    if not missing_items:
        print("[+] Good news! All unique inventory packages are already present in your allowlist.")
        sys.exit(0)
        
    # 5. Present Diff to User
    print(f"[*] Found {len(missing_items)} unique inventory item(s) MISSING from the allowlist:")
    print("-" * 80)
    print(f"{'No.':<5} | {'Item ID':<35} | {'Marketplace':<12} | {'Publisher':<20} | {'Endpoints':<10}")
    print("-" * 80)
    for idx, item in enumerate(missing_items, start=1):
        item_id = item.get("item_id", "")
        # Shorten display strings if they are too long
        disp_id = item_id[:33] + ".." if len(item_id) > 35 else item_id
        market = item.get("marketplace", "")
        publisher = item.get("publisher_name") or "Unknown"
        disp_pub = publisher[:18] + ".." if len(publisher) > 20 else publisher
        endpoints = item.get("endpoint_count", 0)
        
        print(f"{idx:<5} | {disp_id:<35} | {market:<12} | {disp_pub:<20} | {endpoints:<10}")
    print("-" * 80)
    
    # 6. Interactive Curation Prompt
    print("[*] Selection Instructions:")
    print("    - Enter individual numbers (e.g. '1, 5, 8')")
    print("    - Enter ranges of numbers (e.g. '3-12, 15-20')")
    print("    - Type 'all' to allowlist all missing items")
    print("    - Type 'none' or press Enter to cancel")
    print("-" * 80)
    
    user_input = input("Enter your selections: ").strip()
    selected_indices = parse_selection_input(user_input, len(missing_items))
    
    if not selected_indices:
        print("[X] Action canceled. No items selected for upload.")
        sys.exit(0)
        
    items_to_upload = [missing_items[idx - 1] for idx in sorted(selected_indices)]
    
    print("-" * 60)
    print(f"[!] Confirming: You selected {len(items_to_upload)} item(s) to add to the allowlist:")
    for idx, item in enumerate(items_to_upload[:10], start=1):
        print(f"    - {item.get('item_id')} ({item.get('marketplace')})")
    if len(items_to_upload) > 10:
        print(f"    - ... and {len(items_to_upload) - 10} more items.")
        
    confirm = input("Proceed with uploading these items? (yes/no): ").strip().lower()
    if confirm not in ["yes", "y"]:
        print("[X] Upload aborted.")
        sys.exit(0)
        
    # 7. Execute Chunked Batch Upload
    chunks = [items_to_upload[i:i + CHUNK_SIZE] for i in range(0, len(items_to_upload), CHUNK_SIZE)]
    print(f"\n[*] Uploading {len(items_to_upload)} items in {len(chunks)} chunk(s)...")
    print("-" * 60)
    
    attempt_batch = True
    for idx, chunk in enumerate(chunks, start=1):
        print(f"[*] Processing Chunk {idx}/{len(chunks)} ({len(chunk)} items)...")
        success = attempt_batch and upload_batch(headers, chunk, attempt_batch=True)
        if not success:
            attempt_batch = False
            upload_batch(headers, chunk, attempt_batch=False)
            
        time.sleep(2)
        
    print("-" * 60)
    print("[+] Interactive allowlist synchronization complete!")
    print("[*] Note: Policy compilations on the Koi backend take approximately 10-20 minutes.")

if __name__ == "__main__":
    main()
