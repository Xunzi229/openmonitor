#!/usr/bin/env python3
"""临时：拉取 ldxp 新加店铺昵称与最新商品，更新 config.json 名称。"""

from __future__ import annotations

import argparse

from tmp_shop_fetch import (
    fetch_and_report,
    load_config,
    save_config,
    upsert_shop,
)


# 此前从 URL 新加的 ldxp 商户（name 曾为 token 占位）
LDXP_TOKENS = [
    "YY4KVE2T",
    "33X1D2BV",
    "ELO8O03T",
    "doge",
    "jojo",
    "ChatShare",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="拉取 ldxp 店铺并更新 config 名称")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印，不写 config.json",
    )
    parser.add_argument(
        "--token",
        action="append",
        dest="tokens",
        help="额外指定 token，可多次使用",
    )
    args = parser.parse_args()

    tokens = list(LDXP_TOKENS)
    if args.tokens:
        for t in args.tokens:
            if t not in tokens:
                tokens.append(t)

    config = load_config()
    entries = [(token, None) for token in tokens]
    results = fetch_and_report("ldxp", entries, config)

    if args.dry_run:
        print("\n[dry-run] 未写入 config.json")
        return

    stats = {"added": 0, "updated": 0, "skipped": 0}
    for info in results:
        action = upsert_shop(config, "ldxp", info["token"], info["name"])
        stats[action] += 1
        print(f"  config: {action} {info['token']} -> {info['name']}")

    save_config(config)
    print(
        f"\n完成: added={stats['added']} updated={stats['updated']} "
        f"skipped={stats['skipped']}"
    )


if __name__ == "__main__":
    main()
