# BTC 公开永续行情采集

只读取 MEXC 官方 `https://api.mexc.com` 的 BTC_USDT 公开 USDT 永续行情。无交易所登录、认证、账户访问或订单。仓库不包含本金、推送凭证、交易策略或仓位。

官方接口依据：[集成指南](https://www.mexc.com/api-docs/futures/integration-guide)、[服务器时间](https://www.mexc.com/api-docs/futures/market-endpoints/get-server-time)、[合约信息](https://www.mexc.com/api-docs/futures/market-endpoints/get-contract-info)、[K线](https://www.mexc.com/api-docs/futures/market-endpoints/get-candlestick-data)。本包固定使用官方新主机，不使用备用域名、代理、区域选择或环境代理。

先读取服务器时间和合约元数据。必须明确满足 BTC 基础币、USDT 计价及结算、futureType=1、state=0、type=1、preMarket=false，才读取 Min60 和 Min5 K线；两次 K线请求间隔 2.1 秒，最后重新读取服务器时间。价格只采用官方标准 open/high/low/close，忽略语义未明确的 real* 字段，不把参考开盘价当作保证的实际成交价。

归一化后包含 721 根小时线（720 根完成＋当前）和 300 根5分钟线（299根完成＋当前）。时间参数是官方秒级 start/end，K线完成状态由服务器时间推导。严格校验 UTC 对齐、连续性、重复、价格、OHLC、非负成交量、当前K线和120秒时钟偏差。缺口、跨周期使当前K线缺失、403、429、非零业务码或网络错误立即失败，不重试，不补读。

公开运行文件只有：

- `snapshots/latest.json`：八字段 schemaVersion/source/instId/generatedAt/clock/hour/five/contract，source=mexc、instId=BTC_USDT。
- `snapshots/last_attempt.json`：仅 utcHour/attemptedAt，用于每个UTC小时最多一次尝试。

小时标记必须先刷新公开 main，再以非强制 Git push 成功认领。schedule 与 workflow_dispatch 都遵循同一标记和共享 concurrency；每次 HTTP 前及最终写入前再次检查当前小时。失败或不确定不清除标记；同小时不再采集。快照全部通过后原子替换，失败保留旧快照及其原生成时间。日志只输出安全状态和固定错误，不输出响应正文、上游消息或快照。

工作流每小时第7分钟启动，最多运行10分钟，等待当前小时第13分00秒至第13分05秒的采集窗口。迟到直接失败。手动首次验收不等待，但同受小时认领；不要重置标记或重复运行来重试。只使用公开 main 和标准 Ubuntu runner，维持 GitHub 公开仓库 Actions 免费范围，不启用付费资源。

首次 MEXC 手动测试 [run 38058773384](https://github.com/xiaomai-bot/btc-market-data-relay/actions/runs/38058773384) 已成功，采集约3秒、发布约1秒；这不能证明自动Cron已执行，也不是延迟保证。GitHub调度、Git提交和CDN可迟到，消费者应按实际时钟拒绝旧数据；本仓库不保证每小时及时到达。外部消费者的部署、健康和CPU需另外验收，不因公共代码发布而自动获得验证。

离线测试（网络、时钟及等待均mock）：

```sh
python3 -m unittest discover -s tests -v
```

全部51项通过。完整采集由工作流执行：

```sh
python3 collector.py --mode full --output snapshots/latest.json --required-attempt-marker snapshots/last_attempt.json
```

默认metadata模式也要求已成功认领的小时标记，仅检查公开时间与元数据，不写行情快照。发布令牌只在发布步骤的进程内存使用，不保存到文件、Git配置或日志。
