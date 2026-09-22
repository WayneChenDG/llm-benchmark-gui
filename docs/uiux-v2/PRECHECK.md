# PRECHECK — LLM-BENCHMARK-GUI UI/UX 改版（v2）

## 任务边界
- 目标：把本工具改造成克制、清晰、可信的企业级大模型性能基准测试 / 结果对比 / 客户验收报告客户端。
- 用户明确约束：**这是 Python + Tkinter 单机客户端，不分前后端，不改成 Web 应用；只改 UI/UX，不动测试逻辑。**
- 因此：保留 runner / metrics / stream_parser / result_store / result_db / history_db / high_concurrency_runner 全部行为与产物格式；只改视觉层、信息层级、页面组织与文案。

## 改前状态（实测）
- 入口 `llm_benchmark.py`（11,302 行），`llm_benchmark_app/` 8 个模块，`tests/` 158 用例。
- 页签 5 个：参数设置 / 基准测试 / 并发扫测 / 环境档案 / 历史记录。
- 改前测试基线：`pytest tests/ -q` → **157 passed / 1 failed**，失败用例 `tests/test_result_store.py::TestListRuns::test_sorted_newest_first`（单独跑通过、全量跑失败）。
- 数据现状：SQLite 三层 profile 有种子行；`benchmark_runs` 等表 0 行；磁盘无 `results/` → **空态必须做，且演示数据必须标注**。

## 改前视觉缺陷（截图实测记录，BEFORE/ 10 张）
1. 指标卡左色条使用**彩虹色**（TTFT 蓝 / FVT 琥珀 / TPS 绿 / RPS 蓝 / TPOT 蓝），无色彩语义；
2. **语义色误用**：E2E P95 用红色（error），把「延迟」误报成「故障」；
3. 表格列被截断（"Backer"、"Qua" 之类），表头文字宽于列宽；
4. 空态无引导（空白卡片，无下一步动作）；
5. 窗口按屏幕比例固定尺寸、**不可缩放**；ttk 默认灰主题，对比度不足（改前审计 text_muted 仅 2.37–2.56）；
6. 信息层级扁平：关键数字与异常不突出，无「待处理事项」概念。

## 回滚点（改版前已立）
- Git tag：`uiux-before-v1-20260922_142619`
- 打包备份：`backups/llm-benchmark-gui_pre-uiux_20260922_142619.tar.gz`（6.1M，609 文件）

## 风险与对策
| 风险 | 对策 |
|---|---|
| 改到测试逻辑 | 只新建 `ui_theme.py` / `ui_pages.py` 承载视觉层，`llm_benchmark.py` 只做「接线 + 替换控件外观」；指标计算与产物格式零改动 |
| 令牌散落、页面各自取色 | 令牌权威落在 `llm_benchmark_app/ui_theme.py` 的 `TOKENS`，`llm_benchmark.py` 的 `C_STYLE` 由其派生；新增审计脚本扫描块外硬编码色值 |
| 对比度不达标 | 新增 WCAG 2.1 审计脚本（门禁项 + 信息项分离），纳入交付 |
| 演示数据被误当真实性能 | 端点/模型名带 `demo`，总览横幅、证据块、历史列表标签、导出件四处标注「演示数据」 |

## 验收口径（可验证）
1. `python3 -m py_compile` 全通过，headless 实例化 7 个页签全 ok；
2. WCAG 审计门禁项全 PASS（exit=0）；
3. 用本地 mock OpenAI 兼容端点跑**真实流式链路**（含故意失败用例），逐页截图：创建测试 → 进度 → 结果 → 失败定位 → 对比 → 导出；
4. `pytest tests/` 结果不低于改前基线（157 passed，1 个既有 flaky 用例除外）。
