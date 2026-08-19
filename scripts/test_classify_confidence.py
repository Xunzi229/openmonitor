#!/usr/bin/env python3
"""方案1归类 + 缺货保留/店铺清理 相关测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import monitor as m  # noqa: E402


def _config() -> dict:
    return {
        "categories": [
            {
                "name": "免费号",
                "keywords": ["free", "免费", "白嫖"],
                "enabled": True,
            },
            {
                "name": "Plus号",
                "keywords": ["plus", "plus号"],
                "enabled": True,
            },
            {
                "name": "Team号",
                "keywords": ["team", "团队"],
                "enabled": True,
            },
            {
                "name": "其他GPT",
                "keywords": [],
                "enabled": True,
                "fallback": True,
            },
        ],
        "monitor_keywords": ["gpt", "chatgpt"],
        "classify_llm": {"enabled": False, "api_key": ""},
        "shops": [
            {"platform": "ldxp", "token": "keep", "name": "keep"},
        ],
    }


def test_high_confidence_unique_keyword():
    cfg = _config()
    label, decision = m.classify_keyword_decision("ChatGPT Plus成品号月卡", cfg)
    assert decision == "high", decision
    assert label == "Plus号", label


def test_cdk_card_tools_not_plus():
    cfg = _config()
    cases = [
        "菲区提炼CDK 次卡 直卡支付链接 4361 5502 卡头开通plus必备",
        "Plus开通必备 CDK卡头",
        "ChatGPT 虚拟卡 支付链接 开通plus",
    ]
    for name in cases:
        label, decision = m.classify_keyword_decision(name, cfg)
        assert decision == "high", (name, decision)
        assert label == "其他GPT", (name, label)
    # 真实 Plus 月卡仍应命中 Plus号
    label, decision = m.classify_keyword_decision("ChatGPT Plus成品号月卡", cfg)
    assert label == "Plus号" and decision == "high"


def test_classify_names_returns_detail():
    cfg = _config()
    results = m.classify_names(["ChatGPT Plus成品号月卡"], cfg)
    assert results[0][0] == "Plus号"
    assert results[0][1] == "keyword"
    assert results[0][2] == "rule_high"


def test_cache_reuses_keyword_when_llm_enabled():
    cfg = _config()
    cfg["classify_llm"] = {"enabled": True, "api_key": "x"}
    old = {
        "ldxp:t:g1": {
            "name": "ChatGPT Plus成品号",
            "type": "Plus号",
            "classify_source": "keyword",
            "classify_detail": "rule_high",
        }
    }
    cached = m.cached_classify_label("ldxp:t:g1", "ChatGPT Plus成品号", old, cfg)
    assert cached[0] == "Plus号"
    assert cached[1] == "keyword"
    assert cached[2] == "sku_cache"


def test_stock_zero_kept_in_snapshot():
    cfg = _config()
    items = [
        {
            "name": "ChatGPT Plus成品号",
            "price": 10,
            "link": "https://example.com/a",
            "goods_key": "g1",
            "category": {"name": "gpt"},
            "extend": {"stock_count": 0},
        }
    ]
    grouped = m.filter_and_classify(
        items, cfg, platform="ldxp", shop_token="keep"
    )
    assert any(p["stock"] == 0 for p in grouped.get("Plus号", []))
    snap = m.build_snapshot("ldxp", "keep", "keep", grouped)
    assert "ldxp:keep:g1" in snap
    assert snap["ldxp:keep:g1"]["stock"] == 0
    assert "classify_detail" in snap["ldxp:keep:g1"]
    assert "classify_label" in snap["ldxp:keep:g1"]


def test_purge_missing_shop_products():
    cfg = _config()
    state = {
        "ldxp:keep:g1": {"shop": "ldxp:keep", "name": "a", "type": "Plus号"},
        "ldxp:gone:g2": {"shop": "ldxp:gone", "name": "b", "type": "Plus号"},
    }
    sent = {"ldxp:keep:g1": {}, "ldxp:gone:g2": {}}
    pool = {"Plus号": ["ldxp:keep:g1", "ldxp:gone:g2"], "免费号": [], "Team号": [], "其他GPT": []}
    removed = m.purge_missing_shop_products(state, sent, pool, cfg)
    assert "ldxp:gone:g2" in removed
    assert "ldxp:gone:g2" not in state
    assert "ldxp:keep:g1" in state
    assert "ldxp:gone:g2" not in sent
    assert pool["Plus号"] == ["ldxp:keep:g1"]


def test_pool_entry_excludes_stock_zero():
    cfg = _config()
    assert m.pool_entry_eligible({"price": 1, "stock": 0, "type": "Plus号"}, cfg) is False
    assert m.pool_entry_eligible(
        {
            "price": 1,
            "stock": 2,
            "type": "Plus号",
            "last_updated_at": m.current_timestamp(),
        },
        cfg,
    )


def test_classify_names_skips_llm_for_high_confidence():
    cfg = _config()
    cfg["classify_llm"] = {"enabled": True, "api_key": "x"}
    called = []

    def fake_llm(names, config):
        called.append(list(names))
        return ["其他GPT"] * len(names)

    m.call_classify_llm = fake_llm  # type: ignore
    results = m.classify_names(
        [
            "ChatGPT Plus成品号月卡",
            "ChatGPT 成品账号批发",
            "ChatGPT Plus成品号月卡",
        ],
        cfg,
    )
    assert results[0][:2] == ("Plus号", "keyword")
    assert results[2][:2] == ("Plus号", "keyword")
    assert results[1][1] == "llm"
    assert results[1][2] == "llm"
    assert called and called[0] == ["ChatGPT 成品账号批发"]


if __name__ == "__main__":
    test_high_confidence_unique_keyword()
    test_cdk_card_tools_not_plus()
    test_classify_names_returns_detail()
    test_cache_reuses_keyword_when_llm_enabled()
    test_stock_zero_kept_in_snapshot()
    test_purge_missing_shop_products()
    test_pool_entry_excludes_stock_zero()
    test_classify_names_skips_llm_for_high_confidence()
    print("all passed")
