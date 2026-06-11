# llm-batch-msg-label

【标签定义 → Prompt 构造 → 并发调用 → 鲁棒解析 → 断点续跑" 自动化流水线】

![cover](imgs/cover.jpg)

## 亮点

- **断点续跑** — 标签级进度 + 配置快照，中断后自动续跑，配置变更时警告
- **Evidence 举证** — LLM 返回行号自动还原为原始消息文本，无需人工回查
- **三级并发** — 批次大小 × 批次拆分 × 标签并发，灵活控制吞吐
- **鲁棒解析** — 容错识别 JSON / JSONL / 代码块，校验序号范围 + 标签名归一化
- **标签外部化** — Markdown 定义标签，multi/single 两种匹配模式，支持自动扫描
- **可观测性** — 完整日志（模型输入输出、每批耗时），企微通知推送

## 架构

```
┌─────────────────────────────────────────────────────┐
│  入口层    main.py — CLI 参数解析 → 启动打标流程     │
├─────────────────────────────────────────────────────┤
│  编排层    BatchLabeler — 串联全流程，标签级并发      │
├──────────────┬──────────────────────────────────────┤
│  LLM 集成层  │ PromptBuilder + LLMClient            │
│              │ 标签解析 → 模板填充 → API 调用 → 响应解析 │
├──────────────┼──────────────────────────────────────┤
│  数据访问层  │ FileLoader + DBManager               │
│              │ Parquet/CSV/Excel/JSON + StarRocks   │
├──────────────┼──────────────────────────────────────┤
│  状态追踪层  │ ProgressWriter — 断点续跑核心         │
├──────────────┼──────────────────────────────────────┤
│  通知层      │ Notifier → 企业微信 Webhook          │
├──────────────┴──────────────────────────────────────┤
│  基础设施层  Logger (单例) + Config (多环境)         │
├─────────────────────────────────────────────────────┤
│  配置与资源  config.json / 标签定义.md / Prompt 模板 │
├─────────────────────────────────────────────────────┤
│  数据层      data/*.parquet / output/progress.json  │
└─────────────────────────────────────────────────────┘
```

**文件结构：**

```
├── main.py                        # 入口：参数解析 → 配置加载 → 启动打标
├── config.json                    # 配置文件
├── prompts/
│   └── prompt_template_format.md  # Prompt 模板
├── labels/                        # 标签定义（Markdown）
├── scripts/
│   ├── config.py                  # 配置读取 + CLI 覆盖
│   ├── batch_labeler.py           # 编排核心：进度检查 → 分批 → 并发
│   ├── file_loader.py             # 数据加载 + 时间转换 + 去 HTML
│   ├── prompt_builder.py          # Prompt 构造 + 标签解析 + 响应解析
│   ├── llm_client.py              # LLM API 调用 + 重试
│   ├── progress_writer.py         # 断点续跑：文件级 + Label 级进度
│   ├── db_manager.py              # StarRocks/Impala 查询 + 导入导出
│   ├── notifier.py                # 企微通知
│   └── logger.py                  # 日志
├── data/                          # DB 导出的本地文件（自动跳过已存在的）
├── output/                        # 打标结果 + 进度文件
└── logs/                          # 运行日志
```

## 快速开始

```bash
# 编辑配置（填入 LLM API 地址和密钥）
vim config.json

# 准备标签定义文件（见下方「Label MD 写法」）
# 放到任意路径，通过以下方式指定：
#   - config.json 的 label.files 配置
#   - CLI --labels_file 参数
#   - 都不配则自动扫描 labels/ 目录下的 .md 文件

# 本地文件打标
python main.py --data_file data/chat_20260501.parquet

# DB 导出 + 打标
python main.py --db_table dw.fact_chat --db_date_field create_time --db_date_field_value 20260501
```

## Label MD 写法

标签定义文件用 Markdown 格式，**一个文件可包含多个标签**，用 `# 标签名` 分隔。每个标签写清楚「算」和「不算」的规则，帮助 LLM 精准判断。

**格式：**

```markdown
# 标签名
一句话概括标签含义。

算：
- 命中的情况1（示例语句）
- 命中的情况2（示例语句）

不算：
- 不命中的情况1（示例语句）
- 不命中的情况2（示例语句）
```

完整示例见 [`labels/举例.md`](labels/举例.md)。

**`label_match` 模式：**

| 模式 | 含义 | 适用场景 |
|------|------|----------|
| `multi` | 一条对话可命中多个标签 | 流失预警 + 升级诉求可同时命中 |
| `single` | 一条对话最多命中一个标签，取最匹配 | 互斥分类（如：咨询/投诉/建议） |

标签文件放到任意路径，三种指定方式（优先级从高到低）：

**① CLI 参数** — `--labels_file` + 可选 `--labels_match`
```bash
python main.py --data_file data/test.parquet \
  --labels_file "labels/a.md,labels/b.md" \
  --labels_match "multi,single"
```

**② config.json** — `label.files` 数组，每项 `path`（必填）+ `label_match`（默认 `multi`）
```json
"label": {
  "files": [
    { "path": "labels/a.md", "label_match": "single" },
    { "path": "labels/b.md" }
  ]
}
```

**③ 自动扫描** — `config.json` 未配置 `label.files` 且 CLI 未指定 `--labels_file`，自动扫描 `labels/` 目录下所有 `.md` 文件，默认 `multi`

## 数据源

### 本地文件

指定 `--data_file`，支持 parquet / csv / xlsx / json。数据格式要求见下方「数据文件格式要求」。

### DB 数据库

指定 `--db_table` + `--db_date_field_value`，从 StarRocks 查询导出到本地 parquet，再进入打标流程。可选 `--db_result_table` 回写结果到数据库。

目前仅支持 StarRocks，如需适配其他数据库请自行扩展 `scripts/db_manager.py`。



## 最终 Prompt

Prompt 模板（`prompts/prompt_template_format.md`）有 4 个占位符，运行时自动填充：

| 占位符 | 填入内容 |
|--------|----------|
| `{{labels_text}}` | 标签定义（算/不算规则 + 合法标签名清单） |
| `{{dialogues_text}}` | 分批对话内容（带序号和 line 范围） |
| `{{match_rule}}` | multi/single 匹配规则 |
| `{{output_format}}` | JSON 输出格式约定 |

**填充后的完整示例：**

```
对以下客户对话进行标签分类。每段对话包含多条消息，请判断每段对话是否命中以下标签，若命中则给出确定依据（哪条消息）。


# 标签定义：
1. 流失预警
客户表现出离开倾向但尚未明确离开，仍有挽留窗口。下游用于触发挽留策略。

算：
- 比较竞品暗示要走（"XX银行利率比你们高多了"）
- 提到正在考虑其他选项（"我再看看别的平台吧"）
- 询问销户/解绑流程但未执行（"怎么注销账户"）
- 消极回应挽留（"随便吧"、"都行"）→ 不是真的无所谓，是失望后的放弃信号

不算：
- 已明确要求销户（"帮我注销"）→ 已流失，非预警
- 正常询问产品信息（"你们利率多少"）→ 无离开倾向
- 单纯抱怨未暗示离开（"手续费太高了"）

2. 升级诉求
客户当前问题未解决且诉求强度升级，需要优先处理或高级客服介入。下游用于触发工单升级。

算：
- 反复来电同一问题（"这是第三次打了，还没解决"）
- 明确要求升级处理（"让你们主管来"、"找能拍板的人"）
- 设定最后期限（"今天不解决我就投诉了"）
- 从咨询转为施压（前几轮正常沟通，突然语气转变）

不算：
- 首次来电咨询（"我想问下…"）→ 未升级
- 正常催促（"麻烦快一点"）→ 诉求强度未升级
- 单纯情绪宣泄无具体诉求（"太气人了"）→ 无待解决问题

⚠️ label_value 只能填以下值之一（原样复制，不可修改）：
- 流失预警
- 升级诉求

# 输出:
- 直接输出 JSON 数组，禁止在 JSON 前后添加任何分析、说明、解释文字
- 不要用 markdown 代码块包裹。
- 同一对话+同一标签只需输出一个对象，每个对象单独一行
- 列出最多三个关键依据的消息line序号数组
- {"conv": "对话序号数字", "label_value": "命中的标签名", "line": "关键依据的消息序号数组"}


# 规则：
- 按对话序号依次逐条分析判断，不可遗漏不可乱造
- 仅根据客户明确表达的意图判断，不要推测隐含意图
- ⚠️ 对话line序号严禁越界：每个对话标注了该对话的line序号范围，line数组中的每个数字都必须在该范围内
- 命中多个标签 → 输出多个对象
- 不命中任何标签 → 不输出该对话


# 对话列表：
---对话1---
[1] 你好，我想问下你们定期存款利率是多少
[2] 嗯，XX银行三年期能给到2.9，你们呢
[3] 行吧，我再对比对比
（共3条消息，line范围:1-3）

---对话2---
[1] 这是第三次打电话了，上回说48小时解决
[2] 现在都第五天了还没人联系我
[3] 让你们主管来处理，今天必须给我个说法
（共3条消息，line范围:1-3）

---对话3---
[1] 麻烦帮我查下账户余额
[2] 好的谢谢
（共2条消息，line范围:1-2）
```

期望 LLM 返回：

```json
[{"conv": 1, "label_value": "流失预警", "line": [2, 3]},
 {"conv": 2, "label_value": "升级诉求", "line": [1, 3]}]
```

- 对话1：比较竞品 + "再对比对比" → 命中流失预警
- 对话2：反复来电 + 要求主管 → 命中升级诉求
- 对话3：正常查询 → 不命中，不输出

LLM 返回的 `line` 序号会被自动还原为原始消息文本作为 `evidence`，无需人工回查对话定位依据：

```
LLM 返回:  {"conv": 1, "label_value": "流失预警", "line": [2, 3]}
自动还原:  evidence = "[15:30:15] 嗯，XX银行三年期能给到2.9，你们呢\n---\n[15:30:28] 行吧，我再对比对比"
```

## CLI 使用

### 本地文件打标

```bash
python main.py --data_file data/chat_20260501.parquet
```

`--config` 默认读 `config.json`，可指定专属配置：

```bash
python main.py --config config_product.json --data_file data/chat_20260501.parquet
```

### DB 导出 + 打标 + 结果回写

```bash
python main.py \
  --db_table dw.fact_chat \
  --db_date_field create_time \
  --db_date_field_value 20260501 \
  --db_date_format yyyyMMdd \
  --db_result_table dw.label_result_20260501
```

从 DB 按日期拉数据 → 打标 → 自动上传结果表。本地 parquet 已存在时跳过 DB 查询，直接进入打标。

### 中断后续跑

```bash
# 跟上次一模一样的命令再跑一次
python main.py --db_table dw.fact_chat --db_date_field create_time --db_date_field_value 20260501
```

自动：跳过 DB 查询（本地文件在）→ 跳过已完成的 label → 从断点 offset 继续。

### 试运行：只看 Prompt 不调 LLM

```bash
python main.py \
  --data_file data/sample.parquet \
  --dry_run
```

构造完整 prompt 并打印，不消耗 token。验证标签定义、数据格式是否正确。

### 切模型 + 调并发 + 只跑指定标签

```bash
python main.py \
  --data_file data/chat_20260501.parquet \
  --model_id qwen-plus-latest \
  --batch_size 200 \
  --batch_size_split 4 \
  --batch_label 3 \
  --labels_file "labels/a.md,labels/b.md" \
  --labels_match "multi,single"
```

- 换便宜模型跑
- batch 200 条，拆 4 片并发调 LLM，3 个 label 并行
- 只跑 2 个标签，流失预警用 multi，物流问题用 single

### 强制清空重跑

```bash
python main.py \
  --data_file data/chat_20260501.parquet \
  --force_run
```

清空已有进度和结果，从头跑。场景：换了标签定义或换了模型。

## 输出结构

```
output/
├── progress.json                    # 进度文件（断点续跑依据）
└── {数据文件名}_{文件大小}/           # file_key，由数据文件的文件名+大小生成
    ├── 举例/                        # label md 文件名（非标签名）
    │   ├── result.jsonl             # 打标结果
    │   ├── parse_errors.jsonl       # 解析失败的响应
    │   └── parse_warnings.jsonl     # 解析异常但已兜底的响应
    ├── 其他标签文件名/
    │   └── ...
    └── merged_result.jsonl          # 所有 label 合并结果
```

**result.jsonl 每行格式：**

```json
{"token_id": "d4e01310", "label": "举例", "label_value": "流失预警", "evidence": "[15:30:15] 嗯，XX银行三年期能给到2.9，你们呢\n---\n[15:30:28] 行吧，我再对比对比"}
{"token_id": "a7f29c01", "label": "举例", "label_value": "升级诉求", "evidence": "[14:02:10] 这是第三次打电话了，上回说48小时解决\n---\n[14:02:40] 让你们主管来处理，今天必须给我个说法"}
{"token_id": "b3e81d45", "label": "举例", "label_value": "流失预警", "evidence": "[No.3] 怎么注销账户"}
```

- `label` — label md 文件名（非标签名）
- `label_value` — 命中的标签名
- `evidence` — LLM 返回的 line 序号自动还原为原始消息文本，多条证据用 `---` 分隔

## 断点续跑机制

```
运行中断
  ↓ 重新执行相同命令
DB 模式：query_to_file() → 本地文件已存在 → 跳过 DB 查询
  ↓
resolve_pending_labels() → 读 progress.json → 只返回未完成的 label
  ↓
writer.init_progress() → file_key 已存在 → 不重置进度
  ↓
LabelWriter.init_progress() → 返回已保存的 offset
  ↓
process_label() → 跳过 offset 之前的 batch → 从断点继续
```

进度按 `{文件名}_{文件大小}` 匹配，只要数据文件没变就能正确续跑。配置变更（模型、batch_size 等）会触发警告，建议 `--force_run` 重跑。

## 数据文件格式要求

支持 parquet / csv / xlsx / json，必须包含：

- **主键列**：每条对话的唯一标识（`primary_key` 指定）
- **对话内容列**：`message_column` 指定，格式为 `时间+分隔符+内容`，多条消息用 `message_multi_sep` 分隔

示例（`message_time_format=timestamp_ms`, `message_time_sep=:`, `message_multi_sep=$$$`）：

| token_id | dialogue_content |
|----------|-----------------|
| d4e01310 | 1704067218000:你好$$$1704067225000:我要退货 |

`message_time_format=none` 时，整条消息当纯文本处理，不拆时间。

## 配置说明

`config.json` 分 7 个模块：

### file — 数据文件配置

| 字段 | 必填 | 说明 |
|------|:----:|------|
| `primary_key` | ✅ | 数据主键字段名（如 `token_id`） |
| `message_column` | ✅ | 对话内容字段名（如 `dialogue_content`） |
| `message_time_format` | ✅ | 消息时间格式：`timestamp_ms` / `timestamp_s` / `yyyymmddhhmmss` / `iso8601` / `raw` / `none` |
| `message_time_sep` | ✅ | 时间与内容的分隔符（如 `:`），`none` 模式下无效 |
| `message_multi_sep` | ✅ | 同一字段内多条消息的分隔符（如 `$$$`） |
| `output_dir` | | 输出目录，默认 `output` |
| `result_dir` | | 结果目录，默认用 `output_dir` |
| `result_file` | | 合并结果文件名，空则自动生成 |
| `log_dir` | | 日志目录，默认 `logs` |

### model — LLM 模型配置

| 字段 | 必填 | 说明 |
|------|:----:|------|
| `base_url` | ✅ | API 地址，OpenAI 兼容格式（如 `http://host:port/v1`） |
| `api_key` | ✅ | API 密钥 |
| `model_id` | ✅ | 模型标识（如 `deepseek-v4-pro`） |
| `model_timeout` | | 单次请求超时秒数，默认 `120` |
| `max_retries` | | 失败最大重试次数，默认 `3` |
| `max_tokens` | | 响应最大 token 数，默认 `4096` |

### prompt — Prompt 配置

| 字段 | 必填 | 说明 |
|------|:----:|------|
| `prompt_template_path` | ✅ | Prompt 模板文件路径 |

### label — 标签配置

| 字段 | 必填 | 说明 |
|------|:----:|------|
| `files` | | 标签定义文件列表，每项含 `path`（必填）和 `label_match`（默认 `multi`）。不配则自动扫描 `labels/` 目录 |

### processing — 处理配置

| 字段 | 必填 | 说明 |
|------|:----:|------|
| `batch_size` | | 每次 LLM 调用处理的对话条数，默认 `20` |
| `batch_size_split` | | 每批拆成几份并发调 LLM，默认 `1` |
| `batch_label` | | label 级并发数，默认 `1` |
| `dry_run` | | 试运行模式，只构造 prompt 不调 LLM |

### db — 数据库配置

| 字段 | 说明 |
|------|------|
| `host` / `port` / `username` / `password` / `database` | StarRocks 连接信息 |
| `table` | 数据表名 |
| `date_field` / `date_format` | 按日期筛选的字段和格式 |
| `table_output_dir` | DB 导出文件保存目录，默认 `data` |
| `result_table` | 结果回写表名 |
| `s3.*` | S3 配置（StarRocks 导入导出使用） |

### notify — 通知配置

| 字段 | 说明 |
|------|------|
| `wecom_webhook_key` | 企业微信 webhook key |
| `title` | 推送标题，默认 `批量打标` |

