# 主题与配色（v2.1.0）

## 现状：两套主题，顶栏可切换

| 主题 | 观感 | 默认 |
|---|---|---|
| `navy` 深海军蓝 | 顶栏 / 页签条 / 表头 / 状态栏＝深海军蓝深底白字；内容区白底 | ✅ 默认 |
| `light` 浅色 | 顶栏 / 页签条 / 表头 / 状态栏＝白底深字；内容区白底 | — |

切换位置：**顶栏右侧「主题」下拉**（在「语言」左边）。选择后立即生效并写入
`llm_benchmark.ini` 的 `[ui] theme = navy|light`，下次启动沿用。
测试运行期间禁止切换（会弹提示并回退），因为切换需要重建界面。
主题与语言相互独立：切换语言会**同步刷新主题下拉的显示名**（中/英），但不会改变
当前主题；切换主题也不会改变语言。（历史缺陷：2.1.0 里切语言后下拉标签不刷新，
导致英文界面下点主题没反应——已在 2.1.1 修复，见 `RELEASE-2.1.1.md`。）

## 令牌权威：`llm_benchmark_app/ui_theme.py`

- 两套调色板是两个 dict 字面量：`_LIGHT`（浅色）与 `_NAVY`（深海军蓝）。
- `THEMES = {"navy": _NAVY, "light": _LIGHT}`；`TOKENS` 恒指向**当前**主题的字典
  （`set_theme(name)` 原地 `clear()+update()`，因此所有 `TOKENS[...]` 读取点立即生效）。
- 令牌分五组：
  - surface：`bg_main / bg_card / bg_header / bg_input / bg_inset / bg_hover / bg_stripe`
  - text：`text_primary / text_secondary / text_muted / text_inverse / text_disabled`
  - border：`border / border_light / border_strong / border_focus`
  - 品牌：`accent / accent_hover / accent_pressed / accent_disabled / accent_light /
    accent_soft / accent_text`（品牌蓝 `#0060F0` 固定，不随主题变）
  - **nav（新增，承载"深底条带"）**：`nav_bg / nav_bg_active / nav_fg / nav_fg_muted /
    nav_border / nav_accent`
    深海军蓝主题：`nav_bg #14263D`、`nav_bg_active #1D3454`、`nav_fg #F4F7FC`、
    `nav_fg_muted #A6B8D3`、`nav_border #0D1B2D`、`nav_accent #6BA6FF`
    浅色主题：`nav_bg #FFFFFF`、`nav_bg_active #F1F4F9`、`nav_fg #0F172A`、
    `nav_fg_muted #475569`、`nav_border #DCE3EC`、`nav_accent #0060F0`

## 改配色怎么做

1. 只改颜色 → 编辑 `ui_theme.py` 里对应主题的令牌值（两套主题键集必须一致，有单测把关）。
2. ttk 控件（顶栏/页签/表头/状态栏/下拉）的样式由 `apply_ttk_styles()` 从 `TOKENS` 派生，
   改令牌即可，无需改样式代码。
3. 跑门禁：`python3 scripts/uiux_contrast_audit.py --out docs/uiux-v2/CONTRAST_AUDIT.md`
   —— **两套主题分别门禁**（各 34 对，文字对比度 ≥4.5，非文字 ≥3.0），exit≠0 即不达标。
4. 跑测试：`python3 -m pytest tests/test_theme.py -q`（键集一致 / nav 对比度 / 原地切换）。

## 新增主题怎么做

1. 在 `ui_theme.py` 里加一个完整 dict 字面量（形如 `_MYTHEME: dict[str, str] = {...}`，
   **必须与现有主题键集完全一致**，否则 `test_palettes_have_identical_key_sets` 失败）。
2. 注册：`THEMES = {"navy": _NAVY, "light": _LIGHT, "mytheme": _MYTHEME}`。
3. i18n 加显示名：`zh_CN`/`en_US` 各加 `"theme.mytheme"`（顶栏下拉自动出现该选项）。
4. 跑对比度审计 + `tests/test_theme.py`。

## 为什么切换要重建界面

Tkinter 的颜色在**控件构建时**写入（`bg=`/`fg=`）与 ttk 样式里，没有全局主题钩子。
因此切换实现为：`set_theme()` → `_refresh_c_style()`（C_STYLE 原地更新）→
`_rebuild_color_maps()`（类级颜色映射重建）→ 销毁并重建 `root` 全部子控件（含 ttk 样式）。
代价是切换瞬间界面重建一次；收益是不遗留任何"半旧半新"的控件。

## 相关文件

- `llm_benchmark_app/ui_theme.py` —— 调色板 + ttk 样式 + 组件工厂（令牌权威）
- `llm_benchmark.py` —— `C_STYLE` 派生与 `_refresh_c_style()`、`_rebuild_color_maps()`、
  `_load_theme() / _save_theme_config() / _on_theme_selected() / _apply_theme()`
- `scripts/uiux_contrast_audit.py` —— 两套主题的对比度门禁
- `tests/test_theme.py` —— 主题一致性测试
- `scripts/_probe_theme.py` —— 开发用探针（两套主题构建 + 界面内切换 + 截图）
