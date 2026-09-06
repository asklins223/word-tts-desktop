"""Command-line entry point for the reusable page workflow."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .constants import ADMIN_URL, API_BASE_URL
from .errors import PlatformInputError
from .flow import build_dry_run_result
from .normalization import normalize_spec
from .runtime import _default_profile_dir, execute_live

def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except OSError as exc:
        raise PlatformInputError(f"无法读取输入文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PlatformInputError(f"输入文件不是合法 JSON: {path}: {exc.msg}") from exc
    if not isinstance(value, Mapping):
        raise PlatformInputError("输入文件根节点必须是 JSON 对象")
    return value

def _error_result(error: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "FAIL",
        "side_effect_policy": "PAGE_UI_ONLY"
        if isinstance(error, PlatformInputError)
        else "UNKNOWN",
        "error": str(error),
    }
    steps = getattr(error, "steps", None)
    feedback = getattr(error, "feedback", None)
    if steps:
        result["steps"] = steps
    if feedback:
        result["feedback"] = feedback
        if feedback.get("paperId") is not None:
            result["paperId"] = feedback["paperId"]
    return result

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通过外部平台页面控件录入题型专项试卷；脚本只监听只读反馈接口。"
    )
    parser.add_argument("spec", type=Path, help="页面语义 JSON 输入文件")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--execute",
        action="store_true",
        help="显式打开可见 Chrome 并执行页面录入",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="只做本地校验并输出页面动作计划（默认行为）",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help="独立 Chrome 登录配置目录；默认使用 .runtime/platform_input_chrome_profile",
    )
    parser.add_argument(
        "--login-timeout",
        type=float,
        default=300,
        help="等待登录和管理页加载的秒数，默认 300",
    )
    parser.add_argument(
        "--stay-on-page",
        action="store_true",
        help="保存后停留在第二步并保持 Chrome 打开；按 Ctrl+C 结束脚本",
    )
    parser.add_argument(
        "--edit-existing",
        action="store_true",
        help="只编辑列表中同名的既有试卷；找不到时失败，不创建新试卷",
    )
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        raw = _load_json(args.spec)
        spec = normalize_spec(raw, base_dir=args.spec.resolve().parent)
        if not args.execute:
            result = build_dry_run_result(
                spec,
                edit_existing=args.edit_existing,
            )
        else:
            result = execute_live(
                spec,
                profile_dir=args.profile_dir or _default_profile_dir(),
                admin_url=ADMIN_URL,
                api_base=API_BASE_URL,
                login_timeout=args.login_timeout,
                return_to_list=not args.stay_on_page,
                edit_existing=args.edit_existing,
                keep_browser_open=args.stay_on_page,
            )
    except (PlatformInputError, OSError) as exc:
        result = _error_result(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
