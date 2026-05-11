# LLM Benchmark — LLM 并发性能测试工具

OpenAI 兼容接口的压力测试与吞吐分析工具，纯 Python 标准库实现，无需额外依赖。

## 功能特性

- **并发压力测试** — 支持 1/4/8/16/32/64 等多档并发预设
- **三阶段预检** — 连通性检测 → 基线测试 → 压力测试，层层把关
- **完整指标** — 延迟 (min/avg/max/P50/P95/P99)、TTFT、Token/s、成功率
- **智能诊断** — 自动分析延迟放大、排队等待、吞吐瓶颈等问题
- **历史记录** — SQLite 持久化存储，支持对比历次测试结果
- **报告导出** — 自动生成格式化测试报告并保存为 txt

## 快速开始

```bash
# 直接运行
python llm_benchmark.py

# 调试模式（输出详细日志）
python llm_benchmark.py -debug
```

## 界面截图

启动后默认最大化，包含三个标签页：

| 标签页 | 功能 |
|--------|------|
| 参数设置 | 配置 API 地址、密钥、模型、提示词、并发参数 |
| 基准测试 | 实时显示进度、状态卡片、直方图、详细报告 |
| 历史记录 | 浏览/对比历次测试数据 |

## 使用流程

1. 在「参数设置」填写 API 地址和密钥
2. 先用**并发数=1**测基线延迟
3. 逐步提高并发数测试吞吐上限
4. 查看「基准测试」页的直方图和诊断建议
5. 在「历史记录」对比不同配置的结果

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
- 无需额外安装依赖（纯标准库：tkinter + sqlite3 + urllib + threading）

## 项目结构

```
llm-benchmark/
├── llm_benchmark.py          # 主程序
└── llm_benchmark_history.db  # 测试历史（自动生成）
```

## 许可证

MIT License
