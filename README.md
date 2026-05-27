# JISUMAN LLM Benchmark GUI

OpenAI 兼容接口的 LLM serving benchmark 桌面工具。  
主程序使用 Python 标准库 Tkinter 构建 GUI，核心 benchmark 不依赖重型运行时；可选安装 `matplotlib` 用于并发扫测图表展示和 PNG 导出。

Metrics aligned with vLLM bench serve (TTFT / TPOT / ITL / E2EL) and NVIDIA GenAI-Perf / NIM-style serving metrics (output token throughput, request throughput).

---

## 功能特性

- **JISUMAN 品牌 GUI**：全局 header 显示 JISUMAN 标识，支持受控尺寸 logo。
- **中英文界面切换**：默认简体中文，可切换 English；语言偏好保存到 `llm_benchmark.ini`。
- **单次基准测试**：连通性检测、模型列表获取、基线请求、并发压力测试。
- **并发扫测（高并发模式）**：按并发档位批量测试，asyncio + aiohttp，支持 C512/C1024。Windows 自动使用 ProactorEventLoop（IOCP），不受 `select()` FD_SETSIZE=512 限制。
- **ResultStore 文件系统存储**：每次跑测自动保存到 `results/runs/<run_id>/`，包含 `result.json`、`summary.json`、`report.txt`、`report.md`、`artifacts_manifest.json`、`charts/`。
- **模型无关流式解析器**：集中式 `_parse_stream_chunk` 统一处理 `delta.content` / `delta.reasoning_content` / `delta.reasoning`（Qwen3）/ `choices[].text` / tool delta。
- **Stream Parser Profile**：每次 benchmark 自动生成 `parser_profile`，记录 `parser_mode`、`observed_delta_keys`、`generated_fields`、`usage_source` 等能力档案。
- **细粒度流式时序指标**：TTFT / TPOT / ITL / E2E 基础上增加 FVT / FVG / Generated ITL / Answer ITL / Reasoning ITL 等，均严格区分 None（不可用）与 0.0（真实零值）。
- **stream_debug 诊断**：捕获 `observed_delta_keys`、`unknown_delta_keys`、`generated_field_counts` 及前 5 个 delta 样本，自动警告异常情况。
- **Artifact Resolver**：5 层路径解析（manifest → DB → sweep 字段 → legacy DB 列 → glob 扫描），写入 `artifact_resolver_debug.log` 诊断。
- **历史记录**：三来源合并显示（ResultStore / Result DB / 旧版 DB），双击详情自动判断 sweep vs single。
- **历史详情 sweep 路由**：`is_sweep_record()` 依据 `run_type`、`config`、`run_id`、`result.json` payload 判断类型，`Output TP / TTFT P95 / E2E P95` 列值不影响路由。
- **扫测详情（RS sweep）**：概览、并发档位表、2×2 sweep analysis chart（matplotlib 内嵌）、专家分析、报告文本、输出文件，独立「重新生成图表」按钮保存 `charts/sweep_analysis.png`。
- **跨平台依赖安装器**：`scripts/install_deps.py` → Linux apt/dnf/yum、Windows winget、macOS Homebrew，全部需确认才执行 sudo。
- **指标一致性校验**：自动检查 token 汇总、吞吐公式、TTFT 与流式模式一致性。
- **配置持久化**：API、模型、prompt、测试参数、语言偏好保存到 `llm_benchmark.ini`。

---

## 快速开始

```bash
cd /opt/llm-benchmark

# GUI
python3 llm_benchmark.py

# 调试模式
python3 llm_benchmark.py -debug

```

Ubuntu 桌面快捷方式：

```bash
cp llm_benchmark.desktop ~/Desktop/
```

### 安装依赖

```bash
# 一键安装（Linux）
bash scripts/install_deps_linux.sh

# 或使用 Python 入口（自动检测平台）
python3 scripts/install_deps.py

# 仅验证当前依赖状态
python3 scripts/verify_deps.py
```

手动安装可选依赖：

```bash
# sweep 图表展示和 PNG 导出
python3 -m pip install matplotlib

# 更好的 header logo 缩放（可选）
python3 -m pip install Pillow

# 完整依赖
python3 -m pip install -r requirements.txt
```

---

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
- E2E 端到端延迟分布直方图：Benchmark 完成后自动展开并绘制。
- 详细报告：包含测试配置、指标摘要、失败诊断和一致性校验。

### 并发扫测

扫测配置：并发列表、请求倍数、输出选项（JSON / Markdown / PNG / 历史记录）。

扫测完成后展示：

- 扫测状态日志。
- 2×2 图形分析（E2E 延迟趋势 / TPS 趋势 / TTFT-TPOT 趋势 / 效率与稳定性）。
- 专家分析简评。
- 输出文件路径。

高并发注意事项：

| 平台 | C≤256 | C512/C768/C1024 |
|------|-------|-----------------|
| Linux | 默认 epoll，无 FD 限制 | 建议 `ulimit -n 65535` |
| Windows | 默认 Proactor | 自动使用 ProactorEventLoop (IOCP) |
| macOS | 默认 kqueue | 建议提高文件描述符限制 |

### 历史记录

History tab 显示三来源合并的历史列表：ResultStore（`S:` 前缀）、Result DB（`R:` 前缀）、旧版 DB（数字 ID）。

列：时间 / 类型 / 模型 / 环境 / 硬件 / Backend / 量化 / 负载 / Output TP / TTFT P95 / E2E P95 / 状态

双击历史记录：

- **single**：显示摘要、指标、E2E latency chart；操作按钮：打开结果目录、重新生成报告、重新生成图表。
- **sweep**：显示扫测概览、并发档位表、2×2 图表（已有 PNG 优先，否则实时渲染）、专家分析、输出文件；操作按钮：打开结果目录、重新生成报告（sweep markdown）、重新生成图表（sweep_analysis.png）。

> `Output TP / TTFT P95 / E2E P95` 仅用于列表快速浏览，不影响详情页类型、图表类型或报告类型。

---

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

### 细粒度流式时序指标

| 指标 | 说明 |
|------|------|
| First Generated Token | 请求发出 → 首个非空 delta（content / reasoning / choices.text） |
| First Answer Token | 请求发出 → 首个非空 delta.content（仅回答正文） |
| First Reasoning Token | 请求发出 → 首个非空 delta.reasoning_content 或 delta.reasoning |
| First Generated Gap | First Generated Token − First JSON Chunk |
| Generated ITL | 相邻生成内容 chunk 间隔（主 ITL 来源） |
| Answer ITL | 相邻 delta.content chunk 间隔 |
| Reasoning ITL | 相邻 reasoning 字段 chunk 间隔 |
| Stream Event ITL | 相邻 choices chunk 间隔（校准参考） |
| Visible TPOT | (E2E − First Generated Token) / (tokens − 1)，仅诊断用 |

> 所有细粒度指标在无数据时显示 **N/A**，不伪造 0.000s。

---

## ResultStore 目录结构

每次跑测在 `results/runs/<run_id>/` 下生成：

```text
results/runs/
└── 20260527_143000__qwen3-27b__real_api__single__C8_N80_I50_O512/
    ├── result.json              # 完整结果（含 detail[]）
    ├── summary.json             # 轻量索引（历史列表数据来源）
    ├── report.txt               # 文本报告
    ├── report.md                # Markdown 报告
    ├── config.json              # 测试配置快照
    ├── artifacts_manifest.json  # 产物清单
    └── charts/
        ├── e2e_latency_histogram.png   # 单次测试 E2E 延迟分布
        ├── e2e_latency_histogram.json  # 直方图元数据
        └── sweep_analysis.png          # sweep 2×2 分析图（扫测时生成）
```

`run_id` 格式：`YYYYMMDD_HHMMSS__<model>__<mode>__<obj>__<workload>`

---

## 默认配置

| 参数 | 默认值 |
|------|--------|
| 并发预设 | 标准基线 — C8/N80 |
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
| 标准基线 — C8/N80 | 8 | 80 |
| 中高并发 — C16/N160 | 16 | 160 |
| 压力测试 — C32/N320 | 32 | 320 |

---

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

语言代码：`zh_CN`（简体中文）/ `en_US`（English）

---

## 项目结构

```text
llm-benchmark/
├── llm_benchmark.py              # 主程序（GUI + 控制层）
├── llm_benchmark.desktop         # Ubuntu 桌面启动
├── logo.png                      # JISUMAN header logo（可选）
├── llm_benchmark.ini             # 配置文件（自动生成）
├── llm_benchmark_history.db      # 旧版历史数据库（自动生成）
├── requirements.txt              # Python 依赖（core）
├── requirements-tokenizer.txt    # Python 依赖（tokenizer 可选）
├── llm_benchmark_app/            # 功能模块
│   ├── artifact_resolver.py      # 5 层产物路径解析
│   ├── customer_metrics.py       # 客户验收指标
│   ├── high_concurrency_runner.py # HC asyncio runner + 事件循环管理
│   ├── history_db.py             # 旧版历史 DB 接口
│   ├── metrics.py                # 指标聚合
│   ├── result_db.py              # Result DB 接口
│   ├── result_store.py           # ResultStore 文件系统存储
│   ├── runner.py                 # 单次请求 runner
│   ├── stream_parser.py          # 模型无关流式解析器
│   └── token_calibration.py      # Token 数校准
├── scripts/                      # 依赖安装
│   ├── install_deps.py           # 跨平台入口
│   ├── install_deps_linux.sh     # apt/dnf/yum
│   ├── install_deps_macos.sh     # Homebrew
│   ├── install_deps_windows.ps1  # winget
│   └── verify_deps.py            # 依赖验证
├── tests/                        # 自动化测试
│   ├── test_result_store.py      # ResultStore 单元测试
│   └── test_customer_metrics.py  # 客户指标测试
├── results/                      # 跑测结果（.gitignore）
├── data/                         # 旧版结果 DB
└── screenshots/                  # 截图目录
```

---

## 系统要求

| 依赖 | 必需 | 说明 |
|------|------|------|
| Python 3.8+ | ✅ | Windows 推荐 3.10+（ProactorEventLoop 默认） |
| Tkinter | ✅ | 标准库 GUI |
| `sqlite3` | ✅ | 标准库 |
| `aiohttp>=3.9` | ✅（高并发扫测） | C256+ 需要；`pip install aiohttp` |
| `matplotlib>=3.5` | 可选 | sweep 图表和 PNG 导出 |
| `Pillow>=9.0` | 可选 | 更好的 logo 缩放、历史详情图片显示 |
| `reportlab>=3.6` | 可选 | 客户报告 PDF 生成 |
| `python-docx>=1.1` | 可选 | DOCX 报告 |
| LibreOffice | 可选 | DOCX → PDF 转换 |

Windows C512/C1024 高并发扫测：Python 3.8+ 默认 ProactorEventLoop，无需额外配置。

---

## 支持的 API

所有兼容 OpenAI Chat Completions 的接口：

| 服务 / 模型系列 | 流式字段 | 备注 |
|----------------|---------|------|
| vLLM | `delta.content` | 标准格式 |
| 通义千问 / Qwen3 | `delta.reasoning` + `delta.content` | reasoning 字段已原生支持 |
| DeepSeek-R1 | `delta.reasoning_content` + `delta.content` | reasoning_content 字段支持 |
| GLM | `delta.content` | 标准格式 |
| OpenAI / GPT | `delta.content` | 标准格式 |
| 其他 OpenAI-compatible | 自动检测 | 未知字段记录到 stream_debug |

---

## 开发验证

```bash
# 语法检查
python3 -m py_compile llm_benchmark.py
python3 -m py_compile llm_benchmark_app/*.py

# 自动化测试
python3 -m pytest tests/ -q

# 流式解析器 golden fixture 测试
python3 - <<'PY'
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("llm_benchmark", Path("llm_benchmark.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m._test_stream_parser_fixtures()
print("FIXTURE_TESTS_PASS")
PY

# 源码标记检查
python3 - <<'PY'
from pathlib import Path
text = Path("llm_benchmark.py").read_text(encoding="utf-8")
for p in Path("llm_benchmark_app").glob("*.py"):
    text += "\n" + p.read_text(encoding="utf-8")
required = [
    "is_sweep_record", "resolve_history_artifacts",
    "run_async_clean", "_new_hc_event_loop",
    "ParsedStreamChunk", "_parse_stream_chunk", "parser_profile",
    "shutdown_asyncgens", "return_exceptions=True",
    "too many file descriptors", "charts",
]
for item in required:
    print(item, "FOUND" if item in text else "MISSING")
PY
```

有 Xvfb 的环境可做 GUI 启动检查：

```bash
xvfb-run -a python3 llm_benchmark.py
```

---

## 许可证

MIT License
