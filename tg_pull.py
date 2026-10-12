# tg_pull.py — GitHub Actions / local runner for Telegram restricted channel pulls
import asyncio
import builtins
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import FloodWaitError, ChannelPrivateError, UsernameNotOccupiedError

# ── force line-buffered output (GitHub Actions log friendly) ──
_real_print = builtins.print
def print(*a, **k):
    k.setdefault("flush", True)
    _real_print(*a, **k)
# ─────────────────────────────────────────────────────────────

# ─── CONFIG (env-injected) ───────────────────────────────
API_ID         = int(os.environ.get("TG_API_ID", "30321244"))
API_HASH       = os.environ.get("TG_API_HASH", "4a877e997290766381a388f081cbefc9")
STRING_SESSION = os.environ["TG_STRING_SESSION"]
SOURCE         = os.environ.get("TG_SOURCE", "https://t.me/PaniKomadu")

STATE          = Path("state/pull_state.json")
STATE.parent.mkdir(exist_ok=True)
DL_DIR         = Path("_downloads"); DL_DIR.mkdir(exist_ok=True)

UPLOAD_TO_SAVED = True
VIDEO_NOTES     = False
LIMIT           = 0

SCAN_LOG_EVERY  = 50                  # print scan progress every N messages
MAX_RUNTIME_SEC = int(5.5 * 3600)     # 5h 30m — exit cleanly before GitHub's 6h kill
COMMIT_EVERY    = 20                  # git push state every N moved videos (0 = disable)
# ─────────────────────────────────────────────────────────

client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"last_id": 0, "done": []}


def save_state(s):
    STATE.write_text(json.dumps(s))


def git_checkpoint(moved):
    """Commit + push state file so a mid-job crash still preserves progress."""
    if not COMMIT_EVERY or moved == 0 or moved % COMMIT_EVERY != 0:
        return
    try:
        subprocess.run(["git", "add", "state/pull_state.json"], check=True)
        subprocess.run(
            ["git", "-c", "user.name=gh-bot",
             "-c", "user.email=gh-bot@users.noreply.github.com",
             "commit", "-m", f"chore: state checkpoint @ moved={moved} [skip ci]"],
            check=True,
        )
        subprocess.run(["git", "push"], check=True)
        print(f"[git] checkpoint committed @ moved={moved}")
    except subprocess.CalledProcessError as e:
        print(f"[git] checkpoint skipped: {e}")


def _is_round_video(doc):
    for attr in getattr(doc, "attributes", []):
        if type(attr).__name__ == "VideoSize" and getattr(attr, "is_round", False):
            return True
    return False


def is_plain_video(msg):
    if not isinstance(msg.media, MessageMediaDocument):
        return False
    doc = msg.media.document
    if not getattr(doc, "mime_type", "").startswith("video/"):
        return False
    if not VIDEO_NOTES and _is_round_video(doc):
        return False
    return True


async def ensure_joined(entity):
    try:
        await client(JoinChannelRequest(entity))
        print("[+] joined")
    except Exception as e:
        print(f"[i] join skipped: {e}")


async def main():
    await client.start()
    state = load_state()
    start_ts = time.time()

    try:
        entity = await client.get_entity(SOURCE)
    except (UsernameNotOccupiedError, ChannelPrivateError) as e:
        print(f"[!] cannot resolve source: {e}")
        return

    print(f"[+] source: {getattr(entity, 'title', entity.id)}")
    await ensure_joined(entity)

    scanned = 0
    found   = 0
    moved   = 0
    last_seen_id = state["last_id"]

    print(f"[~] resume point: msg_id > {state['last_id']}  |  already done: {len(state['done'])}")
    print(f"[~] runtime cap: {MAX_RUNTIME_SEC/3600:.1f}h  |  checkpoint every {COMMIT_EVERY} videos")
    print(f"[~] scanning...")

    async for msg in client.iter_messages(entity, min_id=state["last_id"], reverse=True):
        # runtime guard — exit cleanly before GitHub's 6h kill
        if time.time() - start_ts > MAX_RUNTIME_SEC:
            print(f"[~] runtime limit reached ({MAX_RUNTIME_SEC/3600:.1f}h) — exiting cleanly")
            break

        scanned += 1
        last_seen_id = msg.id

        if scanned % SCAN_LOG_EVERY == 0:
            elapsed = int(time.time() - start_ts)
            print(f"[scan] seen {scanned}  |  found: {found}  |  moved: {moved}  |  last_id: {msg.id}  |  elapsed: {elapsed}s")

        if not is_plain_video(msg):
            continue

        found += 1
        print(f"[+] video found  msg {msg.id}  (#{found} this run)")

        if msg.id in state["done"]:
            print(f"[=] {msg.id} already in state — skip")
            continue

        if getattr(msg.media, "ttl_seconds", None):
            print(f"[skip] {msg.id} ttl media")
            continue

        try:
            path = await client.download_media(msg, file=DL_DIR)
            print(f"[↓] {msg.id}  →  {path}")

            if UPLOAD_TO_SAVED:
                await client.send_file(
                    "me",
                    path,
                    caption=None,
                    supports_streaming=True,
                )
                print(f"[↑] {msg.id}  →  Saved Messages")

            state["done"].append(msg.id)
            state["last_id"] = msg.id
            save_state(state)

            if UPLOAD_TO_SAVED:
                try: os.remove(path)
                except OSError: pass

            moved += 1
            git_checkpoint(moved)

            if LIMIT and moved >= LIMIT:
                break

        except FloodWaitError as e:
            print(f"[!] flood {e.seconds}s — sleeping")
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            print(f"[!] {msg.id}: {e}")

    # final state save (in case loop exited before any move)
    save_state(state)

    elapsed = int(time.time() - start_ts)
    print(f"[✓] done")
    print(f"    scanned: {scanned}")
    print(f"    videos found: {found}")
    print(f"    moved to Saved Messages: {moved}")
    print(f"    last_id: {last_seen_id}")
    print(f"    elapsed: {elapsed}s")


with client:
    client.loop.run_until_complete(main())
