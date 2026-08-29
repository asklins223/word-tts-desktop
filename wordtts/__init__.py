"""Word TTS 合成核心包（正式公共 API）。

按职责拆分的模块：
  - bootstrap       模块级引导（stdout 编码、资源目录、ffmpeg、pydub 补丁）
  - config          常量配置（音色、版本契约、讯飞参数、合并切割阈值）
  - tts_config      声音/输出配置规范化与角色参数槽位
  - audio_io        音频导出与文件名工具
  - speakers        w/m 标识与通用角色解析
  - synthesis       单题合成核心（角色段落展开与音频拼接）
  - composite_cut   多人配音合并音频安全切割
  - composite_plan  多人配音作品批次计划
  - batch           批量生成引擎（单段分组 / 合并切割）
  - progress        进度记录、断点续传与 ZIP 打包
  - xunfei_bridge   讯飞配音引擎守卫导入

题型的解析器与静态元数据（颜色、识别标记、音色策略）在
``question_types`` 包中；导入本包会先执行 bootstrap 的模块级引导。
"""

from . import bootstrap  # noqa: F401  # 引导必须最先执行
from . import (  # noqa: F401
    audio_io,
    batch,
    composite_cut,
    composite_plan,
    config,
    progress,
    speakers,
    synthesis,
    tts_config,
    xunfei_bridge,
)

from .audio_io import _looks_like_mp3_file, sanitize_dirname  # noqa: F401
from .batch import (  # noqa: F401
    _synth_items_batch,
    _synth_items_batch_composite,
    generate_item_audio,
)
from .composite_cut import (  # noqa: F401
    CompositeCutError,
    _edge_silence_length,
    _select_composite_silence_runs,
    cut_composite_audio,
    format_composite_cut_diagnostics,
)
from .composite_plan import (  # noqa: F401
    CompositePlanError,
    build_composite_work_plan,
)
from .config import (  # noqa: F401
    AUDIO_ALGORITHM_VERSION,
    BACKEND_CONTRACT_VERSION,
    COMPOSITE_MAX_ITEMS_PER_WORK,
    DEFAULT_GENERATION_MODE,
    FEMALE_VOICE,
    FORMAT_MAP,
    GENERATION_MODE_COMPOSITE,
    GENERATION_MODE_SINGLE,
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
from .progress import (  # noqa: F401
    build_progress,
    create_zip,
    get_completed_file_list,
    get_session_dir,
    load_progress,
    save_progress,
)
from .speakers import (  # noqa: F401
    default_voice_for_item,
    parse_speakers,
    parse_speakers_with_roles,
)
from .synthesis import (  # noqa: F401
    _concat_audio_segments,
    _synth_item,
    build_synthesis_segments,
)
from .tts_config import (  # noqa: F401
    DEFAULT_FEMALE_ROLE_KEY,
    DEFAULT_MALE_ROLE_KEY,
    ROLE_CONFIG_PREFIX,
    clamp_tts_param,
    normalize_role_config_key,
    normalize_role_key,
    normalize_tts_config,
    role_config_key,
)
from .xunfei_bridge import _xunfei, _XUNFEI_AVAILABLE  # noqa: F401

__all__ = [
    "AUDIO_ALGORITHM_VERSION",
    "BACKEND_CONTRACT_VERSION",
    "COMPOSITE_MAX_ITEMS_PER_WORK",
    "CompositeCutError",
    "CompositePlanError",
    "DEFAULT_FEMALE_ROLE_KEY",
    "DEFAULT_GENERATION_MODE",
    "DEFAULT_MALE_ROLE_KEY",
    "FEMALE_VOICE",
    "FORMAT_MAP",
    "GENERATION_MODE_COMPOSITE",
    "GENERATION_MODE_SINGLE",
    "LEGACY_AUDIO_ALGORITHM_VERSION",
    "MALE_VOICE",
    "OUTPUT_BASE",
    "PARSER_VERSION",
    "QUALITY_BITRATE",
    "ROLE_CONFIG_PREFIX",
    "TTS_CONFIG_VERSION",
    "TTS_FEMALE_RATE_DEFAULT",
    "TTS_MALE_RATE_DEFAULT",
    "TTS_PARAM_DEFAULT",
    "TTS_PARAM_MAX",
    "TTS_PARAM_MIN",
    "TYPE_COLORS",
    "WORD_CATEGORIES",
    "_XUNFEI_AVAILABLE",
    "_edge_silence_length",
    "_looks_like_mp3_file",
    "_concat_audio_segments",
    "_select_composite_silence_runs",
    "_synth_item",
    "_synth_items_batch",
    "_synth_items_batch_composite",
    "_xunfei",
    "build_composite_work_plan",
    "build_progress",
    "build_synthesis_segments",
    "clamp_tts_param",
    "create_zip",
    "cut_composite_audio",
    "default_voice_for_item",
    "format_composite_cut_diagnostics",
    "generate_item_audio",
    "get_completed_file_list",
    "get_session_dir",
    "load_progress",
    "normalize_role_config_key",
    "normalize_role_key",
    "normalize_tts_config",
    "parse_speakers",
    "parse_speakers_with_roles",
    "role_config_key",
    "sanitize_dirname",
    "save_progress",
]
