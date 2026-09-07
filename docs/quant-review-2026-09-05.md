# Quant 代码审查与修复记录

审查日期：2026-09-05。范围：本机 Python 项目的研究数据、ML 验证、shadow 信号、合成撮合和 Alpaca Paper 生命周期。没有调用任何下单/撤单接口，没有改变两层交易开关，没有在报告中读取或展示密钥。

## 结论

现在是一套可继续研究的原型，不是已验证有交易优势的策略，也不是生产 HFT 系统。本轮修复优先解决“收益是否算对、数据在当时是否可知、订单是否可能失控”。保留现有梯度提升树作为可解释、可复算的基线，没有把更大或更新的模型直接接入交易。

完整测试 **88 项通过**；`pip check` 无依赖冲突，源码编译检查通过。这证明已覆盖的工程行为通过测试，不证明盈利或所有异常都已覆盖。NumPy 2.5 / pandas 2.3 日期运算仍有弃用警告，应在独立环境验证升级，不宜忽视未来兼容性。

## 已直接修复的问题

P1 表示可能破坏交易状态或研究结论，应在继续研究前处理；P2 表示重要的可靠性和可复现性问题。

| 优先级 | 原问题与影响 | 本轮修复 | 主要位置 |
| --- | --- | --- | --- |
| P1 | `partially_filled` 可能被当作 `filled`，撤单 ACK 被当作终止，超时可能留下未知敞口 | 精确枚举状态；先确认入场终止再处理真实已成交量；不确定提交按原 client ID 查询；残余/未知状态持久化阻断 | `alpaca_paper.py`、`tests/test_alpaca_lifecycle.py` |
| P1 | 更换 audit 路径可以绕过旧的异常阻断 | 按 API key 哈希锁定固定项目状态目录；兼容保留旧标记；同 key 换日志路径仍阻断 | `data/paper-state/`、`alpaca_paper.py` |
| P1 | 未成交订单没有完整预留风险；其他标的报价可触发陈旧报价成交；回放结束可制造成交 | 按方向预留挂单风险；只消费本标的到达后的报价；共享可见流动性；限价约束滑点；EOF 取消未完成订单 | `risk.py`、`execution.py`、`engine.py` |
| P1 | 股票/加密货币交易日不一致时，组合平均方式隐含改变资金分配 | 固定标的等权资金份额，缺失/FLAT 份额持现金；日历日年化；最大回撤计入第一日亏损；明确完整往返成本 | `ml/metrics.py` |
| P1 | 跨市场日线特征连接和训练/推理不一致；可能静默使用旧行 | 用保守可用日期进行向后时间连接、过期限制；拒绝无效最新特征；修复 RSI 单边上涨；增加未来数据扰动测试 | `ml/features.py`、`ml/regime.py` |
| P1 | Shadow 未预先固定目标，后补第一条 bar 会改变原预测问题 | 写入前固定目标 session/开收盘；禁止迟到和陈旧特征；只结算该目标，缺失保持 pending；完整保存特征和结算 OHLC/hash | `shadow.py`、`sessions.py` |
| P1 | 完整 BTC 前日 bar 发布时，下一 UTC 日开盘已经过去 | 暂停 BTC daily shadow；不再假设能成交于已经过去的价格，等待独立延迟入场标签 | `shadow.py`、`scheduler.py` |
| P1 | SEC 季度/累计期或不同单位可能覆盖；FRED 版本/分页不完整；Yahoo 回填冒充历史可知 | 保留报表期间起止、单位、版本；校验 FRED 实时区间和分页；按当地保守时间设可用性；Yahoo 按实际首次观察留版本 | `sources/`、`warehouse.py` |
| P2 | 旧模型与新特征可被混用；默认自动 early stopping 使用非时间验证；单类别测试折被跳过 | 模型 SHA-256 与 metadata/特征契约配对；冻结迭代数、关闭内部自动 early stopping；保留单类别折并将 AUC 标空 | `ml/training.py`、`ml/walkforward.py` |
| P2 | 跨日期重复 shadow、部分写入、锁获取太晚；审计链只看尾部 | 目标 session 唯一键；打开 DB 前加锁；结算和新增同一事务；报告原子替换；完整审计链验证/文件锁/fsync | `shadow_cli.py`、`audit.py` |
| P2 | 日历默认历史范围可能丢弃早期数据；可选依赖未声明完整 | 按输入日期范围申请日历并测试 1993 年数据；补 data 依赖；记录已测试版本快照 | `sessions.py`、`pyproject.toml`、`requirements-tested.txt` |

日历包含提前收盘、假日与 DST 处理。收盘后 30 分钟只是本项目的保守缓冲，不能保证供应商数据已经最终定稿。数据库与 JSON 文件不是跨资源共同事务：若报告写失败，数据库仍可从 journal 重建报告。

## 修正后的模型结果

使用已有 Yahoo 缓存离线重训，没有为提高分数筛选新股票或事后优化阈值。模型仍为 HistGradientBoostingClassifier，10 个自身特征 + 9 个市场环境特征。12 段 expanding walk-forward，37,279 条可用训练/评估数据，13,424 条按折时间外预测，评估窗口 2018-05-25 至 2026-09-03。

| 指标 | 结果 |
| --- | ---: |
| ROC AUC | 0.5137 |
| 准确率 | 51.59% |
| Brier score | 0.2518 |
| Log-loss / 训练集基准 | 0.6970 / 0.6926（模型更差） |
| 5 bps 往返成本累计收益 | +1.15% |
| 5 bps Sharpe / 最大回撤 | 0.066 / -26.01% |
| 0 / 10 / 20 bps 累计收益 | +50.55% / -32.05% / -69.34% |
| 自动 Paper 信号准入 | 未通过 |

解读：排序能力略高于 0.5，不等于有统计可信的可交易优势；概率质量没有胜过基准，且结果对成本非常敏感。准入阈值本身也是研究约定，不是行业认证。

这些是 SPY、JPM、XOM、WMT、JNJ、BTC-USD 的六标的理论 open-to-close 结果。固定资金份额、每日再平衡的研究口径没有完整建模现金/保证金、借券、实时成交、资金费率和市场冲击。尤其 BTC 标签的执行时点问题尚未重训解决，所以这些数字不能视为可执行策略回测。报告中的基准 +212.80% 是同样资金口径的日内做多，并非买入持有。

新旧结果同时变更了特征和收益计算，不能把数字变化归因为“模型提升”。历史样本在多轮研究中已经被反复观察，不能继续冒充全新的最终留出集。

复算命令（会覆盖默认 v2 候选文件；新实验请指定新输出名）：

```bash
.venv/bin/python main.py ml2 --offline
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

详细证据位于 `artifacts/yahoo_walkforward_v2_model.json`，逐条预测位于 `artifacts/yahoo_walkforward_v2_oos.csv`。模型 metadata 与 joblib 通过 SHA-256 配对；只可加载自己信任的文件，哈希不是恶意 pickle/joblib 的隔离沙箱。`daily_pit_v2` 是特征契约名称，不代表 Yahoo 历史已成为严格 PIT 数据。

## 本轮实际运行与数据迁移

- 备份：`artifacts/review-backup-KwMivkOT/` 保存旧数据库、模型、metadata、shadow 报告和 README；没有删除这些研究证据。
- 仓库迁移至 schema v4；旧 SEC/FRED 模糊记录保留在 legacy 表，不参与新的 as-of 查询。现有两类数据行数均为 0。
- 70,712 条旧 Yahoo `adjusted-v1` 行保留，但全部排除在严格 as-of 查询之外。后续新快照只从实际首次观察时间开始可用；不能恢复过去并未记录的供应商版本。
- 新闻有 2,500 条 first-seen 元数据，没有文章正文、情绪特征或实际进入 ML 的新闻信号。基本面、宏观 vintage、历史指数成分仍未真正填充并接入模型。
- 实际执行了一次 `shadow cycle`，只下载行情和写本地 journal/report，未调用交易接口。
- 旧 6 条无新契约/无预定目标的 shadow 记录保留为 `INVALIDATED_LEGACY`，原状态保存在 `legacy_status`，不混入新绩效。
- 新增 5 条 v2 PENDING 信号：特征截止 2026-09-04，按 XNYS 日历锁定目标 2026-09-08，SPY/JPM/XOM/WMT/JNJ 全部为 FLAT。尚无已结算新样本，不能报告 forward 收益。
- BTC 明确 skipped。两层有效 Paper 开关核验为关闭；已有订单审计链验证通过。原每日本机 shadow 调度设置保留，本轮未新增或更改定时任务。

## 接下来按什么顺序做

### 1. 先冻结研究协议与可执行标签

优先把美股日线做成一条完整、可信的链路：明确决策时间、入场/退出窗口、可成交报价与延迟，先设实验预算、成功条件、成本场景与最终保留样本，再开始选模型。BTC 单独设计完成 bar 后的延迟入场目标；期权不要复用股票方向标签。当前 0.55/0.45 阈值未经收益效用与校准优化，不应直接当交易规则。

### 2. 补真正可知、可复现的数据

先填 SEC/ALFRED 的必要字段并做实体/时间对齐；新闻需要可信的历史首次到达时间、去重、公司实体关联及版本。未来加入新闻模型时，其训练截止时间和历史事实记忆也需要检查。市场数据需要退市/改名/历史成分、公司行动及数据授权记录。今天回填的 Yahoo 调整价、今天的指数成分或今天的大模型回答都不能替代当年可知信息。

### 3. 设计能证明增量价值的模型实验

将成本后的条件收益/风险作为目标候选，不仅比较涨跌准确率。设置恒定概率、线性/逻辑回归、当前树模型等固定基线；按时间做内层选择与外层验证，增加分资产/年份/市场环境归因、概率校准、区块 bootstrap 不确定区间和多重试验控制。任何新模型都必须在同一预声明口径下胜出，再保持冻结模型做未来观察；不能因历史测试不好就反复调参直到通过。

### 4. 再补生产级组合与执行

建立统一账户级订单状态机：持仓与活动订单对账、交易更新流、重连、账户/资产权限、坏报价和断流保护、每日风险重置、人工 kill switch、审计外部锚点及恢复演练。当前 API-key 级本机锁不能代替跨 key/跨主机的账户级协调；密钥轮换后的阻断迁移也需操作规程。

组合层需处理相关性、集中度、波动目标、保证金、借券、容量、尾部风险和压力情景。期权另需完整链/报价、合约生命周期、美式行权/指派、Greeks、分红与利率曲线；当前 Black-Scholes 只是合成数据校验。高频另需带序列号的逐笔/盘口、时间同步、延迟分布和排队模型，不能用 Yahoo 日线或本机定时脚本宣称 HFT。

### 5. 最后才扩大模拟实盘

目前先保持不下单的 forward shadow。待时序和数据验收完成，再单独审批受限 Paper 接口验收与更长观察期；不能把测试通过当作策略准入通过。Alpaca 官方明确指出 Paper 不完整反映市场冲击、延迟滑点、队列等因素，且模拟成交量不按 NBBO 可见数量约束，因此 Paper 盈利也不是实盘收益保证。[Alpaca Paper 官方说明](https://docs.alpaca.markets/us/docs/paper-trading)

## 每个 section 的职责

| 文件/目录 | 职责与边界 |
| --- | --- |
| `domain.py` / `options.py` | Decimal 交易对象、乘数记账与简化期权定价；不是生产期权风险系统 |
| `data.py` / `strategy.py` | 可复现合成报价与盘口基线；不是历史市场证据 |
| `risk.py` / `execution.py` / `engine.py` | 本地风控、延迟撮合、时序驱动和记账 |
| `audit.py` | 检测链损坏、并发锁与持久写入；没有外部锚点时不能保证防篡改 |
| `alpaca_paper.py` / `alpaca_cli.py` | 受限的手动 Paper 通道与订单生命周期；不会运行 ML 自动交易 |
| `sources/` / `warehouse.py` | 外生数据解析、版本存储、保守可用时间和 as-of 查询 |
| `ml/yahoo.py` / `features.py` / `regime.py` | Yahoo 研究缓存、自身特征和市场环境对齐 |
| `ml/training.py` / `walkforward.py` / `metrics.py` | 基线训练、时间验证、统一收益/成本口径与准入报告 |
| `sessions.py` / `scheduler.py` | 交易日历、完整 bar 条件、有效开关与进程锁 |
| `shadow.py` / `shadow_cli.py` | 冻结模型推理、预声明目标、幂等 journal、未来结算；完全不下单 |
| `tests/` / `requirements-tested.txt` | 回归测试与本次环境版本记录；后者不是带哈希的跨平台锁文件 |

## 核对的官方技术依据

HistGradientBoosting 的 `early_stopping='auto'` 与验证集参数是本轮检查内部验证行为的依据；本项目选择关闭它并使用固定迭代数，外层仍按时间切分。[scikit-learn 官方 API](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html)

SEC Company Facts 提供单位、期间与申报上下文；本项目的本地午夜可用规则是保守建模选择，不是 SEC 保证的精确发布时间。[SEC EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

FRED observations 参数定义了 output_type、实时区间、limit/offset 等；本项目保留版本并校验分页，不只读取一次响应就假设历史完整。[FRED 官方 API](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)
