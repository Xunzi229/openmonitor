#!/usr/bin/env python3
"""从 data/site.json 提取 ldxp/catfk 店铺写入 config.json。"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_FILE = ROOT / "data" / "site.json"
CONFIG_FILE = ROOT / "config.json"

PATTERNS = {
    "ldxp": re.compile(r"https?://pay\.ldxp\.cn/shop/([^/?#]+)", re.I),
    "catfk": re.compile(r"https?://catfk\.com/shop/([^/?#]+)", re.I),
}


def shop_key(platform: str, token: str) -> tuple[str, str]:
    return (platform, token)


def extract_shops(site: dict) -> dict[tuple[str, str], dict]:
    extracted: dict[tuple[str, str], dict] = {}
    for row in site.get("rows", []):
        site_info = row.get("site") or {}
        url = site_info.get("url", "")
        name = (site_info.get("name") or "").strip()
        for platform, pattern in PATTERNS.items():
            match = pattern.search(url)
            if not match:
                continue
            token = match.group(1)
            key = shop_key(platform, token)
            shop = {"token": token, "name": name or token}
            if platform != "ldxp":
                shop["platform"] = platform
            prev = extracted.get(key)
            if not prev or (name and len(name) >= len(prev.get("name", ""))):
                extracted[key] = shop
    return extracted


def merge_config(config: dict, extracted: dict[tuple[str, str], dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for shop in config.get("shops", []):
        platform = shop.get("platform", "ldxp")
        merged[shop_key(platform, shop["token"])] = dict(shop)
    for key, shop in extracted.items():
        if key not in merged:
            merged[key] = shop

    def sort_key(item: dict) -> tuple:
        platform = item.get("platform", "ldxp")
        return (0 if platform == "ldxp" else 1, item.get("name", item["token"]).lower())

    return sorted(merged.values(), key=sort_key)


def main() -> None:
    site = json.loads(SITE_FILE.read_text(encoding="utf-8"))
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    extracted = extract_shops(site)
    config["shops"] = merge_config(config, extracted)
    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    ldxp_n = sum(1 for s in config["shops"] if s.get("platform", "ldxp") == "ldxp")
    catfk_n = sum(1 for s in config["shops"] if s.get("platform") == "catfk")
    print(
        f"done: total={len(config['shops'])}, "
        f"from_site={len(extracted)}, ldxp={ldxp_n}, catfk={catfk_n}"
    )


if __name__ == "__main__":
    main()
