"""进度记录、断点续传与 ZIP 打包。"""


import json
import os
import re
import zipfile
from datetime import datetime

from wordtts.audio_io import sanitize_dirname
from wordtts.config import (
    AUDIO_ALGORITHM_VERSION,
    FORMAT_MAP,
    OUTPUT_BASE,
    PARSER_VERSION,
)
from wordtts.tts_config import normalize_tts_config


# ============================================================================
# 进度记录与断点续传
# ============================================================================

def get_session_dir(source_filename):
    """根据源文件名获取会话目录路径。"""
    dirname = sanitize_dirname(source_filename)
    return os.path.join(OUTPUT_BASE, dirname)


def load_progress(session_dir):
    """加载进度文件，返回进度字典或 None。"""
    progress_path = os.path.join(session_dir, "progress.json")
    if os.path.exists(progress_path):
        try:
            with open(progress_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # 校验必需的字段是否存在
            required = ("items", "completed", "total_items", "config", "status")
            if all(k in data for k in required):
                return data
            return None
        except Exception:
            return None
    return None


def save_progress(session_dir, progress):
    """保存进度文件（原子写入：先写临时文件再 rename，防止中断时损坏）。"""
    progress_path = os.path.join(session_dir, "progress.json")
    tmp_path = os.path.join(session_dir, "progress.json.tmp")
    progress["updated_at"] = datetime.now().isoformat()
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(progress, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, progress_path)
    except (OSError, ValueError, TypeError):
        # 磁盘满、权限错误或序列化失败时，清理临时文件但不中断处理
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _category_to_prefix(category):
    """将 category 转为文件名前缀，去除"录音稿"后缀。"""
    if not category:
        return "audio"
    # 去掉"录音稿"后缀
    prefix = category.replace("录音稿", "").strip()
    # 处理"模仿朗读-外网" → "模仿朗读_外网"
    prefix = prefix.replace("-", "_")
    # 清理不安全字符
    prefix = re.sub(r'[\\/:*?"<>|]', '_', prefix)
    return prefix if prefix else "audio"


def _sanitize_filename_stem(value):
    """清理解析器指定的文件名主体；空值表示仍使用默认命名规则。"""
    stem = str(value or "").strip()
    stem = re.sub(r'[\\/:*?"<>|]', '_', stem).strip(' .')
    return stem[:120]


def _unique_filename_stem(stem, used_stems):
    """以大小写不敏感方式避免同一批任务中的文件名冲突。"""
    candidate = stem
    suffix = 2
    while candidate.casefold() in used_stems:
        candidate = f"{stem}_{suffix}"
        suffix += 1
    used_stems.add(candidate.casefold())
    return candidate


def build_progress(source_filename, source_path, parse_results, config):
    """
    构建初始进度数据结构。
    每条解析结果（每个音频条目）独立生成一个音频文件。

    文件命名规则：
      - 信息获取题目（听选信息题目/回答问题题目）：问题x.mp3（x 为题号）
      - 其他题型：题型-录音稿x.mp3（x 为同题型内的顺序号）
    """
    config = {
        **normalize_tts_config(config),
        "audio_algorithm_version": AUDIO_ALGORITHM_VERSION,
        "parser_version": PARSER_VERSION,
    }
    ext = FORMAT_MAP["mp3"][1].lstrip('.')
    items = []
    # 每个子题型独立编号
    seq_by_cat = {}
    used_filename_stems = set()

    for result in parse_results:
        doc_type = result["doc_type"]
        raw_items = result["items"]

        for raw_item in raw_items:
            cat = raw_item.get("category", "")
            prefix = _category_to_prefix(cat)
            seq_by_cat[prefix] = seq_by_cat.get(prefix, 0) + 1
            default_seq = seq_by_cat[prefix]
            requested_stem = _sanitize_filename_stem(raw_item.get("filename_stem"))
            if requested_stem:
                filename_stem = _unique_filename_stem(requested_stem, used_filename_stems)
                try:
                    seq = int(raw_item.get("number"))
                except (TypeError, ValueError):
                    seq = default_seq
                item_id = f"{prefix}_{filename_stem}"
            else:
                # 其他题型：题型-录音稿x
                seq = default_seq
                filename_stem = _unique_filename_stem(
                    f"{prefix}-录音稿{seq}", used_filename_stems
                )
                item_id = filename_stem
            text_preview = raw_item.get("text", "")[:80].replace('\n', ' ')
            # 解析器只负责题型音色与文件命名；三项声音参数统一由当前配置提供。
            voice_override = raw_item.get("voice") or None      # "male" / "female" / None
            items.append({
                "id": item_id,
                "doc_type": doc_type,
                "category": cat,
                "seq": seq,
                "filename": f"{filename_stem}.{ext}",
                "status": "pending",
                "output_path": None,
                "error": None,
                # single_segment 批量提交后即使下载/导出失败，也保留每个
                # 逻辑片段对应的 worksId，下一轮可以只重试下载而不重复计费。
                "xunfei_works_ids": {},
                # 页面已确认提交但 worksId 漏捕获时保存的作品名对账键。
                "xunfei_ambiguous_works": {},
                "text_preview": text_preview,
                "merged": False,
                "merged_count": 1,
                "raw_item": raw_item,
                "voice_override": voice_override,
            })

    return {
        "source_file": source_filename,
        "source_path": source_path,
        # 方案 6.4：progress.json 只是规范化模型的兼容投影；这些元数据
        # 标记投影代际与来源，数据库永远是唯一写入源。
        "projection": {
            "schema_version": 1,
            "projection_generation": 1,
            "parser_version": PARSER_VERSION,
            "audio_algorithm_version": AUDIO_ALGORITHM_VERSION,
            "source_model": "atomic-question-model/v1",
        },
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "status": "parsing",
        "config": config,
        "parse_results": parse_results,
        "total_items": len(items),
        "completed": 0,
        "failed": 0,
        "items": items,
    }


def get_completed_file_list(progress):
    """从进度数据中获取已完成的文件列表。"""
    files = []
    for item in progress.get("items", []):
        if item["status"] == "done" and item["output_path"]:
            raw_item = item.get("raw_item", {})
            files.append({
                "id": item["id"],
                "filename": item["filename"],
                "path": item["output_path"],
                "doc_type": item["doc_type"],
                "category": item["category"],
                "text": raw_item.get("text", ""),
                "text_preview": item.get("text_preview", raw_item.get("text", "")[:80]),
                "voice_keys": list(item.get("voice_keys") or []),
            })
    return files


# ============================================================================
# ZIP 打包
# ============================================================================

def create_zip(session_dir, progress):
    """创建包含所有音频和 JSON 的 ZIP 包。"""
    zip_path = os.path.join(session_dir, "output.zip")

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 添加所有已完成的音频文件
        for item in progress["items"]:
            if item["status"] == "done" and item["output_path"] and os.path.exists(item["output_path"]):
                arcname = "audio/" + item["filename"]
                zf.write(item["output_path"], arcname)

        # 添加解析结果 JSON
        parsed_path = os.path.join(session_dir, "parsed.json")
        if os.path.exists(parsed_path):
            zf.write(parsed_path, "parsed.json")

        # 添加进度/清单 JSON
        manifest = {
            "source_file": progress["source_file"],
            "created_at": progress["created_at"],
            "completed": progress["completed"],
            "failed": progress["failed"],
            "total_items": progress["total_items"],
            "config": progress["config"],
            "files": [
                {"filename": item["filename"], "doc_type": item["doc_type"],
                 "category": item["category"], "status": item["status"],
                 "merged": item.get("merged", False),
                 "merged_count": item.get("merged_count", 1)}
                for item in progress["items"]
            ],
        }
        manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
        zf.writestr("manifest.json", manifest_json)

    return zip_path
