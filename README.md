# SMIS → Telegram 挂刀监控

定时抓取 [smis.club/exchange](https://smis.club/exchange)，筛选低挂刀比例 + 有一定跌幅的商品，通过 Telegram 推送，并在 7 天保护期后提醒复核。

## 默认策略

- 目标：平台买入价 / 到手 Steam 余额 ≤ **0.70**（约七折）
- 7 日跌幅 ≥ 3%
- 日成交量 ≥ 50
- 持有 7 天后在 Steam 挂底价卖出换余额

## 配置 `config.json`

```json
{
  "filters": {
    "ratio_max": 0.70,
    "drop_min_pct": 3,
    "volume_min": 50,
    "price_min": 1,
    "price_max": 5000,
    "platforms": []
  }
}
```

- `ratio_max`：挂刀比例上限（网站「挂刀比例」已是扣手续费后的到手比例）
- `drop_min_pct`：7 日跌幅至少多少个百分点（3 表示跌幅 ≥ 3%）
- `volume_min`：日成交量下限
- `platforms`：空数组 = 全部平台；可填 `["BUFF", "C5"]` 等

## GitHub Actions 使用

1. 把本目录所有文件推到你的仓库
2. Settings → Secrets and variables → Actions 添加：
   - `TG_BOT_TOKEN`：BotFather 创建的 Token
   - `TG_CHAT_ID`：你的 Chat ID
3. Actions 里手动跑一次 **SMIS monitor**
4. 之后按 schedule 自动跑（约每 15 分钟）

## 本地调试

```bash
pip install -r requirements.txt
playwright install chromium
export TG_BOT_TOKEN=你的token
export TG_CHAT_ID=你的chat_id
python main.py
```

日志会打印解析到的商品和过滤结果，方便确认是否抓到数据。

## 注意

- 网站有阿里云验证码，偶尔可能导致抓取失败，重试即可
- 页面结构变化后可能需要调整选择器
- 本工具只做提醒，不自动下单
