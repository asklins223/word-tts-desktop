"""常量配置：音色、版本契约、讯飞三参数与合并切割阈值。"""


import os

from wordtts.bootstrap import BASE_DIR


# ============================================================================
# 常量配置
# ============================================================================

OUTPUT_BASE = os.path.join(BASE_DIR, "tts_output")
os.makedirs(OUTPUT_BASE, exist_ok=True)

# 音色配置 — 讯飞配音发音人 key
# 女声 → 英语-Amanda；词汇题型同样使用该女声（无单独音色）
FEMALE_VOICE = "amanda"
# 男声 → 英语-George
MALE_VOICE = "george"

# 词汇题型不再使用单独音色，统一走默认女声。
WORD_CATEGORIES = frozenset({"单词", "例句"})

# 每条解析结果（每道题）最终独立导出一个音频文件；合并模式只在讯飞端
# 临时合并作品，下载后仍会按安全停顿恢复为题目级文件。

# 导出格式：讯飞音频统一落地为 MP3，保留单一格式避免不同平台产生差异。
FORMAT_MAP = {
    "mp3": ("mp3", ".mp3"),
}

QUALITY_BITRATE = {
    "48 kbps（低）": "48k",
    "128 kbps（标准）": "128k",
    "192 kbps（高）": "192k",
    "320 kbps（极高）": "320k",
}

# 音频生成算法版本。改变讯飞音频拼接、合并切割策略或参数寻址方式时递增，
# 避免复用旧算法产物。4 是原有“单段合成后拼接”算法，保留为历史兼容版本。
LEGACY_AUDIO_ALGORITHM_VERSION = 4
AUDIO_ALGORITHM_VERSION = 8

# Electron 主进程用它确认内置后端和渲染器来自同一次构建。旧版客户端如果
# 把旧后端混进新前端，不能继续以“看似启动成功”的方式走逐条生成流程。
BACKEND_CONTRACT_VERSION = 5

# 解析器版本。解析逻辑变更（如音色分配、文件命名规则、音频边界等）时递增，
# 避免断点续传复用旧解析结果（旧结果可能缺少 voice/filename_stem 等字段）。
PARSER_VERSION = 14

# 讯飞平台三项声音参数：均为整数 0-100，50 为平台默认值。
# 女声 Amanda 默认 50/50/50，男声 George 默认 35/50/50（语速 35）。
TTS_PARAM_MIN = 0
TTS_PARAM_MAX = 100
TTS_PARAM_DEFAULT = 50
TTS_FEMALE_RATE_DEFAULT = 50
TTS_MALE_RATE_DEFAULT = 35
TTS_CONFIG_VERSION = 5
DEFAULT_FEMALE_ROLE_KEY = "__default_female__"
DEFAULT_MALE_ROLE_KEY = "__default_male__"
ROLE_CONFIG_PREFIX = "role:"

# 生成方式。composite_cut 使用讯飞多人配音作品一次提交，再按人工停顿
# 安全切割；single_segment 保留原有逐逻辑片段生成流程。
GENERATION_MODE_COMPOSITE = "composite_cut"
GENERATION_MODE_SINGLE = "single_segment"
GENERATION_MODES = (
    GENERATION_MODE_COMPOSITE,
    GENERATION_MODE_SINGLE,
)
DEFAULT_GENERATION_MODE = GENERATION_MODE_COMPOSITE

# 讯飞编辑器当前显示单次最多约 10000 字。为多人配音标记、编辑器 JSON
# 以及接口字段预留空间，不把上限顶满；拆分只发生在完整题目之间。
COMPOSITE_MAX_TEXT_LENGTH = 9000
# 合并作品同时受讯飞编辑器的行数、页面选区稳定性和切割候选数量影响。
# 超过这个条目数时拆成多个作品；只有超长任务才会因此增加提交次数，
# 普通任务仍然保持“全部文本一次生成后切割”。
COMPOSITE_MAX_ITEMS_PER_WORK = 120
COMPOSITE_BOUNDARY_MS = 2000
COMPOSITE_SILENCE_FRAME_MS = 20
COMPOSITE_SILENCE_CORE_DBFS = -50.0
COMPOSITE_SILENCE_EDGE_DBFS = -36.0
COMPOSITE_MIN_CORE_SILENCE_MS = 300
COMPOSITE_MIN_SAFE_SILENCE_MS = 450
# 讯飞页面插入的 2 秒停顿是切割定位标记；普通语句间隙通常明显短于
# 这个值。候选足够时优先使用长标记，避免第三个边界被自然停顿抢走。
COMPOSITE_MARKER_MIN_CORE_MS = 900
# 强标记应接近页面插入的 2 秒停顿。强标记数量必须与边界数一致；数量不足
# 时，只允许在候选数恰好等于边界数的情况下使用较宽松的长停顿集合；数量
# 多于边界或候选仍有歧义时宁可失败，不把自然停顿静默当成题目边界。
COMPOSITE_MARKER_STRONG_MIN_CORE_MS = 1400
COMPOSITE_MARKER_TARGET_TOLERANCE_MS = 650
COMPOSITE_EDGE_KEEP_MS = 90
COMPOSITE_EDGE_TRIM_MIN_MS = 180
# 合并作品最外层的静音不属于题目之间的人工边界。只在它确实很长时
# 才整理，避免把弱首辅音或自然尾音当成可删除的静音；内部边界仍使用
# 更短的保护间隔，以免每段音频残留约 1 秒的合并停顿。
COMPOSITE_OUTER_EDGE_KEEP_MS = 120
COMPOSITE_OUTER_EDGE_TRIM_MIN_MS = 600
COMPOSITE_MIN_OUTPUT_MS = 40


TYPE_COLORS = {
    "信息获取": "#0e7490",
    "听后选择": "#2563eb",
    "听后应答": "#7c3aed",
    "课文跟读": "#15803d",
    "信息转述及询问": "#b45309",
    "模仿朗读": "#9f1239",
    "词汇": "#1e40af",
}
