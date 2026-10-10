# OKX 公开行情采集包（尚未部署）

这是一个供审阅的免费候选方案。它尚未发布到 GitHub，未证明 GitHub 托管节点能够读取 OKX，也未完成自动采集或 Cloudflare 消费验收。

每小时只运行一次，固定从 `https://openapi.okx.com` 顺序读取 BTC-USDT-SWAP 最新 300 根 1H、间隔 2100ms 后最新 300 根 5m、服务器时间与公开合约信息。HTTP429 或业务50011立即失败；不重试、不改域名或IP。使用 Python 标准库，无需安装依赖。

只有所有数据校验通过才原子替换 `snapshots/latest.json`。小时与5m均保留官方300行及确认标志；小时新鲜度只用已完成K线判断，现有Cloudflare解析器排除未完成H1，不让其进入信号或ATR计算。5m保留当前未完成K线。时间偏差超过120秒、缺口、重复、过期K线、异常OHLC或不匹配的合约都会失败。失败不会修改旧行情快照的生成时间或发布旧行情快照。

公开运行数据文件只有 `snapshots/latest.json` 和 `snapshots/last_attempt.json`。后者只含 `utcHour` 与实际 `attemptedAt`，无用户信息；它用于在调用市场之前认领当前UTC小时。schedule与dispatch均先无认证刷新公开main，在现有concurrency串行运行约束下，只有将尝试标记非强制提交/推送成功后才允许取行情。同小时已有标记直接跳过，失败不清标记，推送超时或拒绝不能继续访问市场，跨小时也重新检查。不能依靠排队事件的旧`GITHUB_SHA`读取来去重。

公开文件仅包含固定来源、合约标识、生成时间、OKX公开时间、K线与5个合约类型字段。它不含用户本金、策略、参考仓位、微信密钥、账户信息或其他凭据。微信密钥与仓位计算留在现有Cloudflare私密配置。

将本目录作为一个**新公开仓库的根目录**，默认分支必须为 `main`。标准 Ubuntu GitHub Actions 在公开仓库免费；私有仓库或大型runner不在此方案内。Workflow只允许公开main分支执行，权限仅contents-write，不使用交易所认证。发布token仅在GitHub步骤的进程内存中使用，不写文件或日志。仅显式stage/commit上述两个允许路径，不提交整个目录。

候选GitHub调度为每小时第11分钟。先完成小时认领，仅真实`schedule`事件再运行`wait_for_window.py`：等到当前UTC小时第12分15秒才采集；已到该点则立即，晚于第13分30秒则失败，后续行情读取不会执行。等待后重新核验同一窗口，超时不顺延至下一小时。`workflow_dispatch`首次验收不等待，但同样受小时尝试标记限制。等待程序不调用市场接口，也不增加采集频率；整个job仍限5分钟。

拟议Cloudflare消费时间为每小时第13分钟，仍每小时一次；**这一变更尚未部署，须在授权后单独实施**。采集快照使用实际采集结束时间，绝不提前写入或伪造时间戳。GitHub Cron可能迟到、被丢弃；公开仓库长期无活动可能自动停用。这个自动时序**尚未验证**，没有准时承诺。消费者必须独立检查生成时间、交易所时间与最新小时/5mK线，任何超过现有120秒新鲜度限制的数据均拒绝，不放宽限制来掩盖延迟。原入场信号15分钟规则保持不变。

第12分15秒是开始采集的目标，不能保证快照已在第13分钟前公开：四次请求、提交、推送、GitHub CDN以及Cloudflare调度均可能延迟。允许至第13分30秒才开始采集的迟到任务，也可能已错过本小时Cloudflare读取。此时消费者拒绝上一小时旧快照，本小时停止新开仓提醒；没有重试或提前伪造时间来掩盖竞态。可用性必须以真实自动执行证明。

候选Cloudflare模块用OKX时钟判断K线完成与ATR/历史回放，用实际Worker时钟判断开仓提醒15分钟期限，并在实际发送前再次核验。过期或没有可验证期限的旧入场消息不发送；行情120秒新鲜度与入场15分钟期限是两项独立限制。微信接收端的传输/阅读延迟仍无法由本程序保证。

本地离线测试：

```sh
python3 -m unittest discover -s tests -v
```

当前34项Python mock测试通过，覆盖429/50011即停、公开字段、K线校验、时间窗口及同小时持久认领/失败保留/跨小时拒绝。另已完成候选Worker的离线边界测试：实际开仓截止、D1认领延迟越界、旧payload拒绝、私有deadline不向推送服务发送、ATR与5m回放保留交易所时钟。候选Worker由同级目录的`build_github_feed_candidate.py`独立构建，未修改生产`worker.mjs`或`deploy.mjs`。这些检查使用合成数据与mock网络；没有真实等待、GitHub发布、交易所请求或微信测试。

首次连通性验收只使用未被认领UTC小时的 `workflow_dispatch` 运行一次，不能把它当作自动Cron成功。需随后核验真实小时Cron、公开快照新鲜度、Cloudflare真实自动消费、持久化和免费CPU用量。发生429后停止，本小时不追加手动采集。创建公开仓库和启用此工作流前仍需用户明确同意；授权范围应包含上述公开行情与尝试时间标记。本包本身未执行上述动作。

收费及调度说明：[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)、[schedule event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。
