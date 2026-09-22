# RELEASE 2.1.0 —— 顶栏修正 + 深海军蓝主题（可界面内切换）

发布日期：2026-09-22　　分支：master　　版本单一来源：`llm_benchmark.py: APP_VERSION`

## 本版改了什么

### 1. 顶栏左上角标题（用户报「显示不全」）
- 原因：56px 顶栏里放了「22px 字标 + 标题 + 副标题」三行，副标题被挤出可视区、
  字标被压扁——文字实际不可见。
- 处置：按用户要求**只保留标题文字** `JISUMEN LLM Benchmark GUI v2.1.0`，
  顶栏高度 56 → 48px。（需要恢复字标时：`_load_header_logo()` 仍在，把其返回值
  作为 `Label(image=...)` pack 到标题左侧即可，见 `_build_header` 注释。）

### 2. 色彩配置可修改 → 两套主题 + 顶栏切换
- 新增**商务深海军蓝**主题（默认）：顶栏 / 页签条 / 表格表头 / 状态栏＝深底白字，
  内容区保持白底，保证数据可读性。
- 保留原**浅色**主题；顶栏右侧新增「主题」下拉，选择即生效，写入
  `llm_benchmark.ini [ui] theme`。测试运行期间禁止切换（弹提示后回退）。
- 实现：调色板收敛为 `ui_theme.py` 的 `THEMES`（`navy` / `light`），
  `set_theme()` 原地更新 `TOKENS`；切换时重建界面（Tkinter 颜色在构建期写入控件）。
  详见 `docs/uiux-v2/THEMES.md`。

### 3. 顺带修掉的两个真实缺陷（本次主题工作暴露）
- **状态 chip 不可见**：顶栏状态胶囊 `pack_propagate(False)` 只锁了宽度、没给高度，
  高度塌成 1px，内容一直不可见（浅色白底白字没人发现）。改为内容自适应 + 导航条配色。
- **底部状态栏被挤成 1px**：`body` 请求高度大于窗口时，后 pack 的状态栏被压掉；
  改为 `pack(before=body)`，状态栏恢复 32px（截图可见「就绪」）。

### 4. 测试与门禁
- `pytest`：**163 passed**（原 158 + 主题 5 项）。
- 对比度门禁扩为**两套主题分别门禁**：各 34 对门禁项全 PASS，exit=0；
  新增 nav 条带配对（顶栏/表头/页签/状态栏文字 on 导航底 + 高亮底）。
- 打包脚本排除开发探针与截图目录，避免 dev 产物进包。

## 包与取包位置

| 形态 | 文件 | 说明 |
|---|---|---|
| 完整版 | `jisumen-llm-benchmark-2.1.0-linux-x86_64.tar.gz` | 含离线 wheels，目标机无需联网 |
| 精简版 | `jisumen-llm-benchmark-2.1.0-linux-x86_64-slim.tar.gz` | 联网装依赖 |

- 构建机：`/opt/llm-benchmark/dist/`
- 文件服务器（推荐）：`\\192.168.1.254\data\software\installers\jisumen-llm-benchmark\2.1.0\`
  （Linux 侧 `/data/software/installers/jisumen-llm-benchmark/2.1.0/`）

哈希与包内 VERSION：

```
完整版  jisumen-llm-benchmark-2.1.0-linux-x86_64.tar.gz       130,430,171 B (124.4 MiB)
        sha256 1811e2536dd0b9e6f876f1968d8183ba798ce26fc1147491bd2502305399ae5f
精简版  jisumen-llm-benchmark-2.1.0-linux-x86_64-slim.tar.gz    936,576 B (0.89 MiB)
        sha256 907116fc02a7951a6110eb3fe2ee09ed46c1b913139d81442777916277bba471

包内 VERSION：version=2.1.0 / arch=x86_64 / python=3.10 / git_rev=9ce0659
             built_at=2026-09-22T15:48:49+08:00
```

## 验收结论（真机实测，隔离目录，不触碰在跑服务）

```
bash packaging/accept-release.sh dist/jisumen-llm-benchmark-2.1.0-linux-x86_64.tar.gz \
     http://127.0.0.1:8123/v1
==> 验收结果: PASS=11 FAIL=0
```

- 包体检查：sha256 校验通过；不含 `llm_benchmark.ini` / `results/` / `backups/` /
  `__pycache__` / `.db`；含 VERSION 与离线 wheels ✓
- 隔离安装：`install.sh` 成功；`verify-install.sh` **PASS=16 FAIL=0**（含 GUI 冒烟
  12s 存活、依赖齐备、`APP_VERSION=2.1.0`）
- 真实负载：安装副本跑 C1/N2 走本地 mock OpenAI 兼容端点 →
  `success=2 fail=0 success_rate=100.0 ttft_avg=0.119s`
- 本地门禁：`pytest` **163 passed**；`scripts/uiux_contrast_audit.py` 两套主题各 34 对
  门禁项全 PASS（exit=0）
- 界面内主题切换：探针实测 navy → light → navy 往返切换不崩，7 个页签与配置项保持
  （见 `scripts/_probe_theme.py`、截图 `docs/uiux-v2/theme/`）

### 验收过程中发现并修正的一处脚本错误（非产品缺陷）

第一次验收 FAIL=1：安装副本跑基准 0 成功、报 HTTP 404。定位为
**验收脚本用法错误**——`run_benchmark()` 直接使用传入 URL，URL 归一化（补
`/chat/completions`）由 GUI 在调用前完成；脚本传入裸 `/v1` 因此 404。
改为调用 `normalize_api_url()` 后 PASS=11 FAIL=0。产品代码未因此改动。
（该 404 是被上一版新增的"必须判成功数 ≥1"门禁抓住的，而非误判为通过。）

## 安装（目标机 Linux 桌面 x86_64）

```
tar -xzf jisumen-llm-benchmark-2.1.0-linux-x86_64.tar.gz
cd jisumen-llm-benchmark-2.1.0-linux-x86_64 && bash install.sh
~/jisumen-llm-benchmark/bin/jisumen-benchmark
# 自检：bash verify-install.sh --install-dir ~/jisumen-llm-benchmark --smoke
```

前置：`python3-tk`、`fonts-noto-cjk`（中文字体）。

## 实测（真机验收，隔离目录，不触碰在跑服务）

见 `packaging/accept-release.sh` 输出与本文档"验收结论"节。

## 诚实边界

- 主题切换采用"重建界面"实现，切换瞬间界面重建一次；测试运行中不允许切换。
- 两套主题共用同一套品牌蓝 `#0060F0`（品牌识别不随主题变），语义色与图表系列色同值。
- 未接入项未变（仍标「未记录」，不伪装），见 `OPEN_ITEMS.md`。
