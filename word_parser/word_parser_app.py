#!/usr/bin/env python3
"""
Word 文档解析工具 — 桌面应用
================================
基于 Gradio 的 Web UI，上传 Word 文档后自动识别题型并解析为 JSON。

支持场景：
  - 上传单一题型的文档（如仅「信息获取」）
  - 上传包含多个题型的综合文档（自动检测并分别解析）

打包为桌面应用：
  macOS:  bash build_mac.sh
  Windows: build_windows.bat
"""

import os
import sys
import json
import traceback

# ---- PyInstaller 兼容：将打包资源路径加入 sys.path ----
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(sys.executable)
    _RESOURCE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    if _RESOURCE_DIR not in sys.path:
        sys.path.insert(0, _RESOURCE_DIR)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _RESOURCE_DIR = _BASE_DIR

import gradio as gr

from word_parser import (
    parse_document_auto,
    PARSER_MAP,
)


# ============================================================================
# 路径配置
# ============================================================================
BASE_DIR = _BASE_DIR
OUTPUT_DIR = os.path.join(BASE_DIR, "word_parsed")
os.makedirs(OUTPUT_DIR, exist_ok=True)

TYPE_COLORS = {
    "信息获取": "#3b82f6",
    "课文跟读": "#10b981",
    "信息转述及询问": "#f59e0b",
    "模仿朗读": "#8b5cf6",
    "词汇": "#ef4444",
}

TYPE_DESCRIPTIONS = {
    "信息获取": "提取「听选信息」「回答问题」的题目与录音稿",
    "课文跟读": "提取句子跟读（去序号排序）、段落跟读、语篇跟读",
    "信息转述及询问": "提取「第一节 信息转述」的录音稿",
    "模仿朗读": "提取每个单元的外网(2篇)和教材(1篇)朗读素材",
    "词汇": "预留接口，未来接入",
}


# ============================================================================
# 主题与样式
# ============================================================================

CUSTOM_THEME = gr.themes.Soft(
    primary_hue=gr.themes.colors.blue,
    secondary_hue=gr.themes.colors.sky,
    neutral_hue=gr.themes.colors.slate,
    font=["system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
    font_mono=["SF Mono", "Menlo", "Consolas", "monospace"],
    radius_size=gr.themes.sizes.radius_md,
    spacing_size=gr.themes.sizes.spacing_md,
).set(
    background_fill_primary="#f3f4f6",
    background_fill_primary_dark="#0f172a",
    background_fill_secondary="#ffffff",
    background_fill_secondary_dark="#111827",
    block_background_fill="transparent",
    block_background_fill_dark="transparent",
    block_border_color="transparent",
    block_border_color_dark="transparent",
    block_border_width="0px",
    block_border_width_dark="0px",
    block_radius="0px",
    body_text_color="#111827",
    body_text_color_dark="#e2e8f0",
    body_text_color_subdued="#6b7280",
    body_text_color_subdued_dark="#94a3b8",
    block_label_text_color="#6b7280",
    block_label_text_color_dark="#94a3b8",
    block_title_text_color="#111827",
    block_title_text_color_dark="#e2e8f0",
    input_background_fill="#ffffff",
    input_background_fill_dark="#1e293b",
    input_border_color="#d1d5db",
    input_border_color_dark="#374151",
    input_border_width="1px",
    input_radius="6px",
    input_placeholder_color="#9ca3af",
    input_placeholder_color_dark="#64748b",
    button_primary_background_fill="#2563eb",
    button_primary_background_fill_hover="#1d4ed8",
    button_primary_text_color="#ffffff",
    button_primary_text_color_dark="#ffffff",
    button_primary_border_color="transparent",
    button_secondary_background_fill="#ffffff",
    button_secondary_background_fill_dark="#1e293b",
    button_secondary_background_fill_hover="#f3f4f6",
    button_secondary_background_fill_hover_dark="#334155",
    button_secondary_border_color="#d1d5db",
    button_secondary_border_color_dark="#374151",
    button_secondary_text_color="#374151",
    button_secondary_text_color_dark="#94a3b8",
    container_radius="0px",
    shadow_drop="none",
    shadow_drop_lg="none",
    shadow_inset="none",
    shadow_spread="0px",
    shadow_spread_dark="0px",
)

CUSTOM_CSS = """
/* ===== CSS 变量 ===== */
:root {
    --c-bg: #f0f0f0;
    --c-panel: #ffffff;
    --c-sidebar: #f7f7f8;
    --c-toolbar: #f0f0f0;
    --c-statusbar: #f0f0f0;
    --c-text: #1a1a1a;
    --c-text-sub: #555555;
    --c-text-muted: #999999;
    --c-border: #d4d4d4;
    --c-border-light: #e8e8e8;
    --c-accent: #2563eb;
    --c-accent-hover: #1d4ed8;
    --c-accent-bg: #eff6ff;
    --c-hover: #ececec;
    --c-scrollbar: #c0c0c0;
    --c-code-bg: #f3f4f6;
    --c-code-text: #1f2937;
    --c-quote-bg: #f9fafb;
    --c-quote-border: #93c5fd;
    --c-quote-text: #4b5563;
}

body.dark {
    --c-bg: #0d1117;
    --c-panel: #161b22;
    --c-sidebar: #0d1117;
    --c-toolbar: #0d1117;
    --c-statusbar: #0d1117;
    --c-text: #e6edf3;
    --c-text-sub: #8b949e;
    --c-text-muted: #6e7681;
    --c-border: #30363d;
    --c-border-light: #21262d;
    --c-accent: #58a6ff;
    --c-accent-hover: #79c0ff;
    --c-accent-bg: rgba(56,139,253,0.15);
    --c-hover: #1c2128;
    --c-scrollbar: #30363d;
    --c-code-bg: #1c2128;
    --c-code-text: #e6edf3;
    --c-quote-bg: rgba(56,139,253,0.1);
    --c-quote-border: #58a6ff;
    --c-quote-text: #8b949e;
}

/* ===== 全局 ===== */
* { box-sizing: border-box !important; }

html, body {
    margin: 0 !important;
    padding: 0 !important;
    height: 100% !important;
    overflow: hidden !important;
    background: var(--c-bg) !important;
}

.gradio-container {
    max-width: 100% !important;
    width: 100% !important;
    height: 100vh !important;
    margin: 0 !important;
    padding: 0 !important;
    background: var(--c-bg) !important;
    color: var(--c-text) !important;
    overflow: hidden !important;
}

/* 隐藏 Gradio footer */
footer.svelte-zxu34v,
.show-api, .built-with, .settings,
button.show-api, a.built-with, button.settings,
.record, .show-api-divider {
    display: none !important;
}

main.fillable {
    width: 100% !important;
    max-width: 100% !important;
    height: 100vh !important;
    padding: 0 !important;
    background: var(--c-bg) !important;
    gap: 0 !important;
    overflow: hidden !important;
}

/* ===== 整体布局：垂直三段 ===== */
.wrap, .contain {
    flex-direction: column !important;
    width: 100% !important;
    height: 100% !important;
    gap: 0 !important;
    overflow: hidden !important;
}
.contain > .column {
    flex-grow: 1 !important;
    width: 100% !important;
    height: 100% !important;
    gap: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    overflow: hidden !important;
}

/* Gradio 内部透明 */
.gr-block, .gr-form, .gr-group, .gr-panel,
.gradio-container .form, .gradio-container .block, .gradio-container .wrap {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    border-radius: 0 !important;
}
.column, .gradio-column {
    background: transparent !important;
    border: none !important;
    padding: 0 !important;
    gap: 0 !important;
}

/* ===== 顶部工具栏（兼标题栏） ===== */
#toolbar {
    flex-direction: row !important;
    align-items: center !important;
    gap: 0 !important;
    height: 48px !important;
    min-height: 48px !important;
    flex-shrink: 0 !important;
    background: transparent !important;
    border-bottom: none !important;
    padding: 12px 16px 0 16px !important;
    box-sizing: border-box !important;
    -webkit-app-region: drag;
}
#toolbar > .form { flex: 0 0 auto !important; -webkit-app-region: no-drag; }
#toolbar > .form:first-child { flex: 1 1 auto !important; -webkit-app-region: drag; }

.toolbar-wrap {
    display: flex;
    align-items: center;
    height: 28px;
    gap: 10px;
    padding: 0;
    flex: 1;
    flex-wrap: nowrap !important;
    -webkit-app-region: drag;
}

/* 窗口控制按钮 - macOS 风格红黄绿 */
.window-controls {
    display: inline-flex !important;
    flex-direction: row !important;
    align-items: center !important;
    justify-content: center !important;
    gap: 8px;
    flex-shrink: 0;
    flex-wrap: nowrap !important;
    height: 14px;
    line-height: 0;
    font-size: 0;
    -webkit-app-region: no-drag;
    margin-right: 4px;
    vertical-align: middle;
}
.win-btn {
    display: inline-block !important;
    width: 12px !important;
    height: 12px !important;
    min-width: 12px !important;
    max-width: 12px !important;
    min-height: 12px !important;
    max-height: 12px !important;
    border-radius: 50% !important;
    border: none !important;
    padding: 0 !important;
    margin: 0 !important;
    cursor: pointer;
    position: relative;
    box-sizing: border-box !important;
    vertical-align: top !important;
    line-height: 0 !important;
    font-size: 0 !important;
    overflow: hidden;
    -webkit-app-region: no-drag;
    transition: filter 0.15s;
}
.win-btn:hover { filter: brightness(0.88); }
.win-btn:active { filter: brightness(0.75); }
.win-btn .win-icon {
    opacity: 0;
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    font-size: 8px;
    line-height: 1;
    color: rgba(0,0,0,0.45);
    font-weight: 700;
    pointer-events: none;
}
.window-controls:hover .win-icon { opacity: 1; }
.win-close { background: #ff5f57; }
.win-min { background: #febc2e; }
.win-max { background: #28c840; }

/* 品牌标题 */
.toolbar-brand {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    font-weight: 600;
    color: var(--c-text-sub);
    white-space: nowrap;
    user-select: none;
    -webkit-app-region: drag;
}
.toolbar-brand .brand-mark {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    border-radius: 4px;
    background: #2563eb;
    color: #fff;
    font-size: 11px;
    font-weight: 700;
    flex-shrink: 0;
}
.toolbar-spacer { flex: 1; }

/* 工具栏按钮 */
#parse-btn {
    background: #2563eb !important;
    border: 1px solid #2563eb !important;
    color: #fff !important;
    font-size: 12px !important;
    font-weight: 500 !important;
    padding: 0 16px !important;
    border-radius: 5px !important;
    height: 28px !important;
    min-height: 28px !important;
    line-height: 1 !important;
    box-shadow: none !important;
    transition: background 0.1s !important;
    flex: 0 0 auto !important;
    width: auto !important;
    -webkit-app-region: no-drag;
}
#parse-btn:hover {
    background: #1d4ed8 !important;
    border-color: #1d4ed8 !important;
}
#clear-btn {
    background: transparent !important;
    border: 1px solid var(--c-border) !important;
    color: var(--c-text-sub) !important;
    font-size: 12px !important;
    font-weight: 400 !important;
    padding: 0 14px !important;
    border-radius: 5px !important;
    height: 28px !important;
    min-height: 28px !important;
    line-height: 1 !important;
    box-shadow: none !important;
    transition: all 0.1s !important;
    flex: 0 0 auto !important;
    width: auto !important;
    -webkit-app-region: no-drag;
}
#clear-btn:hover {
    border-color: var(--c-text-muted) !important;
    background: var(--c-hover) !important;
}

/* ===== 主体区域 ===== */
#body {
    flex-direction: row !important;
    flex: 1 !important;
    min-height: 0 !important;
    overflow: hidden !important;
    gap: 0 !important;
}

/* ===== 左侧边栏 ===== */
#sidebar {
    width: 280px !important;
    min-width: 280px !important;
    max-width: 280px !important;
    background: var(--c-sidebar) !important;
    border-right: 1px solid var(--c-border) !important;
    padding: 0 !important;
    overflow-y: auto !important;
    overflow-x: hidden !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 0 !important;
    height: 100% !important;
    flex-shrink: 0 !important;
}
#sidebar::-webkit-scrollbar { width: 6px; }
#sidebar::-webkit-scrollbar-track { background: transparent; }
#sidebar::-webkit-scrollbar-thumb { background: var(--c-scrollbar); border-radius: 3px; }

/* 侧边栏内部 */
#sidebar .gr-group, #sidebar .styler {
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
}

.sidebar-section {
    padding: 14px 16px;
    border-bottom: 1px solid var(--c-border-light);
}
.sidebar-section-title {
    font-size: 11px;
    font-weight: 600;
    color: var(--c-text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 8px;
}

/* 上传区 */
#file-upload {
    min-height: 60px !important;
    max-height: 120px !important;
    padding: 0 16px !important;
    margin: 0 !important;
    flex-shrink: 0 !important;
}
#file-upload .wrap, #file-upload .svelte-1uj8rng {
    min-height: 60px !important;
    max-height: 120px !important;
    padding: 0 !important;
}
#file-upload .svelte-8prmba,
#file-upload button {
    border: 1px dashed var(--c-border) !important;
    border-radius: 6px !important;
    background: var(--c-panel) !important;
    min-height: 60px !important;
    max-height: 120px !important;
    font-size: 12px !important;
    color: var(--c-text-muted) !important;
    transition: all 0.1s !important;
    width: 100% !important;
    padding: 12px !important;
}
#file-upload .svelte-8prmba:hover,
#file-upload button:hover {
    border-color: var(--c-accent) !important;
    background: var(--c-accent-bg) !important;
    color: var(--c-accent) !important;
}
#file-upload .svelte-1vmd51o {
    flex-direction: column !important;
    gap: 4px !important;
}
#file-upload .svelte-1vmd51o .icon-wrap svg {
    width: 20px !important;
    height: 20px !important;
}
#file-upload .or { display: none !important; }
#file-upload label { display: none !important; }

/* 题型说明 */
#sidebar-types {
    flex: 1 !important;
}
.types-note {
    font-size: 11.5px;
    color: var(--c-text-muted);
    line-height: 1.7;
}
.types-note .type-tag {
    color: var(--c-text-sub);
}

/* ===== 主面板 ===== */
#main-panel {
    flex: 1 !important;
    background: var(--c-panel) !important;
    display: flex !important;
    flex-direction: column !important;
    min-width: 0 !important;
    overflow: hidden !important;
    height: 100% !important;
    padding: 0 !important;
    min-height: 0 !important;
}
#main-panel .gr-group, #main-panel .styler {
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    height: 100% !important;
    display: flex !important;
    flex-direction: column !important;
}

/* Tab */
#main-panel .tabs {
    height: 100% !important;
    display: flex !important;
    flex-direction: column !important;
}
#main-panel .tab-wrapper {
    flex-shrink: 0 !important;
    border-bottom: 1px solid var(--c-border) !important;
    background: var(--c-toolbar) !important;
}
#main-panel .tab-container {
    display: flex !important;
    gap: 0 !important;
    padding: 0 16px !important;
    height: 36px !important;
}
#main-panel .tab-container button {
    font-size: 12px !important;
    font-weight: 400 !important;
    color: var(--c-text-muted) !important;
    border: none !important;
    border-bottom: 2px solid transparent !important;
    border-radius: 0 !important;
    padding: 0 14px !important;
    height: 36px !important;
    background: transparent !important;
    transition: all 0.1s !important;
}
#main-panel .tab-container button:hover {
    color: var(--c-text-sub) !important;
    background: var(--c-hover) !important;
}
#main-panel .tab-container button.selected {
    color: var(--c-accent) !important;
    border-bottom-color: var(--c-accent) !important;
    font-weight: 500 !important;
    background: transparent !important;
}
#main-panel .tabitem {
    flex: 1 !important;
    overflow: hidden !important;
    padding: 0 !important;
}
#main-panel .tabitem > .column {
    height: 100% !important;
}

/* 预览区 */
#preview-area {
    height: 100% !important;
    overflow-y: auto !important;
    padding: 20px 24px !important;
    color: var(--c-text) !important;
    max-height: none !important;
    flex: 1 !important;
}
#preview-area::-webkit-scrollbar { width: 6px; }
#preview-area::-webkit-scrollbar-track { background: transparent; }
#preview-area::-webkit-scrollbar-thumb { background: var(--c-scrollbar); border-radius: 3px; }
#preview-area h2 {
    font-size: 14px !important;
    font-weight: 600 !important;
    color: var(--c-text) !important;
    margin: 18px 0 4px 0 !important;
    padding-bottom: 4px;
    border-bottom: 1px solid var(--c-border-light);
}
#preview-area blockquote {
    border-left: 2px solid var(--c-quote-border) !important;
    background: var(--c-quote-bg) !important;
    padding: 4px 10px !important;
    margin: 2px 0 4px 0 !important;
    border-radius: 0 4px 4px 0 !important;
    font-size: 12px !important;
    color: var(--c-quote-text) !important;
    line-height: 1.5 !important;
}
#preview-area hr {
    border: none !important;
    border-top: 1px solid var(--c-border-light) !important;
    margin: 12px 0 !important;
}
#preview-area ul { padding-left: 0 !important; list-style: none !important; }
#preview-area li {
    padding: 4px 0 !important;
    font-size: 12px !important;
    color: var(--c-text) !important;
    border-bottom: 1px solid var(--c-border-light);
}
#preview-area li:last-child { border-bottom: none; }
#preview-area code {
    background: var(--c-code-bg) !important;
    color: var(--c-code-text) !important;
    font-size: 11px !important;
    padding: 1px 4px !important;
    border-radius: 3px !important;
    font-family: "SF Mono", "Menlo", "Consolas", monospace !important;
}
#preview-area strong { color: var(--c-text) !important; }
#preview-area em { color: var(--c-text-sub) !important; }
#preview-area .empty-hint {
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: center !important;
    height: 100% !important;
    text-align: center;
    color: var(--c-text-muted);
    font-size: 13px;
    gap: 8px;
}
#preview-area .empty-hint .empty-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 48px;
    height: 48px;
    border-radius: 10px;
    background: var(--c-accent-bg);
    color: var(--c-accent);
    font-size: 20px;
    font-weight: 700;
    margin-bottom: 4px;
}

/* JSON 输出 */
#json-output, #json-output textarea {
    background: var(--c-panel) !important;
    border: none !important;
    box-shadow: none !important;
    border-radius: 0 !important;
    height: 100% !important;
}
#json-output textarea {
    font-family: "SF Mono", "Menlo", "Consolas", monospace !important;
    font-size: 12px !important;
    line-height: 1.6 !important;
    color: var(--c-text) !important;
    min-height: 300px !important;
    border-radius: 0 !important;
    padding: 20px 24px !important;
}

/* 下载区 */
#download-area {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    text-align: center;
}
.dl-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    background: #2563eb !important;
    color: #fff !important;
    border: 1px solid #2563eb !important;
    font-size: 13px !important;
    font-weight: 500;
    padding: 10px 28px !important;
    border-radius: 6px !important;
    cursor: pointer;
    transition: background 0.15s, transform 0.1s;
    -webkit-app-region: no-drag;
    outline: none;
}
.dl-btn:hover {
    background: #1d4ed8 !important;
    border-color: #1d4ed8 !important;
}
.dl-btn:active {
    transform: scale(0.97);
}

/* ===== 底部状态栏 ===== */
#statusbar {
    flex-direction: row !important;
    align-items: center !important;
    gap: 0 !important;
    height: 28px !important;
    min-height: 28px !important;
    flex-shrink: 0 !important;
    background: transparent !important;
    border-top: none !important;
    padding: 0 16px !important;
}
#statusbar > .form { flex: 1 1 auto !important; }
#statusbar > .form:last-child { flex: 0 0 auto !important; }
#statusbar .block { padding: 0 !important; margin: 0 !important; }

#status-text, #status-text textarea, #status-text label {
    background: transparent !important;
    border: none !important;
    font-size: 11px !important;
    color: var(--c-text-sub) !important;
    padding: 0 !important;
    height: 20px !important;
    line-height: 20px !important;
}
#status-text .input-container, #status-text .svelte-1hguek3 {
    border: none !important;
    background: transparent !important;
    padding: 0 !important;
}

/* 状态栏统计 */
.stats-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    height: 20px;
}
.stat-pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 1px 8px;
    background: transparent;
    border: 1px solid var(--c-border);
    border-radius: 10px;
    font-size: 10.5px;
    color: var(--c-text-sub);
    font-weight: 400;
    line-height: 1;
}
.stat-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
}
.stat-count {
    color: var(--c-accent);
    font-weight: 600;
}
"""


# ============================================================================
# 核心处理函数
# ============================================================================

def _get_filepath(file_obj):
    if file_obj is None:
        return None
    if isinstance(file_obj, list):
        file_obj = file_obj[0] if file_obj else None
    if file_obj is None:
        return None
    if isinstance(file_obj, str):
        return file_obj
    for attr in ("name", "path"):
        v = getattr(file_obj, attr, None)
        if isinstance(v, str) and v:
            return v
    if isinstance(file_obj, dict):
        return file_obj.get("name") or file_obj.get("path")
    return None


def process_file(file_obj):
    filepath = _get_filepath(file_obj)
    if not filepath:
        raise gr.Error("请先上传 Word 文档（.docx）")

    if not filepath.lower().endswith('.docx'):
        raise gr.Error("仅支持 .docx 格式的 Word 文档")

    if not os.path.exists(filepath):
        raise gr.Error(f"文件不存在: {filepath}")

    try:
        results, summary = parse_document_auto(filepath)
    except Exception as e:
        raise gr.Error(f"解析失败: {e}\n{traceback.format_exc()}")

    if not results:
        raise gr.Error(summary or "未识别到任何题型内容，请检查文档格式")

    json_str = json.dumps(results, ensure_ascii=False, indent=2)

    source_name = os.path.splitext(os.path.basename(filepath))[0]
    json_filename = f"{source_name}_parsed.json"
    json_path = os.path.join(OUTPUT_DIR, json_filename)
    with open(json_path, 'w', encoding='utf-8') as f:
        f.write(json_str)

    preview_md = _build_preview(results, summary)
    stats_md = _build_stats_bar(results)

    total = sum(r["item_count"] for r in results)
    type_names = "、".join(r["doc_type"] for r in results)
    status_text = f"完成 — {summary}  |  题型：{type_names}  |  共 {total} 条"

    # 下载按钮 HTML：嵌入文件路径，点击时调用 pywebview 原生保存或浏览器下载
    dl_html = _build_download_html(json_path, json_filename)

    return json_str, preview_md, stats_md, status_text, json_path, dl_html


def _build_download_html(json_path, json_filename):
    """构建下载按钮 HTML，兼容 pywebview 和浏览器两种模式。"""
    import base64
    import json as _json
    import html as _html
    # 读取文件内容并转 base64，用于浏览器模式下载
    try:
        with open(json_path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('ascii')
    except Exception:
        b64 = ''

    # 用 json.dumps 安全转义字符串为 JS 字面量（双引号）
    js_path = _json.dumps(json_path)
    js_name = _json.dumps(json_filename)

    onclick_js = (
        "try{window.pywebview.api.save_file("
        + js_path + "," + js_name
        + ")}catch(e){"
        "var a=document.createElement('a');"
        "a.href='data:application/json;base64," + b64 + "';"
        "a.download=" + js_name + ";"
        "a.click();}"
    )

    # HTML 属性转义，避免双引号冲突
    escaped_js = _html.escape(onclick_js, quote=True)

    return (
        '<div style="text-align:center; padding:40px 20px;">'
        '<div style="margin-bottom:20px; color:var(--c-text-sub); font-size:13px;">'
        '解析完成，点击下方按钮下载 JSON 文件'
        '</div>'
        f'<button class="dl-btn" onclick="{escaped_js}">'
        '<span>下载 JSON 文件</span>'
        '</button>'
        '<div style="margin-top:16px; color:var(--c-text-muted); font-size:11px;">'
        'JSON 文件同时已保存到 word_parsed/ 目录'
        '</div>'
        '</div>'
    )


def _empty_download_html():
    return (
        '<div style="text-align:center; padding:40px 20px;">'
        '<div style="color:var(--c-text-sub); font-size:13px;">'
        '解析完成后可在此下载 JSON 文件'
        '</div>'
        '</div>'
    )


def _build_stats_bar(results):
    total = sum(r["item_count"] for r in results)
    parts = ['<div class="stats-wrap">']
    for r in results:
        color = TYPE_COLORS.get(r["doc_type"], "#64748b")
        parts.append(
            f'<span class="stat-pill">'
            f'<span class="stat-dot" style="background:{color}"></span>'
            f'<span>{r["doc_type"]} <span class="stat-count">{r["item_count"]}</span></span>'
            f'</span>'
        )
    parts.append(
        f'<span class="stat-pill">'
        f'<span>共 <span class="stat-count">{total}</span> 条</span>'
        f'</span>'
    )
    parts.append('</div>')
    return ''.join(parts)


def _build_preview(results, summary):
    lines = []
    for result in results:
        doc_type = result["doc_type"]
        count = result["item_count"]
        lines.append(f"## {doc_type}")

        if doc_type in TYPE_DESCRIPTIONS:
            lines.append(f"*{TYPE_DESCRIPTIONS[doc_type]}* — {count} 条\n")

        for item in result["items"]:
            cat = item.get("category", "")
            text = item.get("text", "")
            preview_text = text[:120].replace('\n', ' / ')
            if len(text) > 120:
                preview_text += "..."

            tags = []
            if "number" in item:
                tags.append(f"`#{item['number']}`")
            elif "index" in item:
                tags.append(f"`#{item['index']}`")
            elif "discourse_number" in item:
                tags.append(f"`语篇{item['discourse_number']}`")
            if "unit" in item and item["unit"]:
                tags.append(f"`{item['unit']}`")
            if "section" in item and item["section"]:
                tags.append(f"`{item['section']}`")

            tag_str = " ".join(tags) + " " if tags else ""
            lines.append(f"- **{cat}** {tag_str}")
            lines.append(f"  > {preview_text}\n")

        lines.append("\n---\n")
    return '\n'.join(lines)


def clear_all():
    return (
        None, "", _empty_preview(),
        '<div class="stats-wrap"><span class="stat-pill"><span>等待解析</span></span></div>',
        "就绪", None, _empty_download_html()
    )


def _empty_preview():
    return (
        '<div class="empty-hint">'
        '<div class="empty-icon">W</div>'
        '<div>上传 Word 文档并点击「开始解析」</div>'
        '<div style="font-size:11.5px; color:var(--c-text-muted);">支持 .docx 格式，自动识别题型</div>'
        '</div>'
    )


def get_supported_types_html():
    parts = ['<div class="types-note">']
    items = []
    for doc_type in PARSER_MAP:
        items.append(f'<span class="type-tag">{doc_type}</span>')
    parts.append('、'.join(items))
    parts.append('</div>')
    return ''.join(parts)


# ============================================================================
# 界面
# ============================================================================

with gr.Blocks(title="Word 文档解析工具") as app:

    # ===== 顶部工具栏 =====
    with gr.Row(elem_id="toolbar"):
        gr.HTML(
            '<div class="toolbar-wrap">'
            '<div class="window-controls">'
            '<button class="win-btn win-close" onclick="try{window.pywebview.api.close_app()}catch(e){window.close()}"><span class="win-icon">x</span></button>'
            '<button class="win-btn win-min" onclick="try{window.pywebview.api.minimize_window()}catch(e){}"><span class="win-icon">–</span></button>'
            '<button class="win-btn win-max" onclick="try{window.pywebview.api.toggle_maximize()}catch(e){}"><span class="win-icon">+</span></button>'
            '</div>'
            '<div class="toolbar-brand">'
            '<span class="brand-mark">W</span>'
            '<span>Word 文档解析工具</span>'
            '</div>'
            '<div class="toolbar-spacer"></div>'
            '</div>'
        )
        parse_btn = gr.Button("开始解析", variant="primary", elem_id="parse-btn")
        clear_btn = gr.Button("清除", elem_id="clear-btn")

    # ===== 主体 =====
    with gr.Row(elem_id="body", equal_height=False):

        # ---------- 侧边栏 ----------
        with gr.Column(scale=0, min_width=280, elem_id="sidebar"):

            with gr.Group(elem_id="sidebar-upload"):
                gr.HTML('<div class="sidebar-section"><div class="sidebar-section-title">文档上传</div></div>')
                file_input = gr.File(
                    label="",
                    file_types=[".docx"],
                    file_count="single",
                    type="filepath",
                    elem_id="file-upload",
                )

            with gr.Group(elem_id="sidebar-types"):
                gr.HTML(
                    '<div class="sidebar-section">'
                    '<div class="sidebar-section-title">支持题型</div>'
                    + get_supported_types_html() +
                    '</div>'
                )

        # ---------- 主面板 ----------
        with gr.Column(scale=1, min_width=400, elem_id="main-panel"):

            with gr.Tabs():
                with gr.Tab("结果预览"):
                    preview_output = gr.HTML(
                        value=_empty_preview(),
                        elem_id="preview-area",
                    )

                with gr.Tab("JSON 数据"):
                    json_output = gr.Textbox(
                        label="",
                        lines=20,
                        max_lines=50,
                        elem_id="json-output",
                        interactive=False,
                        show_label=False,
                    )

                with gr.Tab("下载"):
                    download_html = gr.HTML(
                        value=_empty_download_html(),
                        elem_id="download-area",
                    )
                    current_json_path = gr.State(value=None)

    # ===== 底部状态栏 =====
    with gr.Row(elem_id="statusbar"):
        status_box = gr.Textbox(
            label="",
            interactive=False,
            value="就绪",
            elem_id="status-text",
            container=False,
            show_label=False,
        )
        stats_output = gr.HTML(
            value='<div class="stats-wrap"><span class="stat-pill"><span>等待解析</span></span></div>',
        )

    # ---------- 事件 ----------
    parse_btn.click(
        fn=process_file,
        inputs=[file_input],
        outputs=[json_output, preview_output, stats_output, status_box, current_json_path, download_html],
    )
    clear_btn.click(
        fn=clear_all,
        outputs=[file_input, json_output, preview_output, stats_output, status_box, current_json_path, download_html],
    )


# ============================================================================
# 启动
# ============================================================================

if __name__ == "__main__":
    import threading
    import time

    PORT = 7861
    URL = f"http://127.0.0.1:{PORT}"

    def _run_server():
        app.launch(
            inbrowser=False,
            server_name="127.0.0.1",
            server_port=PORT,
            show_error=True,
            theme=CUSTOM_THEME,
            css=CUSTOM_CSS,
            prevent_thread_lock=False,
        )

    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(URL, timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    if getattr(sys, 'frozen', False):
        import webview

        class WindowApi:
            def __init__(self):
                self._window = None
                self._maximized = False

            def set_window(self, w):
                self._window = w

            def minimize_window(self):
                if self._window:
                    self._window.minimize()

            def toggle_maximize(self):
                if not self._window:
                    return
                try:
                    if self._maximized:
                        self._window.restore()
                        self._maximized = False
                    else:
                        # 通过 JS 获取屏幕尺寸，快速且跨平台
                        try:
                            js = self._window.evaluate_js(
                                'JSON.stringify([screen.width, screen.height])'
                            )
                            if js:
                                import json as _json2
                                dims = _json2.loads(js)
                                w, h = int(dims[0]), int(dims[1])
                                self._window.resize(w, h)
                            else:
                                self._window.resize(1920, 1080)
                        except Exception:
                            self._window.resize(1920, 1080)
                        self._maximized = True
                except Exception:
                    pass

            def close_app(self):
                if self._window:
                    self._window.destroy()
                os._exit(0)

            def save_file(self, source_path, suggested_name):
                import shutil
                try:
                    result = self._window.create_file_dialog(
                        webview.SAVE_DIALOG,
                        save_filename=suggested_name,
                    )
                    if result:
                        dest = result if isinstance(result, str) else result[0]
                        shutil.copy2(source_path, dest)
                        return True
                except Exception:
                    pass
                return False

        api = WindowApi()
        window = webview.create_window(
            title="",
            url=URL,
            width=1200,
            height=800,
            min_size=(900, 600),
            frameless=True,
            easy_drag=True,
            js_api=api,
        )
        api.set_window(window)
        webview.start()
        os._exit(0)
    else:
        import webbrowser
        webbrowser.open(URL)
        server_thread.join()
