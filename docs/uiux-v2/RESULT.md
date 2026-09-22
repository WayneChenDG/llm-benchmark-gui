# RESULT — LLM-BENCHMARK-GUI UI/UX 改版（v2）

## 结论
在**不改测试逻辑、不改前后端形态（Python + Tkinter 单机客户端）**的前提下，完成视觉层与信息层重构：页签 5 → 7（新增总览 / 报告与证据），指标与表格统一口径、统一颜色语义，空态与演示数据可见标注，窗口可缩放。全部结论均由本机真实执行得出。

## 一、验证结果（含可复现命令）

| # | 验证项 | 命令 | 结果 |
|---|---|---|---|
| 1 | 语法编译 | `python3 -m py_compile llm_benchmark.py llm_benchmark_app/ui_theme.py llm_benchmark_app/ui_pages.py` | OK |
| 2 | 无头实例化 | `xvfb-run -a python3 scripts/_probe_geometry.py` | 7 个页签全部构建成功（总览/新建测试/测试与结果/并发扫测/历史对比/报告与证据/环境档案） |
| 3 | 单元测试 | `python3 -m pytest tests/ -q` | **158 passed in 3.76s**（改版前基线：157 passed / 1 flaky failed） |
| 4 | 对比度审计 | `python3 scripts/uiux_contrast_audit.py --out docs/uiux-v2/CONTRAST_AUDIT.md` | **exit=0；门禁项 31/31 PASS，FAIL 0**；禁用组合源码命中 0 |
| 5 | 表头截断（几何探针） | 同上 #2 | 去除全部「表头文字宽于列宽」；历史表 13 列 1406px > 可用宽 → 改为横向滚动 |
| 6 | 端到端真实链路 | `python3 scripts/uiux_demo_run.py --out docs/uiux-v2/AFTER --prefix after`（本地 mock OpenAI 兼容端点，含 15% 故意失败注入） | 两次跑测全部完成，产物落盘，全程无异常对话框（对话框记录为空） |

### 端到端跑测的真实数据（演示数据，来自本地 mock 端点）
| 运行 | 负载 | 成功/失败 | 成功率 | TTFT 均值 | TPOT 均值 | Output TPS | E2E P95 |
|---|---|---|---|---|---|---|---|
| A | C1/N6/I36/O256 | 6 / 0 | 100.0% | 127 ms | 18.0 ms | 53.2 tok/s | 4874 ms |
| B | C8/N40/I34/O243 | 38 / 2 | 95.0% | 122 ms | 18.0 ms | 404.2 tok/s | 4851 ms |

每次运行落盘 7 项产物：`result.json` / `summary.json` / `report.txt` / `report.md` / `config.json` / `charts/` / `artifacts_manifest.json`（格式与改版前完全一致，未被本次改动触及）。

## 二、评审中发现并修复的真实缺陷

| # | 缺陷（评审实测） | 根因 | 修复 |
|---|---|---|---|
| 1 | 指标卡左色条**彩虹色**，且 **E2E P95 用红色**（把延迟误报为故障） | `MetricItem.COLORS` 每指标一个颜色 | 性能类统一品牌蓝，语义色只保留「成功率」；异常通过「待处理事项」与失败横幅表达 |
| 2 | 指标 hover 提示**常驻不消失**（截图里悬空提示框） | 提示窗落在指针下方 → `<Leave>` 永不触发 | 提示窗下移 18px + 6 秒自动隐藏 + 点击隐藏 |
| 3 | E2E 分布图**紫色**且标题与卡片标题重复 | 画布内硬编码紫色渐变 + 重复绘制标题 | 品牌蓝渐变（accent_soft→accent_pressed），去掉画布内重复标题，统计行上移 |
| 4 | 演示横幅文字被折成 3 行、标点孤立成行 | `NoticeBanner` 折行宽度硬编码 420px | 折行宽度随横幅实际宽度自适应（实测标签高 87px → 29px） |
| 5 | 总览「最近运行」表头被截断（`Output TPS (tok` / `成功率 (%`） | 列宽小于表头文字宽 | 按实测文字宽调整列宽并设 minwidth |
| 6 | 历史表表头截断 + 右列被裁 | 13 列总宽 1420px > 可用 1324px | 固定列宽 + 横向滚动条，表头不再被压缩 |
| 7 | 总览「当前配置与环境」首次全显示「未记录」 | 新页面在配置载入前刷新 | `_load_config()` 之后统一刷新新页面；跑测完成后同样刷新 |
| 8 | 报告与证据页无运行时按钮可点却无意义 | 缺禁用态 | 无运行时按钮置为禁用态并给出引导文案 |

## 三、指标分组与口径（规范 §2.3 / §3.4 落地）
结果摘要按口径分为三组，组标题写明口径：
- **延迟（单请求）**：首包延迟 TTFT / 首字延迟 FVT / 单 Token 耗时 TPOT / Token 间隔 ITL / E2E P95
- **吞吐（系统级）**：输出吞吐 TPS / 请求吞吐 RPS
- **可靠性（单请求）**：成功率

## 四、跨页一致性（同口径收敛）
- 表头一律带单位：`TTFT (ms)`、`Output TPS (tok/s)`、`成功率 (%)`、`TTFT P95 (ms)`、`E2E P95 (ms)`。
- 颜色语义全站一致：品牌蓝＝数据/性能，绿＝健康/成功，琥珀＝需注意，红＝故障/失败，灰＝未记录/禁用。
- 演示数据**四处标注**：总览横幅、测试与结果横幅、历史对比选择项标签、报告与证据证据块。

## 五、未做与未接入（如实）
- **未接入的真实数据**（界面标「未记录 / 未绑定」，绝不伪装）：硬件、推理框架、模型档案、环境档案绑定 → 详见 `OPEN_ITEMS.md`（8 项）。
- **未改动**：runner / metrics / stream_parser / result_store / result_db / history_db / high_concurrency_runner 的计算与产物格式；未新增测试类型；未引入 tokenizer（ITL 仍为 SSE chunk 级近似，口径未变）。
- **既有 flaky 用例**（改版前即存在）：`tests/test_result_store.py::TestListRuns::test_sorted_newest_first`，根因是 `rs_list_runs()` 按秒级 `created_at` 排序、同秒保存退化为目录序。本次未修（属测试脆弱性，非功能缺陷），本次全量跑通过（158/158）。

## 六、交付物
- 设计规范：`docs/uiux-v2/DESIGN_SPEC.md`（令牌权威实现见 `llm_benchmark_app/ui_theme.py`，冲突以代码为准）
- 对比度审计报告：`docs/uiux-v2/CONTRAST_AUDIT.md`
- 改版前基线截图：`docs/uiux-v2/BEFORE/`（10 张）
- 改版后截图（带真实演示数据）：`docs/uiux-v2/AFTER/`（10 张）；空态：`docs/uiux-v2/AFTER/EMPTY/`（10 张）
- 前后对比图：`docs/uiux-v2/COMPARE/`（5 张，左＝改版前 / 右＝改版后）
- 已修改页面清单：`docs/uiux-v2/CHANGED_PAGES.md`；待接入清单：`docs/uiux-v2/OPEN_ITEMS.md`
- 验收/工具脚本：`scripts/uiux_demo_run.py`（端到端驱动）、`scripts/demo_mock_openai_server.py`（演示端点）、`scripts/uiux_contrast_audit.py`（对比度审计）、`scripts/uiux_capture.py`（逐页截图）、`scripts/_probe_geometry.py`（几何探针）、`scripts/derive_brand_assets.py`（品牌图派生）

## 七、回滚
- `git tag uiux-before-v1-20260922_142619`
- `backups/llm-benchmark-gui_pre-uiux_20260922_142619.tar.gz`（6.1M / 609 文件）
