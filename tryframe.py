#!/usr/bin/env python3
"""
Render the Instagram frame for real handles, without starting the bot.

    python tryframe.py kushina.uzk
    python tryframe.py kushina.uzk another.handle a.third.one

Each handle is read from Instagram's public profile page - no login - and
saved as a PNG next to this script in previews/. Open them to see exactly
what the bot will attach under the embed for that person: their own profile
photo, their handle, the verified tick, and their counts.

Handles are read one at a time with a pause between them. Firing them all at
once is the quickest way to get your IP rate limited, and a rate-limited read
comes back as "Could not check" - which is never the same as a ban.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "previews"

try:
    import instagram
    import cards
except Exception as exc:  # pragma: no cover
    print(f"Could not load the bot's own modules: {exc}")
    print("Run this from the folder that contains bot.py.")
    raise SystemExit(1)


async def render(handle: str) -> None:
    snapshot = await instagram.lookup(handle, use_cache=False)

    picture = "own profile photo" if snapshot.avatar else "no photo available"
    detail = ""
    if snapshot.state == instagram.OK:
        detail = (f"  {cards.human_count(snapshot.posts)} posts · "
                  f"{cards.human_count(snapshot.followers)} followers · "
                  f"{'private' if snapshot.private else 'public'}"
                  f"{' · verified' if snapshot.verified else ''}")

    print(f"@{handle}: {snapshot.headline}  ({picture})")
    if detail:
        print(detail)
    if snapshot.note:
        print(f"  {snapshot.note}")

    png = cards.render_profile_card(
        snapshot.username or handle,
        full_name=snapshot.full_name,
        followers=snapshot.followers,
        following=snapshot.following,
        posts=snapshot.posts,
        verified=snapshot.verified,
        private=snapshot.private,
        avatar_bytes=snapshot.avatar,
        state=snapshot.state,
        note=snapshot.note or None,
    )
    if not png:
        print("  Pillow is not installed, so no picture was drawn."
              "  Run: pip install -r requirements.txt")
        return

    OUT_DIR.mkdir(exist_ok=True)
    safe = "".join(ch for ch in handle if ch.isalnum() or ch in "._-") or "handle"
    target = OUT_DIR / f"{safe}.png"
    target.write_bytes(png)
    print(f"  saved {target}")


async def main(handles: list[str]) -> int:
    if not instagram.HTTPX_AVAILABLE:
        print("httpx is not installed. Run: pip install -r requirements.txt")
        return 1

    for index, handle in enumerate(handles):
        if index:
            await asyncio.sleep(2.0)
        await render(handle.strip().lstrip("@"))
    return 0


if __name__ == "__main__":
    names = [arg for arg in sys.argv[1:] if arg.strip()]
    if not names:
        print(__doc__.strip())
        raise SystemExit(0)
    raise SystemExit(asyncio.run(main(names)))
