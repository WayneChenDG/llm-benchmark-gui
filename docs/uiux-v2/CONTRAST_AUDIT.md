# JISUMAN LLM Benchmark — WCAG 2.1 Contrast & Token-Discipline Audit

- source: `/opt/llm-benchmark/llm_benchmark_app/ui_theme.py`
- C_STYLE block: lines 72–122
- thresholds: normal text 4.5 (SC 1.4.3), large text / non-text 3.0 (SC 1.4.11)

| 用途 | 前景 | 背景 | 对比度 | 阈值 | 结果 |
|---|---|---|---:|---:|:---:|
| 正文 text_primary / bg_main（门禁） | `text_primary` #0F172A | `bg_main` #F4F6FA | 16.50 | 4.5 | PASS |
| 标签 text_secondary / bg_main（门禁） | `text_secondary` #475569 | `bg_main` #F4F6FA | 7.00 | 4.5 | PASS |
| 提示 text_muted / bg_main（门禁） | `text_muted` #5F6C80 | `bg_main` #F4F6FA | 4.92 | 4.5 | PASS |
| 正文 text_primary / bg_card（门禁） | `text_primary` #0F172A | `bg_card` #FFFFFF | 17.85 | 4.5 | PASS |
| 标签 text_secondary / bg_card（门禁） | `text_secondary` #475569 | `bg_card` #FFFFFF | 7.58 | 4.5 | PASS |
| 提示 text_muted / bg_card（门禁） | `text_muted` #5F6C80 | `bg_card` #FFFFFF | 5.33 | 4.5 | PASS |
| 正文 text_primary / bg_header（门禁） | `text_primary` #0F172A | `bg_header` #FFFFFF | 17.85 | 4.5 | PASS |
| 标签 text_secondary / bg_header（门禁） | `text_secondary` #475569 | `bg_header` #FFFFFF | 7.58 | 4.5 | PASS |
| 提示 text_muted / bg_header（门禁） | `text_muted` #5F6C80 | `bg_header` #FFFFFF | 5.33 | 4.5 | PASS |
| 输入框正文 text_primary / bg_input（门禁） | `text_primary` #0F172A | `bg_input` #FFFFFF | 17.85 | 4.5 | PASS |
| 输入框提示 text_muted / bg_input（门禁） | `text_muted` #5F6C80 | `bg_input` #FFFFFF | 5.33 | 4.5 | PASS |
| 条纹行文字 text_primary / bg_stripe（门禁） | `text_primary` #0F172A | `bg_stripe` #F8FAFD | 17.07 | 4.5 | PASS |
| 条纹行次级 text_secondary / bg_stripe（门禁） | `text_secondary` #475569 | `bg_stripe` #F8FAFD | 7.25 | 4.5 | PASS |
| 悬停行文字 text_primary / bg_hover（门禁） | `text_primary` #0F172A | `bg_hover` #EEF2F8 | 15.89 | 4.5 | PASS |
| 悬停行次级 text_secondary / bg_hover（门禁） | `text_secondary` #475569 | `bg_hover` #EEF2F8 | 6.74 | 4.5 | PASS |
| success_text / success_bg（门禁） | `success_text` #146C3B | `success_bg` #E9F7EF | 5.87 | 4.5 | PASS |
| warning_text / warning_bg（门禁） | `warning_text` #8A5A00 | `warning_bg` #FDF6E7 | 5.51 | 4.5 | PASS |
| error_text / error_bg（门禁） | `error_text` #B3261E | `error_bg` #FDECEB | 5.72 | 4.5 | PASS |
| info_text / info_bg（门禁） | `info_text` #0B5FA5 | `info_bg` #EAF4FB | 5.90 | 4.5 | PASS |
| 主按钮反白 text_inverse / accent（门禁） | `text_inverse` #FFFFFF | `accent` #0060F0 | 5.34 | 4.5 | PASS |
| 禁用组合 白字压 success 实心色（信息） | `text_inverse` #FFFFFF | `success` #2FAF63 | 2.83 | 4.5 | INFO |
| 禁用组合 白字压 error 实心色（信息） | `text_inverse` #FFFFFF | `error` #E5534B | 3.70 | 4.5 | INFO |
| 禁用组合 白字压 warning 实心色（信息） | `text_inverse` #FFFFFF | `warning` #D29922 | 2.52 | 4.5 | INFO |
| 禁用组合 白字压 info 实心色（信息） | `text_inverse` #FFFFFF | `info` #4A9EDA | 2.92 | 4.5 | INFO |
| 强调链接 accent / bg_card（门禁） | `accent` #0060F0 | `bg_card` #FFFFFF | 5.34 | 4.5 | PASS |
| 强调链接 accent / bg_main（门禁） | `accent` #0060F0 | `bg_main` #F4F6FA | 4.94 | 4.5 | PASS |
| 强调文字 accent / accent_light（门禁） | `accent` #0060F0 | `accent_light` #E8F0FE | 4.66 | 4.5 | PASS |
| 强调文字 accent_text / bg_card（门禁） | `accent_text` #0B4FB0 | `bg_card` #FFFFFF | 7.61 | 4.5 | PASS |
| 强调文字 accent_text / accent_light（门禁） | `accent_text` #0B4FB0 | `accent_light` #E8F0FE | 6.64 | 4.5 | PASS |
| 品牌蓝浅底上焦 accent_text / accent_soft（门禁） | `accent_text` #0B4FB0 | `accent_soft` #D3E4FD | 5.90 | 4.5 | PASS |
| 提示文字 text_muted / bg_inset（门禁） | `text_muted` #5F6C80 | `bg_inset` #F1F5F9 | 4.86 | 4.5 | PASS |
| 标签 text_secondary / bg_inset（门禁） | `text_secondary` #475569 | `bg_inset` #F1F5F9 | 6.92 | 4.5 | PASS |
| 强调悬停 accent_hover / bg_card（门禁） | `accent_hover` #0052CF | `bg_card` #FFFFFF | 6.76 | 4.5 | PASS |
| 强调文字 accent / accent_soft（信息） | `accent` #0060F0 | `accent_soft` #D3E4FD | 4.14 | 4.5 | INFO |
| 焦点环 border_focus / bg_card（门禁） | `border_focus` #0060F0 | `bg_card` #FFFFFF | 5.34 | 3.0 | PASS |
| 焦点环 border_focus / bg_main（门禁） | `border_focus` #0060F0 | `bg_main` #F4F6FA | 4.94 | 3.0 | PASS |
| 卡片边框 border / bg_card（信息） | `border` #DCE3EC | `bg_card` #FFFFFF | 1.29 | 3.0 | INFO |
| 卡片边框 border / bg_main（信息） | `border` #DCE3EC | `bg_main` #F4F6FA | 1.20 | 3.0 | INFO |
| 卡片边框 border / bg_hover（信息） | `border` #DCE3EC | `bg_hover` #EEF2F8 | 1.15 | 3.0 | INFO |
| 分割线 border_light / bg_card（信息） | `border_light` #EDF1F6 | `bg_card` #FFFFFF | 1.13 | 3.0 | INFO |
| 分割线 border_light / bg_main（信息） | `border_light` #EDF1F6 | `bg_main` #F4F6FA | 1.05 | 3.0 | INFO |
| 分割线 border_light / bg_header（信息） | `border_light` #EDF1F6 | `bg_header` #FFFFFF | 1.13 | 3.0 | INFO |
| 强调色块 accent / accent_light（信息） | `accent` #0060F0 | `accent_light` #E8F0FE | 4.66 | 3.0 | PASS |
| 表面层次 bg_stripe / bg_main（信息） | `bg_stripe` #F8FAFD | `bg_main` #F4F6FA | 1.03 | 3.0 | INFO |
| 表面层次 bg_hover / bg_main（信息） | `bg_hover` #EEF2F8 | `bg_main` #F4F6FA | 1.04 | 3.0 | INFO |
| 表面层次 bg_inset / bg_main（信息） | `bg_inset` #F1F5F9 | `bg_main` #F4F6FA | 1.01 | 3.0 | INFO |

## 统计（门禁项）

- 门禁配对数: **31**
- PASS: **31**
- FAIL: **0**
- 信息项（结构性/禁用组合，不计入退出码）: **15**，其中未达阈值 14 项
- 禁用组合源码命中（白字压语义实心色）: **0**

### 最差的三对（对比度最低）

| # | 用途 | 前景 | 背景 | 对比度 | 阈值 | 结果 |
|---:|---|---|---|---:|---:|:---:|
| 1 | 强调文字 accent / accent_light | `accent` #0060F0 | `accent_light` #E8F0FE | 4.66 | 4.5 | PASS |
| 2 | 提示文字 text_muted / bg_inset | `text_muted` #5F6C80 | `bg_inset` #F1F5F9 | 4.86 | 4.5 | PASS |
| 3 | 提示 text_muted / bg_main | `text_muted` #5F6C80 | `bg_main` #F4F6FA | 4.92 | 4.5 | PASS |

## 硬编码色值扫描（设计令牌纪律）

- `#RRGGBB` 字面量总出现次数: **24**
- 其中位于 C_STYLE 定义块内: **0**
- 其中位于 C_STYLE 定义块之外: **24** (100.0%)
- 全文件不同色值数: 11；C_STYLE 之外不同色值数: 11

### 出现次数最多的前 10 个色值

| # | 色值 | 总出现次数 | 块外出现次数 |
|---:|---|---:|---:|
| 1 | `#533AFD` | 5 | 5 |
| 2 | `#10B981` | 4 | 4 |
| 3 | `#F59E0B` | 4 | 4 |
| 4 | `#EF4444` | 3 | 3 |
| 5 | `#CBD5E1` | 2 | 2 |
| 6 | `#059669` | 1 | 1 |
| 7 | `#4A90E2` | 1 | 1 |
| 8 | `#8B5CF6` | 1 | 1 |
| 9 | `#DC2626` | 1 | 1 |
| 10 | `#E8A000` | 1 | 1 |

### C_STYLE 块外出现最多的 10 个色值（纪律违规热点）

| # | 色值 | 块外次数 | 行号 |
|---:|---|---:|---|
| 1 | `#533AFD` | 5 | 8504, 8518, 8534, 8570, 8578 |
| 2 | `#10B981` | 4 | 8505, 8519, 8537, 8572 |
| 3 | `#F59E0B` | 4 | 8506, 8540, 8579, 8581 |
| 4 | `#EF4444` | 3 | 8507, 8543, 8575 |
| 5 | `#CBD5E1` | 2 | 1324, 1351 |
| 6 | `#059669` | 1 | 9216 |
| 7 | `#4A90E2` | 1 | 6231 |
| 8 | `#8B5CF6` | 1 | 8546 |
| 9 | `#DC2626` | 1 | 9217 |
| 10 | `#E8A000` | 1 | 6221 |
