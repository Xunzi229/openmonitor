#!/usr/bin/env python3
"""整理 config.json：写入大类/时段配置，迁移店铺运行状态。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitor import (  # noqa: E402
    DEFAULT_CATEGORIES,
    DEFAULT_MONITOR_KEYWORDS,
    DEFAULT_PUSH_SCHEDULE,
    load_config,
)

CONFIG_FILE = ROOT / "config.json"


def ensure_config_structure() -> None:
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    changed = False
    for key in ("feishu_webhook_url", "qiwei_webhook_url", "classify_llm", "webhook_url"):
        if key in config:
            config.pop(key)
            changed = True
    if "categories" not in config:
        config["categories"] = DEFAULT_CATEGORIES
        changed = True
    if "monitor_keywords" not in config:
        config["monitor_keywords"] = DEFAULT_MONITOR_KEYWORDS
        changed = True
    if "push_schedule" not in config:
        config["push_schedule"] = DEFAULT_PUSH_SCHEDULE
        changed = True
    alert_rules = config.get("alert_rules", {})
    if "top_products_per_category" in alert_rules:
        default_top = alert_rules.pop("top_products_per_category")
        for cat in config["categories"]:
            cat.setdefault("top_n", default_top)
        changed = True
    if changed:
        CONFIG_FILE.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("已写入 categories / monitor_keywords / push_schedule")
    load_config(CONFIG_FILE)
    print("配置整理完成")


if __name__ == "__main__":
    ensure_config_structure()
