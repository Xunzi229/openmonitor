#!/usr/bin/env python3
"""临时：拉取 ldxp/catfk 店铺昵称与最新商品（复用 monitor 请求逻辑）。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import monitor  # noqa: E402

CONFIG_PATH = ROOT / "config.json"


def safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("gbk", errors="replace").decode("gbk"))


def shop_key(platform: str, token: str) -> tuple[str, str]:
    return (platform, token)


def fetch_shop_info(
    platform: str,
    token: str,
    config: dict,
    *,
    page_size: int = 20,
) -> dict:
    cfg = monitor.PLATFORMS[platform]
    shop_url = cfg["shop_url"].format(token=token)
    headers = monitor._browser_headers(platform, shop_url)
    payload = monitor._build_goods_list_payload(
        platform, token, 1, page_size, None
    )
    try:
        items = monitor._fetch_goods_list_page(
            cfg["api_url"], headers, payload, config
        )
    except Exception as e:
        return {
            "platform": platform,
            "token": token,
            "shop_url": shop_url,
            "name": token,
            "product_count_page1": 0,
            "products": [],
            "error": str(e),
        }
    nickname = token
    for item in items:
        nick = (item.get("user") or {}).get("nickname", "").strip()
        if nick:
            nickname = nick
            break
    products = []
    for item in items[:10]:
        extend = item.get("extend") or {}
        products.append(
            {
                "name": item.get("name", ""),
                "price": item.get("price"),
                "stock": extend.get("stock_count", 0),
                "link": item.get("link", ""),
            }
        )
    return {
        "platform": platform,
        "token": token,
        "shop_url": shop_url,
        "name": nickname,
        "product_count_page1": len(items),
        "products": products,
    }


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def upsert_shop(
    config: dict,
    platform: str,
    token: str,
    name: str,
    *,
    prefer_longer_name: bool = True,
) -> str:
    """写入/更新 config shops，返回 action: added|updated|skipped。"""
    key = shop_key(platform, token)
    shops: list[dict] = config.setdefault("shops", [])
    index = next(
        (
            i
            for i, s in enumerate(shops)
            if shop_key(s.get("platform", "ldxp"), s["token"]) == key
        ),
        None,
    )
    entry = {"token": token, "name": name}
    if platform != "ldxp":
        entry["platform"] = platform

    if index is None:
        shops.append(entry)
        return "added"

    existing = shops[index]
    old_name = existing.get("name", token)
    if old_name == name:
        return "skipped"
    if prefer_longer_name and len(old_name) > len(name) and old_name != token:
        return "skipped"
    existing["name"] = name
    if platform != "ldxp":
        existing["platform"] = platform
    return "updated"


def fetch_and_report(
    platform: str,
    entries: list[tuple[str, str | None]],
    config: dict,
) -> list[dict]:
    """entries: [(token, hint_name_or_none), ...]"""
    results: list[dict] = []
    for index, (token, hint) in enumerate(entries):
        if index > 0:
            monitor.sleep_between_shops(config)
        info = fetch_shop_info(platform, token, config)
        if hint and (
            info["name"] == token or not info["name"] or info.get("error")
        ):
            info["name"] = hint
        results.append(info)
        safe_print(f"\n[{platform}] {token} -> {info['name']}")
        safe_print(f"  店铺: {info['shop_url']}")
        if info.get("error"):
            safe_print(f"  抓取失败: {info['error']}")
            continue
        safe_print(f"  首页商品数: {info['product_count_page1']}")
        for p in info["products"][:5]:
            safe_print(f"  - {p['name']} | {p['price']}元 | 库存{p['stock']}")
        if len(info["products"]) > 5:
            safe_print(f"  ... 共展示 {min(len(info['products']), 10)} 条")
    return results
