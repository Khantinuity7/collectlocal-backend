#!/usr/bin/env python3
"""
One-click GitHub push for CollectLocal Backend files.
Uses GitHub Contents API to create/update files directly.

Usage:
    1. Generate a GitHub Personal Access Token at:
       https://github.com/settings/tokens/new
       - Check "repo" scope (Full control of private repositories)
       - Click "Generate token" and copy it

    2. Run this script:
       python3 push_files.py YOUR_TOKEN_HERE

    Or set as environment variable:
       export GITHUB_TOKEN=YOUR_TOKEN_HERE
       python3 push_files.py
"""

import sys
import os
import base64
import json
import requests

REPO = "Khantinuity7/collectlocal-backend"
BRANCH = "main"
API_BASE = f"https://api.github.com/repos/{REPO}/contents"

# Files to push (path relative to this script)
FILES_TO_PUSH = [
    {
        "local": "scraper.py",
        "remote": "scraper.py",
        "message": "Switch market pricing to TCGTracking Open API\n\n- Replace pokemontcg.io proxy with TCGTracking API (free, no auth, 55 games)\n- SKU-level pricing by condition (NM/LP/MP/HP/DMG), variant, and language\n- TCGPlayer + Manapool price data, updated daily\n- lookup_tcgtrack_price() with set search, product matching, and caching\n- lookup_combined_market_price() now uses TCGTracking as primary source\n- Fallback chain: TCGTracking → pokemontcg.io → eBay lowest BIN",
    },
    {
        "local": "supabase/functions/tcgplayer-price/index.ts",
        "remote": "supabase/functions/tcgplayer-price/index.ts",
        "message": "Update TCGPlayer price edge function with card number matching\n\n- Accept optional cardNumber param for exact product matching\n- Match by card number first, then name, then fuzzy\n- Prevents wrong variant being returned (e.g. 125/197 vs 215/197)\n- Store products by number + name for fast lookups\n- On-demand market price lookups with SKU-level condition pricing",
    },
]


def get_token():
    """Get GitHub token from argument, environment, or .env file."""
    if len(sys.argv) > 1:
        return sys.argv[1]

    # Check environment variable
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token

    # Try reading from .env file in same directory
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GITHUB_TOKEN=") and not line.startswith("#"):
                    token = line.split("=", 1)[1].strip()
                    if token and token != "ghp_your_token_here":
                        print(f"  🔑 Using token from .env file")
                        return token

    print("\n❌ No GitHub token provided!")
    print("\nUsage: python3 push_files.py YOUR_GITHUB_TOKEN")
    print("\nOr add GITHUB_TOKEN=ghp_xxx to your .env file")
    print("\nTo create a token:")
    print("  1. Go to https://github.com/settings/tokens/new")
    print('  2. Check "repo" scope')
    print("  3. Generate and copy the token")
    sys.exit(1)


def push_file(token, local_path, remote_path, commit_message):
    """Push a single file to GitHub using the Contents API."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Read local file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    full_path = os.path.join(script_dir, local_path)

    if not os.path.exists(full_path):
        print(f"  ❌ File not found: {full_path}")
        return False

    with open(full_path, "rb") as f:
        content = base64.b64encode(f.read()).decode("utf-8")

    # Check if file already exists (need SHA for updates)
    url = f"{API_BASE}/{remote_path}?ref={BRANCH}"
    resp = requests.get(url, headers=headers)

    payload = {
        "message": commit_message,
        "content": content,
        "branch": BRANCH,
    }

    if resp.status_code == 200:
        # File exists — include SHA for update
        sha = resp.json()["sha"]
        payload["sha"] = sha
        print(f"  📝 Updating existing file (SHA: {sha[:7]})")
    else:
        print(f"  🆕 Creating new file")

    # Push the file
    url = f"{API_BASE}/{remote_path}"
    resp = requests.put(url, headers=headers, json=payload)

    if resp.status_code in (200, 201):
        commit_sha = resp.json()["commit"]["sha"][:7]
        print(f"  ✅ Success! Commit: {commit_sha}")
        return True
    else:
        print(f"  ❌ Failed: {resp.status_code} — {resp.json().get('message', resp.text[:200])}")
        return False


def main():
    token = get_token()
    print(f"\n🚀 Pushing files to github.com/{REPO} (branch: {BRANCH})\n")

    success_count = 0
    for file_info in FILES_TO_PUSH:
        print(f"📦 {file_info['local']} → {file_info['remote']}")
        if push_file(token, file_info["local"], file_info["remote"], file_info["message"]):
            success_count += 1
        print()

    print(f"{'='*50}")
    print(f"Done! {success_count}/{len(FILES_TO_PUSH)} files pushed successfully.")
    if success_count == len(FILES_TO_PUSH):
        print(f"🎉 View your repo: https://github.com/{REPO}")
    print()


if __name__ == "__main__":
    main()
