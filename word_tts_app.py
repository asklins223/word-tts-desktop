#!/usr/bin/env python3
"""
Word 文档解析 + 讯飞配音音频生成 — 兼容门面
================================================
本模块原为一体化实现，现已按职责拆分到 ``wordtts/`` 包：

  - wordtts.bootstrap       模块级引导（stdout 编码、资源目录、ffmpeg、pydub 补丁）
  - wordtts.config          常量配置（音色、版本契约、讯飞参数、合并切割阈值）
  - wordtts.tts_config      声音/输出配置规范化与角色参数槽位
  - wordtts.audio_io        音频导出与文件名工具
  - wordtts.speakers        w/m 标识与通用角色解析
  - wordtts.synthesis       单题合成核心（角色段落展开与音频拼接）
  - wordtts.composite_cut   多人配音合并音频安全切割
  - wordtts.composite_plan  多人配音作品批次计划
  - wordtts.batch           批量生成引擎（单段分组 / 合并切割）
  - wordtts.progress        进度记录、断点续传与 ZIP 打包
  - wordtts.xunfei_bridge   讯飞配音引擎守卫导入

这里保留全部历史名称的 re-export，供 server.py、workflow 层和既有
测试继续按 ``word_tts_app`` 的接口使用。

引擎与音色规则（统一使用讯飞配音 peiyin.xunfei.cn）：
  - w/W 标识 → 女声 英语-Amanda
  - m/M 标识 → 男声 英语-George
  - 无标识   → 默认女声 英语-Amanda
  - 词汇题型（单词/例句）统一使用默认女声 英语-Amanda（无单独音色）
  - 生成音频时自动去除 w/m 标识
  - 可调参数为讯飞平台三参数：语速 / 语调 / 音量（0-100，50=默认）
"""

# 先导入 wordtts 包：其 __init__ 会最先执行 bootstrap 的模块级引导
#（编码配置、资源目录注入、ffmpeg 定位、pydub 兼容补丁）。
import wordtts  # noqa: F401

from wordtts.audio_io import (
    _looks_like_mp3_file,
    export_audio,
    now_str,
    sanitize_dirname,
)
from wordtts.batch import (
    _synth_items_batch,
    _synth_items_batch_composite,
    generate_item_audio,
)
from wordtts.composite_cut import (
    CompositeCutError,
    _audio_dbfs,
    _edge_silence_length,
    _find_composite_silence_runs,
    _select_composite_silence_runs,
    _trim_composite_edge_silence,
    cut_composite_audio,
    format_composite_cut_diagnostics,
)
from wordtts.composite_plan import (
    CompositePlanError,
    _composite_item_from_spec,
    _stable_composite_work_id,
    build_composite_work_plan,
)
from wordtts.config import (
    AUDIO_ALGORITHM_VERSION,
    BACKEND_CONTRACT_VERSION,
    COMPOSITE_BOUNDARY_MS,
    COMPOSITE_MAX_ITEMS_PER_WORK,
    COMPOSITE_MAX_TEXT_LENGTH,
    DEFAULT_GENERATION_MODE,
    FEMALE_VOICE,
    FORMAT_MAP,
    GENERATION_MODE_COMPOSITE,
    GENERATION_MODE_SINGLE,
    GENERATION_MODES,
    LEGACY_AUDIO_ALGORITHM_VERSION,
    MALE_VOICE,
    OUTPUT_BASE,
    PARSER_VERSION,
    QUALITY_BITRATE,
    TTS_CONFIG_VERSION,
    TTS_FEMALE_RATE_DEFAULT,
    TTS_MALE_RATE_DEFAULT,
    TTS_PARAM_DEFAULT,
    TTS_PARAM_MAX,
    TTS_PARAM_MIN,
    TYPE_COLORS,
    WORD_CATEGORIES,
)
from wordtts.progress import (
    build_progress,
    create_zip,
    get_completed_file_list,
    get_session_dir,
    load_progress,
    save_progress,
)
from wordtts.speakers import (
    default_voice_for_item,
    parse_speakers,
    parse_speakers_with_roles,
)
from wordtts.synthesis import (
    _concat_audio_segments,
    _synth_item,
    _synth_segment,
    build_synthesis_segments,
)
from wordtts.tts_config import (
    DEFAULT_FEMALE_ROLE_KEY,
    DEFAULT_MALE_ROLE_KEY,
    ROLE_CONFIG_PREFIX,
    _normalize_voice_key,
    _normalize_voice_params,
    clamp_tts_param,
    normalize_role_config_key,
    normalize_role_key,
    normalize_tts_config,
    role_config_key,
)
from wordtts.xunfei_bridge import _xunfei, _XUNFEI_AVAILABLE

# 解析器注册表由 word_parser 门面继续提供（server.py 直接读取）。
from word_parser import PARSER_MAP, parse_document_auto  # noqa: F401
