"""真实 TTS 引擎的 AUDIO_GENERATE 适配器（阶段 4 收口）。

把现有讯飞合成核心（``wordtts.batch.generate_item_audio``）挂到统一
OperationAdapter 契约下，替换干跑用的 FakeAudioAdapter：

- 音色策略按方案 2A 优先级解析：小题覆盖 > 文档/题型策略 > 系统默认；
  小题型注册表的 ``voice_policy`` 决定是否强制女声（词汇等）；
- ``execute`` 调真实引擎并落音频文件，回执含文件路径与内容哈希；
  引擎函数可注入（测试 seam），生产路径不注入；
- ``verify`` 校验音频文件真实存在且非空，不能凭引擎返回值假定成功。

本适配器不推进 workflow 状态（由 runner 负责），也不写 progress.json。
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Callable

from .model import SUB_TYPE_REGISTRY
from .runner import (
    OperationResult,
    PreparedOperation,
    ResultStatus,
    TargetSnapshot,
)

EngineFn = Callable[..., Any]


def resolve_voice_policy(sub_type_code: str | None, config: dict) -> dict:
    """音色策略解析：注册表策略 + 配置（小题覆盖留待 question 级策略）。"""
    policy = SUB_TYPE_REGISTRY[sub_type_code].voice_policy if (
        sub_type_code in SUB_TYPE_REGISTRY) else "default"
    resolved = {
        "rate": config.get("rate", 50),
        "volume": config.get("volume", 50),
        "pitch": config.get("pitch", 50),
        "default_voice": config.get("default_voice"),
        "female_voice": config.get("female_voice"),
        "male_voice": config.get("male_voice"),
    }
    if policy == "forced_female":
        # 词汇等：默认音色强制走女声，覆盖不配置的 default_voice
        resolved["default_voice"] = resolved["female_voice"] or "female"
    return resolved


class TtsEngineAudioAdapter:
    """AUDIO_GENERATE 适配器：接 wordtts 合成核心。"""

    operation_type = "AUDIO_GENERATE"

    def __init__(self, *, output_dir: str, engine: EngineFn | None = None):
        self.output_dir = output_dir
        self._engine = engine

    def _engine_fn(self) -> EngineFn:
        if self._engine is not None:
            return self._engine
        from wordtts.batch import generate_item_audio

        return generate_item_audio

    def capabilities(self) -> dict[str, Any]:
        return {
            "scope_kinds": ("QUESTION", "STIMULUS", "CONTENT_UNIT",
                            "GROUP", "MAJOR_SECTION", "DOCUMENT"),
            "partial_success": True,
            "needs_audio_artifact": False,
        }

    def validate(self, snapshot: TargetSnapshot, config: dict) -> None:
        if snapshot.primary is None:
            raise ValueError("音频任务缺少主目标")

    def prepare(self, snapshot: TargetSnapshot,
                config: dict) -> PreparedOperation:
        sub_type = config.get("sub_type_code")
        voice = resolve_voice_policy(sub_type, config)
        spec = {
            "item_id": snapshot.operation_id,
            "text": config["text"],
            **voice,
        }
        return PreparedOperation(
            payload=spec,
            delivery_units=({"unit_id": f"delivery:{snapshot.operation_id}:1",
                             "item_id": spec["item_id"]},),
        )

    def execute(self, prepared: PreparedOperation,
                fencing_token: int) -> dict[str, Any]:
        spec = prepared.payload
        os.makedirs(self.output_dir, exist_ok=True)
        # item_id 含冒号（operation:...），Windows 文件名非法：消毒
        safe_name = re.sub(r'[\\/:*?"<>|]', "-", spec["item_id"])
        out_path = os.path.join(
            self.output_dir, f"{safe_name}_{fencing_token}.mp3")
        audio = self._engine_fn()(
            spec["text"], spec["rate"], spec["volume"], spec["pitch"],
            default_voice=spec.get("default_voice"),
            female_voice=spec.get("female_voice"),
            male_voice=spec.get("male_voice"),
        )
        data = self._as_bytes(audio, out_path)
        with open(out_path, "wb") as fh:
            fh.write(data)
        return {
            "artifact_path": out_path,
            "artifact_sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "item_id": spec["item_id"],
        }

    def verify(self, receipt: dict[str, Any]) -> OperationResult:
        path = receipt.get("artifact_path")
        if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
            # 引擎返回但产物缺失：结果未知，进入人工确认而不是假装成功
            return OperationResult(status=ResultStatus.AMBIGUOUS,
                                   receipt=receipt,
                                   error_code="ARTIFACT_MISSING")
        return OperationResult(status=ResultStatus.SUCCEEDED, receipt=receipt)

    @staticmethod
    def _as_bytes(audio: Any, out_path: str) -> bytes:
        if isinstance(audio, (bytes, bytearray)):
            return bytes(audio)
        if isinstance(audio, str) and os.path.exists(audio):
            with open(audio, "rb") as fh:
                return fh.read()
        if isinstance(audio, dict):
            for key in ("path", "file", "audio_path"):
                value = audio.get(key)
                if isinstance(value, str) and os.path.exists(value):
                    with open(value, "rb") as fh:
                        return fh.read()
            if isinstance(audio.get("audio"), (bytes, bytearray)):
                return bytes(audio["audio"])
        raise RuntimeError(f"无法定位引擎音频产物: {out_path}")
