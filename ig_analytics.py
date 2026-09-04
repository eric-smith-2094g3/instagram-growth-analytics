#!/usr/bin/env python3
"""ig_analytics - track instagram follower trends locally."""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

APP_DIR = Path.home() / ".config" / "ig_analytics"
DB_PATH = APP_DIR / "data.db"
WATCHLIST_PATH = APP_DIR / "watchlist.json"

# Instagram's public profile page embeds shared data in a <script> tag.
# This pattern extracts the JSON payload from the first matching block.
_SHARED_DATA_RE = re.compile(
    r'<script[^>]*>window\._sharedData\s*=\s*(\{.*?\});</script>'
)

# Fallback for newer pages that use inline scripts with different markers.
_ADDITIONAL_DATA_RE = re.compile(
    r'<script[^>]*>window\.__additionalDataLoaded\s*\(\s*[\'"][^\'"]*[\'"]\s*,\s*(\{.*?\})\s*\)\s*;</script>'
)

def _ensure_app_dir():
    APP_DIR.mkdir(parents=True, exist_ok=True)

def _init_db():
    _ensure_app_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handle TEXT NOT NULL,
            username TEXT,
            full_name TEXT,
            biography TEXT,
            followers INTEGER,
            following INTEGER,
            posts INTEGER,
            is_private INTEGER,
            is_verified INTEGER,
            profile_pic_url TEXT,
            fetched_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_handle ON snapshots(handle)"
    )
    conn.commit()
    conn.close()

def _get_watchlist():
    if not WATCHLIST_PATH.exists():
        return []
    with open(WATCHLIST_PATH, "r") as f:
        return json.load(f)

def _save_watchlist(handles):
    _ensure_app_dir()
    with open(WATCHLIST_PATH, "w") as f:
        json.dump(handles, f, indent=2)

def _extract_user_from_html(body: str) -> dict:
    # Try _sharedData first (legacy but still around on some profiles)
    match = _SHARED_DATA_RE.search(body)
    if match:
        data = json.loads(match.group(1))
        return data["entry_data"]["ProfilePage"][0]["graphql"]["user"]

    # Try __additionalDataLoaded (newer layout, July 2024+)
    match = _ADDITIONAL_DATA_RE.search(body)
    if match:
        data = json.loads(match.group(1))
        return data["graphql"]["user"]

    # Last resort: look for any <script> containing "user":
    # This is brittle but catches edge cases.
    fallback = re.search(r'"user"\s*:\s*(\{[^}]*"username"[^}]*\})', body)
    if fallback:
        # Not a full user object, but we can at least grab basics if present.
        # Usually fails; mostly here to give a clearer error.
        pass

    raise RuntimeError("could not locate user data in page")

def _fetch_profile(handle: str) -> dict:
    url = f"https://www.instagram.com/{handle}/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;" "q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
    }

    # Retry with backoff; IG front-end is flaky on cold requests.
    last_exc = None
    for attempt in range(3):
        try:
            with httpx.Client(follow_redirects=True, timeout=30) as client:
                r = client.get(url, headers=headers)
                r.raise_for_status()
                body = r.text
            break
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            raise
    else:
        raise last_exc

    # print(f"body length: {len(body)}")  # debug
    user = _extract_user_from_html(body)

    return {
        "username": user.get("username"),
        "full_name": user.get("full_name"),
        "biography": user.get("biography"),
        "followers": user.get("edge_followed_by", {}).get("count"),
        "following": user.get("edge_follow", {}).get("count"),
        "posts": user.get("edge_owner_to_timeline_media", {}).get("count"),
        "is_private": user.get("is_private"),
        "is_verified": user.get("is_verified"),
        "profile_pic_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
    }

def _store_snapshot(handle: str, profile: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO snapshots
        (handle, username, full_name, biography, followers, following,
         posts, is_private, is_verified, profile_pic_url, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            handle,
            profile.get("username"),
            profile.get("full_name"),
            profile.get("biography"),
            profile.get("followers"),
            profile.get("following"),
            profile.get("posts"),
            int(profile.get("is_private", False)),
            int(profile.get("is_verified", False)),
            profile.get("profile_pic_url"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

def _latest_snapshot(handle: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """
        SELECT handle, username, full_name, biography, followers,
               following, posts, is_private, is_verified,
               profile_pic_url, fetched_at
        FROM snapshots
        WHERE handle = ?
        ORDER BY fetched_at DESC
        LIMIT 1
        """,
        (handle,),
    ).fetchone()
    conn.close()
    if not row:
        return {}
    return {
        "handle": row[0],
        "username": row[1],
        "full_name": row[2],
        "biography": row[3],
        "followers": row[4],
        "following": row[5],
        "posts": row[6],
        "is_private": bool(row[7]),
        "is_verified": bool(row[8]),
        "profile_pic_url": row[9],
        "fetched_at": row[10],
    }

def _format_number(n):
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)

def _show_trend(handle: str, since_days: int = 30):
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    rows = conn.execute(
        """
        SELECT followers, fetched_at
        FROM snapshots
        WHERE handle = ? AND fetched_at > ?
        ORDER BY fetched_at ASC
        """,
        (handle, cutoff),
    ).fetchall()
    conn.close()

    if len(rows) < 2:
        print(f"{handle}: need at least 2 snapshots in last {since_days}d for trend")
        return

    first_followers, first_at = rows[0]
    last_followers, last_at = rows[-1]
    delta = last_followers - first_followers

    print(f"{handle}")
    print(f"  first: {_format_number(first_followers)} ({first_at[:10]})")
    print(f"  last:  {_format_number(last_followers)} ({last_at[:10]})")
    print(f"  delta: {delta:+,}")

def cmd_add(args):
    handles = _get_watchlist()
    handle = args.handle.lstrip("@")
    if handle in handles:
        print(f"@{handle} already in watchlist")
        return 0
    handles.append(handle)
    _save_watchlist(handles)
    print(f"added @{handle}")
    return 0

def cmd_remove(args):
    handles = _get_watchlist()
    handle = args.handle.lstrip("@")
    if handle not in handles:
        print(f"@{handle} not in watchlist")
        return 1
    handles.remove(handle)
    _save_watchlist(handles)
    print(f"removed @{handle}")
    return 0

def cmd_list(_args):
    handles = _get_watchlist()
    if not handles:
        print("no entries, add one with --add-handle <handle>")
        return 0
    for h in handles:
        snap = _latest_snapshot(h)
        if snap:
            print(f"@{h} ({_format_number(snap['followers'])} followers)")
        else:
            print(f"@{h} (no data yet)")
    return 0

def cmd_fetch(_args):
    handles = _get_watchlist()
    if not handles:
        print("no entries, add one with --add-handle <handle>")
        return 0
    for h in handles:
        try:
            profile = _fetch_profile(h)
            _store_snapshot(h, profile)
            print(f"@{h}: fetched {_format_number(profile['followers'])} followers")
        except Exception as exc:
            print(f"@{h}: failed - {exc}")
    return 0

def cmd_trend(args):
    handles = _get_watchlist()
    if not handles:
        print("no entries, add one with --add-handle <handle>")
        return 0
    since = args.since if hasattr(args, "since") else 30
    for h in handles:
        _show_trend(h, since_days=since)
    return 0

def cmd_prune(args):
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    cur = conn.execute("DELETE FROM snapshots WHERE fetched_at < ?", (cutoff,))
    print(f"pruned {cur.rowcount} old snapshots")
    conn.commit()
    conn.close()
    return 0

def main():
    _init_db()

    parser = argparse.ArgumentParser(
        prog="ig_analytics",
        description="track instagram follower trends locally",
        usage="python ig_analytics.py [-h] {add,remove,list,fetch,trend,prune} ...",
    )
    subparsers = parser.add_subparsers(dest="command")

    add_p = subparsers.add_parser("add", help="add handle to watchlist")
    add_p.add_argument("handle", help="instagram handle (with or without @)")

    rem_p = subparsers.add_parser("remove", help="remove handle from watchlist")
    rem_p.add_argument("handle", help="instagram handle (with or without @)")

    subparsers.add_parser("list", help="list watched handles")
    subparsers.add_parser("fetch", help="fetch latest data for all handles")

    trend_p = subparsers.add_parser("trend", help="show follower trends")
    trend_p.add_argument(
        "--since",
        type=int,
        default=30,
        help="number of days to look back (default: 30)",
    )

    prune_p = subparsers.add_parser("prune", help="delete snapshots older than N days")
    prune_p.add_argument("--days", type=int, default=90, help="default 90")

    args = parser.parse_args()

    if args.command is None:
        parser.print_usage()
        sys.exit(2)

    handlers = {
        "add": cmd_add,
        "remove": cmd_remove,
        "list": cmd_list,
        "fetch": cmd_fetch,
        "trend": cmd_trend,
        "prune": cmd_prune,
    }
    sys.exit(handlers[args.command](args) or 0)

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
