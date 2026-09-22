# RELEASE 2.0.0 — JISUMEN LLM Benchmark GUI（首个发行版，UI/UX v2）

- 版本：`2.0.0`（`llm_benchmark.py: APP_VERSION`，包内 `VERSION` 含版本/架构/Python/git_rev/构建时间）
- 构建时间：2026-09-22 15:16（构建机：jisuman-ai-srv-001，Debian 系 Linux x86_64，Python 3.10.12）
- 包内 `VERSION.git_rev = 25f1f58`（该提交已包含打包器 `packaging/`，可据以重新构建）
- git：提交 `25f1f58`（master），tag `v2.0.0`（Gitea `jisuman/llm-benchmark-gui` 与 GitHub `WayneChenDG/llm-benchmark-gui` 均已同步）
- 回滚点（改版前）：tag `uiux-before-v1-20260922_142619`

## 一、发行包

| 包 | 大小 | sha256 |
|---|---|---|
| `jisumen-llm-benchmark-2.0.0-linux-x86_64.tar.gz`（**完整版，含离线 wheels**） | 124.4 MiB | `6ccb35987b3db5788dcd4722571c7d4db2cb029b3436f1c97df58737b589a491` |
| `jisumen-llm-benchmark-2.0.0-linux-x86_64-slim.tar.gz`（**精简版，联网安装依赖**） | 0.88 MiB | `76143471e2062a665913a2f5509e2884ad931263c592224880a21343b216a5cb` |

> 说明：同版本号先前曾产出过一版（哈希 `bce4c2ae…`/`a9e0d7b4…`），其 `VERSION.git_rev` 指向尚未包含打包器的提交，已在交付前整批重建覆盖，未被任何人取用；上表为最终交付哈希。

取包位置：
- 本地（构建机）：`/opt/llm-benchmark/dist/`
- **文件服务器（推荐取包处，已用真实 SMB 客户端以 `jisuman` 身份复核可见）**：
  `\\192.168.1.254\data\software\installers\jisumen-llm-benchmark\2.0.0\`
  Linux 侧：`/data/software/installers/jisumen-llm-benchmark/2.0.0/`（远端 `sha256sum -c` = OK）

> 取包说明：该 share（`[data]` → `/data`，valid users = jisuman/root）里包是可见的；若 Explorer 里看不到，按 F5 刷新目录（Windows 会缓存 SMB 目录视图）。曾短暂放在 `/data/jisumen/releases/2.0.0/`，为符合服务器既有惯例（`software/installers/`）已整批移动，旧路径不再保留，避免两处副本。

包内：应用源码（`llm_benchmark.py` + `llm_benchmark_app/`）、`VERSION`、`SHA256SUMS`、`install.sh`、`verify-install.sh`、`scripts/`、`tests/`、`docs/uiux-v2/*.md`、`requirements*.txt`、`LICENSE`、`logo.png`；完整版另含 `wheels/py3.10-x86_64/`（离线安装用）。

**包内刻意不含**：`.git/`、`backups/`、`results/`（跑测产物）、`*.db`（运行时数据库）、`llm_benchmark.ini`（本机站点配置）、`__pycache__/`、`.agents/`、`design-system/`、改版前后截图。

## 二、用户操作（Linux 桌面，x86_64）

```bash
# 1) 解包
tar -xzf jisumen-llm-benchmark-2.0.0-linux-x86_64.tar.gz      # 或 -slim 版
cd jisumen-llm-benchmark-2.0.0-linux-x86_64

# 2) 安装（默认装到 ~/jisumen-llm-benchmark；完整版离线装依赖）
bash install.sh
#   可选： bash install.sh --install-dir /opt/jisumen-benchmark
#   无 venv： bash install.sh --no-venv   （需系统已装依赖）

# 3) 自检（含 GUI 冒烟）
bash verify-install.sh --install-dir ~/jisumen-llm-benchmark --smoke

# 4) 启动
~/jisumen-llm-benchmark/bin/jisumen-benchmark
```

前置：`python3`(3.9+) + `tkinter`（Debian/Ubuntu：`sudo apt-get install -y python3-tk`）、中文字体（`fonts-noto-cjk`）。slim 版需要联网 PyPI。

## 三、本次真机验收（本机隔离安装，未触碰任何运行中的服务）

| 项 | 结果 |
|---|---|
| 包内不得含站点配置/运行时产物（.ini、results/、*.db、backups/、__pycache__） | 全部通过 |
| tarball 与包内 `SHA256SUMS` 校验 | 通过 |
| `install.sh`（完整版，**离线** wheels 安装） | 成功 |
| `verify-install.sh --smoke` | **PASS=16 FAIL=0**，含 GUI 启动后 12s 存活 |
| 用**安装副本**跑真实基准（本地 mock OpenAI 兼容端点，C1/N2） | 两次验收：`success=2 fail=0`（100%）与 `success=1 fail=1`（50%，来自 mock 端点 15% 故意失败注入）；TTFT avg 0.115s / 0.154s |
| 验收 harness | `packaging/accept-release.sh` → **PASS=11 FAIL=0**（两次独立运行均全绿） |

## 四、已知边界（未覆盖，如实标注）

- 只构建/验证了 **Linux x86_64 + Python 3.10** 的 wheel 集合；Python 3.11/3.12、arm64、Windows、macOS 需各自 `pip download` 预取后才可离线安装（slim 版在任何平台联网安装均可）。
- GUI 冒烟在 Xvfb 虚拟显示下通过；未在真实桌面环境（GNOME/KDE）与多屏/缩放场景下人工验证。
- 未做 PyInstaller 单文件免安装包；未做 `.deb`。
- 报告导出链路（DOCX/PDF）依赖 LibreOffice 与中文字体，包内不包含这些系统依赖。
- 首次启动会以“被测端点 + 模型”为标识读取/写入本机 `llm_benchmark.ini`；界面「环境档案」等字段在未绑定 profile 时显示「未记录」（见 `OPEN_ITEMS.md`）。
