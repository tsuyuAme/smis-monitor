# SMIS → Telegram 挂刀监控

第一版目标：定时读取 `smis.club/exchange`，筛选“挂刀比例 ≤ 0.65 且 7 日跌幅 ≥ 5%”的商品，Telegram 推送；记录发现时间，7 天后再次复核并提醒出售。

## 默认策略

- 挂刀比例：`<= 0.65`
- 7 日跌幅：`>= 5%`，即七日涨跌 `<= -5%`
- 成交量：`>= 50`
- 持有/等待：7 天
- 出售方案：Steam 挂底价（追求更高 Steam 余额，而非立即成交）
- 轮询：GitHub Actions 每小时的 `03/18/33/48` 分钟，即约每 15 分钟一次

## GitHub 配置

1. 新建 GitHub 仓库，把本目录所有文件上传。
2. 进入 `Settings → Secrets and variables → Actions`，添加：
   - `TG_BOT_TOKEN`：Telegram BotFather 创建的 Bot Token
   - `TG_CHAT_ID`：你的私聊 Chat ID
3. `Actions` 中手动执行一次 `SMIS monitor` 验证。
4. 之后由 `schedule` 自动运行。

## 修改筛选条件

直接修改 `config.json`：

```json
"filters": {
  "ratio_max": 0.65,
  "drop_min_pct": 5,
  "volume_min": 50,
  "price_min": null,
  "price_max": null,
  "platforms": []
}
```

例如只监控 BUFF：

```json
"platforms": ["BUFF"]
```

## 注意

本版本使用 Playwright 从渲染后的页面读取排行榜表格。网站结构变化后可能需要调整 `main.py` 的解析逻辑。没有实现自动购买、自动挂 Steam 单或自动交易，只做提醒和 7 天复核。
