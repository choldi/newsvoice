#!/usr/bin/env python3
"""Verificador de operatividad de feeds RSS para el podcast AI Pulse.

Uso:
    python check_feeds.py
    python check_feeds.py --verbose
"""

import argparse
import re
import sys
import urllib.request

FEEDS = {
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "Verge AI": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "Wired AI": "https://www.wired.com/feed/tag/ai/latest/rss",
    "MarkTechPost": "https://www.marktechpost.com/feed/",
    "arXiv stat.ML": "https://export.arxiv.org/rss/stat.ML",
}


def fetch_feed(url: str, timeout: int = 25) -> tuple[int, str, int]:
    """Fetch a feed and return (status_code, content_type, item_count)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) news-engine/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
            content_type = resp.headers.get("Content-Type", "")
            # Count <item> or <entry> tags
            items = len(re.findall(r"<item[ >]", body)) or len(re.findall(r"<entry[ >]", body))
            return status, content_type, items
    except Exception as e:
        return 0, str(e), 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check AI Pulse podcast feeds")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show feed titles")
    args = parser.parse_args()

    print("Verificando feeds del podcast AI Pulse...\n")
    all_ok = True
    total_items = 0

    for name, url in FEEDS.items():
        status, ctype, items = fetch_feed(url)
        ok = status == 200 and items > 0
        all_ok = all_ok and ok
        total_items += items

        status_str = "OK" if ok else "FAIL"
        print(f"  {name:14}  HTTP {status:3}  {ctype:30}  items={items:3}  [{status_str}]")

        if args.verbose and ok:
            # Show first 3 titles
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": "news-engine/1.0"})
                ) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", body, re.S)
                for t in titles[1:4]:
                    print(f"               - {t.strip()[:80]}")
            except Exception:
                pass

    print(f"\nTotal items: {total_items}")
    print(f"Estado general: {'TODOS OK' if all_ok else 'ALGUNOS FALLAN'}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
