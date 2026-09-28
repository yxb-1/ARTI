# ARTI：预测市场监测与证据分析

ARTI 从 Polymarket 和 Kalshi 的公开 API 抓取预测市场行情，保存 SQLite 历史快照，计算价格变化和异常信号，并可用单 Agent 或一轮双 Agent 辩论生成可复核的观察报告。它只读取公开数据，不执行交易，也不自动认定两个平台的市场对应同一事件。

## 安装与运行

需要 Python 3.12 或更新版本，以及 [uv](https://docs.astral.sh/uv/)。在项目目录执行：

```bash
uv sync
uv run arti
```

默认使用仓库中的 `config.example.json`，抓取两个平台的数据，只做程序分析。结果写入 `report.json`，历史快照写入 `arti.sqlite3`。这两个运行文件不会提交到 Git。

需要修改日常设置时，复制一份本地配置：

```bash
cp config.example.json config.json
```

编辑 `config.json` 即可。若选择 `single` 或 `debate` 模式，还需复制脱敏的环境变量模板并填写自己的百炼密钥和工作空间地址：

```bash
cp .env.example .env
uv run arti
```

`.env` 和 `config.json` 都不会提交到 Git。密钥只放在 `.env`，不要写入配置或报告。可以用命令行临时覆盖配置，例如 `uv run arti --platforms kalshi --mode single --limit 1`。`uv run arti --help` 可查看所有参数。

## 配置

| 字段 | 作用 | 示例默认值 |
| --- | --- | --- |
| `platforms` | 要抓取的平台，可选一个或两个 | `polymarket`, `kalshi` |
| `mode` | `none` 只分析；`single` 每市场一次模型调用；`debate` 每市场四轮调用 | `none` |
| `model` | 百炼模型名称 | `qwen3.8-flash` |
| `limit` | 每个平台最终选入报告的市场数上限 | `5` |
| `db` | SQLite 快照文件路径，相对于项目目录 | `arti.sqlite3` |
| `output` | JSON 报告路径，相对于项目目录 | `report.json` |
| `jump_pp` | 概率跳变阈值，单位为百分点 | `8` |
| `poll_count` | 连续抓取轮数 | `1` |
| `interval` | 多轮抓取间隔，单位为秒 | `300` |

报告文件每次运行会覆盖；SQLite 快照持续积累。设置 `poll_count > 1` 时，程序在两轮之间等待 `interval` 秒。首次抓取通常缺少可比较历史；持续采样后才会出现 5 分钟、1 小时和 24 小时的变化指标。

## 数据与判断流程

1. **抓取**：并行读取两个平台的开放市场。Polymarket 按近 24 小时成交量抓取候选；Kalshi 排除组合市场。适配器只接受能明确映射到 YES 的二元市场。
2. **统一与存储**：记录市场 ID、YES 价格及价格来源、买卖报价、成交量与单位、流动性或报价深度、平台更新时间和本系统抓取时间。每次抓取的候选市场都会存入 SQLite。
3. **筛选与分析**：排除关闭、价格无效、数据过旧等市场；按平台分别计算关注度评分。历史快照用于计算概率变化、成交量激增等信号。不同平台的原始成交量和流动性单位不直接互比。
4. **模型判断**：`single` 整理证据和风险；`debate` 由研究 Agent 提议、质疑 Agent 反驳、研究 Agent 回应，最后由质疑 Agent 综合。每个市场独立调用。完整角色约束见 [PROMPTS.md](PROMPTS.md)。

模型只能引用输入中存在的证据 ID。API JSON Schema 和本地 Pydantic 校验共同约束输出。最终状态为 `watch`、`investigate`、`avoid` 或 `insufficient_evidence`；它们是研究观察状态，不是交易指令。模型失败时仍保留行情和程序分析，不生成替代判断。

## 报告结构

`report.json` 包含：

- `settings`：本次生效的非敏感配置。
- `reports`：各选中市场的行情、程序分析、模型判断或 `assessment_error`。辩论前三轮保存在 `debate_turns`，最后结论保存在 `assessment`；中途失败时，已校验的轮次仍会保留。
- `skipped_assessments`：模型提供方因内容审查拒绝的市场及原因。程序最多再尝试三个候选补足 `limit`。
- `fetch_errors`：公开 API 的抓取错误。

公开 API 或模型服务可能超时、限流或拒绝输入；这些情况会在报告中明确标记。市场价格是交易报价隐含的概率，不代表事件已被证实。模型也不会查询新闻或外部事实。

## 测试

```bash
uv run pytest -q
```

固定样本测试覆盖平台字段转换、快照窗口、异常规则、候选筛选、证据 ID 校验和辩论失败时的发言保存。测试使用模拟模型响应，不调用真实模型。平台接口参考：[Polymarket 文档](https://docs.polymarket.com/) · [Kalshi Get Markets](https://docs.kalshi.com/api-reference/market/get-markets)。
