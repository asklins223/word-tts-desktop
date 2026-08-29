"""Word TTS 合成核心包。

原 word_tts_app.py 的实现按职责拆分到本包；word_tts_app 保留为兼容
门面，继续对外暴露原有名称。导入本包时会先执行 bootstrap 的模块级
引导（编码、路径、ffmpeg、pydub 补丁）。
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
