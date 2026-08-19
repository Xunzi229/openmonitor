# OpenMonitor

多平台 GPT 账号价格监控 & 飞书群自动推送工具。

监听多个店铺平台的 ChatGPT 商品价格，按类别（免费号 / Plus / Team / Pro / 其他GPT）自动分类、筛选全网最低价 Top 10，价格变动时通过飞书机器人卡片消息实时推送告警。

## 功能特性

- **多平台监控** — 支持 `ldxp`（pay.ldxp.cn）和 `catfk`（catfk.com）两大平台，可配置 120+ 店铺
- **智能分类** — 自动识别 GPT 商品并归类为：免费号、Plus号、Team号、Pro号、其他GPT
- **Top 10 价格池** — 每个品类维护全网最低价 Top 10，滚动更新
- **变动检测** — 监测价格下降、新商品上架、商品下架、改名、库存变化等
- **去重推送** — 已发送的告警自动去重，避免重复通知
- **飞书卡片** — 以 Markdown 表格卡片推送到飞书群，含商品名、渠道、价格、变动幅度、购买链接
- **后台守护** — 支持后台 daemon 模式持续运行，PID 文件防重复启动
- **零依赖** — 纯 Python 标准库，无需安装任何第三方包

## 快速开始

### 环境要求

- Python 3.6+（Linux 服务器常用 `python3`）
- 部署前可执行兼容性自检：

```bash
python3 scripts/verify_py36.py
python3 -m py_compile monitor.py
python3 monitor.py --once -c config.json
```

### 配置

1. 复制敏感配置：`cp config.secrets.example.json config.secrets.json`，填入 webhook / LLM
2. 编辑 `config.json`（店铺、分类等，可提交；**不要**写 webhook / `classify_llm`）

```json
{
  "interval_seconds": 60,
  "shops_per_batch": 10,
  "push_schedule": {
    "weekdays": [1, 2, 3, 4, 5],
    "time_ranges": [{"start": "07:00", "end": "19:00"}]
  },
  "monitor_keywords": ["gpt", "chatgpt"],
  "categories": [
    {"name": "免费号", "keywords": ["\\bfree\\b", "免费", "白嫖"], "top_n": 10, "enabled": true},
    {"name": "Plus号", "keywords": ["\\bplus\\b", "plus号"], "top_n": 10, "enabled": true},
    {"name": "Team号", "keywords": ["\\bteam\\b", "团队"], "top_n": 10, "enabled": true},
    {"name": "Pro号", "keywords": ["\\bpro\\b"], "top_n": 10, "enabled": true},
    {"name": "其他GPT", "keywords": [], "top_n": 10, "enabled": true, "fallback": true}
  ],
  "shops": [
    {
      "platform": "ldxp",
      "token": "店铺token",
      "name": "店铺名称",
      "status": "active"
    }
  ]
}
```

- `push_schedule`：推送时段；`fetch_outside_push_schedule: true` 时时段外仍抓取，仅暂停推送
- `min_push_price` / `max_push_price`：全局价格区间；分类可单独设 `max_push_price`
- `categories`：大类关键词 + 每类 `top_n`（免费号建议 3）
- `monitor_keywords`：进入监控的商品关键词
- `config.json` 修改后自动热更新，无需重启
- 店铺 `status`/`last_updated_at` 在 `data/shops_state.json`

### 运行

```bash
# 后台守护模式（默认，自动 fork 后台进程）
python monitor.py

# 前台持续运行
python monitor.py --watch

# 单次执行一次检查后退出
python monitor.py --once

# 强制推送当前 Top 10 报告到飞书
python monitor.py --force

# 自定义配置文件路径
python monitor.py -c /path/to/config.json

# 自定义轮询间隔（秒）
python monitor.py -i 120


# 查看是否在跑
python -c "import monitor; print(monitor.is_watch_running())"

# windows 停止6376对应启动时候的pid
Stop-Process -Id 6376 -Force

# 重启
python openmonitor/monitor.py
```

### 导入店铺

如果你有 `data/site.json` 格式的店铺数据，可以批量导入：

```bash
python scripts/import_sites.py
```

### Linux 后台（systemd）

```bash
sudo cp deploy/openmonitor.service /etc/systemd/system/
# 编辑 WorkingDirectory / ExecStart 为实际路径
sudo systemctl daemon-reload
sudo systemctl enable --now openmonitor
sudo journalctl -u openmonitor -f
```

## 项目结构

```
openmonitor/
├── monitor.py                 # 主程序（全部逻辑）
├── config.json                # 配置文件（webhook、轮询间隔、店铺列表）
├── scripts/
│   └── import_sites.py        # 店铺批量导入工具
├── data/
│   ├── prices.json            # 全量商品价格状态快照
│   ├── top10.json             # 各品类 Top 10 最低价商品
│   ├── sent.json              # 已推送记录（去重用）
│   ├── site.json              # 原始店铺数据
│   └── watch.pid              # 后台进程 PID 文件
└── .gitignore
```

## 工作原理

```
config.json 定义店铺列表
       ↓
run_once() 按批次抓取店铺商品（每批 10 个，间隔 2~3s）
       ↓
过滤 GPT 相关商品，按类别分类，仅保留有库存项
       ↓
与 prices.json 历史状态对比，检测变动
       ↓
重新计算各品类 Top 10 最低价池
       ↓
价格下降 → 去重 → 格式化为飞书 Markdown 卡片 → Webhook 推送
       ↓
持久化状态到 data/*.json
```

## 告警触发条件

| 条件 | 说明 |
|------|------|
| 价格下降 | TopN 池内 SKU 价格低于 `prices.json` 上次记录 |
| 新进 TopN | 新 SKU 进入某类 TopN 池（含集体涨价后挤入前十） |
| `--force` | 强制推送当前 TopN 全表 |

可在 `alert_rules` 中关闭：`push_on_price_drop` / `push_on_top10_new_entry`

## 频率控制

- 每次 API 请求间隔 ≥ 1.5 秒
- 店铺间随机延迟 2~3 秒
- 每个店铺 20 分钟冷却期（避免频繁访问）
- 飞书消息按 10 行分批发送（适配 20KB 消息限制）

## 店铺配置说明

每个店铺条目包含以下字段：

```json
{
  "platform": "ldxp | catfk",
  "token": "店铺唯一标识",
  "name": "店铺显示名称",
  "status": "active | error | disabled",
  "last_fetch": "最后成功抓取时间（自动维护）",
  "error": "错误信息（status=error 时）"
```

- `active` — 正常监控
- `error` — 抓取失败（记录错误信息，下次自动重试）
- `disabled` — 手动暂停监控

## GitHub Actions 部署

可在 GitHub 上定时跑监控，并把 `data/*.json` 持久化到仓库（`watch.pid` 仍被忽略）。

### 1. 仓库 Secrets

在 **Settings → Secrets and variables → Actions** 添加：

| Secret | 说明 |
|--------|------|
| `FEISHU_WEBHOOK_URL` | 飞书机器人 Webhook（可选） |
| `QIWEI_WEBHOOK_URL` | 企业微信 Webhook（可选） |

`config.json` 里的 webhook / `classify_llm` 请放在本地 `config.secrets.json`（已 gitignore），或用环境变量覆盖。

### 2. 配置文件

- 把 `config.json`（店铺列表、分类、监控时段等）提交到仓库，作为**默认配置**
- Webhook / `classify_llm` 放 `config.secrets.json` 或 Actions Secrets，不要写进可提交配置
- Actions 运行时会在 `config.json` 之上叠加环境变量覆盖（见下文）

### 3. Actions 里自定义配置

优先级：**环境变量 > config.json**

#### 方式 A：单个字段（Variables / workflow env）

在 **Settings → Secrets and variables → Actions → Variables** 添加，或在 `monitor.yml` 的 `env:` 里写：

| 环境变量 | 对应 config 字段 | 示例 |
|----------|------------------|------|
| `FEISHU_WEBHOOK_URL` | `feishu_webhook_url` | Secret |
| `QIWEI_WEBHOOK_URL` | `qiwei_webhook_url` | Secret |
| `MONITOR_INTERVAL_SECONDS` | `interval_seconds` | `60` |
| `MONITOR_SHOPS_PER_BATCH` | `shops_per_batch` | `10` |
| `MONITOR_SHOP_UPDATE_INTERVAL_MINUTES` | `shop_update_interval_minutes` | `20` |
| `MONITOR_PAGE_SIZE` | `page_size` | `100` |
| `MONITOR_SHOP_FETCH_DELAY_MIN_SEC` | `shop_fetch_delay_min_sec` | `2` |
| `MONITOR_SHOP_FETCH_DELAY_MAX_SEC` | `shop_fetch_delay_max_sec` | `3` |

workflow 示例：

```yaml
env:
  MONITOR_SHOPS_PER_BATCH: "5"
  MONITOR_INTERVAL_SECONDS: "120"
```

#### 方式 B：JSON 深度覆盖（复杂配置）

用 Variable 或 Secret `MONITOR_CONFIG_JSON` 传入 JSON 对象，会**深度合并**进 `config.json`：

```json
{
  "push_schedule": {
    "weekdays": [1, 2, 3, 4, 5, 6, 7],
    "time_ranges": [{"start": "00:00", "end": "23:59"}]
  },
  "categories": [
    {"name": "免费号", "keywords": ["free", "免费"], "top_n": 5, "enabled": true}
  ]
}
```

适合只改监控时段、TopN、分类关键词等，不必改仓库里的 `config.json`。

#### 方式 C：单独 CI 配置文件

复制一份 `config.ci.json`，在 workflow 里改命令：

```yaml
run: python monitor.py --once -c config.ci.json
```

店铺列表、分类等全放 CI 专用文件；Webhook 仍可用 Secret 环境变量覆盖。

### 4. 工作流

已提供 `.github/workflows/monitor.yml`：

- 默认每 5 分钟执行 `python monitor.py --once`
- 用 **Actions Cache** 恢复上次 `data/`（首次无缓存时从空状态开始）
- 每轮结束后把 `prices.json` / `sent.json` / `top10.json` / `shops_state.json` 提交回仓库

手动触发：Actions 页 → **Price Monitor** → **Run workflow**。

### 5. 本地与 Actions 共用数据

若希望本地和 CI 共用同一份 data，可把 `data/*.json` 一并提交；`.gitignore` 仅忽略 `data/watch.pid`。

### 6. 下架 SKU 清理

某店铺抓取成功后，该店已不在商品列表中的 SKU 会从 `prices.json`、`sent.json`、`top10.json` 中移除，避免长期保留过期数据。

## License

MIT
