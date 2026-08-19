#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监听多平台店铺 GPT 商品价格，分类整理后推送到飞书群。"""

import sys

if sys.version_info[0] < 3:
    sys.stderr.write("请使用 Python 3 运行: python3 monitor.py\n")
    sys.exit(1)
if sys.version_info < (3, 6):
    sys.stderr.write(
        "需要 Python 3.6+，当前 %s\n" % sys.version.split()[0]
    )
    sys.exit(1)

import argparse
import gzip
import json
import logging
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

STATE_FILE = Path(__file__).parent / "data" / "prices.json"
SENT_FILE = Path(__file__).parent / "data" / "sent.json"
TOP10_FILE = Path(__file__).parent / "data" / "top10.json"
SHOPS_STATE_FILE = Path(__file__).parent / "data" / "shops_state.json"
WATCH_PID_FILE = Path(__file__).parent / "data" / "watch.pid"
WATCH_META_FILE = Path(__file__).parent / "data" / "watch.meta.json"
FEISHU_MSG_FILE = Path(__file__).parent / "data" / "feishu_messages.json"
CARDNAV_STATE_FILE = Path(__file__).parent / "data" / "cardnav_state.json"
LOG_DIR = Path(__file__).parent / "data" / "logs"
SCRIPT_FILE = Path(__file__).resolve()
_LOG = logging.getLogger("openmonitor")
_LOG_DATE = ""
_LOG_CONSOLE_READY = False
_feishu_token_cache: Dict[str, Union[float, str]] = {"token": "", "expire_at": 0.0}
SHOP_RUNTIME_FIELDS = ("status", "last_updated_at", "last_error", "last_error_at")

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def daily_log_path(now: Optional[datetime] = None) -> Path:
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    return LOG_DIR / f"monitor-{day}.log"


def _rotate_log_file_if_needed() -> None:
    global _LOG_DATE
    today = datetime.now().strftime("%Y-%m-%d")
    if _LOG_DATE == today:
        return
    for handler in list(_LOG.handlers):
        if isinstance(handler, logging.FileHandler):
            handler.close()
            _LOG.removeHandler(handler)
    path = daily_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    )
    _LOG.addHandler(file_handler)
    if not _LOG_DATE:
        _LOG.info("日志文件: %s", path.resolve())
    _LOG_DATE = today


def setup_logging(
    log_file: Optional[Path] = None,
    *,
    log_to_console: Optional[bool] = None,
) -> None:
    """初始化日志：按自然日写入 data/logs/monitor-YYYY-MM-DD.log。"""
    global _LOG_CONSOLE_READY
    if log_to_console is None:
        log_to_console = sys.stdout.isatty()

    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False

    if log_file is not None:
        for handler in list(_LOG.handlers):
            if isinstance(handler, logging.FileHandler):
                handler.close()
                _LOG.removeHandler(handler)
        path = log_file
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
        )
        _LOG.addHandler(file_handler)
        _LOG.info("日志文件: %s", path.resolve())
    else:
        _rotate_log_file_if_needed()

    if log_to_console and not _LOG_CONSOLE_READY:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
        )
        _LOG.addHandler(stream_handler)
    _LOG_CONSOLE_READY = True


def log(message: str) -> None:
    setup_logging()
    _rotate_log_file_if_needed()
    _LOG.info(message)


def log_shop_fetch_skus(
    platform: str,
    shop_name: str,
    snapshot: dict,
    removed: Set[str],
    batch_index: int,
    batch_total: int,
) -> None:
    log(
        f"已更新 [{platform}] {shop_name} ({batch_index}/{batch_total})，"
        f"入库 {len(snapshot)} SKU"
    )
    if snapshot:
        for sku in sorted(snapshot):
            item = snapshot[sku]
            name = (item.get("name") or "").replace("\n", " ")[:80]
            log(
                f"  + {sku} | {item.get('type', '')} | {name} | "
                f"{item.get('price')}元 | 库存{item.get('stock', 0)}"
            )
    else:
        log("  (无符合条件 SKU)")
    for sku in sorted(removed):
        log(f"  - 下架 {sku}")

PLATFORMS = {
    "ldxp": {
        "api_url": "https://pay.ldxp.cn/shopApi/Shop/goodsList",
        "origin": "https://pay.ldxp.cn",
        "shop_url": "https://pay.ldxp.cn/shop/{token}",
        "goods_type": "card",
        "category_id": 0,
    },
    "catfk": {
        "api_url": "https://catfk.com/shopApi/Shop/goodsList",
        "origin": "https://catfk.com",
        "shop_url": "https://catfk.com/shop/{token}",
        "goods_type": "card",
        "category_id": 0,
        "visitorid": "openmonitor-browser",
    },
}

_SIMPLE_ENGLISH_KEYWORD_RE = re.compile(r"^[a-zA-Z]+$")

DEFAULT_CATEGORIES = [
    {
        "name": "免费号",
        "keywords": ["free", "免费", "白嫖"],
        "description": (
            "实际售卖免费/白嫖的 ChatGPT 账号本身；"
            "接码、邮箱等辅助服务不算。"
        ),
        "top_n": 10,
        "enabled": True,
        "push": False,
    },
    {
        "name": "UPI",
        "keywords": ["upi"],
        "description": "实际与 UPI 支付或开通相关的商品。",
        "top_n": 5,
        "enabled": True,
        "push": False,
    },
    {
        "name": "重置",
        "keywords": ["重置"],
        "description": "实际是账号重置类服务。",
        "top_n": 5,
        "enabled": True,
        "push": False,
    },
    {
        "name": "Plus号",
        "keywords": ["plus", "plus号", "plus帐", "plus账"],
        "description": (
            "实际售卖 ChatGPT Plus 官方会员账号/订阅/席位本身"
            "（如成品号、独享月卡、质保首登会员）。"
            "以下绝不是 Plus号："
            "接码/验证码/邮箱/绑定工具；"
            "中转站、镜像站、API 中转；"
            "标题含「xx刀/20刀/plus20刀」等额度或中转套餐"
            "（如「plus20刀不限时」）；"
            "CDK、卡头、次卡、直卡、支付链接、虚拟卡、"
            "「开通plus必备」等支付开通工具。"
        ),
        "top_n": 10,
        "enabled": True,
    },
    {
        "name": "Team号",
        "keywords": ["team", "团队"],
        "description": (
            "实际售卖 ChatGPT Team 账号或团队席位本身；"
            "接码/邮箱/中转站/镜像站/xx刀额度等不算。"
        ),
        "top_n": 10,
        "enabled": True,
        "push": False,
    },
    {
        "name": "Pro号",
        "keywords": ["pro"],
        "description": (
            "实际售卖 ChatGPT Pro 账号或订阅本身；"
            "接码/邮箱/中转站/镜像站/xx刀额度等不算。"
        ),
        "top_n": 10,
        "enabled": True,
        "push": False,
    },
    {
        "name": "其他GPT",
        "keywords": [],
        "description": (
            "不属于以上任一精准分类的 GPT 相关商品，"
            "包括接码、邮箱、绑定工具、中转站、镜像站、"
            "xx刀额度套餐、杂项服务等。"
        ),
        "top_n": 10,
        "enabled": True,
        "fallback": True,
        "push": False,
    },
]
DEFAULT_MONITOR_KEYWORDS = ["gpt", "chatgpt"]
DEFAULT_PUSH_SCHEDULE = {
    "weekdays": [1, 2, 3, 4, 5],
    "time_ranges": [{"start": "07:00", "end": "19:00"}],
}
DEFAULT_ALERT_RULES = {
    "push_on_price_drop": True,
    "push_on_top10_new_entry": True,
}
DEFAULT_CLASSIFY_LLM = {
    "enabled": True,
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o-mini",
    "batch_size": 20,
    "timeout_sec": 60,
}

FEISHU_MAX_BYTES = 20 * 1024
QIWEI_MAX_BYTES = 4096
MIN_API_INTERVAL_SEC = 1.5
DEFAULT_SHOP_UPDATE_INTERVAL_MINUTES = 20
DEFAULT_SHOPS_PER_BATCH = 10
DEFAULT_SHOP_FETCH_DELAY_MIN_SEC = 2.0
DEFAULT_SHOP_FETCH_DELAY_MAX_SEC = 3.0
DEFAULT_POOL_ITEM_STALE_HOURS = 2
DEFAULT_ALERT_SHOP_FRESH_SECONDS = 180
SHOP_API_RETRY_TIMES = 3
CARDNAV_API_URL = "https://cardnav.xyz/api/shop-products.json"
DEFAULT_CARDNAV_POLL_INTERVAL_MINUTES = 10
CARDNAV_SHOP_PATTERNS = {
    "ldxp": re.compile(r"https?://pay\.ldxp\.cn/shop/([^/?#]+)", re.I),
    "catfk": re.compile(r"https?://catfk\.com/shop/([^/?#]+)", re.I),
}
_last_api_call_at = 0.0

TABLE_HEADER = "| 商品 | 渠道商店 | 价格 | 价格浮动 |"
TABLE_SEPARATOR = "| :----- | :----- | :----: | :----: |"
ALERT_TABLE_HEADER = (
    "| 变动 | 分类 | 商品 | 渠道商店 | 价格 | 价格浮动 |"
)
ALERT_TABLE_SEPARATOR = "| :----- | :----- | :----- | :----- | :----: | :----: |"


def _wait_api_interval() -> None:
    global _last_api_call_at
    now = time.monotonic()
    wait = MIN_API_INTERVAL_SEC - (now - _last_api_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_api_call_at = time.monotonic()


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def shop_key(shop: dict) -> Tuple[str, str]:
    return (shop.get("platform", "ldxp"), shop["token"])


def dedupe_shops(shops: List[dict]) -> List[dict]:
    seen: Set[Tuple[str, str]] = set()
    result: List[dict] = []
    for shop in shops:
        key = shop_key(shop)
        if key in seen:
            continue
        seen.add(key)
        result.append(shop)
    return result


# 敏感配置：只放 config.secrets.json，不写入可提交的 config.json
SECRET_CONFIG_KEYS = ("feishu_webhook_url", "qiwei_webhook_url", "classify_llm")
SECRETS_FILE_NAME = "config.secrets.json"


def secrets_path_for(config_path: Path) -> Path:
    return config_path.parent / SECRETS_FILE_NAME


def load_secrets(config_path: Path) -> dict:
    path = secrets_path_for(config_path)
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 必须是 JSON 对象")
    return {k: data[k] for k in SECRET_CONFIG_KEYS if k in data}


def strip_secrets(config: dict) -> dict:
    return {k: v for k, v in config.items() if k not in SECRET_CONFIG_KEYS}


def save_config(config: dict, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(strip_secrets(config), f, ensure_ascii=False, indent=2)
        f.write("\n")


def get_categories(config: dict) -> List[dict]:
    categories = config.get("categories") or DEFAULT_CATEGORIES
    return [cat for cat in categories if cat.get("enabled", True)]


def get_category_order(config: dict) -> List[str]:
    return [cat["name"] for cat in get_categories(config)]


def get_category_top_n(config: dict, category_name: str) -> int:
    for cat in get_categories(config):
        if cat["name"] == category_name:
            return int(cat.get("top_n", 10))
    return 10


def is_category_push_enabled(config: dict, category_name: str) -> bool:
    categories = config.get("categories") or DEFAULT_CATEGORIES
    for cat in categories:
        if cat.get("name") == category_name:
            return cat.get("push", True)
    return True


def new_pool_entry_kind(config: dict, category_name: str) -> str:
    return f"新进Top{get_category_top_n(config, category_name)}"


def is_new_pool_entry_kind(kind: str) -> bool:
    return str(kind).startswith("新进Top")


def normalize_config_shops(config: dict) -> dict:
    original = config.get("shops", [])
    deduped = dedupe_shops(original)
    if len(deduped) != len(original):
        removed = len(original) - len(deduped)
        log(
            f"配置去重提示: 发现 {removed} 个重复店铺，运行时将忽略重复项"
        )
    config["shops"] = deduped
    return config


def load_shops_state() -> dict:
    if not SHOPS_STATE_FILE.exists():
        return {}
    with open(SHOPS_STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_shops_state(state: dict) -> None:
    SHOPS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SHOPS_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def shop_state_key(platform: str, token: str) -> str:
    return shop_id(platform, token)


def get_shop_runtime(shops_state: dict, platform: str, token: str) -> dict:
    return shops_state.get(shop_state_key(platform, token), {})


def migrate_shop_runtime_from_config(config: dict, config_path: Path) -> bool:
    shops_state = load_shops_state()
    migrated = False
    clean_shops: List[dict] = []
    for shop in config.get("shops", []):
        platform = shop.get("platform", "ldxp")
        token = shop["token"]
        key = shop_state_key(platform, token)
        runtime = {
            field: shop[field]
            for field in SHOP_RUNTIME_FIELDS
            if field in shop
        }
        if runtime:
            shops_state[key] = {**shops_state.get(key, {}), **runtime}
            migrated = True
        clean_shops.append(
            {
                k: v
                for k, v in shop.items()
                if k not in SHOP_RUNTIME_FIELDS
            }
        )
    if migrated:
        config["shops"] = dedupe_shops(clean_shops)
        save_config(config, config_path)
        save_shops_state(shops_state)
        log(
            f"已将店铺 status/last_updated_at 迁移至 {SHOPS_STATE_FILE.name}"
        )
    return migrated


def resolve_webhook_targets(config: dict) -> List[Tuple[str, str]]:
    feishu = (
        config.get("feishu_webhook_url") or config.get("webhook_url") or ""
    ).strip()
    qiwei = (config.get("qiwei_webhook_url") or "").strip()
    targets: List[Tuple[str, str]] = []
    if feishu:
        targets.append(("feishu", feishu))
    if qiwei:
        targets.append(("qiwei", qiwei))
    return targets


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


ENV_SCALAR_OVERRIDES: Dict[str, Tuple[str, type]] = {
    "FEISHU_WEBHOOK_URL": ("feishu_webhook_url", str),
    "QIWEI_WEBHOOK_URL": ("qiwei_webhook_url", str),
    "MONITOR_INTERVAL_SECONDS": ("interval_seconds", int),
    "MONITOR_SHOPS_PER_BATCH": ("shops_per_batch", int),
    "MONITOR_SHOP_UPDATE_INTERVAL_MINUTES": (
        "shop_update_interval_minutes",
        int,
    ),
    "MONITOR_PAGE_SIZE": ("page_size", int),
    "MONITOR_MIN_PUSH_PRICE": ("min_push_price", float),
    "MONITOR_MAX_PUSH_PRICE": ("max_push_price", float),
    "MONITOR_SHOP_FETCH_DELAY_MIN_SEC": ("shop_fetch_delay_min_sec", float),
    "MONITOR_SHOP_FETCH_DELAY_MAX_SEC": ("shop_fetch_delay_max_sec", float),
    "MONITOR_POOL_ITEM_STALE_HOURS": ("pool_item_stale_hours", float),
}


def apply_env_overrides(config: dict) -> dict:
    for env_key, (config_key, caster) in ENV_SCALAR_OVERRIDES.items():
        val = os.environ.get(env_key, "").strip()
        if not val:
            continue
        config[config_key] = caster(val)

    for env_key, config_key in (
        ("FEISHU_APP_ID", "feishu_app_id"),
        ("FEISHU_APP_SECRET", "feishu_app_secret"),
        ("FEISHU_CHAT_ID", "feishu_chat_id"),
    ):
        val = os.environ.get(env_key, "").strip()
        if val:
            config[config_key] = val

    llm = dict(config.get("classify_llm") or {})
    for env_key, config_key in (
        ("CLASSIFY_LLM_API_KEY", "api_key"),
        ("CLASSIFY_LLM_BASE_URL", "base_url"),
        ("CLASSIFY_LLM_MODEL", "model"),
    ):
        val = os.environ.get(env_key, "").strip()
        if val:
            llm[config_key] = val
    enabled = os.environ.get("CLASSIFY_LLM_ENABLED", "").strip().lower()
    if enabled in ("1", "true", "yes"):
        llm["enabled"] = True
    elif enabled in ("0", "false", "no"):
        llm["enabled"] = False
    if llm:
        config["classify_llm"] = {**DEFAULT_CLASSIFY_LLM, **llm}

    fetch_outside = os.environ.get(
        "MONITOR_FETCH_OUTSIDE_PUSH_SCHEDULE", ""
    ).strip().lower()
    if fetch_outside in ("1", "true", "yes"):
        config["fetch_outside_push_schedule"] = True
    elif fetch_outside in ("0", "false", "no"):
        config["fetch_outside_push_schedule"] = False

    json_text = os.environ.get("MONITOR_CONFIG_JSON", "").strip()
    if json_text:
        try:
            override = json.loads(json_text)
        except json.JSONDecodeError as e:
            raise ValueError(f"MONITOR_CONFIG_JSON 不是合法 JSON: {e}") from e
        if not isinstance(override, dict):
            raise ValueError("MONITOR_CONFIG_JSON 必须是 JSON 对象")
        config = deep_merge(config, override)
    return config


def parse_config(raw: dict, config_path: Path) -> dict:
    config = strip_secrets(dict(raw))
    secrets = load_secrets(config_path)
    if secrets:
        config = deep_merge(config, secrets)
    if config.get("webhook_url") and not config.get("feishu_webhook_url"):
        config["feishu_webhook_url"] = config["webhook_url"]
    config["alert_rules"] = {
        **DEFAULT_ALERT_RULES,
        **config.get("alert_rules", {}),
    }
    if "categories" not in config:
        config["categories"] = DEFAULT_CATEGORIES
    if "monitor_keywords" not in config:
        config["monitor_keywords"] = DEFAULT_MONITOR_KEYWORDS
    if "push_schedule" not in config:
        config["push_schedule"] = DEFAULT_PUSH_SCHEDULE
    config["classify_llm"] = {
        **DEFAULT_CLASSIFY_LLM,
        **(config.get("classify_llm") or {}),
    }
    config = apply_env_overrides(config)
    migrate_shop_runtime_from_config(config, config_path)
    return normalize_config_shops(config)


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return parse_config(json.load(f), path)


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.secrets_path = secrets_path_for(path)
        self._mtime = 0.0
        self._secrets_mtime = 0.0
        self._config: Optional[dict] = None

    def _current_mtimes(self) -> Tuple[float, float]:
        mtime = self.path.stat().st_mtime
        secrets_mtime = (
            self.secrets_path.stat().st_mtime
            if self.secrets_path.is_file()
            else 0.0
        )
        return mtime, secrets_mtime

    def get(self) -> dict:
        mtime, secrets_mtime = self._current_mtimes()
        if (
            self._config is None
            or mtime != self._mtime
            or secrets_mtime != self._secrets_mtime
        ):
            if self._config is not None:
                log("检测到配置变化，已热更新")
            self._config = load_config(self.path)
            self._mtime = mtime
            self._secrets_mtime = secrets_mtime
        return self._config


SHOP_STATUS_ACTIVE = "active"
SHOP_STATUS_ERROR = "error"
SHOP_STATUS_DISABLED = "disabled"
SHOP_STATUS_BANNED = "banned"
SHOP_STATUS_SKIPPED = "skipped"
SHOP_PERMANENT_SKIP_MARKERS = ("封禁", "店铺链接不存在")


def is_shop_permanent_skip_error(error: Union[Exception, str]) -> bool:
    text = str(error)
    return any(marker in text for marker in SHOP_PERMANENT_SKIP_MARKERS)


def permanent_skip_status(error: Union[Exception, str]) -> str:
    text = str(error)
    if "店铺链接不存在" in text:
        return SHOP_STATUS_SKIPPED
    if "封禁" in text:
        return SHOP_STATUS_BANNED
    return SHOP_STATUS_SKIPPED


def permanent_skip_reason(error: Union[Exception, str]) -> str:
    text = str(error)
    if "店铺链接不存在" in text:
        return "店铺链接不存在"
    if "封禁" in text:
        return "店铺已封禁"
    return "店铺不可用"


def sync_permanent_skip_shops(shops_state: dict) -> int:
    """将历史 error 且错误信息匹配的店铺转为永久跳过。"""
    changed = 0
    for entry in shops_state.values():
        if entry.get("status") != SHOP_STATUS_ERROR:
            continue
        last_error = entry.get("last_error", "")
        if not is_shop_permanent_skip_error(last_error):
            continue
        entry["status"] = permanent_skip_status(last_error)
        changed += 1
    if changed:
        save_shops_state(shops_state)
    return changed


def get_shop_status(shops_state: dict, platform: str, token: str) -> str:
    return get_shop_runtime(shops_state, platform, token).get(
        "status", SHOP_STATUS_ACTIVE
    )


def is_shop_monitored(shops_state: dict, platform: str, token: str) -> bool:
    return get_shop_status(shops_state, platform, token) in (
        SHOP_STATUS_ACTIVE,
        SHOP_STATUS_ERROR,
    )


def get_shop_update_interval_minutes(config: dict) -> int:
    return int(config.get("shop_update_interval_minutes", DEFAULT_SHOP_UPDATE_INTERVAL_MINUTES))


def parse_shop_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def current_timestamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def get_pool_item_stale_hours(config: dict) -> float:
    value = config.get("pool_item_stale_hours")
    if value is None:
        value = (config.get("alert_rules") or {}).get("pool_item_stale_hours")
    if value is None:
        return DEFAULT_POOL_ITEM_STALE_HOURS
    return float(value)


def is_pool_item_fresh(item: dict, config: dict, now: Optional[datetime] = None) -> bool:
    updated = parse_shop_timestamp(item.get("last_updated_at"))
    if not updated:
        return False
    stale_hours = get_pool_item_stale_hours(config)
    elapsed = (now or datetime.now()) - updated
    return elapsed.total_seconds() < stale_hours * 3600


def backfill_item_timestamps(state: dict, shops_state: dict) -> int:
    """为缺少 last_updated_at 的商品回填店铺最近抓取时间。"""
    filled = 0
    for item in state.values():
        if item.get("last_updated_at"):
            continue
        shop_sid = item.get("shop", "")
        if ":" not in shop_sid:
            item["last_updated_at"] = current_timestamp()
            filled += 1
            continue
        platform, token = shop_sid.split(":", 1)
        runtime = get_shop_runtime(shops_state, platform, token)
        item["last_updated_at"] = runtime.get("last_updated_at") or current_timestamp()
        filled += 1
    return filled


def count_stale_pool_items(state: dict, config: dict) -> int:
    return sum(
        1 for item in state.values() if not is_pool_item_fresh(item, config)
    )


def is_shop_updated_today(
    shops_state: dict, platform: str, token: str, now: Optional[datetime] = None
) -> bool:
    last_updated = parse_shop_timestamp(
        get_shop_runtime(shops_state, platform, token).get("last_updated_at")
    )
    if not last_updated:
        return False
    return last_updated.date() == (now or datetime.now()).date()


def count_daily_refresh_status(
    config: dict, shops_state: dict, now: Optional[datetime] = None
) -> Tuple[int, int]:
    """返回 (当日待刷新店铺数, 监控店铺总数)。"""
    monitored = [
        shop
        for shop in config.get("shops", [])
        if is_shop_monitored(
            shops_state, shop.get("platform", "ldxp"), shop["token"]
        )
    ]
    pending = sum(
        1
        for shop in monitored
        if not is_shop_updated_today(
            shops_state,
            shop.get("platform", "ldxp"),
            shop["token"],
            now,
        )
    )
    return pending, len(monitored)


def is_daily_refresh_complete(
    config: dict, shops_state: dict, now: Optional[datetime] = None
) -> bool:
    pending, total = count_daily_refresh_status(config, shops_state, now)
    return total > 0 and pending == 0


def is_shop_due_for_update(
    shops_state: dict, platform: str, token: str, interval_minutes: int
) -> bool:
    runtime = get_shop_runtime(shops_state, platform, token)
    last_updated = parse_shop_timestamp(runtime.get("last_updated_at"))
    if not last_updated:
        return True
    elapsed = datetime.now() - last_updated
    return elapsed.total_seconds() >= interval_minutes * 60


def get_alert_shop_fresh_seconds(config: dict) -> float:
    value = config.get("alert_shop_fresh_seconds")
    if value is None:
        return float(DEFAULT_ALERT_SHOP_FRESH_SECONDS)
    return float(value)


def is_shop_updated_within(
    shops_state: dict,
    platform: str,
    token: str,
    within_seconds: float,
    now: Optional[datetime] = None,
) -> bool:
    """店铺是否在 within_seconds 内更新过。"""
    last_updated = parse_shop_timestamp(
        get_shop_runtime(shops_state, platform, token).get("last_updated_at")
    )
    if not last_updated:
        return False
    elapsed = (now or datetime.now()) - last_updated
    return elapsed.total_seconds() <= within_seconds


def alert_shop_ids(alerts: List[dict], state: dict) -> Set[str]:
    shops: Set[str] = set()
    for alert in alerts:
        sku = alert.get("sku") or ""
        item = state.get(sku)
        if item and item.get("shop"):
            shops.add(item["shop"])
    return shops


def filter_alerts_by_shop_freshness(
    alerts: List[dict],
    state: dict,
    shops_state: dict,
    within_seconds: float,
    now: Optional[datetime] = None,
) -> List[dict]:
    """只保留店铺在 within_seconds 内已验过库存的告警。"""
    kept: List[dict] = []
    for alert in alerts:
        sku = alert.get("sku") or ""
        item = state.get(sku)
        if not item:
            continue
        shop_sid = item.get("shop") or ""
        if ":" not in shop_sid:
            continue
        platform, token = shop_sid.split(":", 1)
        if is_shop_updated_within(
            shops_state, platform, token, within_seconds, now=now
        ):
            kept.append(alert)
    return kept


def ingest_shop_snapshot(
    shop: dict,
    *,
    config: dict,
    state: dict,
    sent_state: dict,
    pool: Dict[str, List[str]],
    old_state: dict,
    shops_state: dict,
) -> Set[str]:
    """抓取店铺商品并覆盖 state 中该店数据，返回被移除的 SKU。"""
    token = shop["token"]
    name = shop.get("name", token)
    platform = shop.get("platform", "ldxp")
    sid = shop_id(platform, token)
    items = fetch_goods(
        token,
        platform=platform,
        page_size=config.get("page_size", 100),
        goods_type=shop.get("goods_type"),
        config=config,
    )
    update_shop_runtime(
        shops_state,
        platform,
        token,
        status=SHOP_STATUS_ACTIVE,
        mark_updated=True,
    )
    grouped = filter_and_classify(
        items,
        config,
        platform=platform,
        shop_token=token,
        old_state=old_state,
    )
    snapshot = build_snapshot(platform, token, name, grouped)
    return replace_shop_snapshot(
        sid,
        snapshot,
        state=state,
        sent_state=sent_state,
        pool=pool,
        config=config,
    )


def refresh_stale_alert_shops(
    shops: Set[str],
    *,
    config: dict,
    state: dict,
    sent_state: dict,
    pool: Dict[str, List[str]],
    old_state: dict,
    shops_state: dict,
    within_seconds: Optional[float] = None,
) -> int:
    """增量推送前：告警店铺若超过 within_seconds 未更新则重拉覆盖。返回重拉成功数。"""
    if not shops:
        return 0
    fresh_sec = (
        float(within_seconds)
        if within_seconds is not None
        else get_alert_shop_fresh_seconds(config)
    )
    refreshed = 0
    for sid in sorted(shops):
        if ":" not in sid:
            continue
        platform, token = sid.split(":", 1)
        if is_shop_updated_within(shops_state, platform, token, fresh_sec):
            continue
        shop = find_shop(config, platform, token)
        if not shop:
            continue
        name = shop.get("name", token)
        if refreshed > 0:
            sleep_between_shops(config)
        log(
            f"增量推送前重拉店铺（{fresh_sec:g}秒内未更新）: "
            f"[{platform}] {name}"
        )
        try:
            ingest_shop_snapshot(
                shop,
                config=config,
                state=state,
                sent_state=sent_state,
                pool=pool,
                old_state=old_state,
                shops_state=shops_state,
            )
            refreshed += 1
        except Exception as e:
            log(f"增量推送前重拉失败 [{platform}] {name}: {e}")
            if is_shop_permanent_skip_error(e):
                update_shop_runtime(
                    shops_state,
                    platform,
                    token,
                    status=permanent_skip_status(e),
                    error=str(e),
                    mark_updated=True,
                )
            else:
                update_shop_runtime(
                    shops_state,
                    platform,
                    token,
                    status=SHOP_STATUS_ERROR,
                    error=str(e),
                    mark_updated=True,
                )
    return refreshed


def preserve_shop_state(
    old_state: dict, new_state: dict, shop_ids: Set[str]
) -> None:
    for sku, item in old_state.items():
        if item.get("shop") in shop_ids:
            new_state[sku] = item


def _shop_sort_key(
    shop: dict, shops_state: dict
) -> datetime:
    platform = shop.get("platform", "ldxp")
    runtime = get_shop_runtime(shops_state, platform, shop["token"])
    return parse_shop_timestamp(runtime.get("last_updated_at")) or datetime.min


def select_shops_for_batch(
    shops: List[dict],
    shops_state: dict,
    config: dict,
    *,
    force_fetch: bool,
    now: Optional[datetime] = None,
) -> Tuple[List[dict], int]:
    batch_size = int(config.get("shops_per_batch", DEFAULT_SHOPS_PER_BATCH))
    interval = get_shop_update_interval_minutes(config)
    monitored = [
        shop
        for shop in shops
        if is_shop_monitored(
            shops_state, shop.get("platform", "ldxp"), shop["token"]
        )
    ]
    if force_fetch:
        candidates = sorted(
            monitored, key=lambda shop: _shop_sort_key(shop, shops_state)
        )
        return candidates, len(candidates)
    pending_daily = [
        shop
        for shop in monitored
        if not is_shop_updated_today(
            shops_state,
            shop.get("platform", "ldxp"),
            shop["token"],
            now,
        )
    ]
    if pending_daily:
        pending_daily.sort(key=lambda shop: _shop_sort_key(shop, shops_state))
        return pending_daily[:batch_size], len(pending_daily)
    due = [
        shop
        for shop in monitored
        if is_shop_due_for_update(
            shops_state,
            shop.get("platform", "ldxp"),
            shop["token"],
            interval,
        )
    ]
    due.sort(key=lambda shop: _shop_sort_key(shop, shops_state))
    return due[:batch_size], len(due)


def sleep_between_shops(config: dict) -> None:
    """店铺之间、分页之间、重试前的 API 请求间隔。"""
    delay_min = float(
        config.get("shop_fetch_delay_min_sec", DEFAULT_SHOP_FETCH_DELAY_MIN_SEC)
    )
    delay_max = float(
        config.get("shop_fetch_delay_max_sec", DEFAULT_SHOP_FETCH_DELAY_MAX_SEC)
    )
    if delay_max < delay_min:
        delay_max = delay_min
    time.sleep(random.uniform(delay_min, delay_max))


def _api_interval_config(config: Optional[dict]) -> dict:
    if config is not None:
        return config
    return {
        "shop_fetch_delay_min_sec": DEFAULT_SHOP_FETCH_DELAY_MIN_SEC,
        "shop_fetch_delay_max_sec": DEFAULT_SHOP_FETCH_DELAY_MAX_SEC,
    }


def _sleep_api_interval(config: Optional[dict]) -> None:
    sleep_between_shops(_api_interval_config(config))


def find_shop(config: dict, platform: str, token: str) -> Optional[dict]:
    key = (platform, token)
    for shop in config.get("shops", []):
        if shop_key(shop) == key:
            return shop
    return None


def update_shop_runtime(
    shops_state: dict,
    platform: str,
    token: str,
    *,
    status: str,
    error: Optional[str] = None,
    mark_updated: bool = False,
) -> bool:
    key = shop_state_key(platform, token)
    entry = shops_state.setdefault(key, {"status": SHOP_STATUS_ACTIVE})
    entry["status"] = status
    if mark_updated:
        entry["last_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if error:
        entry["last_error"] = error
        entry["last_error_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        entry.pop("last_error", None)
        entry.pop("last_error_at", None)
    save_shops_state(shops_state)
    return True


def shop_id(platform: str, token: str) -> str:
    return f"{platform}:{token}"


def shop_page_url(platform: str, token: str) -> str:
    cfg = PLATFORMS.get(platform, PLATFORMS["ldxp"])
    return cfg["shop_url"].format(token=token)


def shop_page_url_from_shop_id(shop_id_value: str) -> str:
    platform, token = shop_id_value.split(":", 1)
    return shop_page_url(platform, token)


def shop_page_url_from_item(item: dict) -> str:
    shop = item.get("shop", "")
    if ":" in shop:
        platform, token = shop.split(":", 1)
    else:
        platform = item.get("platform", "ldxp")
        token = shop
    return shop_page_url(platform, token)


def _read_http_json(resp) -> dict:
    data = resp.read()
    encoding = resp.headers.get("Content-Encoding", "").lower()
    if encoding == "gzip" or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data.decode())


def _browser_headers(platform: str, shop_url: str) -> Dict[str, str]:
    cfg = PLATFORMS[platform]
    origin = cfg["origin"]
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": origin,
        "priority": "u=1, i",
        "referer": shop_url,
        "sec-ch-ua": (
            '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": BROWSER_USER_AGENT,
        "visitorid": str(cfg.get("visitorid", "openmonitor-browser")),
    }


def _build_goods_list_payload(
    platform: str,
    token: str,
    page: int,
    page_size: int,
    goods_type: Optional[str],
) -> dict:
    cfg = PLATFORMS[platform]
    return {
        "token": token,
        "keywords": "",
        "category_id": cfg.get("category_id", 0),
        "goods_type": goods_type or cfg["goods_type"],
        "current": page,
        "pageSize": page_size,
    }


def _parse_goods_list_response(body: dict) -> List[dict]:
    if body.get("code") != 1:
        raise RuntimeError(f"API 错误: {body.get('msg', body)}")
    return body.get("data", {}).get("list", [])


def _post_goods_list_page(
    api_url: str,
    headers: Dict[str, str],
    payload: dict,
) -> dict:
    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return _read_http_json(resp)


def _call_with_shop_api_retry(
    config: Optional[dict],
    action,
    *,
    action_label: str = "店铺 API",
):
    last_error: Optional[Exception] = None
    max_attempts = SHOP_API_RETRY_TIMES + 1
    for attempt in range(1, max_attempts + 1):
        try:
            return action()
        except Exception as e:
            last_error = e
            if is_shop_permanent_skip_error(e):
                raise
            if attempt >= max_attempts:
                raise
            interval_cfg = _api_interval_config(config)
            delay_min = float(
                interval_cfg.get(
                    "shop_fetch_delay_min_sec", DEFAULT_SHOP_FETCH_DELAY_MIN_SEC
                )
            )
            delay_max = float(
                interval_cfg.get(
                    "shop_fetch_delay_max_sec", DEFAULT_SHOP_FETCH_DELAY_MAX_SEC
                )
            )
            log(
                f"{action_label} 失败（{attempt}/{max_attempts}）: {e}，"
                f"等待 {delay_min:g}~{delay_max:g} 秒后重试"
            )
            _sleep_api_interval(config)
    if last_error:
        raise last_error
    raise RuntimeError(f"{action_label} 请求失败")


def _fetch_goods_list_page(
    api_url: str,
    headers: Dict[str, str],
    payload: dict,
    config: Optional[dict],
) -> List[dict]:
    def _do_fetch() -> List[dict]:
        body = _post_goods_list_page(api_url, headers, payload)
        return _parse_goods_list_response(body)

    return _call_with_shop_api_retry(config, _do_fetch, action_label="店铺 API")


def fetch_goods(
    token: str,
    platform: str = "ldxp",
    page_size: int = 100,
    goods_type: Optional[str] = None,
    max_pages: int = 50,
    config: Optional[dict] = None,
) -> List[dict]:
    cfg = PLATFORMS.get(platform)
    if not cfg:
        raise ValueError(f"未知平台: {platform}")
    shop_url = cfg["shop_url"].format(token=token)
    headers = _browser_headers(platform, shop_url)
    all_items: List[dict] = []
    for page in range(1, max_pages + 1):
        if page > 1:
            _sleep_api_interval(config)
        payload = _build_goods_list_payload(
            platform, token, page, page_size, goods_type
        )
        items = _fetch_goods_list_page(
            cfg["api_url"], headers, payload, config
        )
        all_items.extend(items)
        if len(items) < page_size:
            break
    return all_items


def cardnav_enabled(config: dict) -> bool:
    return bool(config.get("cardnav_enabled", True))


def get_cardnav_poll_interval_minutes(config: dict) -> int:
    return int(
        config.get(
            "cardnav_poll_interval_minutes",
            DEFAULT_CARDNAV_POLL_INTERVAL_MINUTES,
        )
    )


def load_cardnav_state() -> dict:
    if not CARDNAV_STATE_FILE.exists():
        return {}
    with open(CARDNAV_STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_cardnav_state(state: dict) -> None:
    CARDNAV_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CARDNAV_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_cardnav_due(config: dict) -> bool:
    if not cardnav_enabled(config):
        return False
    state = load_cardnav_state()
    last_at = state.get("last_fetch_at")
    if not last_at:
        return True
    last_ts = parse_shop_timestamp(last_at)
    if not last_ts:
        return True
    elapsed = datetime.now() - last_ts
    return elapsed.total_seconds() >= get_cardnav_poll_interval_minutes(config) * 60


def _cardnav_browser_headers() -> Dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "priority": "u=1, i",
        "referer": "https://cardnav.xyz/shops",
        "sec-ch-ua": (
            '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": BROWSER_USER_AGENT,
    }


def _get_json_url(url: str, headers: Dict[str, str], timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _read_http_json(resp)


def fetch_cardnav_data(config: Optional[dict] = None) -> dict:
    def _do_fetch() -> dict:
        body = _get_json_url(CARDNAV_API_URL, _cardnav_browser_headers())
        if not isinstance(body, dict):
            raise RuntimeError("CardNav 响应不是 JSON 对象")
        if "sites" not in body:
            raise RuntimeError(f"CardNav 响应缺少 sites: {body.keys()}")
        return body

    return _call_with_shop_api_retry(config, _do_fetch, action_label="CardNav API")


def parse_cardnav_shop_url(url: str) -> Optional[Tuple[str, str]]:
    for platform, pattern in CARDNAV_SHOP_PATTERNS.items():
        match = pattern.search(url or "")
        if match:
            return platform, match.group(1)
    return None


def cardnav_site_to_shop_entry(site: dict) -> Optional[dict]:
    parsed = parse_cardnav_shop_url(site.get("url", ""))
    if not parsed:
        return None
    platform, token = parsed
    name = (site.get("name") or token).strip()
    if not name:
        return None
    entry = {"token": token, "name": name}
    if platform != "ldxp":
        entry["platform"] = platform
    return entry


def build_config_shop_key_set(config: dict) -> Set[Tuple[str, str]]:
    keys: Set[Tuple[str, str]] = set()
    for shop in config.get("shops", []):
        platform = shop.get("platform", "ldxp")
        keys.add((platform, shop["token"].lower()))
    return keys


def sync_cardnav_shops(config: dict, config_path: Path) -> int:
    """从 CardNav 拉取店铺列表，将新店铺写入 config.json（已有店铺跳过）。"""
    data = fetch_cardnav_data(config)
    sites = data.get("sites") or []
    existing_keys = build_config_shop_key_set(config)
    shops: List[dict] = config.setdefault("shops", [])
    added = 0
    skipped = 0
    unsupported = 0

    for site in sites:
        entry = cardnav_site_to_shop_entry(site)
        if not entry:
            unsupported += 1
            continue
        platform = entry.get("platform", "ldxp")
        key = (platform, entry["token"].lower())
        if key in existing_keys:
            skipped += 1
            continue
        shops.append(entry)
        existing_keys.add(key)
        added += 1
        log(
            f"CardNav 新增店铺: [{platform}] {entry['name']} ({entry['token']})"
        )

    if added:
        config["shops"] = dedupe_shops(shops)
        save_config(config, config_path)

    save_cardnav_state(
        {
            "last_fetch_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "site_count": len(sites),
            "added_shops": added,
            "skipped_existing": skipped,
            "unsupported_sites": unsupported,
        }
    )
    log(
        f"CardNav 同步：{len(sites)} 家店铺，"
        f"新增 {added} 家，已有 {skipped} 家，"
        f"不支持平台 {unsupported} 家"
    )
    return added


def matches_monitor_keywords(text: str, config: dict) -> bool:
    lower = text.lower()
    keywords = config.get("monitor_keywords") or DEFAULT_MONITOR_KEYWORDS
    return any(keyword.lower() in lower for keyword in keywords)


def is_gpt_product(item: dict, config: dict) -> bool:
    name = item.get("name") or ""
    category = (item.get("category") or {}).get("name", "")
    if matches_monitor_keywords(name, config) or matches_monitor_keywords(
        category, config
    ):
        return True
    lower_name = name.lower()
    lower_cat = category.lower()
    for cat in get_categories(config):
        for keyword in cat.get("keywords", []):
            kw = keyword.lower()
            if kw and (kw in lower_name or kw in lower_cat):
                return True
    return False


def normalize_keyword_pattern(keyword: str) -> str:
    """纯英文词自动加 \\b 边界；中文/混合词、手写正则保持原样。"""
    text = keyword.strip()
    if not text:
        return text
    if "\\" in text or any(ch in text for ch in "^$.[]()|+*?"):
        return text
    if _SIMPLE_ENGLISH_KEYWORD_RE.match(text):
        return rf"\b{re.escape(text)}\b"
    return text


# 关键词前的否定语（长串优先），如「不含plus」「无plus」「not plus」
_KEYWORD_NEGATION_PREFIXES = (
    "不含",
    "没有",
    "不是",
    "无需",
    "无须",
    "without ",
    "without",
    "non-",
    "non ",
    "non",
    "not ",
    "not",
    "no ",
    "no",
    "无",
    "非",
)


def is_keyword_negated(text: str, start: int) -> bool:
    """判断 text[start:] 处的关键词是否处于否定语境（如「不含plus」）。"""
    if start <= 0:
        return False
    prefix = text[:start].rstrip().lower()
    for neg in _KEYWORD_NEGATION_PREFIXES:
        n = neg.lower().rstrip()
        if prefix.endswith(n):
            return True
    return False


def keyword_matches_in_name(name: str, keyword: str) -> bool:
    """商品名是否匹配关键词，排除否定语境下的误匹配。"""
    text = (name or "").strip()
    kw = (keyword or "").strip()
    if not text or not kw:
        return False
    lower = text.lower()
    kw_lower = kw.lower()

    start = 0
    while True:
        pos = lower.find(kw_lower, start)
        if pos == -1:
            break
        if not is_keyword_negated(lower, pos):
            return True
        start = pos + 1

    pattern = normalize_keyword_pattern(keyword)
    if pattern:
        for match in re.finditer(pattern, lower, re.IGNORECASE):
            if not is_keyword_negated(lower, match.start()):
                return True
    return False


def is_non_account_gpt_product(name: str) -> bool:
    """非账号本体：接码/邮箱/中转站/镜像/xx刀额度/CDK卡头等。"""
    text = (name or "").lower()
    if not text:
        return False
    markers = (
        "接码",
        "验证码",
        "子邮箱",
        "隐私邮箱",
        "绑定专用",
        "开plus绑定",
        "开 plus 绑定",
        "开plus 绑定",
        "开 plus绑定",
        "sms",
        "otp",
        "中转",
        "镜像",
        # CDK / 卡头 / 支付工具：开通 Plus 的辅助品，不是会员号本身
        "cdk",
        "卡头",
        "次卡",
        "直卡",
        "支付链接",
        "虚拟卡",
        "开通plus必备",
        "开通 plus 必备",
        "开通plus 必备",
        "开通 plus必备",
    )
    if any(marker in text for marker in markers):
        return True
    # 「20刀」「plus20刀」等额度/中转套餐，不是 Plus 会员号
    if re.search(r"\d+\s*刀", text):
        return True
    if "刀" in text and "plus" in text:
        return True
    return False


def is_auxiliary_gpt_service(name: str) -> bool:
    return is_non_account_gpt_product(name)


def _fallback_category_name(config: dict) -> str:
    for category in get_categories(config):
        if category.get("fallback"):
            return category["name"]
    return "其他GPT"


def keyword_category_hits(name: str, config: dict) -> List[str]:
    """返回标题命中的非兜底分类（按 categories 顺序，每类最多一次）。"""
    hits: List[str] = []
    for category in get_categories(config):
        if category.get("fallback"):
            continue
        for keyword in category.get("keywords", []):
            if keyword_matches_in_name(name, keyword):
                hits.append(category["name"])
                break
    return hits


def classify_keyword_decision(name: str, config: dict) -> Tuple[str, str]:
    """关键字决策。返回 (分类, decision)，decision 为 high|low。

    high：可直接采用，不调模型。
    low：无命中或多类冲突，需模型（或关键字兜底）。
    """
    fallback = _fallback_category_name(config)
    if is_non_account_gpt_product(name):
        return fallback, "high"
    hits = keyword_category_hits(name, config)
    if len(hits) == 1:
        return hits[0], "high"
    if not hits:
        return fallback, "low"
    return hits[0], "low"


def classify_by_keywords(name: str, config: dict) -> str:
    """关键字归类（兜底）。"""
    label, _decision = classify_keyword_decision(name, config)
    if _decision == "high":
        return label
    # 低置信：仍按原逻辑取首个命中或兜底
    return label


def classify_product(name: str, config: dict) -> str:
    return classify_by_keywords(name, config)


def get_classify_llm_config(config: dict) -> dict:
    return {**DEFAULT_CLASSIFY_LLM, **(config.get("classify_llm") or {})}


def is_classify_llm_enabled(config: dict) -> bool:
    cfg = get_classify_llm_config(config)
    return bool(cfg.get("enabled") and str(cfg.get("api_key") or "").strip())


def _default_category_description(name: str) -> str:
    for cat in DEFAULT_CATEGORIES:
        if cat["name"] == name:
            return str(cat.get("description") or "")
    return ""


def build_category_definitions(config: dict) -> List[dict]:
    """供模型使用的分类定义：名称 + 语义说明。"""
    defs: List[dict] = []
    for cat in get_categories(config):
        name = cat["name"]
        # 内置分类优先用代码里的最新语义，避免旧 config 描述过时
        desc = _default_category_description(name) or (
            cat.get("description") or ""
        ).strip()
        entry = {"name": name, "meaning": desc}
        if cat.get("fallback"):
            entry["fallback"] = True
        defs.append(entry)
    return defs


def _extract_json_array(text: str) -> Optional[list]:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("labels", "categories", "result", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
    except json.JSONDecodeError:
        pass
    start = raw.find("[")
    end = raw.rfind("]")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return None


def _classify_llm_prompt(names: List[str], config: dict) -> str:
    definitions = build_category_definitions(config)
    category_names = [d["name"] for d in definitions]
    return (
        "你是商品归类器。对每个商品，先判断它「实际在卖什么」，"
        "再从分类定义中选最精准的一个分类名。\n"
        "硬性要求：\n"
        "1. 按商品实质归类，不要被标题里偶然出现的关键字误导。\n"
        "2. 接码/验证码/SMS/子邮箱/隐私邮箱/绑定邮箱等辅助工具，"
        "即使含 plus/team/pro/free，也不是对应账号分类。\n"
        "3. 中转站、镜像站、API 中转、标题含「xx刀/20刀/plus20刀」"
        "的额度或中转套餐（如「plus20刀不限时」），"
        "不是 Plus/Team/Pro 会员号，应归入兜底分类。\n"
        "4. CDK、卡头、次卡、直卡、支付链接、虚拟卡、"
        "「开通plus必备」等支付/开通向导类商品，"
        "即使标题含 plus，也不是 Plus 会员号，应归入兜底分类。\n"
        "5. 每个商品只能选一个分类，必须从可选分类名中原样选择。\n"
        "6. 只返回 JSON 字符串数组，长度与商品列表相同，不要其他文字。\n"
        f"分类定义：{json.dumps(definitions, ensure_ascii=False)}\n"
        f"可选分类名：{json.dumps(category_names, ensure_ascii=False)}\n"
        f"商品列表：{json.dumps(names, ensure_ascii=False)}"
    )


def call_classify_llm(names: List[str], config: dict) -> Optional[List[str]]:
    """OpenAI 兼容接口批量归类；失败返回 None。"""
    if not names:
        return []
    cfg = get_classify_llm_config(config)
    prompt = _classify_llm_prompt(names, config)
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        return None
    url = f"{base_url}/chat/completions"
    payload = {
        "model": cfg.get("model") or "gpt-4o-mini",
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": "只输出 JSON 字符串数组，按商品实质精准分类。",
            },
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.get('api_key', '')}",
    }
    timeout = float(cfg.get("timeout_sec") or 60)
    log(
        f"模型归类请求: {len(names)} 个商品，"
        f"model={payload['model']} url={url}"
    )
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = _read_http_json(resp)
        content = (
            ((body.get("choices") or [{}])[0].get("message") or {}).get(
                "content"
            )
            or ""
        )
        labels = _extract_json_array(content)
        if labels is None:
            log(f"模型归类返回无法解析: {str(content)[:200]}")
            return None
        log(f"模型归类成功: {len(labels)} 个")
        return [str(x).strip() for x in labels]
    except Exception as e:
        log(f"模型归类请求失败: {e}")
        return None


def classify_names(
    names: List[str],
    config: dict,
    name_cache: Optional[Dict[str, Tuple[str, str, str]]] = None,
) -> List[Tuple[str, str, str]]:
    """批量归类：高置信关键字直出；低置信才调模型。

    返回 (分类, source, detail)：
    - source: keyword | llm
    - detail: rule_high | rule_low | llm | name_cache
    """
    if not names:
        return []

    cache: Dict[str, Tuple[str, str, str]] = dict(name_cache or {})
    results: List[Tuple[str, str, str]] = [("", "", "")] * len(names)
    pending_indexes: List[int] = []

    for i, name in enumerate(names):
        cached = cache.get(name)
        if cached is not None:
            # 同名复用时标记为 name_cache，保留原 source
            label, source, _detail = cached
            results[i] = (label, source, "name_cache")
            continue
        label, decision = classify_keyword_decision(name, config)
        if decision == "high" or not is_classify_llm_enabled(config):
            detail = "rule_high" if decision == "high" else "rule_low"
            results[i] = (label, "keyword", detail)
            cache[name] = results[i]
            continue
        pending_indexes.append(i)

    if not pending_indexes:
        return results

    pending_names = [names[i] for i in pending_indexes]
    unique_names: List[str] = []
    seen: Set[str] = set()
    for name in pending_names:
        if name in seen or name in cache:
            continue
        seen.add(name)
        unique_names.append(name)

    cfg = get_classify_llm_config(config)
    batch_size = max(1, int(cfg.get("batch_size") or 20))
    valid = set(get_category_order(config))

    for start in range(0, len(unique_names), batch_size):
        chunk = unique_names[start : start + batch_size]
        labels = call_classify_llm(chunk, config)
        if labels is None or len(labels) != len(chunk):
            log(f"模型归类本批失败，{len(chunk)} 个改用关键字")
            for name in chunk:
                label, decision = classify_keyword_decision(name, config)
                detail = "rule_high" if decision == "high" else "rule_low"
                cache[name] = (label, "keyword", detail)
            continue
        for name, label in zip(chunk, labels):
            if (
                label in _ACCOUNT_CATEGORY_HINTS
                and is_non_account_gpt_product(name)
            ):
                fb, decision = classify_keyword_decision(name, config)
                detail = "rule_high" if decision == "high" else "rule_low"
                cache[name] = (fb, "keyword", detail)
            elif label in valid:
                cache[name] = (label, "llm", "llm")
            else:
                fb, decision = classify_keyword_decision(name, config)
                detail = "rule_high" if decision == "high" else "rule_low"
                cache[name] = (fb, "keyword", detail)

    for i in pending_indexes:
        name = names[i]
        label, source, detail = cache[name]
        results[i] = (label, source, detail)

    # 同批重复标题：第二次及以后记为 name_cache
    seen_name: Set[str] = set()
    for i in pending_indexes:
        name = names[i]
        label, source, detail = results[i]
        if name in seen_name:
            results[i] = (label, source, "name_cache")
        else:
            seen_name.add(name)
    return results


_ACCOUNT_CATEGORY_HINTS = ("Plus号", "Team号", "Pro号", "免费号")

CLASSIFY_DETAIL_LABELS = {
    "rule_high": "规则-高置信",
    "rule_low": "规则-低置信/兜底",
    "llm": "模型",
    "sku_cache": "缓存-SKU",
    "name_cache": "缓存-同名",
}


def classify_detail_label(detail: str) -> str:
    return CLASSIFY_DETAIL_LABELS.get(detail, detail or "未知")


def build_name_classify_cache(
    old_state: Optional[dict], config: dict
) -> Dict[str, Tuple[str, str, str]]:
    """按商品标题复用历史分类；同名优先保留 llm 结果。"""
    cache: Dict[str, Tuple[str, str, str]] = {}
    if not old_state:
        return cache
    valid = set(get_category_order(config))
    for item in old_state.values():
        name = item.get("name") or ""
        label = item.get("type")
        if not name or label not in valid:
            continue
        if is_non_account_gpt_product(name) and label in _ACCOUNT_CATEGORY_HINTS:
            continue
        source = item.get("classify_source") or "keyword"
        detail = item.get("classify_detail") or (
            "llm" if source == "llm" else "rule_high"
        )
        prev = cache.get(name)
        if prev is None or (source == "llm" and prev[1] != "llm"):
            cache[name] = (label, source, detail)
    return cache


def cached_classify_label(
    sku: str,
    name: str,
    old_state: Optional[dict],
    config: dict,
) -> Optional[Tuple[str, str, str]]:
    """店铺+商品已归类且名称未变时复用。返回 (分类, source, detail)。"""
    if not old_state or not sku:
        return None
    cached = old_state.get(sku)
    if not cached:
        return None
    if cached.get("name") != name:
        return None
    label = cached.get("type")
    if label not in set(get_category_order(config)):
        return None
    if is_non_account_gpt_product(name) and label in _ACCOUNT_CATEGORY_HINTS:
        return None
    source = cached.get("classify_source") or "keyword"
    detail = "sku_cache"
    return label, source, detail


def reclassify_state(state: dict, config: dict) -> int:
    """修正失效分类，并纠正非账号本体被误归到账号类的项。"""
    valid_labels = set(get_category_order(config))
    changed = 0
    for item in state.values():
        name = item.get("name", "")
        current = item.get("type")
        need_fix = current not in valid_labels or (
            is_non_account_gpt_product(name)
            and current in _ACCOUNT_CATEGORY_HINTS
        )
        if not need_fix:
            # 补齐历史缺失的识别详情
            if not item.get("classify_detail"):
                src = item.get("classify_source") or "keyword"
                detail = "llm" if src == "llm" else "rule_high"
                item["classify_detail"] = detail
                item["classify_label"] = classify_detail_label(detail)
                changed += 1
            elif not item.get("classify_label"):
                item["classify_label"] = classify_detail_label(
                    item.get("classify_detail", "")
                )
                changed += 1
            continue
        label, decision = classify_keyword_decision(name, config)
        if label not in valid_labels:
            continue
        detail = "rule_high" if decision == "high" else "rule_low"
        item["type"] = label
        item["classify_source"] = "keyword"
        item["classify_detail"] = detail
        item["classify_label"] = classify_detail_label(detail)
        changed += 1
    return changed


def is_valid_product(product: dict) -> bool:
    name = (product.get("name") or "").strip()
    link = (product.get("link") or "").strip()
    goods_key = (product.get("goods_key") or "").strip()
    price = product.get("price")
    if not name or not link or not goods_key:
        return False
    if not isinstance(price, (int, float)) or price < 0:
        return False
    if not link.startswith("http"):
        return False
    return True


def get_max_push_price(config: dict) -> Optional[float]:
    value = config.get("max_push_price")
    if value is None:
        value = (config.get("alert_rules") or {}).get("max_push_price")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_min_push_price(config: dict) -> Optional[float]:
    value = config.get("min_push_price")
    if value is None:
        value = (config.get("alert_rules") or {}).get("min_push_price")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_category_max_push_price(config: dict, category: str) -> Optional[float]:
    for cat in get_categories(config):
        if cat.get("name") != category:
            continue
        value = cat.get("max_push_price")
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                break
    return get_max_push_price(config)


def is_within_push_price(
    price,
    config: dict,
    category: Optional[str] = None,
) -> bool:
    if not isinstance(price, (int, float)):
        return False
    amount = float(price)
    min_price = get_min_push_price(config)
    if min_price is not None and amount < min_price:
        return False
    if category:
        max_price = get_category_max_push_price(config, category)
    else:
        max_price = get_max_push_price(config)
    if max_price is not None and amount > max_price:
        return False
    return True


def fetch_outside_push_schedule_enabled(config: dict) -> bool:
    return bool(config.get("fetch_outside_push_schedule", True))


def make_sku(platform: str, token: str, goods_key: str) -> str:
    return f"{shop_id(platform, token)}:{goods_key}"


def filter_and_classify(
    items: List[dict],
    config: dict,
    *,
    platform: str = "ldxp",
    shop_token: str = "",
    old_state: Optional[dict] = None,
) -> Dict[str, List[dict]]:
    order = get_category_order(config)
    grouped: Dict[str, List[dict]] = {k: [] for k in order}
    old_state = old_state or {}
    name_cache = build_name_classify_cache(old_state, config)
    pending: List[dict] = []
    for item in items:
        if not is_gpt_product(item, config):
            continue
        # 缺货（stock<=0）仍保留进池数据，真正接口里不存在的才会在 replace 时删除
        stock = (item.get("extend") or {}).get("stock_count", 0)
        try:
            stock = int(stock)
        except (TypeError, ValueError):
            stock = 0
        product = {
            "name": item.get("name", ""),
            "price": item.get("price", 0),
            "stock": stock,
            "link": item.get("link", ""),
            "goods_key": item.get("goods_key", ""),
        }
        if not is_valid_product(product):
            continue
        sku = (
            make_sku(platform, shop_token, product["goods_key"])
            if shop_token
            else ""
        )
        cached = cached_classify_label(
            sku, product["name"], old_state, config
        )
        if cached is None:
            cached = name_cache.get(product["name"])
            if cached is not None:
                label, source, _prev_detail = cached
                cached = (label, source, "name_cache")
        if cached is None:
            pending.append(product)
            continue
        label, source, detail = cached
        name_cache[product["name"]] = (label, source, detail)
        if label not in grouped:
            continue
        if not is_within_push_price(product["price"], config, label):
            continue
        product["classify_source"] = source
        product["classify_detail"] = detail
        grouped[label].append(product)

    if pending:
        classified = classify_names(
            [p["name"] for p in pending], config, name_cache=name_cache
        )
        for product, (label, source, detail) in zip(pending, classified):
            name_cache[product["name"]] = (label, source, detail)
            if label not in grouped:
                continue
            if not is_within_push_price(product["price"], config, label):
                continue
            product["classify_source"] = source
            product["classify_detail"] = detail
            grouped[label].append(product)

    for label in grouped:
        # 有货优先，同价按原价序
        grouped[label].sort(key=lambda x: (x["stock"] <= 0, x["price"]))
    return grouped


def build_snapshot(
    platform: str,
    shop_token: str,
    shop_name: str,
    grouped: Dict[str, List[dict]],
) -> dict:
    sid = shop_id(platform, shop_token)
    updated_at = current_timestamp()
    items = {}
    for label, products in grouped.items():
        for p in products:
            key = f"{sid}:{p['goods_key']}"
            detail = p.get("classify_detail") or (
                "llm" if p.get("classify_source") == "llm" else "rule_high"
            )
            items[key] = {
                "shop": sid,
                "shop_name": shop_name,
                "platform": platform,
                "type": label,
                "name": p["name"],
                "price": p["price"],
                "stock": p["stock"],
                "link": p["link"],
                "classify_source": p.get("classify_source", "keyword"),
                "classify_detail": detail,
                "classify_label": classify_detail_label(detail),
                "last_updated_at": updated_at,
            }
    return items


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    migrated = {}
    for key, val in state.items():
        if key.count(":") == 1:
            token, goods_key = key.split(":", 1)
            new_key = f"ldxp:{token}:{goods_key}"
            migrated[new_key] = {
                **val,
                "shop": f"ldxp:{token}",
                "platform": "ldxp",
                "shop_name": val.get("shop_name", token),
            }
        else:
            migrated[key] = val
    return migrated


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_sent() -> dict:
    if not SENT_FILE.exists():
        return {}
    with open(SENT_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_sent(sent: dict) -> None:
    SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(sent, f, ensure_ascii=False, indent=2)


def item_signature(item: dict) -> str:
    kind = item.get("kind", "列表")
    return f"{kind}:{item.get('price')}:{item.get('price_change', '')}"


def already_sent_item(item: dict, sent_state: dict) -> bool:
    sku = item.get("sku")
    if not sku:
        return False
    prev = sent_state.get(sku)
    if not prev:
        return False
    return prev.get("signature") == item_signature(item)


def sanitize_pool(
    pool: Dict[str, List[str]], catalog: dict, config: dict
) -> Dict[str, List[str]]:
    return {
        label: [sku for sku in pool.get(label, []) if sku in catalog]
        for label in get_category_order(config)
    }


def in_current_catalog(sku: str, catalog: dict) -> bool:
    return bool(sku) and sku in catalog


def filter_sendable(
    items: List[dict],
    sent_state: dict,
    catalog: Optional[dict] = None,
    config: Optional[dict] = None,
) -> List[dict]:
    result: List[dict] = []
    for item in items:
        sku = item.get("sku") or ""
        if not sku:
            continue
        kind = item.get("kind")
        if kind != "店铺SKU变化" and catalog is not None and not in_current_catalog(
            sku, catalog
        ):
            continue
        if kind not in ("店铺SKU变化", "SKU下架") and not is_valid_product(item):
            continue
        if (
            config is not None
            and kind not in ("店铺SKU变化", "SKU下架")
            and not is_within_push_price(
                item.get("price"), config, item.get("type")
            )
        ):
            continue
        if config is not None and not is_category_push_enabled(
            config, item.get("type", "")
        ):
            continue
        if (
            config is not None
            and catalog is not None
            and kind not in ("店铺SKU变化", "SKU下架")
        ):
            catalog_item = catalog.get(sku)
            if catalog_item and catalog_item.get("stock", 0) <= 0:
                continue
            if catalog_item and not is_pool_item_fresh(catalog_item, config):
                continue
        if already_sent_item(item, sent_state):
            continue
        result.append(item)
    return result


def record_sent_items(sent_state: dict, items: List[dict]) -> None:
    sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item in items:
        sku = item.get("sku")
        if not sku:
            continue
        sent_state[sku] = {
            "price": item["price"],
            "kind": item.get("kind", "列表"),
            "signature": item_signature(item),
            "sent_at": sent_at,
            "name": item.get("name", ""),
            "type": item.get("type", ""),
            "shop": item.get("shop", ""),
        }


def prune_sent_state(sent_state: dict, active_skus: Set[str]) -> None:
    for sku in list(sent_state):
        if sku not in active_skus:
            del sent_state[sku]


def sku_belongs_to_shop(sku: str, shop_sid: str) -> bool:
    return sku.startswith(f"{shop_sid}:")


def purge_shop_stale_skus(
    shop_sid: str,
    active_skus: Set[str],
    *,
    state: dict,
    sent_state: dict,
    pool: Dict[str, List[str]],
    config: dict,
) -> Set[str]:
    """移除某店铺中已不在商品列表里的 SKU（prices / sent / top10 池）。"""
    removed: Set[str] = set()

    def is_stale(sku: str) -> bool:
        return sku_belongs_to_shop(sku, shop_sid) and sku not in active_skus

    for sku in list(state.keys()):
        if is_stale(sku):
            del state[sku]
            removed.add(sku)

    for sku in list(sent_state.keys()):
        if is_stale(sku):
            del sent_state[sku]
            removed.add(sku)

    for label in get_category_order(config):
        kept: List[str] = []
        for sku in pool.get(label, []):
            if is_stale(sku):
                removed.add(sku)
            else:
                kept.append(sku)
        pool[label] = kept

    return removed


def replace_shop_snapshot(
    shop_sid: str,
    snapshot: dict,
    *,
    state: dict,
    sent_state: dict,
    pool: Dict[str, List[str]],
    config: dict,
) -> Set[str]:
    """用最新抓取结果替换某店铺 SKU，并清理已下架项。"""
    for sku in list(state.keys()):
        if sku_belongs_to_shop(sku, shop_sid):
            del state[sku]
    removed = purge_shop_stale_skus(
        shop_sid,
        set(snapshot.keys()),
        state=state,
        sent_state=sent_state,
        pool=pool,
        config=config,
    )
    state.update(snapshot)
    return removed


def load_top10_pool(config: dict) -> Dict[str, List[str]]:
    order = get_category_order(config)
    if not TOP10_FILE.exists():
        return {label: [] for label in order}
    with open(TOP10_FILE, encoding="utf-8") as f:
        pool = json.load(f)
    result: Dict[str, List[str]] = {}
    for label in order:
        entries = pool.get(label, [])
        skus: List[str] = []
        for entry in entries:
            if isinstance(entry, str):
                skus.append(entry)
            elif isinstance(entry, dict) and entry.get("sku"):
                skus.append(str(entry["sku"]))
        result[label] = skus
    return result


def save_top10_pool(
    pool: Dict[str, List[str]],
    state: Optional[dict] = None,
) -> None:
    """保存 TopN 池；附带库存与归类来源详情。"""
    TOP10_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = state or {}
    payload: Dict[str, List[dict]] = {}
    for label, skus in pool.items():
        rows: List[dict] = []
        for sku in skus:
            item = state.get(sku) or {}
            detail = item.get("classify_detail") or (
                "llm" if item.get("classify_source") == "llm" else "rule_high"
            )
            rows.append(
                {
                    "sku": sku,
                    "name": item.get("name", ""),
                    "price": item.get("price"),
                    "stock": item.get("stock", 0),
                    "shop": item.get("shop", ""),
                    "classify_source": item.get("classify_source", "keyword"),
                    "classify_detail": detail,
                    "classify_label": item.get("classify_label")
                    or classify_detail_label(detail),
                }
            )
        payload[label] = rows
    with open(TOP10_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def config_shop_id_set(config: dict) -> Set[str]:
    return {
        shop_id(shop.get("platform", "ldxp"), shop["token"])
        for shop in config.get("shops", [])
        if shop.get("token")
    }


def purge_missing_shop_products(
    state: dict,
    sent_state: dict,
    pool: Dict[str, List[str]],
    config: dict,
) -> Set[str]:
    """配置中已不存在的店铺：删除其 prices / sent / top10 商品。"""
    valid_shops = config_shop_id_set(config)
    removed: Set[str] = set()
    for sku in list(state.keys()):
        shop_sid = (state[sku] or {}).get("shop") or ""
        if not shop_sid or shop_sid not in valid_shops:
            del state[sku]
            removed.add(sku)
    for sku in list(sent_state.keys()):
        if sku in removed:
            del sent_state[sku]
            continue
        parts = sku.split(":")
        if len(parts) >= 3:
            sid = f"{parts[0]}:{parts[1]}"
            if sid not in valid_shops:
                del sent_state[sku]
                removed.add(sku)
    for label in get_category_order(config):
        kept: List[str] = []
        for sku in pool.get(label, []):
            if sku in removed or sku not in state:
                removed.add(sku)
                continue
            kept.append(sku)
        pool[label] = kept
    return removed


def pool_entry_eligible(item: dict, config: Optional[dict] = None) -> bool:
    price = item.get("price")
    if not isinstance(price, (int, float)) or price < 0:
        return False
    if item.get("stock", 0) <= 0:
        return False
    if config is not None and not is_within_push_price(
        price, config, item.get("type")
    ):
        return False
    if config is not None and not is_pool_item_fresh(item, config):
        return False
    return True


def purge_state_out_of_price_range(state: dict, config: dict) -> int:
    """移除不在价格区间内的商品（按分类上限 + 全局最低价）。"""
    removed = 0
    for sku in list(state):
        item = state[sku]
        if is_within_push_price(
            item.get("price"), config, item.get("type")
        ):
            continue
        del state[sku]
        removed += 1
    return removed


def purge_state_over_max_price(state: dict, config: dict) -> int:
    return purge_state_out_of_price_range(state, config)


def compute_top_pool(state: dict, config: dict) -> Dict[str, List[str]]:
    """每类取当前有库存商品中价格最低的 top_n 个，与涨跌无关。"""
    order = get_category_order(config)
    grouped: Dict[str, List[Tuple[str, float]]] = {k: [] for k in order}
    for sku, item in state.items():
        label = item.get("type", "其他GPT")
        if label not in grouped:
            continue
        if not pool_entry_eligible(item, config):
            continue
        grouped[label].append((sku, item["price"]))
    pool: Dict[str, List[str]] = {}
    for label in order:
        ranked = sorted(grouped[label], key=lambda x: (x[1], x[0]))
        top_n = get_category_top_n(config, label)
        pool[label] = [sku for sku, _ in ranked[:top_n]]
    return pool


def pool_skus(pool: Dict[str, List[str]]) -> Set[str]:
    return {sku for skus in pool.values() for sku in skus}


def pool_shops(pool: Dict[str, List[str]], state: dict) -> Set[str]:
    shops: Set[str] = set()
    for sku in pool_skus(pool):
        item = state.get(sku)
        if item:
            shops.add(item["shop"])
    return shops


def pool_changed(
    old_pool: Dict[str, List[str]],
    new_pool: Dict[str, List[str]],
    config: dict,
) -> bool:
    for label in get_category_order(config):
        if old_pool.get(label, []) != new_pool.get(label, []):
            return True
    return False


def affected_pool_categories(
    old_pool: Dict[str, List[str]],
    new_pool: Dict[str, List[str]],
    config: dict,
) -> Set[str]:
    changed: Set[str] = set()
    for label in get_category_order(config):
        if old_pool.get(label, []) != new_pool.get(label, []):
            changed.add(label)
    return changed


def state_to_product(sku: str, item: dict) -> dict:
    platform = item.get("platform", "ldxp")
    shop_name = item.get("shop_name", item.get("shop", ""))
    channel = shop_name if platform == "ldxp" else f"{shop_name}({platform})"
    return {
        "sku": sku,
        "goods_key": sku.split(":")[-1],
        "type": item["type"],
        "name": item["name"],
        "shop": channel,
        "price": item["price"],
        "link": item["link"],
        "shop_link": shop_page_url_from_item(item),
    }


def products_from_pool(
    pool: Dict[str, List[str]], state: dict, config: dict
) -> Dict[str, List[dict]]:
    by_category: Dict[str, List[dict]] = {}
    for label in get_category_order(config):
        products: List[dict] = []
        for sku in pool.get(label, []):
            if not in_current_catalog(sku, state):
                continue
            item = state[sku]
            if item.get("stock", 0) <= 0:
                continue
            if not is_pool_item_fresh(item, config):
                continue
            product = state_to_product(sku, item)
            if not is_valid_product(product):
                continue
            if not is_within_push_price(product["price"], config, label):
                continue
            products.append(product)
        if products:
            by_category[label] = products
    return by_category


def _cheap_threshold(rules: dict, category: str) -> float:
    return rules["cheap_price_thresholds"].get(category, float("inf"))


def _is_cheap(price: float, category: str, rules: dict) -> bool:
    return price <= _cheap_threshold(rules, category)


def _alert_item(
    key: str,
    kind: str,
    item: dict,
    *,
    price_change: str,
    old_price: Optional[float] = None,
) -> dict:
    alert = {
        "sku": key,
        "kind": kind,
        "type": item["type"],
        "name": item["name"],
        "shop": item.get("shop_name", item["shop"]),
        "price": item["price"],
        "price_change": price_change,
        "link": item["link"],
        "shop_link": shop_page_url_from_item(item),
        "goods_key": key.split(":")[-1],
    }
    if old_price is not None:
        alert["old_price"] = old_price
    return alert


def _group_skus_by_shop(state: dict) -> Dict[str, dict]:
    by_shop: Dict[str, dict] = {}
    for sku, item in state.items():
        shop_id = item["shop"]
        entry = by_shop.setdefault(
            shop_id,
            {"name": item.get("shop_name", shop_id), "skus": set()},
        )
        entry["skus"].add(sku)
    return by_shop


def detect_shop_sku_changes(old: dict, new: dict) -> List[dict]:
    alerts: List[dict] = []
    old_shops = _group_skus_by_shop(old)
    new_shops = _group_skus_by_shop(new)

    for shop_id in set(old_shops) | set(new_shops):
        old_skus = old_shops.get(shop_id, {}).get("skus", set())
        new_skus = new_shops.get(shop_id, {}).get("skus", set())
        added = len(new_skus - old_skus)
        removed = len(old_skus - new_skus)
        if not added and not removed:
            continue

        shop_name = (new_shops.get(shop_id) or old_shops[shop_id])["name"]
        _platform, token = shop_id.split(":", 1)
        shop_url = shop_page_url_from_shop_id(shop_id)
        alerts.append(
            {
                "sku": f"shop_change:{shop_id}",
                "kind": "店铺SKU变化",
                "type": "店铺",
                "name": f"SKU新增{added}个/移除{removed}个",
                "shop": shop_name,
                "price": 0,
                "price_change": f"+{added}/-{removed}",
                "link": shop_url,
                "shop_link": shop_url,
                "goods_key": token,
            }
        )
    return alerts


def detect_alerts(old: dict, new: dict, rules: dict) -> List[dict]:
    alerts: List[dict] = []
    all_keys = set(old) | set(new)

    for key in all_keys:
        o, n = old.get(key), new.get(key)
        if o is None and n is not None:
            change = "新增"
            if _is_cheap(n["price"], n["type"], rules):
                change = "新增(低价)"
            alerts.append(_alert_item(key, "SKU上架", n, price_change=change))
            continue

        if o is not None and n is None:
            alerts.append(
                {
                    "sku": key,
                    "kind": "SKU下架",
                    "type": o["type"],
                    "name": o["name"],
                    "shop": o.get("shop_name", o["shop"]),
                    "price": o["price"],
                    "price_change": "下架",
                    "link": o["link"],
                    "shop_link": shop_page_url_from_item(o),
                    "goods_key": key.split(":")[-1],
                }
            )
            continue

        if not o or not n:
            continue

        if o["name"] != n["name"]:
            alerts.append(
                _alert_item(
                    key,
                    "SKU更名",
                    n,
                    price_change=f"更名",
                )
            )

        if rules.get("push_on_stock_change", True) and o.get("stock") != n.get(
            "stock"
        ):
            alerts.append(
                _alert_item(
                    key,
                    "库存变化",
                    n,
                    price_change=f"库存{o['stock']}→{n['stock']}",
                )
            )

        price_change = _format_price_change(n["price"], o["price"])
        if n["price"] < o["price"]:
            alerts.append(
                _alert_item(
                    key, "降价", n, price_change=price_change, old_price=o["price"]
                )
            )
        elif n["price"] > o["price"]:
            alerts.append(
                _alert_item(
                    key, "涨价", n, price_change=price_change, old_price=o["price"]
                )
            )
        elif rules["push_on_cheap_deal"] and _is_cheap(
            n["price"], n["type"], rules
        ) and not _is_cheap(o["price"], o["type"], rules):
            alerts.append(
                _alert_item(
                    key, "低价", n, price_change=price_change, old_price=o["price"]
                )
            )

    if rules.get("push_on_sku_change", True):
        alerts.extend(detect_shop_sku_changes(old, new))

    return alerts


def detect_top10_price_drops(
    new_pool: Dict[str, List[str]],
    old_state: dict,
    new_state: dict,
    config: dict,
) -> List[dict]:
    alerts: List[dict] = []
    for sku in pool_skus(new_pool):
        if not in_current_catalog(sku, new_state):
            continue
        old_item = old_state.get(sku)
        new_item = new_state[sku]
        if not is_category_push_enabled(config, new_item.get("type", "")):
            continue
        if not is_within_push_price(new_item.get("price"), config, new_item.get("type")):
            continue
        if not is_pool_item_fresh(new_item, config):
            continue
        if not old_item or new_item["price"] >= old_item["price"]:
            continue
        price_change = _format_price_change(new_item["price"], old_item["price"])
        alerts.append(
            _alert_item(
                sku,
                "降价",
                new_item,
                price_change=price_change,
                old_price=old_item["price"],
            )
        )
    return alerts


def detect_top10_new_entries(
    old_pool: Dict[str, List[str]],
    new_pool: Dict[str, List[str]],
    old_state: dict,
    new_state: dict,
    config: dict,
) -> List[dict]:
    """检测新进入某类 TopN 池的 SKU（N 取自 categories.top_n）。"""
    if not pool_skus(old_pool):
        return []

    alerts: List[dict] = []
    for label in get_category_order(config):
        if not is_category_push_enabled(config, label):
            continue
        top_n = get_category_top_n(config, label)
        old_top = list(old_pool.get(label, []))[:top_n]
        new_top = list(new_pool.get(label, []))[:top_n]
        if not old_top:
            continue
        old_skus = set(old_top)
        kind = new_pool_entry_kind(config, label)
        for sku in new_top:
            if sku in old_skus:
                continue
            if not in_current_catalog(sku, new_state):
                continue
            new_item = new_state[sku]
            if not is_within_push_price(
                new_item.get("price"), config, new_item.get("type")
            ):
                continue
            if not is_pool_item_fresh(new_item, config):
                continue
            old_item = old_state.get(sku)
            if old_item:
                price_change = _format_price_change(
                    new_item["price"], old_item["price"]
                )
                old_price = old_item["price"]
            else:
                price_change = "新增"
                old_price = None
            alerts.append(
                _alert_item(
                    sku,
                    kind,
                    new_item,
                    price_change=price_change,
                    old_price=old_price,
                )
            )
    return alerts


def detect_top10_pool_alerts(
    old_pool: Dict[str, List[str]],
    new_pool: Dict[str, List[str]],
    old_state: dict,
    new_state: dict,
    config: dict,
) -> List[dict]:
    rules = config.get("alert_rules", {})
    new_entries: List[dict] = []
    if rules.get("push_on_top10_new_entry", True):
        new_entries = detect_top10_new_entries(
            old_pool, new_pool, old_state, new_state, config
        )
    new_entry_skus = {alert["sku"] for alert in new_entries}

    price_drops: List[dict] = []
    if rules.get("push_on_price_drop", True):
        price_drops = [
            alert
            for alert in detect_top10_price_drops(
                new_pool, old_state, new_state, config
            )
            if alert["sku"] not in new_entry_skus
        ]
    return new_entries + price_drops


def _parse_hm(value: str) -> int:
    hour, minute = value.strip().split(":", 1)
    return int(hour) * 60 + int(minute)


def _weekday_config_to_python(day: int) -> int:
    """配置 1=周一 … 7=周日，转为 Python weekday（0=周一）。"""
    if day == 7:
        return 6
    return day - 1


WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def push_schedule_reason(config: dict, now: Optional[datetime] = None) -> Optional[str]:
    schedule = config.get("push_schedule")
    if not schedule:
        return None
    now = now or datetime.now()

    weekdays = schedule.get("weekdays")
    if weekdays is not None:
        allowed = {_weekday_config_to_python(int(day)) for day in weekdays}
        if now.weekday() not in allowed:
            allowed_text = "、".join(
                WEEKDAY_LABELS[_weekday_config_to_python(int(day))]
                for day in sorted(weekdays, key=int)
            )
            return f"今日不可提醒（仅 {allowed_text}）"

    time_ranges = schedule.get("time_ranges") or []
    if not time_ranges:
        return None

    current = now.hour * 60 + now.minute
    in_range = False
    range_texts: List[str] = []
    for item in time_ranges:
        start = _parse_hm(item["start"])
        end = _parse_hm(item["end"])
        range_texts.append(f"{item['start']}-{item['end']}")
        if start <= end:
            if start <= current <= end:
                in_range = True
        elif current >= start or current <= end:
            in_range = True
    if not in_range:
        return f"当前不在提醒时段（{' / '.join(range_texts)}）"
    return None


def is_in_push_schedule(config: dict, now: Optional[datetime] = None) -> bool:
    return push_schedule_reason(config, now) is None


def should_push(
    alerts: List[dict],
    force_push: bool,
    old_state: dict,
    config: dict,
    *,
    daily_full: bool = False,
    daily_refresh_complete: bool = True,
) -> bool:
    if force_push:
        return True
    if not old_state:
        return False
    if daily_full:
        return True
    if not daily_refresh_complete:
        return False
    return any(
        (
            a.get("kind") == "降价"
            or is_new_pool_entry_kind(a.get("kind", ""))
        )
        and is_category_push_enabled(config, a.get("type", ""))
        for a in alerts
    )


def _escape_table_cell(text: str) -> str:
    return text.replace("|", "｜").replace("\n", " ").strip()


def _format_price_change(current: float, previous: Optional[float]) -> str:
    if previous is None:
        return "新增"
    diff = round(current - previous, 4)
    if diff == 0:
        return "持平"
    if diff < 0:
        return f"下浮¥{abs(diff):g}"
    return f"上浮¥{diff:g}"


def _resolve_previous_price(
    sku: str, sent_state: dict, old_state: dict
) -> Optional[float]:
    if sku in sent_state:
        return sent_state[sku].get("price")
    if sku in old_state:
        return old_state[sku].get("price")
    return None


def enrich_price_change(
    item: dict, sent_state: dict, old_state: dict
) -> dict:
    sku = item.get("sku", "")
    previous = _resolve_previous_price(sku, sent_state, old_state)
    item["price_change"] = _format_price_change(item["price"], previous)
    return item


def _markdown_link(text: str, url: str) -> str:
    label = _escape_table_cell(text).replace("[", "【").replace("]", "】")
    return f"[{label}]({url})"


def _format_table_row(product: dict) -> str:
    name = _markdown_link(product["name"], product["link"])
    shop_link = product.get("shop_link") or product["link"]
    shop = _markdown_link(product["shop"], shop_link)
    price = f"¥{product['price']}"
    change = _escape_table_cell(product.get("price_change", "—"))
    return f"| {name} | {shop} | {price} | {change} |"


def _format_alert_row(alert: dict) -> str:
    kind = _escape_table_cell(alert["kind"])
    category = _escape_table_cell(alert["type"])
    name = _markdown_link(alert["name"], alert["link"])
    shop_link = alert.get("shop_link") or alert["link"]
    shop = _markdown_link(alert["shop"], shop_link)
    price = (
        "—"
        if alert["kind"] in ("店铺SKU变化",)
        else f"¥{alert['price']}"
    )
    change = _escape_table_cell(
        alert.get("price_change")
        or _format_price_change(alert["price"], alert.get("old_price"))
    )
    return f"| {kind} | {category} | {name} | {shop} | {price} | {change} |"


def _collect_products_by_category(
    shops_data: List[Tuple[str, str, str, dict]],
    config: dict,
) -> Dict[str, List[dict]]:
    order = get_category_order(config)
    grouped: Dict[str, List[dict]] = {k: [] for k in order}
    for shop_name, shop_token, platform, shop_grouped in shops_data:
        channel = shop_name if platform == "ldxp" else f"{shop_name}({platform})"
        for label in order:
            for product in shop_grouped.get(label, []):
                if not is_valid_product(product):
                    continue
                grouped[label].append(
                    {
                        "sku": make_sku(platform, shop_token, product["goods_key"]),
                        "goods_key": product["goods_key"],
                        "type": label,
                        "name": product["name"],
                        "shop": channel,
                        "price": product["price"],
                        "link": product["link"],
                        "shop_link": shop_page_url(platform, shop_token),
                    }
                )
    for label in grouped:
        grouped[label].sort(key=lambda x: x["price"])
        grouped[label] = grouped[label][: get_category_top_n(config, label)]
    return grouped


def _format_category_markdown(label: str, products: List[dict], top_n: int) -> str:
    count = len(products)
    lines = [
        f"### {label} (低价Top{count}/{top_n})",
        "",
        TABLE_HEADER,
        TABLE_SEPARATOR,
    ]
    lines.extend(_format_table_row(product) for product in products)
    return "\n".join(lines)


def _build_feishu_card(title: str, markdown: str) -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "body": {
                "direction": "vertical",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": markdown,
                        "text_align": "left",
                    }
                ],
            },
        },
    }


def _format_alert_batches(alerts: List[dict], batch_size: int = 10) -> List[str]:
    rows = [_format_alert_row(alert) for alert in alerts]
    batches: List[str] = []
    for index in range(0, len(rows), batch_size):
        chunk = rows[index : index + batch_size]
        suffix = f" ({index // batch_size + 1})" if len(rows) > batch_size else ""
        lines = [f"### 变动提醒{suffix}", ""]
        lines.extend([ALERT_TABLE_HEADER, ALERT_TABLE_SEPARATOR, *chunk])
        batches.append("\n".join(lines))
    return batches


def _card_markdown(payload: dict) -> str:
    return payload["card"]["body"]["elements"][0]["content"]


def _feishu_card_title(payload: dict) -> str:
    header = payload.get("card", {}).get("header", {}) or {}
    title = header.get("title", {}) or {}
    return str(title.get("content", "")).strip()


def _payload_to_qiwei_markdown_v2(payload: dict) -> str:
    """转为企微 markdown_v2 内容（支持表格，见文档 path/99110）。"""
    title = _feishu_card_title(payload)
    body = _card_markdown(payload)
    sections: List[str] = []
    if title:
        sections.extend([f"### {title}", ""])
    sections.append(body)
    return _with_sent_timestamp("\n".join(sections))


def _split_qiwei_markdown(content: str, max_bytes: int = QIWEI_MAX_BYTES) -> List[str]:
    encoded_len = len(content.encode("utf-8"))
    if encoded_len <= max_bytes:
        return [content]

    chunks: List[str] = []
    current: List[str] = []
    for line in content.splitlines():
        candidate = "\n".join([*current, line]) if current else line
        if len(candidate.encode("utf-8")) > max_bytes and current:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _post_qiwei_webhook(webhook_url: str, content: str) -> None:
    body = {"msgtype": "markdown_v2", "markdown_v2": {"content": content}}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(data) > QIWEI_MAX_BYTES:
        raise RuntimeError(f"企微消息超长: {len(data)} 字节")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    _wait_api_interval()
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
    if result.get("errcode") != 0:
        raise RuntimeError(f"企微推送失败: {result}")


def send_qiwei(webhook_url: str, messages: List[dict]) -> None:
    sent_parts = 0
    for payload in messages:
        payload = json.loads(json.dumps(payload, ensure_ascii=False))
        content = _payload_to_qiwei_markdown_v2(payload)
        for part in _split_qiwei_markdown(content):
            _post_qiwei_webhook(webhook_url, part)
            sent_parts += 1
    if sent_parts > len(messages):
        log(f"企微消息已拆分为 {sent_parts} 条发送")


def _with_sent_timestamp(markdown: str) -> str:
    sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"> 发送时间: {sent_at}\n\n{markdown}"


def feishu_openapi_configured(config: dict) -> bool:
    return bool(
        str(config.get("feishu_app_id", "")).strip()
        and str(config.get("feishu_app_secret", "")).strip()
        and str(config.get("feishu_chat_id", "")).strip()
    )


def load_feishu_messages() -> dict:
    if not FEISHU_MSG_FILE.exists():
        return {"daily_full_push_date": "", "days": {}}
    with open(FEISHU_MSG_FILE, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"daily_full_push_date": "", "days": {}}
    data.setdefault("daily_full_push_date", "")
    data.setdefault("days", {})
    return data


def save_feishu_messages(data: dict) -> None:
    FEISHU_MSG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FEISHU_MSG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_daily_first_push(
    config: dict,
    shops_state: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> bool:
    if not config.get("push_schedule"):
        return False
    shops_state = shops_state if shops_state is not None else load_shops_state()
    if not is_daily_refresh_complete(config, shops_state, now):
        return False
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    store = load_feishu_messages()
    return store.get("daily_full_push_date") != today


def mark_daily_full_push(now: Optional[datetime] = None) -> None:
    store = load_feishu_messages()
    store["daily_full_push_date"] = (now or datetime.now()).strftime("%Y-%m-%d")
    save_feishu_messages(store)


def record_feishu_message_ids(message_ids: List[str]) -> None:
    if not message_ids:
        return
    store = load_feishu_messages()
    today = datetime.now().strftime("%Y-%m-%d")
    sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    days = store.setdefault("days", {})
    entries = days.setdefault(today, [])
    for message_id in message_ids:
        entries.append({"message_id": message_id, "sent_at": sent_at})
    save_feishu_messages(store)


def get_feishu_tenant_access_token(config: dict) -> str:
    global _feishu_token_cache
    now = time.time()
    cached = str(_feishu_token_cache.get("token", ""))
    expire_at = float(_feishu_token_cache.get("expire_at", 0))
    if cached and now < expire_at - 60:
        return cached

    app_id = str(config.get("feishu_app_id", "")).strip()
    app_secret = str(config.get("feishu_app_secret", "")).strip()
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    _wait_api_interval()
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
    if result.get("code") != 0:
        raise RuntimeError(f"飞书 token 获取失败: {result}")
    token = result["tenant_access_token"]
    _feishu_token_cache = {
        "token": token,
        "expire_at": now + float(result.get("expire", 7200)),
    }
    return token


def _feishu_openapi_request(
    config: dict,
    method: str,
    path: str,
    *,
    body: Optional[dict] = None,
) -> dict:
    token = get_feishu_tenant_access_token(config)
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(
        f"https://open.feishu.cn/open-apis{path}",
        data=data,
        headers=headers,
        method=method,
    )
    _wait_api_interval()
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def feishu_recall_message(config: dict, message_id: str) -> bool:
    try:
        result = _feishu_openapi_request(
            config, "DELETE", f"/im/v1/messages/{message_id}"
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        log(f"飞书撤回 HTTP 错误 {message_id}: {body}")
        return False
    if result.get("code") != 0:
        log(f"飞书撤回失败 {message_id}: {result}")
        return False
    return True


def recall_expired_feishu_messages(config: dict) -> int:
    """次日撤回前一天及更早的飞书消息（需 Open API 配置）。"""
    if not feishu_openapi_configured(config):
        return 0
    today = datetime.now().date()
    store = load_feishu_messages()
    days: dict = store.setdefault("days", {})
    recalled = 0
    for day_str in list(days.keys()):
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            del days[day_str]
            continue
        if day >= today:
            continue
        for entry in days.get(day_str, []):
            message_id = entry.get("message_id")
            if not message_id:
                continue
            if feishu_recall_message(config, message_id):
                recalled += 1
                log(f"已撤回飞书消息 {message_id} ({day_str})")
        del days[day_str]
    save_feishu_messages(store)
    return recalled


def send_feishu_openapi(config: dict, messages: List[dict]) -> List[str]:
    chat_id = str(config.get("feishu_chat_id", "")).strip()
    receive_id_type = str(config.get("feishu_receive_id_type", "chat_id")).strip()
    message_ids: List[str] = []
    for index, payload in enumerate(messages, start=1):
        payload = json.loads(json.dumps(payload, ensure_ascii=False))
        card = payload["card"]
        card["body"]["elements"][0]["content"] = _with_sent_timestamp(
            _card_markdown(payload)
        )
        content = json.dumps(card, ensure_ascii=False)
        if len(content.encode("utf-8")) > FEISHU_MAX_BYTES:
            raise RuntimeError(f"飞书消息超长: 第{index}条 {len(content)} 字节")
        result = _feishu_openapi_request(
            config,
            "POST",
            f"/im/v1/messages?receive_id_type={receive_id_type}",
            body={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": content,
            },
        )
        if result.get("code") != 0:
            raise RuntimeError(f"飞书 Open API 推送失败: {result}")
        message_id = result.get("data", {}).get("message_id")
        if message_id:
            message_ids.append(message_id)
    return message_ids


def send_feishu(webhook_url: str, messages: List[dict]) -> None:
    for index, payload in enumerate(messages, start=1):
        payload = json.loads(json.dumps(payload, ensure_ascii=False))
        element = payload["card"]["body"]["elements"][0]
        element["content"] = _with_sent_timestamp(_card_markdown(payload))
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(data) > FEISHU_MAX_BYTES:
            raise RuntimeError(f"飞书消息超长: 第{index}条 {len(data)} 字节")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"content-type": "application/json"},
            method="POST",
        )
        _wait_api_interval()
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        if result.get("code") != 0:
            raise RuntimeError(f"飞书推送失败: {result}")


def send_notifications(config: dict, messages: List[dict]) -> List[str]:
    targets = resolve_webhook_targets(config)
    feishu_openapi = feishu_openapi_configured(config)
    if not feishu_openapi and not targets:
        raise RuntimeError(
            "未配置 feishu_webhook_url / feishu Open API / qiwei_webhook_url"
        )

    message_ids: List[str] = []
    if feishu_openapi:
        message_ids = send_feishu_openapi(config, messages)
        log(
            f"已推送飞书 Open API ({len(messages)} 条, "
            f"message_id {len(message_ids)} 个)"
        )
    else:
        for channel, webhook_url in targets:
            if channel == "feishu":
                send_feishu(webhook_url, messages)
                log(f"已推送飞书 ({len(messages)} 条)")

    for channel, webhook_url in targets:
        if channel == "qiwei":
            send_qiwei(webhook_url, messages)
            log(f"已推送企微 ({len(messages)} 条)")

    return message_ids


def _prepare_send_payload(
    new_pool: Dict[str, List[str]],
    new_state: dict,
    alerts: List[dict],
    config: dict,
    sent_state: dict,
    old_state: dict,
    *,
    include_full_top10: bool,
    alerts_only: bool = False,
) -> Tuple[List[dict], Dict[str, List[dict]]]:
    sendable_alerts: List[dict] = []
    if not alerts_only:
        sendable_alerts = [
            enrich_price_change(alert, sent_state, old_state)
            for alert in filter_sendable(
                alerts, sent_state, catalog=new_state, config=config
            )
        ]
    sendable_by_category: Dict[str, List[dict]] = {}
    if include_full_top10:
        by_category = products_from_pool(new_pool, new_state, config)
        for label in get_category_order(config):
            if not is_category_push_enabled(config, label):
                continue
            products = [
                enrich_price_change(item, sent_state, old_state)
                for item in by_category.get(label, [])
            ]
            if products:
                sendable_by_category[label] = products
    return sendable_alerts, sendable_by_category


def format_report_from_payload(
    sendable_alerts: List[dict],
    sendable_by_category: Dict[str, List[dict]],
    config: dict,
) -> List[dict]:
    messages: List[dict] = []

    if sendable_alerts:
        for alert_markdown in _format_alert_batches(sendable_alerts):
            messages.append(_build_feishu_card("GPT 价格监控", alert_markdown))

    if not sendable_by_category and not sendable_alerts:
        messages.append(
            _build_feishu_card("GPT 价格监控", "> 暂无需要推送的商品")
        )
        return messages

    for label in get_category_order(config):
        if not is_category_push_enabled(config, label):
            continue
        products = sendable_by_category.get(label, [])
        if not products:
            continue
        top_n = get_category_top_n(config, label)
        markdown = _format_category_markdown(label, products, top_n)
        messages.append(_build_feishu_card(f"GPT 价格监控 - {label}", markdown))

    return messages


def run_once(config: dict, config_path: Path, force_push: bool = False) -> bool:
    schedule_reason = None
    if not force_push:
        schedule_reason = push_schedule_reason(config)
        if schedule_reason and not fetch_outside_push_schedule_enabled(config):
            log(f"{schedule_reason}，暂停抓取和推送")
            return False
        if schedule_reason:
            log(f"{schedule_reason}，继续抓取，推送暂停")

    old_state = load_state()
    old_pool_baseline = load_top10_pool(config)
    old_pool = {label: list(skus) for label, skus in old_pool_baseline.items()}
    sent_state = load_sent()
    shops_state = load_shops_state()
    synced = sync_permanent_skip_shops(shops_state)
    if synced:
        log(f"已将 {synced} 家永久不可用店铺移出监控")
    if is_classify_llm_enabled(config):
        llm_cfg = get_classify_llm_config(config)
        log(
            f"商品归类：规则优先，低置信才用模型 "
            f"(model={llm_cfg.get('model')}, "
            f"base_url={llm_cfg.get('base_url')})"
        )
    else:
        log("商品归类：关键字模式（未启用 classify_llm 或缺少 api_key）")
    new_state = {}
    rules = config["alert_rules"]
    pending_daily, monitored_total = count_daily_refresh_status(
        config, shops_state
    )
    batch_shops, due_count = select_shops_for_batch(
        config["shops"], shops_state, config, force_fetch=force_push
    )
    batch_size = int(config.get("shops_per_batch", DEFAULT_SHOPS_PER_BATCH))
    batch_prefix = (
        f"当日全量刷新 {monitored_total - pending_daily}/{monitored_total}，"
        if pending_daily > 0 and not force_push
        else ""
    )
    log(
        f"{batch_prefix}本批滚动更新 {len(batch_shops)}/{due_count} 家"
        + (f"（每批上限 {batch_size}）" if not force_push else "（强制全量）")
    )

    updated_shop_ids: Set[str] = set()
    for index, shop in enumerate(batch_shops):
        if index > 0:
            sleep_between_shops(config)
        token = shop["token"]
        name = shop.get("name", token)
        platform = shop.get("platform", "ldxp")
        sid = shop_id(platform, token)
        try:
            items = fetch_goods(
                token,
                platform=platform,
                page_size=config.get("page_size", 100),
                goods_type=shop.get("goods_type"),
                config=config,
            )
        except Exception as e:
            log(f"抓取失败 [{platform}] {name}: {e}")
            if is_shop_permanent_skip_error(e):
                update_shop_runtime(
                    shops_state,
                    platform,
                    token,
                    status=permanent_skip_status(e),
                    error=str(e),
                    mark_updated=True,
                )
                log(
                    f"{permanent_skip_reason(e)}，停止监控: "
                    f"[{platform}] {name}"
                )
            elif update_shop_runtime(
                shops_state,
                platform,
                token,
                status=SHOP_STATUS_ERROR,
                error=str(e),
                mark_updated=True,
            ):
                log(f"已标记店铺异常: [{platform}] {name} (status=error)")
            continue
        update_shop_runtime(
            shops_state,
            platform,
            token,
            status=SHOP_STATUS_ACTIVE,
            mark_updated=True,
        )
        grouped = filter_and_classify(
            items,
            config,
            platform=platform,
            shop_token=token,
            old_state=old_state,
        )
        snapshot = build_snapshot(platform, token, name, grouped)
        removed = replace_shop_snapshot(
            sid,
            snapshot,
            state=new_state,
            sent_state=sent_state,
            pool=old_pool,
            config=config,
        )
        updated_shop_ids.add(sid)
        log_shop_fetch_skus(
            platform,
            name,
            snapshot,
            removed,
            index + 1,
            len(batch_shops),
        )

    if is_cardnav_due(config):
        try:
            sync_cardnav_shops(config, config_path)
        except Exception as e:
            log(f"CardNav 同步失败: {e}")

    skipped_shop_ids = {
        shop_id(shop.get("platform", "ldxp"), shop["token"])
        for shop in config["shops"]
    } - updated_shop_ids
    if skipped_shop_ids:
        preserve_shop_state(old_state, new_state, skipped_shop_ids)

    orphaned = purge_missing_shop_products(
        new_state, sent_state, old_pool, config
    )
    if orphaned:
        log(f"已移除不存在店铺的商品 {len(orphaned)} 个")

    reclassified = reclassify_state(new_state, config)
    if reclassified:
        log(f"已按最新分类重归类 {reclassified} 个 SKU")

    removed_over_max = purge_state_out_of_price_range(new_state, config)
    if removed_over_max:
        log(f"已移除价格区间外商品 {removed_over_max} 个")

    backfilled = backfill_item_timestamps(new_state, shops_state)
    if backfilled:
        log(f"已回填 {backfilled} 个商品的最后更新时间")

    stale_count = count_stale_pool_items(new_state, config)
    if stale_count:
        stale_hours = get_pool_item_stale_hours(config)
        log(
            f"跳过 {stale_count} 个超过 {stale_hours:g} 小时未更新的商品"
            "（不入池、不推送）"
        )

    old_pool = sanitize_pool(old_pool_baseline, new_state, config)
    new_pool = compute_top_pool(new_state, config)
    alerts = detect_top10_pool_alerts(
        old_pool, new_pool, old_state, new_state, config
    )
    new_entries = sum(
        1 for a in alerts if is_new_pool_entry_kind(a.get("kind", ""))
    )
    price_drops = sum(1 for a in alerts if a["kind"] == "降价")
    if new_entries or price_drops:
        log(f"TopN 变动: 新进 {new_entries}，降价 {price_drops}")

    save_state(new_state)
    save_top10_pool(new_pool, new_state)
    prune_sent_state(sent_state, set(new_state))
    save_sent(sent_state)

    pending_daily, monitored_total = count_daily_refresh_status(
        config, shops_state
    )
    daily_refresh_complete = pending_daily == 0

    daily_full = (
        is_daily_first_push(config, shops_state) and not force_push
    )
    if feishu_openapi_configured(config):
        recalled = recall_expired_feishu_messages(config)
        if recalled:
            log(f"已撤回过期飞书消息 {recalled} 条")

    if not force_push and schedule_reason:
        log("不在推送时段，数据已更新，跳过推送")
        return False

    if not should_push(
        alerts,
        force_push,
        old_state,
        config,
        daily_full=daily_full,
        daily_refresh_complete=daily_refresh_complete or force_push,
    ):
        if (
            not daily_refresh_complete
            and not force_push
            and (new_entries or price_drops)
        ):
            log(
                f"当日全量刷新未完成（还剩 {pending_daily} 家），暂不推送"
            )
        return False

    include_full_top10 = force_push or daily_full
    alerts_only = daily_full and not force_push
    # 方案一：增量推送只发「已验库存」店铺的告警
    if alerts and not alerts_only:
        fresh_sec = get_alert_shop_fresh_seconds(config)
        alert_shops = alert_shop_ids(alerts, new_state)
        refreshed = refresh_stale_alert_shops(
            alert_shops,
            config=config,
            state=new_state,
            sent_state=sent_state,
            pool=new_pool,
            old_state=old_state,
            shops_state=shops_state,
            within_seconds=fresh_sec,
        )
        if refreshed:
            removed_over_max = purge_state_out_of_price_range(new_state, config)
            if removed_over_max:
                log(f"重拉后移除价格区间外商品 {removed_over_max} 个")
            old_pool = sanitize_pool(old_pool_baseline, new_state, config)
            new_pool = compute_top_pool(new_state, config)
            alerts = detect_top10_pool_alerts(
                old_pool, new_pool, old_state, new_state, config
            )
            new_entries = sum(
                1
                for a in alerts
                if is_new_pool_entry_kind(a.get("kind", ""))
            )
            price_drops = sum(1 for a in alerts if a["kind"] == "降价")
            log(
                f"重拉 {refreshed} 家店铺后 TopN 变动: "
                f"新进 {new_entries}，降价 {price_drops}"
            )
            save_state(new_state)
            save_top10_pool(new_pool, new_state)
            prune_sent_state(sent_state, set(new_state))
            save_sent(sent_state)

        before_count = len(alerts)
        alerts = filter_alerts_by_shop_freshness(
            alerts, new_state, shops_state, fresh_sec
        )
        dropped = before_count - len(alerts)
        if dropped:
            log(
                f"丢弃 {dropped} 条未验库存店铺的增量告警"
                f"（{fresh_sec:g}秒内未更新）"
            )
        if not should_push(
            alerts,
            force_push,
            old_state,
            config,
            daily_full=daily_full,
            daily_refresh_complete=True,
        ):
            log("无已验库存的可推送增量告警，跳过推送")
            return False

    sendable_alerts, sendable_by_category = _prepare_send_payload(
        new_pool,
        new_state,
        alerts,
        config,
        sent_state,
        old_state,
        include_full_top10=include_full_top10,
        alerts_only=alerts_only,
    )
    if not sendable_alerts and not sendable_by_category:
        log("无可推送商品（均已发送且价格未变）")
        save_sent(sent_state)
        return False

    if daily_full and not force_push:
        log("当日全量刷新完成，推送全量 Top10")

    reports = format_report_from_payload(
        sendable_alerts, sendable_by_category, config
    )
    message_ids = send_notifications(config, reports)
    if message_ids:
        record_feishu_message_ids(message_ids)
    if include_full_top10:
        mark_daily_full_push()

    sent_items = list(sendable_alerts)
    for products in sendable_by_category.values():
        sent_items.extend(products)
    record_sent_items(sent_state, sent_items)
    save_sent(sent_state)
    return True


def _daemon_script_path() -> Path:
    return SCRIPT_FILE


def _script_mtime() -> float:
    return SCRIPT_FILE.stat().st_mtime


def load_watch_meta() -> dict:
    if not WATCH_META_FILE.exists():
        return {}
    try:
        with open(WATCH_META_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_watch_meta(pid: int) -> None:
    WATCH_META_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCH_META_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"pid": pid, "script_mtime": _script_mtime()},
            f,
            ensure_ascii=False,
            indent=2,
        )


def clear_watch_meta() -> None:
    _safe_unlink(WATCH_META_FILE)


def stop_watch_daemon() -> bool:
    if not WATCH_PID_FILE.exists():
        clear_watch_meta()
        return False
    try:
        pid = int(WATCH_PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        _safe_unlink(WATCH_PID_FILE)
        clear_watch_meta()
        return False
    if pid == os.getpid():
        return False
    if not _process_alive(pid):
        _safe_unlink(WATCH_PID_FILE)
        clear_watch_meta()
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
        else:
            os.kill(pid, 15)
            time.sleep(0.5)
            if _process_alive(pid):
                os.kill(pid, 9)
    except OSError:
        pass
    _safe_unlink(WATCH_PID_FILE)
    clear_watch_meta()
    return True


def restart_watch_process(config_path: Path, interval: int) -> None:
    cmd = [
        sys.executable,
        str(SCRIPT_FILE),
        "--watch",
        "-c",
        str(config_path),
        "-i",
        str(interval),
    ]
    os.execv(sys.executable, cmd)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq %s" % pid, "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=10,
                check=False,
            )
            return str(pid) in result.stdout
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def is_watch_running() -> bool:
    if not WATCH_PID_FILE.exists():
        return False
    try:
        pid = int(WATCH_PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        _safe_unlink(WATCH_PID_FILE)
        return False
    if pid == os.getpid():
        return False
    if _process_alive(pid):
        return True
    _safe_unlink(WATCH_PID_FILE)
    clear_watch_meta()
    return False


def start_watch_daemon(config_path: Path, interval: int) -> Optional[int]:
    script = _daemon_script_path()
    cmd = [
        sys.executable,
        str(script),
        "--watch",
        "-c",
        str(config_path),
        "-i",
        str(interval),
    ]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd,
        cwd=script.parent,
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    WATCH_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCH_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    save_watch_meta(proc.pid)
    return proc.pid


def ensure_watch_daemon(config_path: Path, interval: int) -> None:
    current_mtime = _script_mtime()
    meta = load_watch_meta()
    if is_watch_running():
        if meta.get("script_mtime") == current_mtime:
            log("监听已在后台运行（Top10 监控）")
            return
        log("检测到 monitor.py 更新，正在重启后台...")
        stop_watch_daemon()
    pid = start_watch_daemon(config_path, interval)
    log(
        f"已在后台挂起监听 PID={pid}，间隔 {interval} 秒"
        f"（Top10 降价/新进推送，代码更新自动重启）"
    )


def watch_loop(
    config_store: ConfigStore, config_path: Path, interval: int, force_push: bool = False
) -> None:
    WATCH_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCH_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    save_watch_meta(os.getpid())
    script_mtime = _script_mtime()
    config = config_store.get()
    batch_size = config.get("shops_per_batch", DEFAULT_SHOPS_PER_BATCH)
    update_min = get_shop_update_interval_minutes(config)
    cardnav_min = get_cardnav_poll_interval_minutes(config)
    cardnav_hint = (
        f"CardNav 每 {cardnav_min} 分钟同步店铺"
        if cardnav_enabled(config)
        else "CardNav 已关闭"
    )
    log(
        f"开始监听，每 {interval} 秒滚动一批（每批 {batch_size} 店，"
        f"店间 {config.get('shop_fetch_delay_min_sec', DEFAULT_SHOP_FETCH_DELAY_MIN_SEC)}-"
        f"{config.get('shop_fetch_delay_max_sec', DEFAULT_SHOP_FETCH_DELAY_MAX_SEC)} 秒，"
        f"同店 {update_min} 分钟内不重复，{cardnav_hint}），"
        f"时段外{'仍抓取' if fetch_outside_push_schedule_enabled(config) else '不抓取'}，"
        f"config.json 修改后自动热更新，"
        f"monitor.py 更新后自动重启，"
        f"Top10 降价/新进推送，Ctrl+C 停止"
    )
    try:
        while True:
            if _script_mtime() != script_mtime:
                log("检测到 monitor.py 更新，正在重启...")
                restart_watch_process(config_path, interval)
            try:
                config = config_store.get()
                pushed = run_once(config, config_path, force_push=force_push)
                status = "已推送" if pushed else "无触发条件，跳过推送"
                log(status)
                force_push = False
            except urllib.error.URLError as e:
                log(f"网络错误: {e}")
            except Exception as e:
                log(f"错误: {e}")
            time.sleep(interval)
    finally:
        if WATCH_PID_FILE.exists():
            try:
                if int(WATCH_PID_FILE.read_text(encoding="utf-8").strip()) == os.getpid():
                    _safe_unlink(WATCH_PID_FILE)
                    clear_watch_meta()
            except ValueError:
                _safe_unlink(WATCH_PID_FILE)
                clear_watch_meta()


def main():
    parser = argparse.ArgumentParser(description="GPT 商品价格监听")
    parser.add_argument(
        "-c", "--config", default="config.json", help="配置文件路径"
    )
    parser.add_argument(
        "-f", "--force", action="store_true", help="强制推送（忽略变动检测）"
    )
    parser.add_argument(
        "-w", "--watch", action="store_true", help="前台持续监听模式"
    )
    parser.add_argument(
        "--once", action="store_true", help="只执行一轮，不挂起后台"
    )
    parser.add_argument(
        "-i", "--interval", type=int, default=None, help="监听间隔秒数"
    )
    args = parser.parse_args()
    setup_logging()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = Path(__file__).parent / config_path
    config_store = ConfigStore(config_path)
    config = config_store.get()
    interval = args.interval or config.get("interval_seconds", 300)

    if args.watch:
        watch_loop(config_store, config_path, interval, force_push=args.force)
    elif args.once or args.force:
        pushed = run_once(config_store.get(), config_path, force_push=args.force)
        log("推送完成" if pushed else "无触发条件，未推送（加 --force 强制推送）")
    else:
        ensure_watch_daemon(config_path, interval)


if __name__ == "__main__":
    main()
