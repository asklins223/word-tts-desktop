"""Generation orchestration for the Xunfei session.

This mixin owns single/composite submission retries, batch grouping, readiness
polling, and result decoding handoff.  It deliberately delegates page actions
and downloads to their own mixins.
"""

from __future__ import annotations

import os
import time
import uuid

from .config import OUTPUT_DIR, PARAM_DEFAULT, clamp_param
from .errors import (
    XunfeiCancelled,
    XunfeiError,
    XunfeiLoginRequired,
    XunfeiQuotaExceeded,
    XunfeiRateLimited,
    XunfeiSubmissionAmbiguous,
    _check_cancel_requested,
    _log,
    _wait_with_cancel,
)
from .helpers import notify_batch_progress as _notify_batch_progress
from .voice_catalog import DEFAULT_FEMALE, get_voice_info


class GenerationMixin:

    def _generate_pending_composite(
        self,
        work,
        *,
        output_name=None,
        max_retries=4,
        cancel_check=None,
    ):
        """通过讯飞多人配音页面提交作品，返回待下载作品信息。

        多人配音的编辑内容、音色标记、参数和停顿全部由可见页面操作完成；
        这里不调用 makeMultipleSpeakerWork 或 order_gen 提交接口。生成按钮
        点击后的 worksId 仍由已有响应监听器捕获，下载阶段继续复用精确
        worksId/签名 URL 流程。
        """
        if not self._logged_in:
            raise XunfeiError("尚未登录，请先调用 login()")
        if not output_name:
            output_name = f".xunfei_composite_{uuid.uuid4().hex}.mp3"
        output_path = os.path.join(OUTPUT_DIR, output_name)
        page = self._page
        works_name = self._normalize_works_name(
            work.get("works_name")
            or f"wordtts_composite_{uuid.uuid4().hex[:8]}"
        )
        last_error = None
        for attempt in range(1, max_retries + 1):
            self._confirm_click_succeeded = False
            self._submission_state_uncertain = False
            _check_cancel_requested(cancel_check)
            submission_confirmed = False
            _log(
                f"[xunfei]   多人配音作品提交 {attempt}/{max_retries}: "
                f"{works_name}（{len(work.get('item_ids') or [])} 道题）"
            )
            try:
                if not self._dismiss_local_draft_prompt(page, cancel_check=cancel_check):
                    raise XunfeiError("讯飞页面被本地缓存恢复弹窗遮挡")
                if page.locator(".ssml-editor").count() == 0:
                    if not self._recover_for_retry(page, cancel_check):
                        raise XunfeiError("页面恢复失败")

                # 文本、连续同配置批量选区、音色/参数和内部停顿均通过
                # 可见讯飞页面完成，并且每次套用前都有选区/标记回读校验。
                if callable(cancel_check):
                    self._prepare_composite_editor(
                        page, work, cancel_check=cancel_check
                    )
                else:
                    self._prepare_composite_editor(page, work)
                _check_cancel_requested(cancel_check)
                self._mark_works_cutoff()
                if callable(cancel_check):
                    self._click_generate(page, cancel_check=cancel_check)
                else:
                    self._click_generate(page)
                try:
                    confirm_kwargs = {"works_name": works_name}
                    if cancel_check is not None:
                        confirm_kwargs["cancel_check"] = cancel_check
                    status = self._confirm_synth(page, **confirm_kwargs)
                except XunfeiSubmissionAmbiguous:
                    raise
                except XunfeiCancelled:
                    raise
                except Exception as confirm_error:
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击，但后续页面状态异常；"
                            "提交结果未拿到本地任务 ID，可直接重新生成",
                            works_name=works_name,
                        ) from confirm_error
                    raise
                if status == "insufficient":
                    raise XunfeiQuotaExceeded("讯飞配音额度不足")
                if status == "login":
                    raise XunfeiLoginRequired("合成过程中弹出登录框，请重新登录")
                if status == "rate_limited":
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击后触发频控，提交结果不确定；"
                            "提交结果未拿到本地任务 ID，可直接重新生成",
                            works_name=works_name,
                        )
                    raise XunfeiRateLimited("触发讯飞频控")
                if status != "ok":
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击，但确认流程未完成；"
                            "提交结果未拿到本地任务 ID，可直接重新生成",
                            works_name=works_name,
                        )
                    raise XunfeiError("确认合成弹窗流程未完成")
                submission_confirmed = True

                try:
                    works_id = self._consume_works_id(
                        timeout=30,
                        cancel_check=cancel_check,
                    )
                except (XunfeiCancelled, XunfeiSubmissionAmbiguous):
                    raise
                except Exception as tracking_error:
                    raise XunfeiSubmissionAmbiguous(
                        "合成已确认提交，但本地未拿到任务 ID；"
                        "不查询讯飞作品列表，可直接重新生成",
                        works_name=works_name,
                    ) from tracking_error
                if not works_id:
                    raise XunfeiSubmissionAmbiguous(
                        "多人配音已确认提交，但本地未拿到任务 ID；"
                        "不查询讯飞作品列表，可直接重新生成",
                        works_name=works_name,
                    )
                pending = {
                    "works_id": str(works_id),
                    "output_path": output_path,
                    "works_name": works_name,
                    "work_id": str(work.get("work_id") or work.get("job_id") or ""),
                    "item_count": int(work.get("item_count") or 0),
                }
                _log(
                    f"[xunfei] ✅ 多人配音作品已提交 worksId={pending['works_id']} "
                    f"work={pending['work_id']}"
                )
                # 此处作品已经提交并拿到 worksId。清理页面属于提交后的
                # best-effort 操作，异常不能回到外层“提交失败”重试，否则
                # 同一作品可能再次扣费。
                try:
                    if callable(cancel_check):
                        self._cleanup_after_item(page, cancel_check=cancel_check)
                    else:
                        self._cleanup_after_item(page)
                except XunfeiCancelled:
                    # 清理发生在 worksId 已确认之后，但取消仍必须透传到
                    # 批处理边界，不能被“清理失败不影响提交”的保护逻辑吞掉。
                    raise
                except Exception as cleanup_error:
                    _log(f"[xunfei]   提交后页面清理异常（不重复提交）: {cleanup_error}")
                return pending
            except (
                XunfeiQuotaExceeded,
                XunfeiLoginRequired,
                XunfeiSubmissionAmbiguous,
                XunfeiCancelled,
            ):
                raise
            except XunfeiRateLimited as error:
                last_error = error
                cooldown = 18 + (time.time() % 10) * 2
                _log(f"[xunfei]   多人配音频控冷却 {cooldown:.0f}s 后重试提交")
                _wait_with_cancel(page, cooldown, cancel_check=cancel_check)
                if attempt < max_retries and not self._recover_for_retry(page, cancel_check):
                    break
            except Exception as error:
                if submission_confirmed:
                    raise XunfeiSubmissionAmbiguous(
                        "多人配音已确认提交，但提交结果整理时发生异常；"
                        "提交结果未拿到本地任务 ID，可直接重新生成",
                        works_name=works_name,
                    ) from error
                last_error = error
                _log(f"[xunfei]   多人配音提交异常: {error}")
                if attempt < max_retries and not self._recover_for_retry(page, cancel_check):
                    break
        raise XunfeiError(f"讯飞多人配音生成失败：{last_error or '已重试仍未成功'}")

    def _generate_pending_one(
        self,
        text,
        output_name=None,
        works_name=None,
        max_retries=4,
        voice_key=None,
        speed=PARAM_DEFAULT,
        pitch=PARAM_DEFAULT,
        volume=PARAM_DEFAULT,
        cancel_check=None,
    ):
        """只提交一条合成并返回 worksId，不在本处下载。

        这是批量流程的第一阶段：页面始终停留在编辑页，成功后只关闭弹窗、
        清空编辑器，保留当前音色和参数缓存给同组下一条任务复用。
        """
        if not self._logged_in:
            raise XunfeiError("尚未登录，请先调用 login()")

        vk = voice_key if voice_key else self.voice_key
        voice_name = get_voice_info(vk)["name"]
        if not output_name:
            output_name = f".xunfei_{uuid.uuid4().hex}.mp3"
        output_path = os.path.join(OUTPUT_DIR, output_name)
        # 作品名必须在点击生成前确定并写入页面。它是 worksId 监听漏捕获
        # 时只用于页面展示和本地日志，不能依赖它找回远端任务。
        works_name = self._normalize_works_name(
            works_name or f"wordtts_{uuid.uuid4().hex[:16]}"
        )

        page = self._page
        last_error = None
        for attempt in range(1, max_retries + 1):
            self._confirm_click_succeeded = False
            self._submission_state_uncertain = False
            _check_cancel_requested(cancel_check)
            submission_confirmed = False
            _log(f"[xunfei]   第 {attempt}/{max_retries} 次尝试提交...")
            try:
                if not self._dismiss_local_draft_prompt(page, cancel_check=cancel_check):
                    raise XunfeiError("讯飞页面被本地缓存恢复弹窗遮挡")
                if page.locator(".ssml-editor").count() == 0:
                    if not self._recover_for_retry(page, cancel_check):
                        raise XunfeiError("页面恢复失败")

                # 同组任务命中这两个缓存时，不会重复切换音色或设置参数。
                if callable(cancel_check):
                    self._select_voice(
                        page, voice_name, voice_key=vk, cancel_check=cancel_check
                    )
                    self._apply_params(
                        page, speed, pitch, volume, cancel_check=cancel_check
                    )
                else:
                    self._select_voice(page, voice_name, voice_key=vk)
                    self._apply_params(page, speed, pitch, volume)

                input_ok = (
                    self._input_text(page, text, cancel_check=cancel_check)
                    if callable(cancel_check)
                    else self._input_text(page, text)
                )
                if not input_ok:
                    raise XunfeiError("文本输入失败")

                _check_cancel_requested(cancel_check)
                self._mark_works_cutoff()
                if callable(cancel_check):
                    self._click_generate(page, cancel_check=cancel_check)
                else:
                    self._click_generate(page)
                try:
                    confirm_kwargs = {"works_name": works_name}
                    if cancel_check is not None:
                        confirm_kwargs["cancel_check"] = cancel_check
                    status = self._confirm_synth(page, **confirm_kwargs)
                except XunfeiSubmissionAmbiguous:
                    raise
                except XunfeiCancelled:
                    raise
                except Exception as confirm_error:
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击，但后续页面状态异常；"
                            "提交结果未拿到本地任务 ID，可直接重新生成",
                            works_name=works_name,
                        ) from confirm_error
                    raise
                if status == "insufficient":
                    raise XunfeiQuotaExceeded("讯飞配音额度不足")
                if status == "login":
                    raise XunfeiLoginRequired("合成过程中弹出登录框，请重新登录")
                if status == "rate_limited":
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击后触发频控，提交结果不确定；"
                            "提交结果未拿到本地任务 ID，可直接重新生成",
                            works_name=works_name,
                        )
                    raise XunfeiRateLimited("触发讯飞频控")
                if status != "ok":
                    if self._confirm_click_succeeded or self._submission_state_uncertain:
                        raise XunfeiSubmissionAmbiguous(
                            "确认合成按钮已点击，但确认流程未完成；"
                            "提交结果未拿到本地任务 ID，可直接重新生成",
                            works_name=works_name,
                        )
                    raise XunfeiError("确认合成弹窗流程未完成")
                submission_confirmed = True

                try:
                    works_id = self._consume_works_id(
                        timeout=30,
                        cancel_check=cancel_check,
                    )
                except (XunfeiCancelled, XunfeiSubmissionAmbiguous):
                    raise
                except Exception as tracking_error:
                    raise XunfeiSubmissionAmbiguous(
                        "合成已确认提交，但本地未拿到任务 ID；"
                        "不查询讯飞作品列表，可直接重新生成",
                        works_name=works_name,
                    ) from tracking_error
                if not works_id:
                    raise XunfeiSubmissionAmbiguous(
                        "合成已确认提交，但本地未拿到任务 ID；"
                        "不查询讯飞作品列表，可直接重新生成",
                        works_name=works_name,
                    )

                pending = {
                    "works_id": str(works_id),
                    "output_path": output_path,
                    "voice_key": vk,
                    "voice_name": voice_name,
                    "works_name": works_name,
                    "speed": clamp_param(speed),
                    "pitch": clamp_param(pitch),
                    "volume": clamp_param(volume),
                }
                _log(
                    f"[xunfei] ✅ 已提交待下载任务 worksId={pending['works_id']} "
                    f"voice={voice_name}"
                )
                # worksId 已经确认，清理失败不能被当作提交失败处理；否则
                # 外层恢复逻辑会重新点击合成并可能产生重复计费。
                try:
                    if callable(cancel_check):
                        self._cleanup_after_item(page, cancel_check=cancel_check)
                    else:
                        self._cleanup_after_item(page)
                except XunfeiCancelled:
                    # 同上：已拿到 worksId 不代表可以忽略用户的取消。
                    raise
                except Exception as cleanup_error:
                    _log(f"[xunfei]   提交后页面清理异常（不重复提交）: {cleanup_error}")
                return pending

            except (
                XunfeiQuotaExceeded,
                XunfeiLoginRequired,
                XunfeiSubmissionAmbiguous,
                XunfeiCancelled,
            ):
                raise
            except XunfeiRateLimited as error:
                last_error = error
                cooldown = 18 + (time.time() % 10) * 2
                _log(f"[xunfei]   频控冷却 {cooldown:.0f}s 后重试提交")
                _wait_with_cancel(page, cooldown, cancel_check=cancel_check)
                self._recover_for_retry(page, cancel_check)
            except Exception as attempt_error:
                if submission_confirmed:
                    raise XunfeiSubmissionAmbiguous(
                        "合成已确认提交，但提交结果整理时发生异常；"
                        "提交结果未拿到本地任务 ID，可直接重新生成",
                        works_name=works_name,
                    ) from attempt_error
                last_error = attempt_error
                _log(f"[xunfei]   第 {attempt} 次提交异常: {attempt_error}")
                if not self._recover_for_retry(page, cancel_check):
                    break

        raise XunfeiError(f"讯飞配音生成失败：{last_error or '已重试仍未成功'}")

    def _wait_for_pending_ready(
        self,
        page,
        pending_items,
        timeout=180,
        cancel_check=None,
    ):
        """批量等待精确 worksId 对应的音频地址就绪。"""
        _check_cancel_requested(cancel_check)
        duplicate_ids = self._duplicate_pending_work_ids(pending_items)
        if duplicate_ids:
            raise XunfeiError(
                "批量任务捕获到重复 worksId，拒绝继续下载以免音频错配："
                + ", ".join(sorted(duplicate_ids))
            )
        remaining = {
            str(item["works_id"]): item
            for item in pending_items
            if item.get("works_id")
        }
        ready = {}
        if not remaining:
            return ready

        deadline = time.time() + timeout
        matched = set()
        target_count = max(len(remaining), 1)
        while remaining and time.time() < deadline:
            _check_cancel_requested(cancel_check)
            fetch_kwargs = {
                "needed_count": target_count,
                "expected_ids": set(remaining),
            }
            if cancel_check is not None:
                fetch_kwargs["cancel_check"] = cancel_check
            records = self._fetch_works_list_pages(page, **fetch_kwargs)
            for record in records:
                if not isinstance(record, dict):
                    continue
                record_id = record.get("id") or record.get("worksId")
                expected = str(record_id) if record_id is not None else ""
                item = remaining.get(expected)
                if item is None:
                    continue
                if expected not in matched:
                    _log(f"[xunfei]   ✅ 作品列表已匹配 worksId: {expected}")
                    matched.add(expected)

                audio_url = record.get("audioUrl")
                if not audio_url:
                    # 列表记录可能先出现、音频地址后补齐；只对同一个
                    # worksId 请求签名 URL，绝不复用其它记录的最新地址。
                    audio_url = self._fetch_sign_url_in_page(
                        page, expected, log_result=False
                    )
                if audio_url:
                    ready[expected] = {
                        **item,
                        "record": dict(record),
                        "download_url": audio_url,
                    }
                    remaining.pop(expected, None)
                    _log(f"[xunfei]   ✅ 匹配作品音频已就绪 worksId: {expected}")

            if remaining:
                if not matched:
                    _log(
                        f"[xunfei]   ⏳ 等待 {len(remaining)} 条作品匹配 worksId"
                    )
                else:
                    _log(
                        f"[xunfei]   ⏳ 仍有 {len(remaining)} 条作品等待音频就绪"
                    )
                _check_cancel_requested(cancel_check)
                _wait_with_cancel(page, 2.0, cancel_check=cancel_check)

        if remaining:
            _log(
                "[xunfei]   ⚠️ 批量等待音频超时，未就绪 worksId: "
                + ", ".join(sorted(remaining))
            )
        return ready

    @staticmethod
    def _group_batch_jobs(jobs):
        """按音色 + 三项参数分组，保留每组首次出现的顺序。"""
        groups = {}
        for job in jobs:
            voice_key = str(job.get("voice_key") or DEFAULT_FEMALE)
            key = (
                voice_key,
                clamp_param(job.get("speed")),
                clamp_param(job.get("pitch")),
                clamp_param(job.get("volume")),
            )
            groups.setdefault(key, []).append(job)
        return list(groups.values())

    def synth_batch(
        self,
        jobs,
        max_retries=4,
        progress_callback=None,
        cancel_check=None,
    ):
        """按音色/参数分组，先全部提交合成，最后统一下载。

        返回 ``job_id -> result``。单条失败会记录在对应结果中，已成功提交
        的其它任务仍会进入统一下载阶段。``progress_callback`` 会在线程内
        收到每条任务的下载事件和最终保存结果；调用方不得在回调中操作
        Playwright 页面。
        """
        if not self._logged_in:
            raise XunfeiError("尚未登录，请先调用 login()")
        normalized_jobs = [dict(job) for job in jobs if isinstance(job, dict)]
        grouped_jobs = self._group_batch_jobs(normalized_jobs)
        pending = []
        results = {}
        reported_progress = set()

        def report_progress(payload):
            if not callable(progress_callback):
                return
            item = dict(payload or {})
            job_id = str(item.get("job_id") or "")
            stage = str(item.get("stage") or "saved")
            report_key = (job_id, stage)
            if job_id and report_key in reported_progress:
                return
            if job_id:
                reported_progress.add(report_key)
            _notify_batch_progress(progress_callback, item)

        for group_index, group in enumerate(grouped_jobs, start=1):
            _check_cancel_requested(cancel_check)
            if not group:
                continue
            sample = group[0]
            _log(
                f"[xunfei] 批量生成分组 {group_index}/{len(grouped_jobs)}: "
                f"{get_voice_info(sample.get('voice_key') or DEFAULT_FEMALE)['name']} "
                f"speed={clamp_param(sample.get('speed'))}, "
                f"pitch={clamp_param(sample.get('pitch'))}, "
                f"volume={clamp_param(sample.get('volume'))}，共 {len(group)} 条"
            )
            for job in group:
                _check_cancel_requested(cancel_check)
                job_id = str(job.get("job_id") or uuid.uuid4().hex)
                try:
                    resume_works_id = str(job.get("resume_works_id") or "").strip()
                    if resume_works_id:
                        output_name = job.get("output_name") or (
                            f".xunfei_{uuid.uuid4().hex}.mp3"
                        )
                        pending_item = {
                            "works_id": resume_works_id,
                            "output_path": os.path.join(OUTPUT_DIR, output_name),
                            "voice_key": str(job.get("voice_key") or DEFAULT_FEMALE),
                            "voice_name": str(job.get("voice_name") or ""),
                            "works_name": self._normalize_works_name(
                                job.get("works_name")
                            ),
                            "speed": clamp_param(job.get("speed")),
                            "pitch": clamp_param(job.get("pitch")),
                            "volume": clamp_param(job.get("volume")),
                        }
                        resumed = True
                        _log(
                            f"[xunfei] ♻️ 复用已提交任务 worksId={resume_works_id} "
                            f"job={job_id}"
                        )
                    else:
                        generate_kwargs = {
                            "output_name": job.get("output_name"),
                            "works_name": job.get("works_name"),
                            "max_retries": max_retries,
                            "voice_key": job.get("voice_key"),
                            "speed": job.get("speed", PARAM_DEFAULT),
                            "pitch": job.get("pitch", PARAM_DEFAULT),
                            "volume": job.get("volume", PARAM_DEFAULT),
                        }
                        if cancel_check is not None:
                            generate_kwargs["cancel_check"] = cancel_check
                        pending_item = self._generate_pending_one(
                            job.get("text", ""),
                            **generate_kwargs,
                        )
                        resumed = False
                    pending_item["job_id"] = job_id
                    pending.append(pending_item)
                    # 统一下载模式下，提交每个音频段也是可见进度的一部分。
                    # 如果只等到下载页全部返回，长文档在生成阶段会一直显示 0%。
                    report_progress({
                        "job_id": job_id,
                        "works_id": pending_item.get("works_id"),
                        "resumed": resumed,
                        "downloaded": False,
                        "stage": "submitted",
                    })
                except (
                    XunfeiQuotaExceeded,
                    XunfeiLoginRequired,
                    XunfeiCancelled,
                ):
                    raise
                except XunfeiSubmissionAmbiguous as error:
                    result = {
                        "job_id": job_id,
                        "downloaded": False,
                        "ambiguous_works_id": True,
                        "works_name": error.works_name or job.get("works_name"),
                        "error": str(error),
                    }
                    results[job_id] = result
                    report_progress({
                        "job_id": job_id,
                        "downloaded": False,
                        "ambiguous_works_id": True,
                        "works_name": result["works_name"],
                        "stage": "saved",
                        "error": result["error"],
                    })
                except Exception as error:
                    result = {
                        "job_id": job_id,
                        "downloaded": False,
                        "error": str(error),
                    }
                    results[job_id] = result
                    report_progress({
                        "job_id": job_id,
                        "downloaded": False,
                        "stage": "saved",
                        "error": result["error"],
                    })

        duplicate_ids = self._duplicate_pending_work_ids(pending)
        pending_for_download = []
        for item in pending:
            _check_cancel_requested(cancel_check)
            works_id = str(item.get("works_id") or "")
            if works_id not in duplicate_ids:
                pending_for_download.append(item)
                continue
            job_id = str(item["job_id"])
            error = f"本批次 worksId 重复，无法安全归属音频：{works_id}"
            result = {
                **item,
                "job_id": job_id,
                "downloaded": False,
                "ambiguous_works_id": True,
                "error": error,
            }
            results[job_id] = result
            report_progress({
                "job_id": job_id,
                "works_id": works_id,
                "ambiguous_works_id": True,
                "downloaded": False,
                "stage": "saved",
                "error": error,
            })

        download_error = None
        try:
            _check_cancel_requested(cancel_check)
            download_kwargs = {}
            if callable(progress_callback):
                download_kwargs["progress_callback"] = report_progress
            if cancel_check is not None:
                download_kwargs["cancel_check"] = cancel_check
            downloaded = self._download_pending_batch(
                pending_for_download,
                **download_kwargs,
            )
        except (XunfeiCancelled, XunfeiLoginRequired):
            raise
        except Exception as error:
            downloaded = {}
            download_error = f"讯飞批量统一下载异常：{error}"
            _log(f"[xunfei] ❌ {download_error}")

        for item in pending_for_download:
            job_id = str(item["job_id"])
            works_id = str(item["works_id"])
            result = downloaded.get(works_id)
            if result:
                results[job_id] = {**result, "job_id": job_id}
            else:
                result = {
                    **item,
                    "job_id": job_id,
                    "downloaded": False,
                    "error": download_error or "合成已提交但统一下载失败",
                }
                results[job_id] = result
            if callable(progress_callback) and (
                job_id,
                "saved",
            ) not in reported_progress:
                report_progress({
                    "job_id": job_id,
                    "works_id": str(result.get("works_id") or works_id),
                    "works_id_invalid": bool(result.get("works_id_invalid")),
                    "downloaded": bool(result.get("downloaded")),
                    "stage": "saved",
                    "error": result.get("error"),
                })
        return results

    def synth_composite(
        self,
        works,
        max_retries=4,
        progress_callback=None,
        resume=None,
        cancel_check=None,
    ):
        """提交多人配音作品并统一下载，返回 work_id -> 文件结果。

        ``resume`` 只复用已经落盘的 worksId，不会因为切割失败而重新计费；
        下载失败时由上层决定是否重试或切换到原有单段模式。
        """
        if not self._logged_in:
            raise XunfeiError("尚未登录，请先调用 login()")
        normalized_works = [dict(work) for work in works if isinstance(work, dict)]
        if not normalized_works:
            return {}
        resume_map = resume if isinstance(resume, dict) else {}
        pending = []
        results = {}
        reported_progress = set()

        def report_progress(payload):
            if not callable(progress_callback):
                return
            item = dict(payload or {})
            work_id = str(item.get("work_id") or item.get("job_id") or "")
            stage = str(item.get("stage") or "saved")
            key = (work_id, stage)
            if work_id and key in reported_progress:
                return
            if work_id:
                reported_progress.add(key)
            _notify_batch_progress(progress_callback, item)

        for work in normalized_works:
            _check_cancel_requested(cancel_check)
            work_id = str(work.get("work_id") or work.get("job_id") or uuid.uuid4().hex)
            work["work_id"] = work_id
            work["job_id"] = work_id
            work.setdefault(
                "output_name",
                f".xunfei_composite_{uuid.uuid4().hex}.mp3",
            )
            work.setdefault(
                "works_name",
                f"wordtts_composite_{int(work.get('work_index') or 1):04d}_{uuid.uuid4().hex[:8]}",
            )
            previous = resume_map.get(work_id)
            previous_id = previous.get("works_id") if isinstance(previous, dict) else None
            try:
                if previous_id:
                    pending_item = {
                        "works_id": str(previous_id),
                        "output_path": os.path.join(OUTPUT_DIR, work["output_name"]),
                        "works_name": str(previous.get("works_name") or work["works_name"]),
                        "work_id": work_id,
                        "job_id": work_id,
                        "item_count": int(work.get("item_count") or 0),
                    }
                    _log(
                        f"[xunfei] ♻️ 复用多人配音作品 worksId={pending_item['works_id']} "
                        f"work={work_id}"
                    )
                else:
                    generate_kwargs = {
                        "output_name": work.get("output_name"),
                        "max_retries": max_retries,
                    }
                    if cancel_check is not None:
                        generate_kwargs["cancel_check"] = cancel_check
                    pending_item = self._generate_pending_composite(
                        work,
                        **generate_kwargs,
                    )
                    pending_item["job_id"] = work_id
                    pending_item["work_id"] = work_id
                pending.append(pending_item)
                report_progress({
                    "work_id": work_id,
                    "job_id": work_id,
                    "works_id": pending_item.get("works_id"),
                    "works_name": pending_item.get("works_name") or work.get("works_name"),
                    "stage": "submitted",
                    "downloaded": False,
                })
            except (
                XunfeiQuotaExceeded,
                XunfeiLoginRequired,
                XunfeiCancelled,
            ):
                raise
            except XunfeiSubmissionAmbiguous as error:
                result = {
                    "work_id": work_id,
                    "downloaded": False,
                    "audio": None,
                    "ambiguous_works_id": True,
                    "works_name": error.works_name or work.get("works_name"),
                    "error": str(error),
                }
                results[work_id] = result
                report_progress({
                    "work_id": work_id,
                    "job_id": work_id,
                    "ambiguous_works_id": True,
                    "works_name": result["works_name"],
                    "stage": "saved",
                    "downloaded": False,
                    "error": result["error"],
                })
            except Exception as error:
                results[work_id] = {
                    "work_id": work_id,
                    "downloaded": False,
                    "audio": None,
                    "error": str(error),
                }
                report_progress({
                    "work_id": work_id,
                    "job_id": work_id,
                    "stage": "saved",
                    "downloaded": False,
                    "error": str(error),
                })

        duplicate_ids = self._duplicate_pending_work_ids(pending)
        pending_for_download = []
        for pending_item in pending:
            _check_cancel_requested(cancel_check)
            works_id = str(pending_item.get("works_id") or "")
            if works_id not in duplicate_ids:
                pending_for_download.append(pending_item)
                continue
            work_id = str(
                pending_item.get("work_id")
                or pending_item.get("job_id")
                or ""
            )
            error = f"本批次 worksId 重复，无法安全归属音频：{works_id}"
            results[work_id] = {
                **pending_item,
                "work_id": work_id,
                "job_id": work_id,
                "downloaded": False,
                "audio": None,
                "ambiguous_works_id": True,
                "error": error,
            }
            report_progress({
                "work_id": work_id,
                "job_id": work_id,
                "works_id": works_id,
                "ambiguous_works_id": True,
                "stage": "saved",
                "downloaded": False,
                "error": error,
            })

        download_error = None
        try:
            _check_cancel_requested(cancel_check)
            download_kwargs = {
                "progress_callback": report_progress if callable(progress_callback) else None,
            }
            if cancel_check is not None:
                download_kwargs["cancel_check"] = cancel_check
            downloaded = self._download_pending_batch(
                pending_for_download,
                **download_kwargs,
            )
        except (XunfeiCancelled, XunfeiLoginRequired):
            raise
        except Exception as error:
            downloaded = {}
            download_error = f"讯飞多人配音统一下载异常：{error}"
            _log(f"[xunfei] ❌ {download_error}")

        for pending_item in pending_for_download:
            _check_cancel_requested(cancel_check)
            work_id = str(pending_item.get("work_id") or pending_item.get("job_id") or "")
            works_id = str(pending_item.get("works_id") or "")
            result = downloaded.get(works_id)
            if result:
                results[work_id] = {**result, "work_id": work_id, "job_id": work_id}
            else:
                results[work_id] = {
                    **pending_item,
                    "work_id": work_id,
                    "job_id": work_id,
                    "downloaded": False,
                    "error": download_error or "多人配音作品已提交但统一下载失败",
                }
            if callable(progress_callback) and (work_id, "saved") not in reported_progress:
                report_progress({
                    "work_id": work_id,
                    "job_id": work_id,
                    "works_id": works_id,
                    "works_id_invalid": bool(results[work_id].get("works_id_invalid")),
                    "stage": "saved",
                    "downloaded": bool(results[work_id].get("downloaded")),
                    "error": results[work_id].get("error"),
                })
        return results

    def synth_one(
        self,
        text,
        output_name=None,
        max_retries=4,
        voice_key=None,
        speed=PARAM_DEFAULT,
        pitch=PARAM_DEFAULT,
        volume=PARAM_DEFAULT,
        cancel_check=None,
        progress_callback=None,
    ):
        """
        在已登录的浏览器会话中生成一条音频。
        生成完成后浏览器与页面状态保持，等待下一条。

        Args:
            text: 要合成的文本
            output_name: 输出文件名（不含路径），None 时自动生成
            max_retries: 最大重试次数
            voice_key: 发音人 key（覆盖默认），如 "amanda"/"george"
            speed/pitch/volume: 讯飞平台三参数（0-100，50=默认）

        Returns: 生成的音频文件路径
        Raises:
            XunfeiQuotaExceeded: 额度不足（应停止整批任务）
            XunfeiRateLimited: 触发频控
            XunfeiLoginRequired: 会话失效
            XunfeiError: 其他生成失败
        """
        if not self._logged_in:
            raise XunfeiError("尚未登录，请先调用 login()")
        _check_cancel_requested(cancel_check)

        vk = voice_key if voice_key else self.voice_key
        voice_name = get_voice_info(vk)["name"]

        if not output_name:
            output_name = f"xunfei_{vk}_{int(time.time())}.mp3"
        output_path = os.path.join(OUTPUT_DIR, output_name)

        generate_kwargs = {
            "output_name": output_name,
            "max_retries": max_retries,
            "voice_key": voice_key,
            "speed": speed,
            "pitch": pitch,
            "volume": volume,
        }
        if callable(cancel_check):
            generate_kwargs["cancel_check"] = cancel_check
        pending = self._generate_pending_one(text, **generate_kwargs)
        _check_cancel_requested(cancel_check)
        download_kwargs = {}
        if callable(progress_callback):
            download_kwargs["progress_callback"] = progress_callback
        if callable(cancel_check):
            download_kwargs["cancel_check"] = cancel_check
        result = self._download_pending_batch([pending], **download_kwargs).get(str(pending["works_id"]))
        _check_cancel_requested(cancel_check)
        output_path = pending["output_path"]
        if result and result.get("downloaded") and os.path.exists(output_path):
            _log(f"[xunfei] ✅ 生成成功 ({os.path.getsize(output_path):,} bytes)")
            return output_path
        raise XunfeiError("合成已完成但未能下载音频")
