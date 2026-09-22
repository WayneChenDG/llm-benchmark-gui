# RELEASE 2.1.1 —— 修复：切成 English 后主题切换失效

发布日期：2026-09-22　　分支：master（主线）　　上一版：2.1.0

## 缺陷与根因（用户报告："切成 english 后，主题切换不正常"）

复现后定位到**同一处根因的两个症状**：

1. 主题下拉的选项文本是在**构建顶栏时**按当时的语言生成的（`values=[...]`），
   切换语言时只刷新了已注册的 i18n 控件文本，**没有刷新下拉的 `values`**
   → 英文界面里主题下拉仍显示中文「深海军蓝 / 浅色」。
2. 选中回调原先靠"显示名 → 主题键"映射（`_theme_key_from_label`）。语言切到英文后
   该函数按英文标签比对，而下拉里传回的是残留中文标签 → 映射失败返回 `None`
   → 回调静默 `return`，**看起来就是"点了没反应"**。

## 修复

- 新增 `_refresh_theme_combo()`：按当前语言重建下拉 `values`，并把当前选中项
  校正为当前主题在该语言下的显示名；在 `_refresh_ui_language()` 中调用。
- `_on_theme_selected()` 增加**按选项下标解析**的兜底（下拉 `values` 顺序 ==
  `theme_keys()` 顺序）：即使标签与当前语言不一致也能正确映射到主题键。
- 运行中禁止切换的提示文案补上 i18n（`theme.busy`，中英各一条），不再硬编码中文。

## 验证（真机 Xvfb + 真实控件，非纯逻辑推演）

`scripts/_probe_theme_i18n.py` —— 8 项断言全 OK，exit=0：

```
0) 起点固定中文
1) 中文下下拉标签 = (深海军蓝, 浅色)              OK
2) 切 English → 标签变 (Navy Blue, Light)        OK   ← 原缺陷 1
3) 英文下选第二项 → ACTIVE_THEME=light           OK   ← 原缺陷 2
4) 英文下切回深海军蓝                            OK
5) 切回中文 → 标签回中文、主题保持 navy          OK
6) 中文下切浅色                                  OK
7) ini 落盘 theme=light / language=zh_CN         OK
```

其它门禁同 2.1.0 保持通过：`pytest` 163 passed（`test_result_store` 的
`test_sorted_newest_first` 为**既有**间歇失败，单独跑必过、与本次改动无关）；
`scripts/uiux_contrast_audit.py` 两套主题门禁项全 PASS（exit=0）。

## 包与取包位置

| 形态 | 文件 |
|---|---|
| 完整版 | `jisumen-llm-benchmark-2.1.1-linux-x86_64.tar.gz` |
| 精简版 | `jisumen-llm-benchmark-2.1.1-linux-x86_64-slim.tar.gz` |

- 文件服务器：`\\192.168.1.254\data\software\installers\jisumen-llm-benchmark\2.1.1\`
- 构建机：`/opt/llm-benchmark/dist/`

哈希、包内 VERSION 与真机验收结论：

```
完整版  jisumen-llm-benchmark-2.1.1-linux-x86_64.tar.gz       130,431,764 B (124.4 MiB)
        sha256 87be63ceff4d907caa0b3f6fbfe253dae959fd16f34348a60a891bbfbdd1ad04
精简版  jisumen-llm-benchmark-2.1.1-linux-x86_64-slim.tar.gz      939,128 B (0.90 MiB)
        sha256 648f5360b386543ad91dd00d7f4877f1a1e5e4dce6e99cc87eaeea7d9513cad0

包内 VERSION：version=2.1.1 / arch=x86_64 / python=3.10 / git_rev=ca54f10
             built_at=2026-09-22T16:05:55+08:00
```

## 真机验收（隔离目录安装，不触碰在跑服务）

```
bash packaging/accept-release.sh dist/jisumen-llm-benchmark-2.1.1-linux-x86_64.tar.gz \
     http://127.0.0.1:8123/v1
==> 验收结果: PASS=11 FAIL=0
```

- 包体：sha256 校验通过；不含 ini / results / backups / `__pycache__` / `.db`；含 VERSION + 离线 wheels
- 隔离安装：`install.sh` 成功；`verify-install.sh` **PASS=16 FAIL=0**（含 GUI 冒烟 12s 存活）
- 真实负载：安装副本 C1/N2 → `success=2 fail=0 success_rate=100.0 ttft_avg=0.124s`

## 安装（目标机 Linux 桌面 x86_64）

```
tar -xzf jisumen-llm-benchmark-2.1.1-linux-x86_64.tar.gz
cd jisumen-llm-benchmark-2.1.1-linux-x86_64 && bash install.sh
~/jisumen-llm-benchmark/bin/jisumen-benchmark
```

从 2.1.0 升级：直接装 2.1.1 覆盖即可；配置（`llm_benchmark.ini` 的 url/模型/主题/语言）
与历史记录（`llm_benchmark_history.db`）在用户目录，不受影响。
