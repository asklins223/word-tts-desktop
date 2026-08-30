"""音频导出与文件名/时间工具。"""


import os
import re
import sys
from datetime import datetime

from wordtts.config import FORMAT_MAP, QUALITY_BITRATE


def export_audio(seg, fmt, quality, out_path):
    """按指定格式与码率导出音频。"""
    if len(seg) < 50:
        raise RuntimeError(f"音频时长过短 ({len(seg)}ms)，无法导出")
    fmt_id, _ext = FORMAT_MAP["mp3"]
    kwargs = {"format": fmt_id}
    br = QUALITY_BITRATE.get(quality, QUALITY_BITRATE["128 kbps（标准）"])
    kwargs["bitrate"] = br
    print(f"[export] 导出: {out_path} fmt={fmt_id} dur={len(seg)}ms bitrate={br}", file=sys.stdout)
    export_result = seg.export(out_path, **kwargs)
    if hasattr(export_result, "close"):
        export_result.close()
    out_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    print(f"[export] 完成: {out_path} ({out_size} bytes)", file=sys.stdout)
    if out_size == 0:
        raise RuntimeError(f"导出文件为空: {out_path}")

    # 讯飞批量下载阶段已经用 FFmpeg 解码过原始 MP3。这里不再复制文件并
    # 启动第二次 FFmpeg 回读，只做轻量容器头检查，避免每条音频额外产生
    # 一次磁盘 I/O 和解码进程；真正的完整回读验证保留给显式调试模式。
    if fmt_id == "mp3" and not _looks_like_mp3_file(out_path):
        raise RuntimeError(f"导出的文件不是有效 MP3: {out_path}")
    return out_path


def _looks_like_mp3_file(path):
    """用 MP3 帧头/ID3 头做快速校验，不启动 ffmpeg。"""
    try:
        with open(path, "rb") as source:
            data = source.read(4096)
    except OSError:
        return False
    if data.startswith(b"ID3"):
        return True
    # 无 ID3 标签的 MP3 可能从任意偏移直接开始 MPEG 帧。
    return any(
        data[index] == 0xFF and (data[index + 1] & 0xE0) == 0xE0
        for index in range(max(0, len(data) - 1))
    )


def sanitize_dirname(name):
    """将文件名转换为安全的目录名。"""
    name = os.path.splitext(name)[0]
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = re.sub(r'\s+', '_', name)
    return name[:80]


def now_str():
    return datetime.now().strftime("%H:%M:%S")
