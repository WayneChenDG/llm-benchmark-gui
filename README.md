# LLM Benchmark — LLM 并发性能测试工具

OpenAI 兼容接口的专业 LLM serving benchmark 工具，纯 Python 标准库实现，无需额外依赖。

Metrics aligned with vLLM bench serve (ttft/tpot/itl/e2el) and NVIDIA GenAI-Perf/NIM-style (output token throughput, request throughput).

## 功能特性

- **流式 TTFT 采集** — `stream=True` 默认开启，SSE 逐行解析，真实记录 Time to First Token
- **专业指标口径** — TTFT / E2E Latency (e2el) / TPOT / ITL / Output Token Throughput / Request RPS，对齐行业标准
- **指标一致性校验** — `validate_metric_consistency()` 自动检查 token 公式、别名一致性、TTFT 不伪造等 12 条规则
- **JISUMAN LLM Benchmark Standard v1** — 完整指标定义字典 `get_metric_standard_definitions()`，报告和 DB 中均标注标准版本
- **4 档预设** — 快速校准 C1/N5 / 标准基线 C8/N80 (默认) / 中高并发 C16/N160 / 压力测试 C32/N320
- **预热请求** — 正式测试前可配置 warmup 请求数 (默认 2)，不计入结果
- **C32 压力确认** — 并发 ≥ 32 时弹窗确认，防止误操作
- **三阶段预检** — 连通性检测 → 模型列表获取 → 基线测试 → 压力测试
- **智能模型选择** — 自动验证模型名称，正确则跳过选择
- **实时状态动画** — 右上角 spinner + 进度 (37/80) + 失败数 (fail=0) + 已耗时 (01:24)
- **8 指标卡** — TTFT / E2E P95 / System Output TPS / Request RPS / Output Tokens / TPOT / ITL / Success Rate
- **延迟直方图** — E2E Latency Distribution (e2el)，历史记录双击可查看
- **详细报告** — 七段式标准报告，含 metric consistency 检查
- **错误诊断** — 分类弹窗 (连接/认证/限流等)，含排查建议
- **配置持久化** — `llm_benchmark.ini`，支持自动保存
- **SQLite 历史** — Schema v2，`benchmark_meta` 表记录标准版本

## 快速开始

```bash
# 命令行运行
python llm_benchmark.py

# Windows 双击运行
# 方式一：双击 llm_benchmark.pyw（推荐，无控制台窗口）
# 方式二：双击 llm_benchmark.py

# Ubuntu 桌面双击运行
cp llm_benchmark.desktop ~/Desktop/

# 调试模式
python llm_benchmark.py -debug
```

## 默认配置

| 参数 | 默认值 |
|------|--------|
| 并发预设 | 标准基线 — C8/N80（默认） |
| 请求总数 | 80 |
| 最大 Token | 512 |
| Temperature | 0.0 |
| 流式模式 | 是 |
| 预热请求 | 2 |

4 档预设：

| 预设 | 并发 | 请求数 |
|------|------|--------|
| 快速校准 — C1/N5 | 1 | 5 |
| **标准基线 — C8/N80（默认）** | **8** | **80** |
| 中高并发 — C16/N160 | 16 | 160 |
| 压力测试 — C32/N320 | 32 | 320 |

## 核心指标

| 指标 | 全称 | 口径 |
|------|------|------|
| TTFT | Time To First Token | 请求发出 → 首个非空输出 chunk |
| E2E / E2EL | End-to-End Latency | 请求发出 → 完整响应结束 |
| TPOT | Time Per Output Token | (E2E - TTFT) / (output_tokens - 1) |
| ITL | Inter-Token Latency | 相邻流式 chunk 间隔 (chunk-level approximation) |
| Output TPS | Output Token Throughput | total_output_tokens / duration_sec |
| Request RPS | Request Throughput | success / duration_sec |

> **指标标准**: JISUMAN LLM Benchmark Standard v1  
> **参考**: vLLM bench serve (ttft/tpot/itl/e2el) · NVIDIA GenAI-Perf/NIM (output_token_throughput/request_throughput)

## 报告示例

```
============================================================
  LLM 并发性能测试报告
============================================================
  测试时间: 2026-05-15 14:30:00

  Metric Standard: JISUMAN LLM Benchmark Standard v1
  Reference: vLLM bench serve + NVIDIA GenAI-Perf/NIM-style serving benchmark

  一、测试配置
  ──────────────────────────────────────────────────────────
  API URL:     http://192.168.1.12:8000/v1/chat/completions
  Model:       qwen3-14b
  Concurrency: 8
  Total Req:   80
  Stream Mode: 流式 (stream=True)
  Benchmark Preset: 标准基线 — C8/N80（默认）
  Warmup Requests:  2
  ...
  七、诊断与建议
  ──────────────────────────────────────────────────────────
  ✓ 指标一致性检查通过 (Metric Consistency Check Passed)。
```

## 配置文件 (llm_benchmark.ini)

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
```

## 支持的 API

所有兼容 OpenAI Chat Completions 接口的服务：
- vLLM 部署的任意模型
- 通义千问 (Qwen) / DeepSeek / GLM / GPT / OpenAI
- 其他 OpenAI 兼容接口

## 系统要求

- Python 3.8+
- 纯标准库：`tkinter` + `sqlite3` + `urllib` + `threading` + `configparser`

## 项目结构

```
llm-benchmark/
├── llm_benchmark.py          # 主程序
├── llm_benchmark.pyw         # Windows 无控制台启动
├── llm_benchmark.desktop     # Ubuntu 桌面启动
├── llm_benchmark.svg         # 应用图标
├── llm_benchmark.ini         # 配置文件（自动生成）
├── llm_benchmark_history.db  # 测试历史（自动生成，Schema v2）
└── screenshots/              # 界面截图
```

## 许可证

MIT License
