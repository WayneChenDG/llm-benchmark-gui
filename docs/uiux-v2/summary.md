# summary — LLM-BENCHMARK-GUI UI/UX 改版 v2（一页摘要）

**改了什么**：Tkinter 客户端视觉层与信息层重构（5 → 7 页签，新增「总览」「报告与证据」）；指标按延迟（单请求）/ 吞吐（系统级）/ 可靠性分组并标注单位与口径；颜色语义全站统一（品牌蓝＝性能、绿＝健康、红＝故障、琥珀＝需注意、灰＝未记录）；空态与演示数据四处可见标注；窗口可缩放。**测试逻辑、产物格式、单机客户端形态一律未变。**

**验证（本机真实执行）**
- 单元测试 `pytest tests/ -q` → **158 passed in 3.76s**（改版前 157 passed / 1 flaky failed）
- 对比度审计 → **exit=0，门禁项 31/31 PASS**，禁用组合（白字压语义实心色）源码命中 0
- 端到端真实链路（本地 mock OpenAI 兼容端点，含 15% 故意失败）→ 两次跑测完成，产物 7 项齐全
  - A：C1/N6/I36/O256 → 6/6 成功、TTFT 127ms、Output TPS 53.2、E2E P95 4874ms
  - B：C8/N40/I34/O243 → 38/2（95.0%）、TTFT 122ms、Output TPS 404.2、E2E P95 4851ms
- 几何探针 → 表头截断清零；历史表改横向滚动
- 评审中修复 8 项真实缺陷（彩虹色/语义误用、提示常驻、紫色图表+重复标题、横幅折行、两处表头截断、新页面配置未刷新、导出按钮缺少禁用态）

**改了哪些文件**
- 新增：`llm_benchmark_app/ui_theme.py`（设计令牌权威）、`llm_benchmark_app/ui_pages.py`（总览/历史对比/报告与证据）
- 接线改造：`llm_benchmark.py`（C_STYLE 派生、7 页签、指标分组与配色、历史表列宽+横滚、提示窗、直方图配色、横幅折行、跑测后刷新）
- 新增脚本：`scripts/{uiux_demo_run,demo_mock_openai_server,uiux_contrast_audit,uiux_capture,_probe_geometry,derive_brand_assets}.py`
- 新增文档：`docs/uiux-v2/{DESIGN_SPEC,PRECHECK,RESULT,CHANGED_PAGES,OPEN_ITEMS,CONTRAST_AUDIT}.md`
- 截图：`docs/uiux-v2/{BEFORE(10),AFTER(10),AFTER/EMPTY(10),COMPARE(5)}`

**改版前后（对比图见 `docs/uiux-v2/COMPARE/`）**
`compare_01_new_test.png`、`compare_02_result.png`、`compare_03_sweep.png`、`compare_04_history.png`、`compare_05_env.png`（左＝改版前，右＝改版后）；「总览」「报告与证据」为新增页，无改版前对应页。

**仍未接入（界面标「未记录」，不伪装）**：硬件、推理框架、模型档案、环境档案绑定，以及「同口径过滤」「跨运行基线告警」「DB 检索」→ 见 `OPEN_ITEMS.md`（8 项）。

**回滚点**：tag `uiux-before-v1-20260922_142619` ＋ `backups/llm-benchmark-gui_pre-uiux_20260922_142619.tar.gz`
