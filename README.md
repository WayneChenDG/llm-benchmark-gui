# LLM Benchmark — LLM 并发性能测试工具

OpenAI 兼容接口的压力测试与吞吐分析工具，纯 Python 标准库实现，无需额外依赖。

## 功能特性

- **并发压力测试** — 支持 1/4/8/16/32/64 等多档并发预设，请求总数自动≥并发数
- **三阶段预检** — 连通性检测 → 模型列表获取 → 基线测试 → 压力测试，层层把关
- **智能模型选择** — 连通成功后自动读取 API 模型列表，弹窗供用户选择，减少手动输入
- **完整指标** — 延迟 (min/avg/max/P50/P95/P99)、TTFT、Token/s、成功率
- **智能诊断** — 自动分析延迟放大、排队等待、吞吐瓶颈等问题
- **延时直方图** — 可视化延迟分布，历史记录双击可查看详细直方图
- **错误弹窗** — 测试失败时弹出分类错误提示和排查建议（连接失败/认证错误/限流等）
- **配置持久化** — 参数保存到 `llm_benchmark.ini`，支持手动保存和自动保存两种模式
- **自动保存** — 开启后每次修改配置自动写入 INI 文件，无需手动操作
- **历史记录** — SQLite 持久化存储，支持对比历次测试结果
- **报告导出** — 可选择是否保存测试报告为 txt 文件
- **Windows 双击启动** — 提供 `.pyw` 无控制台启动，所有文件路径基于脚本目录
- **Ubuntu 桌面启动** — 提供 `.desktop` 文件和应用图标，复制到桌面即可双击运行
- **现代化 UI** — Datadog/Linear 风格仪表盘，自适应屏幕分辨率，中英文等宽显示，紫色进度条

## 快速开始

```bash
# 命令行运行
python llm_benchmark.py

# Windows 双击运行
# 方式一：双击 llm_benchmark.pyw（推荐，无控制台窗口）
# 方式二：双击 llm_benchmark.py（有控制台窗口）

# Ubuntu 桌面双击运行
# 将 llm_benchmark.desktop 复制到桌面即可双击启动：
cp llm_benchmark.desktop ~/Desktop/
# 首次使用需右键 → 「允许启动」(Allow Launching)

# 调试模式（输出详细日志）
python llm_benchmark.py -debug
```

## 界面说明

启动后窗口固定为屏幕分辨率的等比缩放尺寸（以 1920×1080 ≈ 1099×1018 为基准），包含三个标签页：

| 标签页 | 功能 |
|--------|------|
| 参数设置 | 配置 API 地址/密钥/模型/提示词/并发参数，支持保存/重置/自动保存 |
| 基准测试 | 实时显示进度、状态卡片、指标摘要、直方图、详细报告 |
| 历史记录 | 浏览历次测试数据，双击行查看延时分布直方图 |

## 使用流程

1. 在「参数设置」填写 API 地址和密钥
2. 点击「开始测试」，程序自动检测连通性
3. 连通成功后自动弹出模型列表，用户选择要测试的模型
4. 先用**并发数=1**测基线延迟
5. 可选：开启「自动保存配置」，此后所有修改自动写入 INI
6. 可选：选择「保存测试报告」为是，每次测试后生成 txt 报告
7. 逐步提高并发数测试吞吐上限
8. 查看「基准测试」页的直方图和诊断建议
9. 在「历史记录」双击行查看延时直方图，对比不同配置的结果

## 配置文件

首次运行时自动生成 `llm_benchmark.ini`（点击「保存配置」或开启「自动保存配置」），格式如下：

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
temperature = 0.7
total_requests = 20
concurrency = 8   (中度 — 推荐，接近大多数 API 限额)
save_report = 否
auto_save = 否
```

修改 INI 文件后重启程序即可生效，也支持多套配置文件切换。

## 错误诊断

测试失败时会弹出分类错误提示，覆盖以下常见场景：

| 错误类型 | 排查建议 |
|---------|---------|
| 目标主机通讯失败 | 检查 API 地址、端口、网络/防火墙 |
| API 密钥认证失败 (401) | 检查 Key 是否正确、是否过期 |
| 接口权限不足 (403) | 检查模型访问配额和权限 |
| API 接口不存在 (404) | 检查 URL 路径和模型名称 |
| 请求被限流 (429) | 降低并发数，等待配额恢复 |
| 服务器内部错误 (5xx) | 稍后重试，查看服务端日志 |
| 响应格式不兼容 | 确认是否 OpenAI 兼容接口 |

启动崩溃时弹出错误对话框，同时写入 `llm_benchmark_crash.log`。

## 字体说明

程序启动时自动检测系统可用字体：
- **Linux** → Noto Sans CJK SC（中英文等宽）
- **Windows** → Microsoft YaHei UI
- **macOS** → PingFang SC

确保中英文字符显示大小一致。

## 支持的 API

所有兼容 OpenAI Chat Completions 接口的服务：

- 通义千问 (Qwen)
- DeepSeek
- GLM
- GPT / OpenAI
- vLLM 部署的任意模型
- 其他 OpenAI 兼容接口

## 系统要求

- Python 3.8+
- 无需额外安装依赖（纯标准库：tkinter + sqlite3 + urllib + threading + configparser）

## 项目结构

```
llm-benchmark/
├── llm_benchmark.py          # 主程序
├── llm_benchmark.pyw         # Windows 无控制台启动文件
├── llm_benchmark.desktop     # Ubuntu 桌面启动文件
├── llm_benchmark.svg         # 应用图标
├── llm_benchmark.ini         # 配置文件（自动生成）
└── llm_benchmark_history.db  # 测试历史（自动生成）
```

## 许可证

MIT License
