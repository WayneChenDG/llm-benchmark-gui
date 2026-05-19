# JISUMAN LLM Benchmark GUI

OpenAI 兼容接口的 LLM serving benchmark 桌面工具。  
主程序使用 Python 标准库 Tkinter 构建 GUI，核心 benchmark 不依赖重型运行时；可选安装 `matplotlib` 用于并发扫测图表展示和 PNG 导出。

Metrics aligned with vLLM bench serve (TTFT / TPOT / ITL / E2EL) and NVIDIA GenAI-Perf / NIM-style serving metrics (output token throughput, request throughput).

## 功能特性

- **JISUMAN 品牌 GUI**：全局 header 显示 JISUMAN 标识，支持受控尺寸 logo。
- **中英文界面切换**：默认简体中文，可切换 English；语言偏好保存到 `llm_benchmark.ini`。
- **单次基准测试**：连通性检测、模型列表获取、基线请求、并发压力测试。
- **并发扫测**：按并发档位批量测试，输出吞吐、延迟、成功率和专家分析简评。
- **模型无关流式解析器**：集中式 `_parse_stream_chunk` 统一处理 `delta.content` / `delta.reasoning_content` / `delta.reasoning`（Qwen3）/ `choices[].text` / tool delta；未知字段收入 `unknown_delta_keys` 诊断，不影响主流程。
- **Stream Parser Profile**：每次 benchmark 自动生成 `parser_profile`，记录 `parser_mode`（content_only / reasoning_only / reasoning_and_content 等）、`observed_delta_keys`、`generated_fields`、`usage_source` 等能力档案，写入 JSON 报告。
- **细粒度流式时序指标**：在 TTFT / TPOT / ITL / E2E 基础上增加 First Generated Token、First Answer Token、First Reasoning Token、First Generated Gap、Generated ITL、Answer ITL、Reasoning ITL、Stream Event ITL（校准参考），均严格区分 None（不可用）与 0.0（真实零值）。
- **stream_debug 诊断**：捕获 `observed_delta_keys`、`unknown_delta_keys`、`generated_field_counts` 及前 5 个 delta 样本（截断至 200 字符），自动警告未捕获到生成字段、仅有 reasoning 无 content 等情况。
- **轻量状态动画**：右上角文本 spinner 显示 phase、进度、fail 数和耗时，不重绘图表，不影响测试性能。
- **历史记录**：SQLite 保存 single / sweep 两类记录，列表直接显示，双击查看详情和图表。
- **E2E 延迟直方图**：Benchmark tab 内置 Tk Canvas 直方图，支持折叠区展开后的自动重绘。
- **扫测图表与报告**：GUI 内嵌 2x2 sweep chart；可选保存 JSON、Markdown、PNG，并可保存到 History。
- **专业指标口径**：TTFT / FVT / FVG / E2E / TPOT / ITL / TPS / RPS 均保持独立口径。
- **指标一致性校验**：自动检查 token 汇总、吞吐公式、TTFT 与流式模式等一致性。
- **错误诊断**：连接、认证、权限、限流、服务端错误等分类提示。
- **配置持久化**：API、模型、prompt、测试参数、语言偏好可保存到 `llm_benchmark.ini`。

## 快速开始

```bash
cd /opt/llm-benchmark

# GUI
python3 llm_benchmark.py

# 调试模式
python3 llm_benchmark.py -debug

# Windows 可双击
# llm_benchmark.pyw
```

Ubuntu 桌面快捷方式：

```bash
cp llm_benchmark.desktop ~/Desktop/
```

可选依赖安装：

```bash
# sweep 图表展示和 PNG 导出
python3 -m pip install matplotlib

# 更好的 header logo 缩放（可选）
python3 -m pip install Pillow
```

## 默认配置

| 参数 | 默认值 |
|------|--------|
| 并发预设 | 标准基线 — C8/N80（默认） |
| 请求总数 | 80 |
| Max Tokens | 512 |
| Temperature | 0.0 |
| Stream | 是 |
| Warmup | 2 |
| 语言 | 简体中文 |

预设档位：

| 预设 | 并发 | 请求数 |
|------|------|--------|
| 快速校准 — C1/N5 | 1 | 5 |
| 标准基线 — C8/N80（默认） | 8 | 80 |
| 中高并发 — C16/N160 | 16 | 160 |
| 压力测试 — C32/N320 | 32 | 320 |

## 界面说明

### 参数设置

- API 地址、API Key、模型名称查询。
- 系统提示词和用户提示词。
- 生成参数：Max Tokens、Temperature、Stream、Warmup。
- 负载参数：并发预设、总请求数、自定义并发。
- 保存选项：测试报告、自动保存配置。
- 操作按钮：开始基准测试、保存配置、重置配置。

### 基准测试

- 状态卡：服务连接、基础测试、压力测试、测试耗时。
- 8 个指标卡：TTFT、FVT、E2E P95、输出吞吐 TPS、请求吞吐 RPS、TPOT、ITL、成功率。
- E2E 端到端延迟分布（e2el）：Benchmark 完成后自动展开并绘制直方图。
- 详细报告：包含测试配置、指标摘要、失败诊断和一致性校验。

### 并发扫测

扫测配置顺序：

1. 并发列表、请求倍数等扫测参数。
2. 输出选项：保存扫测数据 JSON、保存文字分析报告 Markdown、保存图表 PNG、保存到历史记录。
3. 开始扫测按钮。

扫测完成后会展示：

- 扫测状态日志。
- 2x2 图形分析。
- 专家分析简评。
- 输出文件路径或“未保存”。
- History 中的 sweep 记录。

### 历史记录

History tab 直接显示历史表，不使用折叠区包裹。

列：

- 时间 / 类型 / 模型 / 配置 / 核心结果 / 状态
- English 模式显示 Time / Type / Model / Config / Key Result / Status

支持记录类型：

- `single`：单次基准测试
- `sweep`：并发扫测

双击历史记录：

- single：显示概要、指标、E2E latency chart。
- sweep：显示扫测概览、并发档位表、2x2 图表、专家分析、输出文件路径和 Raw JSON。

## 核心指标

### Benchmark 主指标（与 vLLM bench serve 对齐）

| GUI 显示 | 报告全称 | 口径 |
|---------|---------|------|
| 首包延迟 TTFT | Time To First Token / First Stream Chunk | 请求发出到首个流式 JSON chunk |
| E2E / E2EL | End-to-End Latency | 请求发出到完整响应结束 |
| 输出吞吐 TPS | Output Token Throughput | total_output_tokens / duration_sec |
| 请求吞吐 RPS | Request Throughput | success / duration_sec |
| 单 Token 耗时 TPOT | Time Per Output Token | (E2E - TTFT) / max(output_tokens - 1, 1) |
| Token 间隔 ITL | Inter-Token Latency | generated_itl（相邻生成内容 chunk 间隔） |
| 成功率 | Success Rate | success / total_requests |

### 细粒度流式时序指标（诊断 / 用户体验）

| 指标 | 说明 |
|------|------|
| First Generated Token | 请求发出 → 首个非空 delta（content / reasoning / choices.text） |
| First Answer Token | 请求发出 → 首个非空 delta.content（仅回答正文） |
| First Reasoning Token | 请求发出 → 首个非空 delta.reasoning_content 或 delta.reasoning |
| First Generated Gap | First Generated Token − First JSON Chunk |
| First Answer Gap | First Answer Token − First JSON Chunk |
| Generated ITL | 相邻生成内容 chunk 间隔（主 ITL 来源） |
| Answer ITL | 相邻 delta.content chunk 间隔 |
| Reasoning ITL | 相邻 reasoning 字段 chunk 间隔 |
| Stream Event ITL | 相邻 choices chunk 间隔（包含 role-only，校准参考） |
| Visible TPOT | (E2E − First Generated Token) / (tokens − 1)，仅诊断用 |

> 所有细粒度指标在无数据时显示 **N/A**，不伪造 0.000s。

> 指标标准：JISUMAN LLM Benchmark Standard v1  
> 参考：vLLM bench serve · NVIDIA GenAI-Perf · NIM Benchmark

## 并发扫测输出

可选生成：

- `sweep_result_<timestamp>.json`
- `sweep_analysis_<timestamp>.md`
- `sweep_report_<timestamp>.png`

即使 JSON / Markdown / PNG 保存关闭，只要“保存到历史记录”启用，GUI 仍会把内存中的 sweep 结果写入 History。

PNG 和 GUI 图表依赖 `matplotlib`。未安装时程序不会崩溃，只显示跳过/不可用提示。

## 中文图表字体

程序会在使用 matplotlib 时延迟检测 CJK 字体，并设置：

```python
matplotlib.rcParams["axes.unicode_minus"] = False
```

优先字体包括 Noto Sans CJK、WenQuanYi、Source Han Sans、Microsoft YaHei、SimHei 等。找不到 CJK 字体时不会影响 benchmark，只可能影响中文图表渲染。

## 配置文件

`llm_benchmark.ini` 示例：

```ini
[api]
url = http://192.168.1.12:8000/v1
key = change-me-before-production
model = qwen3.5-122b-a10b-fp8

[prompt]
system = 你是一个有帮助的助手。
user = 请用300字左右介绍机器学习。

[test]
max_tokens = 512
temperature = 0.0
total_requests = 80
concurrency = 标准基线 — C8/N80（默认）
stream_mode = 是
warmup = 2
save_report = 否
auto_save = 否

[ui]
language = zh_CN
```

语言代码：

- `zh_CN`：简体中文
- `en_US`：English

## 历史数据库

默认路径：

```text
llm_benchmark_history.db
```

History schema 支持：

- `record_type = single`
- `record_type = sweep`
- 文件路径字段：`json_path`、`markdown_path`、`png_path`
- sweep 摘要字段：`concurrency_levels`、`max_output_tps`、`recommended_concurrency`、`analysis_summary`

PNG 二进制不会写入 SQLite，只保存文件路径。

## 支持的 API

所有兼容 OpenAI Chat Completions 的接口：

| 服务 / 模型系列 | 流式字段 | 备注 |
|----------------|---------|------|
| vLLM | `delta.content` | 标准格式 |
| 通义千问 / Qwen3 | `delta.reasoning` + `delta.content` | reasoning 字段已原生支持 |
| DeepSeek-R1 | `delta.reasoning_content` + `delta.content` | reasoning_content 字段支持 |
| GLM | `delta.content` | 标准格式 |
| OpenAI / GPT | `delta.content` | 标准格式 |
| 其他 OpenAI-compatible 服务 | 自动检测 | 未知字段记录到 stream_debug |

## 系统要求

- Python 3.8+
- Tkinter
- 标准库：`sqlite3`、`urllib`、`threading`、`configparser`
- 可选：`Pillow` 用于更好的 header logo 缩放
- 可选：`matplotlib` 用于 sweep 图表和 PNG 导出

可选安装命令：

```bash
python3 -m pip install matplotlib Pillow
```

## 项目结构

```text
llm-benchmark/
├── llm_benchmark.py          # 主程序
├── llm_benchmark.pyw         # Windows 无控制台启动
├── llm_benchmark.desktop     # Ubuntu 桌面启动
├── llm_benchmark.svg         # 应用图标
├── logo.png                  # JISUMAN header logo（可选）
├── llm_benchmark.ini         # 配置文件（自动生成）
├── llm_benchmark_history.db  # 历史数据库（自动生成）
└── screenshots/              # 截图目录
```

## 开发验证

```bash
# 语法检查
python3 -m py_compile llm_benchmark.py

# 导入检查
python3 - <<'PY'
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("llm_benchmark", Path("llm_benchmark.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
assert hasattr(m, "LLMBenchmarkApp")
print("IMPORT_CHECK_PASS")
PY

# 流式解析器源码标记检查
python3 - <<'PY'
from pathlib import Path
s = Path("llm_benchmark.py").read_text(encoding="utf-8")
required = ["ParsedStreamChunk","_parse_stream_chunk","parser_profile",
            "reasoning_content","reasoning","first_generated_token",
            "first_answer_token","first_reasoning_token","stream_debug"]
for item in required:
    assert item in s, f"MISSING: {item}"
    print(item, "FOUND")
print("SOURCE_CHECK_PASS")
PY

# 流式解析器 golden fixture 测试（11 个测试用例）
python3 - <<'PY'
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("llm_benchmark", Path("llm_benchmark.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m._test_stream_parser_fixtures()
print("FIXTURE_TESTS_PASS")
PY
```

有 Xvfb 的环境可做 GUI 启动检查：

```bash
xvfb-run -a python3 llm_benchmark.py
```

## 许可证

MIT License
