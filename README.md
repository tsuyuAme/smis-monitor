# SMIS 挂刀监控

定时抓取 [smis.club/exchange](https://smis.club/exchange)，按挂刀比例 / 7 日跌幅 / 成交量等条件筛选，通过 Telegram 推送。

运行方式：

- **GitHub Actions**：配置 Secrets 后约每小时自动跑

## 功能

- 解析 `POST /api/commodity/exchange`（含商品 `id`）
- 过滤：挂刀比例、7 日跌幅、成交量、价格区间、平台
- Telegram：有符合条件时合并推送 **Top 最多 10 条**
- 链接：`https://smis.club/commodity/{id}`（站内详情，方便看走势）
- 记录首次发现，约 7 天后可推「保护期到期复核」
- 状态写入 `data/state.json`（Actions 有变更会自动 commit）

## GitHub Actions

1. 推送本仓库到 GitHub  
2. `Settings → Secrets and variables → Actions` 添加：

| Secret | 说明 |
|--------|------|
| `TG_BOT_TOKEN` | Telegram Bot Token |
| `TG_CHAT_ID` | 接收消息的 Chat ID |

3. 按需修改根目录 `config.json`  
4. 打开 Actions，启用 workflow；可手动 **Run workflow** 测试  

### 定时

```yaml
schedule:
  - cron: "17 * * * *"
```

- **每小时一次**
- 在每小时的 **第 17 分**（UTC）触发，**避开整点**
- 北京时间对应每小时的 **:17** 左右（Actions 可能有几分钟延迟）
- 仍支持 `workflow_dispatch` 手动运行

### 注意

- 若站点风控导致 Actions 长期 0 条，请改用本机方式  
- `data/state.json` 变更会由 workflow 提交回仓库  


### 安装（一次）

```bat
cd /d 本目录
pip install -r requirements.txt
playwright install chromium
```


## 配置 config.json

```json
{
  "site": {
    "url": "https://smis.club/exchange",
    "wait_ms": 8000
  },
  "strategy": {
    "hold_days": 7,
    "sale_method": "Steam挂底价"
  },
  "filters": {
    "ratio_max": 0.70,
    "drop_min_pct": 3,
    "volume_min": 50,
    "price_min": 1,
    "price_max": 5000,
    "platforms": []
  },
  "state": {
    "max_records": 3000
  }
}
```

| 字段 | 含义 |
|------|------|
| `ratio_max` | 挂刀比例上限（越小越“便宜”） |
| `drop_min_pct` | 7 日跌幅至少达到的百分比（如 3 表示跌 ≥ 3%） |
| `volume_min` | 日成交量下限 |
| `price_min` / `price_max` | 平台价格区间 |
| `platforms` | 平台白名单，`[]` 表示不限 |
| `hold_days` | 保护期天数，用于到期复核 |

市面常见比例多在 **0.68～0.72**。若 `ratio_max` 过低或 `volume_min` 过高，容易长期无结果。

## Telegram 示例

```
🟢 挂刀机会 Top 4
策略: Steam挂底价 · 保护期约 7 天
━━━━━━━━━━━━━━━━
1. 某饰品
   比例 0.6221 · 7日 -9.50% · BUFF
   平台价 ¥xx → 到手 ¥xx · 量 120
━━━━━━━━━━━━━━━━
打开挂刀行情
```

## 目录

| 路径 | 说明 |
|------|------|
| `main.py` | 主程序 |
| `config.json` | 过滤与策略 |
| `.github/workflows/monitor.yml` | Actions 工作流 |
| `run_setup.bat` / `run.bat` | Windows 本机脚本 |
| `browser_data/` | 本机浏览器数据（勿提交） |
| `data/state.json` | 监控状态 |
| `requirements.txt` | 依赖 |

## 常见问题

**Q: 有 qualified 但不推送？**  
当前版本只要本轮有符合条件的商品，就会推 Top（最多 10 条）。请确认 Secrets 中 TG 配置正确，并查看 Actions 日志是否有 `[tg]` / 报错。

**Q: 详情链接无效？**  
需使用能解析接口字段 `id` 的 `main.py`，链接格式为 `https://smis.club/commodity/{id}`。

**Q: 一直 0 条？**  
看日志中的过滤条件与 `[skip]` 原因；适当放宽 `ratio_max`、降低 `volume_min`。接口 401 或页面 No Data 时用本机 `run_setup.bat`。

**Q: 如何改频率？**  
编辑 `.github/workflows/monitor.yml` 的 `cron`。不要设置过于频繁，以免触发配额或风控。
