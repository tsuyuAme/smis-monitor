# SMIS 挂刀监控（本机版）

用闲置笔记本定时抓取 smis.club/exchange，筛选后推送到 Telegram。

采用浏览器持久化配置（browser_data/）：首次手动过一次验证，之后自动跑。

## 一、安装（只需一次）

1. 安装 Python 3.11+（勾选 Add to PATH）
2. 打开命令提示符，进入本目录：

```
cd /d 你的路径\smis-monitor
pip install -r requirements.txt
playwright install chromium
```

3. 编辑 run_setup.bat 和 run.bat，填入 TG_BOT_TOKEN 和 TG_CHAT_ID

## 二、首次过验证

双击 run_setup.bat

- 弹出 Chrome，完成验证码直到表格有数据
- 配置保存在 browser_data\

## 三、日常自动跑

双击 run.bat 测试。

任务计划程序：创建基本任务，操作指向 run.bat，起始于本目录。建议每 15～30 分钟。

## 四、验证失效时

再跑一次 run_setup.bat 即可。
