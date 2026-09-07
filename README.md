# Multi-Asset Quant Paper HFT

这是一个事件驱动的多资产 paper-trading 研究骨架：本地合成仿真覆盖美股、加密货币和期权；日线 ML 研究覆盖股票/ETF 与 BTC；当前 forward shadow 仅生成美股/ETF 观察信号。它不是已经验证有效的交易系统。默认只运行本地确定性仿真，订单适配器固定为 Paper 模式。

> “高频”在这里指毫秒级事件、短周期信号和延迟敏感的研究流程，不代表交易所共址、内核旁路或微秒级真实 HFT。示例的合成数据收益没有投资意义。

## 2026-09-05 审查后的状态

完整问题、修复、评估口径和剩余工作见 [中文审查报告](docs/quant-review-2026-09-05.md)。当前模型仍未通过准入：12 折样本外 AUC 0.5137，5 bps 往返成本后累计收益约 1.15%、Sharpe 0.066、最大回撤约 -26.01%；这些是六标的理论日内研究结果，不是已成交收益。10 bps 成本下收益约 -32.05%。不能把修正后的统计口径当作模型改善或可盈利证据。

两层订单开关保持 `NO`。本轮没有下单；旧模型和数据库备份保存在 `artifacts/review-backup-KwMivkOT/`。旧 6 条 shadow 记录保留为 `INVALIDATED_LEGACY`，新的 v2 记录预先锁定目标交易时段。BTC 需独立的延迟入场标签，暂不生成 daily shadow。

当前环境依赖快照见 `requirements-tested.txt`；不要在活跃环境中盲目升级依赖。完整测试需要安装 `.[ml,paper,data]`。

## 2026-09-07：独立 v3 时间目标实验

已完成 [v3 实验说明与结果](docs/research-v3-2026-09-07.md)：分开训练美股/BTC；BTC 用 D−2 完整日线预测目标日，明确决策/进出场/标签可用时间；固定三种基线、long/flat 规则和成本压力场景。结果只写新的 `artifacts/research-v3/` 子目录，**没有替换 v2 观察模型或开启交易**。

美股树模型在 5 bps 成本下研究累计收益 +12.79%、Sharpe 0.373，但 10 bps 时为 -5.59%；BTC 树模型在 25 bps 下为 -72.83%，零成本也为负。两组窗口不同，不能合并或与旧 v2 数字直接比较。这仍是日线价格代理研究，不是已验证可执行回测。全项目 128 项测试通过。

```bash
# 查看已完成结果，不重训、不下单：
.venv/bin/python main.py research show --run-dir artifacts/research-v3/20260907-timing-baseline
```

新增模块职责、实验协议、所有候选和后续数据验收见上面的 v3 说明。

## 2026-09-07：只读分钟行情与报价验收

已完成 [行情接入、成本样本与代码分段说明](docs/marketdata-audit-2026-09-07.md)：实际采集五个股票/ETF 的历史 SIP 和 BTC/USD 的 Alpaca US 行情，共 29,616 条报价、3,267 条通过检查的时段内分钟 bar。JNJ 缺失 2 分钟、BTC 缺失 121 分钟且有 921 根零成交量 bar；保留质量警告，不自动补齐。

这是单日窗口的连接与数据诊断，**不是实时 SIP 权限证明、成交记录或策略收益**。本轮没有下单、更新模型或启动持续采集，原模型、数据库和自动任务保持不变。全项目 175 项测试通过。

```bash
# 校验并查看已保存行情报告，无网络访问：
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-sip-crypto-audit
```

## 2026-09-07：五日、三个时段的只读行情研究

已完成 [多日行情结果与新增代码说明](docs/marketdata-study-2026-09-07.md)：每个标的 5 天、每天开盘附近/午间/收盘附近三个报价窗口。120 个分段全部返回且分页完整，保存 206,663 条报价、16,577 根时段内有效分钟 bar。报告分标的、分时段按日等权统计，并加入 0/250/1000 毫秒历史事件时间偏移诊断；**不是测得的执行延迟、成交回放或策略收益**。

BTC 缺失 369 分钟，午间只有 3/5 天能在预设时间内选到有效报价；JNJ 缺失 4 分钟。模型、数据库、既有自动任务未改变，订单开关关闭。全项目 231 项测试通过。

```bash
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-five-session-study
# 再次单次采集时使用全新目录名：
.venv/bin/python main.py marketdata study --sessions 5 --stock-feed sip --run-dir artifacts/marketdata/NEW-STUDY-NAME
```

## 2026-09-07：目标时刻报价重建与离线回放

已完成 [回放规则、实际结果与新增模块说明](docs/asof-replay-2026-09-07.md)。沿用五日研究的 90 个目标时点，补采各时点前 5 秒、后 2 秒，共 34,555 条报价；仅使用严格早于检查时刻的最新事件。无效或有歧义的更新会使旧报价失效，决策时刻缺失/过期不能由未来报价补救。

在固定“最大报价年龄 1 秒”的研究假设下，66/90 个决策时点通过报价检查，24 个因缺失或过期阻断。0/250/1000 毫秒情景合计 193 次通过参考报价检查、77 次被阻断；**这些不是订单、成交率或收益**。完全离线复算与首次结果逐项一致，未下单、未记模拟成交、未改模型或数据库。全项目 293 项测试通过。

```bash
# 离线校验并查看已完成的回放结果：
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-asof-offline-check
# 再次离线复算必须使用新输出目录，无 API 访问：
.venv/bin/python main.py marketdata replay --source-dir artifacts/marketdata/20260907-asof-preroll --run-dir artifacts/marketdata/NEW-OFFLINE-REPLAY
```

## 2026-09-07：离线意图准入与生命周期审计

已完成 [意图准入规则、模块职责与运行方法](docs/intent-admission-2026-09-07.md)。对上一阶段 90 个历史目标时点，生成 540 个固定 5 美元的 BUY/SELL 合成意图，验证 0/250/1000 毫秒情景，记录 1,620 条带哈希链接的审计事件。当前冻结模型仍未通过准入，**540 个意图全部被拒绝**；行情缺失、过期等问题另行累计，不被模型拒绝原因掩盖。

这是离线工程检查，不是模型预测、实际订单或成交。模型只核对文件哈希和当前批准状态，不反序列化、不训练。网络访问、下单、模拟成交均为 0，现有模型、数据库、证据和自动任务未改变。全项目 359 项测试通过，依赖一致性检查通过。

```bash
.venv/bin/python main.py marketdata show --run-dir artifacts/marketdata/20260907-intent-admission
```

GitHub 仅发布源码、测试、配置模板和文档；`.env`、市场数据、模型和审计产物不上传。发布前必须运行 [密钥与暂存区检查](docs/github-security.md)。上述历史报告命令需要本地已有的证据，单独克隆源码不会获得这些数据。

## 快速运行

最简单的方式是在项目目录直接运行：

```bash
python main.py
python main.py --events 3000 --audit artifacts/my-run.jsonl
```

也可以把它安装成标准 Python package：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
quant-paper demo --events 1500
quant-paper demo --events 1500 --audit artifacts/demo-audit.jsonl
python -m unittest discover -s tests -v
```

如果 macOS 没有系统 Python，可直接用 Codex 工作区提供的 Python，并设置源码路径：

```bash
PYTHONPATH=src /path/to/python3 -m quantpaper.cli demo --events 1500
PYTHONPATH=src /path/to/python3 -m unittest discover -s tests -v
```

## 每个 section 做什么

### 1. `domain.py` — 交易领域模型

定义资产类型、行情、订单、成交、持仓和资金账户。价格、数量和现金统一使用 `Decimal`；期权通过 `multiplier=100` 处理合约乘数。这是回测、paper 和未来实盘共用的语言。

### 2. `data.py` — 可重复行情

生成股票、BTC 和期权的纳秒时间戳报价。固定随机种子保证每次测试得到完全相同的事件流，便于发现代码回归。它只用于工程验证，不能证明策略有效。

### 3. `strategy.py` — 高频研究基线

用盘口买卖量不平衡和极短期动量形成信号，再根据当前库存计算目标仓位。策略只产生订单意图，不直接连接 broker，也不能绕过风控。

### 4. `risk.py` — 独立风控

本地模拟器中的订单经过订单金额、单品种敞口、总敞口、每秒订单数和运行期间亏损检查，未成交订单也占用风险额度。触及亏损上限后启动 kill switch。当前并非完整的多日生产风控：跨交易日重置、保证金和自动风险减仓仍需独立设计；Alpaca 手动测试使用另一个严格限制的订单生命周期，不复用此模拟账户。

### 5. `execution.py` — 本地 paper 撮合

模拟网络/处理延迟、买卖价差、滑点、手续费和订单状态。股票、加密货币和期权使用不同费用模型。它比“当前中间价立即成交”的简单回测更保守，但仍不能完全重现真实排队和市场冲击。

### 6. `engine.py` — 事件循环

按时间顺序连接行情、成交回报、策略、风控、撮合和账户记账。每个信号严格在收到当前事件后产生，避免典型的未来数据泄漏。

### 7. `options.py` — 期权定价校验

提供 Black-Scholes 价格与 Delta/Gamma/Vega，主要用于合成数据和合理性检查。美股期权通常为美式，真实交易以市场报价为准，不能把该函数当成完整生产定价器。

### 8. `reporting.py` — 结果报告

输出净值、PnL、收益率、最大回撤、成交次数、拒单数和费用。后续接入真实历史数据后，可继续增加 Sharpe、成交偏差和按资产/策略归因。

### 9. `alpaca.py` — 安全的 paper gateway

旧 gateway 仅保留只读功能，其直接提交/全撤单方法已禁用，避免绕过生命周期审计。受限手动 Paper 测试统一经过 `alpaca_paper.py`：固定 paper 模式、双开关、精确状态判断、按 key 加锁及异常持久化阻断。

### 10. `configs/paper.toml` — 参数与风险限制

集中管理初始资金、延迟、滑点、策略阈值和风险上限，使代码审查与参数变更分离。所有默认数值仅供演示。

### 11. `tests/` — 自动化验证

覆盖记账、报价与成交因果顺序、挂单风险预留、订单异常和部分成交、训练特征时序、组合收益口径、数据版本、交易日历、shadow 幂等与事务回滚。broker 测试使用 mock，不等同真实接口集成验收。

### 12. `audit.py` — 可检测篡改的审计链

把事件写入 append-only JSONL，重启与追加前验证完整哈希链，写入时加锁并同步磁盘。它能检测普通历史修改，但没有外部可信锚点时不能防止整条链被重写；全链扫描也不适合真实 HFT。

## Alpaca paper 安全启用

1. 复制 `.env.example` 为 `.env`，填入 **paper-only** credentials。
2. 先保持 `ENABLE_ALPACA_PAPER=NO`，只调用 `account()` 验证账户。
3. 当前模型未通过准入，继续保持关闭。下面的启用命令仅说明独立手动通道测试，不代表策略获准自动下单。
4. API 密钥不得提交进 Git。

当前已有受限的 `alpaca` CLI，但不是无人值守自动交易系统。持续交易更新流、自动恢复状态机、跨主机协调和完整账户对账仍待实现。

## Yahoo Finance 机器学习

安装可选 ML 依赖后，可下载每个代码在 Yahoo 上可获得的最大日线历史，建立严格按时间切分的样本外模型：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[ml]'
python main.py ml --symbols SPY JPM XOM WMT JNJ BTC-USD --period max
```

模型使用前一交易日及更早的数据构造收益率、波动率、均线距离、日内区间、成交量异常和 RSI；预测下一交易日 open-to-close 方向。最后 20% 的日期只用于样本外评估，训练区和测试区之间留出 5 个交易日 purge gap。结果扣除可配置交易成本，并保存以下可审计文件：

- `data/yahoo/*.csv`：原始调整后 OHLCV 缓存。
- `artifacts/yahoo_daily_model.joblib`：模型、特征顺序、代码列表和训练截止日。
- `artifacts/yahoo_daily_model.json`：样本外指标、数据 SHA-256 指纹和依赖版本。

模型只有同时满足 `ROC AUC >= 0.53`、`Sharpe >= 0.75`、最大回撤不超过 20% 且扣成本收益为正，才会标记为 `approved_for_paper=true`。这是最低研究门槛，不代表模型适合实盘。

Yahoo 的日线适用于机器学习研究，不是历史 HFT 数据：根据 yfinance 文档，所有小于日线的 intraday 数据最多覆盖最近 60 天。Yahoo/yfinance 数据仅用于个人研究；商业使用前必须自行确认数据授权。Yahoo 也不提供可用于长期训练的完整历史期权链或订单簿。

## 第一步：point-in-time 数据层

研究库使用 DuckDB 同时保存“事件发生时间”和“当时真正可获得时间”，避免把后来发布或修订的数据泄漏进回测。当前已建立行情、基本面、宏观、新闻、指数成分和公司行动表；本阶段先导入现有 Yahoo 日线，后续再逐个接入 SEC、FRED、新闻和成分股历史数据源。

```bash
python main.py warehouse init
python main.py warehouse ingest-yahoo
python main.py warehouse stats
```

默认数据库是 `data/research.duckdb`。schema v4 的 Yahoo 快照按本机实际首次观察时间和内容版本保留，不能把今天回填的调整后价格伪装成历史当天可知数据。旧 `adjusted-v1` 行仍保存，但 `bars_as_of` 不会选中它们。当前 ML 仍直接读取 Yahoo CSV 做探索研究，并未因此自动成为严格 PIT 回测。

## Alpaca 模拟实盘

安装依赖并创建本机配置：

```bash
python -m pip install -e '.[ml,paper,data]'
cp .env.example .env
```

只把 Alpaca **Paper Trading** 的 key 写进本机 `.env`，不要在聊天中发送密钥：

```dotenv
APCA_API_KEY_ID=你的_paper_key
APCA_API_SECRET_KEY=你的_paper_secret
```

只读检查不会下单：

```bash
python main.py alpaca status
python main.py alpaca quote --symbol SPY
```

提交后立即撤销一个极低价格限价单，用来验证订单通道。必须先在 `.env` 明确打开第一层 paper 开关：

```dotenv
ENABLE_ALPACA_PAPER=YES_I_UNDERSTAND
```

```bash
python main.py alpaca submit-cancel --symbol SPY --quantity 1
```

模拟成交的往返测试还需要第二层开关，且代码限制为市场开盘时、每次 1–25 美元：

```dotenv
ENABLE_ALPACA_PAPER_ROUND_TRIP=YES_RUN_SMALL_ROUND_TRIP
```

```bash
python main.py alpaca round-trip --symbol SPY --notional 5
```

所有订单生命周期都会写入 `artifacts/alpaca-paper-audit.jsonl`。适配器固定使用 Alpaca 的 `paper=True`，没有实盘端点切换参数。

超时或提交结果不确定时，使用原 `client_order_id` 查询，不盲目重试。持久化阻断保存在项目固定的 `data/paper-state/<key-hash>.reconciliation.json`；改变日志路径不能解除同一 key 的阻断，旧日志旁标记也会被识别。此时必须人工核对 broker 订单和持仓、保留证据后再决定如何恢复，不能为了重跑直接删除标记。部分成交不等于完全成交，撤单请求被接受也不等于已终止。

## 第二步：市场环境与 walk-forward 验证

第二阶段不覆盖第一阶段模型，而是建立独立的 `ml2` 研究管线：

```bash
python main.py ml2 --symbols SPY JPM XOM WMT JNJ BTC-USD --cost-bps 5
# 复算现有缓存，不下载或修改数据：
python main.py ml2 --offline
```

各 section 的职责：

- `ml/features.py`：10 个标的自身特征，所有输入滞后一日，预测下一交易时段 open-to-close。
- `ml/regime.py`：由 SPY、QQQ、IWM、VIX 和美国十年期收益率代理构造 9 个市场环境特征；使用保守可用日期的 backward-as-of 连接，限制过期数据。
- `ml/walkforward.py`：12 段 expanding walk-forward；每段测试前留出 5 个 session 的 purge gap，并只用更早数据训练。
- 成本压力测试：同时报告 0、5、10、20 bps，不允许只展示最有利的成本假设。
- 稳定性门槛：综合 AUC、Sharpe、回撤、不同折叠的一致性、训练集基准 log-loss 和双倍成本收益必须同时通过。
- `ml/metrics.py`：六标的固定等权资金份额，无数据或 FLAT 时该份额持有现金，不把周末股票资金转给 BTC；按日历日计算收益与年化。成本明确为单个活跃信号的完整往返成本。基准是相同资金分配的日内做多，不是 buy-and-hold。

输出文件：

- `artifacts/yahoo_walkforward_v2_model.joblib`：候选模型，`daily_pit_v2` 特征契约。未通过门槛时不得连接自动下单。
- `artifacts/yahoo_walkforward_v2_model.json`：完整指标、每折日期与表现、成本压力测试、依赖版本、数据指纹及模型 SHA-256。
- `artifacts/yahoo_walkforward_v2_oos.csv`：每条按折时间外预测，可独立复算指标。反复利用这些结果选择模型后，它们不再是独立的最终 holdout。

重训会更新上述默认候选文件；比较新实验时应通过 `--model`、`--metadata`、`--predictions` 指定新文件名，保留当前冻结模型。`shadow` 验证模型与 metadata 的哈希及契约，不接受旧文件静默混用。joblib 只能加载自己信任的本地文件，哈希配对不是对恶意文件的安全沙箱。

Yahoo 的调整后历史价格不是严格的 point-in-time 原始数据库，且当前标的列表存在幸存者偏差。因此即使 `ml2` 通过门槛，也只能进入 Paper 信号观察，不能据此进入实盘。

## 第三步：基本面、宏观 vintage 与新闻

第三阶段把外生数据写入同一个 point-in-time DuckDB，并为每次摄取保存请求参数、记录数和内容 SHA-256。先检查本机配置：

```bash
python main.py sources status
```

### SEC Company Facts

SEC 要求自动访问使用可识别的 User-Agent。先在本机 `.env` 填写你的真实联系邮箱：

```dotenv
SEC_USER_AGENT=QuantPaperResearch your_email@example.com
```

然后按股票摄取 10-K/10-Q 等报表事实：

```bash
python main.py sources sec --ticker JPM
```

`sources/sec.py` 保留申报编号、财务期间起止、单位、申报日期及数值，避免季度值与累计值覆盖。当前路径只使用 filing date 而非精确接受时刻，保守设为申报日之后的纽约当地午夜可用。

### FRED/ALFRED

在 `.env` 写入免费的 FRED API key：

```dotenv
FRED_API_KEY=你的_fred_key
```

```bash
python main.py sources fred --series DFF DGS10 CPIAUCSL UNRATE GDPC1 VIXCLS
```

`sources/fred.py` 使用 real-time period 输出并校验分页，保留 vintage 起止；可用时间保守设为 vintage 日之后的芝加哥当地午夜。API key 不进入请求审计记录，HTTP 异常也不会回显含 key 的请求 URL。

### Alpaca 新闻

新闻连接器复用已配置的 Alpaca Paper 数据凭据：

```bash
python main.py sources news --symbols SPY JPM XOM WMT JNJ BTCUSD --days 365 --limit 1000
```

`sources/alpaca_news.py` 只保存发布时间、首次看到时间、来源、URL、关联标的、质量标记和标题哈希，不保存受版权保护的文章正文。

生成数据覆盖和 point-in-time 约束报告：

```bash
python main.py sources report
```

结果写入 `artifacts/source-coverage.json`。历史回填新闻的 `first_seen_at` 晚于 `published_at`，在取得能证明历史传输时间的数据前，不得直接把这些新闻用于无偏回测。

指数历史成分仍保留为空：当前没有接入具有可靠公告时间和回溯授权的数据供应商。使用今天的成分股回测历史会造成幸存者偏差，因此代码不会自动抓取网页上的“当前成分”冒充历史数据。

## 下一步：forward-only shadow mode

Shadow mode 会刷新已完成的 Yahoo 日线，用冻结模型生成下一未来 session 的概率并写入 DuckDB，但模块不导入 broker、订单或交易客户端，因此不能下单：

```bash
python main.py shadow run
python main.py shadow report
```

每个信号保存生成时间、特征截止日、完整特征 payload/哈希、模型哈希及契约、模型训练截止日、准入状态、概率和方向，并预先锁定下一目标 session 及开收盘 UTC 时间。同一模型、标的与目标 session 唯一，跨周末重复运行也不会重记。

在未来完整日线出现后运行：

```bash
python main.py shadow settle
python main.py shadow report
```

结算器只读取事先声明的目标 session，在正式收盘后 30 分钟且 OHLC 校验通过后结算；缺 bar 就保持 pending，不能拿后一天替代。结算 OHLC 及哈希一并留存。信号必须在目标开盘至少 1 分钟前写入，不能使用陈旧特征去预测已开始的时段。概率 `>=0.55` 记为 LONG，`<=0.45` 记为 SHORT，中间区域记为 FLAT；活跃信号扣除完整往返成本。报告按模型/成本分组，不把多个单信号收益直接相加称作组合 PnL。

结果保存在 `artifacts/shadow-report.json`，原始 journal 位于 `data/research.duckdb` 的 `shadow_signals` 表。当前候选模型未通过准入，因此这些记录仅用于评估，不会触发 Paper 订单。

### 自动 shadow cycle

`python main.py shadow cycle` 在打开 DuckDB 前获取单实例文件锁，校验环境变量和 `.env` 合并后的两层订单开关均为 `NO`，刷新完整日线，再按 XNYS 日历处理结算和新信号。数据库中的结算/新增处于同一事务；JSON 报告通过临时文件原子替换（数据库与报告并非跨资源共同事务）。美股使用收盘后 30 分钟的完整日线，也允许周末补跑，但不能跨过已经开始的目标时段。BTC 的 UTC 日线无隔夜间隙，等完整前日 bar 后无法再假设成交于已过去的次日开盘，故暂时明确跳过，须先重定义延迟入场目标并重训。

Codex heartbeat `Quant Shadow Daily Cycle` 已设置为每天伦敦时间 22:15 在当前任务中执行该命令。通知策略为只提醒失败运行；任务绝不修改下单开关，也不提交 Paper 或真实订单。

这是本机任务，不是有运行时 SLA 的交易服务器；本轮保留原调度设置。休眠、关机或网络故障可能造成错过生成窗口，缺失不能用事后预测补成样本。
