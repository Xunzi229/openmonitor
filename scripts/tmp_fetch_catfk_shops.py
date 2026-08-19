#!/usr/bin/env python3
"""临时：拉取 catfk 店铺信息并写入 config.json（去重）。"""

from __future__ import annotations

import argparse

from tmp_shop_fetch import (
    fetch_and_report,
    load_config,
    save_config,
    upsert_shop,
)


# token, 备注名称（API 无昵称时使用）
CATFK_SHOPS = [
    ("agi", "AGI源头批发中心 - GPT普号、Claude普号、Plus/Pro等"),
    ("ithte", "黑鹅小铺"),
    ("I7DPO2IK", "风里的AI小铺"),
    ("rick", "Rick的AI小铺 - GPT成品号/日抛号"),
    ("4WXNQBCP", "视频中提到的Plus相关商户"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="拉取 catfk 店铺并更新 config")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印，不写 config.json",
    )
    args = parser.parse_args()

    config = load_config()
    results = fetch_and_report("catfk", CATFK_SHOPS, config)

    if args.dry_run:
        print("\n[dry-run] 未写入 config.json")
        return

    stats = {"added": 0, "updated": 0, "skipped": 0}
    for info in results:
        action = upsert_shop(
            config, "catfk", info["token"], info["name"]
        )
        stats[action] += 1
        print(f"  config: {action} {info['token']}")

    save_config(config)
    catfk_n = sum(1 for s in config["shops"] if s.get("platform") == "catfk")
    print(
        f"\n完成: added={stats['added']} updated={stats['updated']} "
        f"skipped={stats['skipped']}, catfk 共 {catfk_n} 家"
    )


if __name__ == "__main__":
    main()
