"""批量生成引擎：单段分组批量与多人配音合并批量。"""


import asyncio
import hashlib
import inspect
import os
import re
import sys
import threading
import uuid

from xunfei.errors import _log

from wordtts.composite_cut import (
    CompositeCutError,
    cut_composite_audio,
    format_composite_cut_diagnostics,
)
from wordtts.composite_plan import build_composite_work_plan
from wordtts.config import COMPOSITE_MIN_OUTPUT_MS, TTS_PARAM_DEFAULT
from wordtts.synthesis import (
    _concat_audio_segments,
    _synth_item,
    build_synthesis_segments,
)
from wordtts.xunfei_bridge import _xunfei, _XUNFEI_AVAILABLE


async def _synth_items_batch(
    item_specs,
    progress_callback=None,
    cancel_check=None,
):
    """批量生成多道题，讯飞端先按音色/参数分组后统一下载。

    返回 ``item_id -> {audio, error}``。每道题的多角色段落仍按照原文顺序
    拼接；单个题目失败不会丢弃同一批里已经成功生成的其它题目。
    """
    if not item_specs:
        return {}
    if not _XUNFEI_AVAILABLE or _xunfei is None:
        raise RuntimeError("讯飞配音引擎不可用（缺少 playwright）")
    if callable(cancel_check) and cancel_check():
        raise asyncio.CancelledError

    jobs = []
    item_job_ids = {}
    job_item_ids = {}
    for item_index, spec in enumerate(item_specs):
        item_id = str(spec["item_id"])
        segment_specs = build_synthesis_segments(
            spec.get("text", ""),
            spec.get("rate", TTS_PARAM_DEFAULT),
            spec.get("volume", TTS_PARAM_DEFAULT),
            spec.get("pitch", TTS_PARAM_DEFAULT),
            default_voice=spec.get("default_voice"),
            female_voice=spec.get("female_voice"),
            male_voice=spec.get("male_voice"),
            voice_configs=spec.get("voice_configs"),
            role_voices=spec.get("role_voices"),
            role_configs=spec.get("role_configs"),
            default_role=spec.get("default_role"),
        )
        ids = []
        resume_works_ids = (
            spec.get("xunfei_works_ids")
            if isinstance(spec.get("xunfei_works_ids"), dict)
            else {}
        )
        resume_ambiguous_names = (
            spec.get("xunfei_ambiguous_works")
            if isinstance(spec.get("xunfei_ambiguous_works"), dict)
            else {}
        )
        for segment in segment_specs:
            job_id = f"{item_id}::segment:{segment['segment_index']}"
            ids.append(job_id)
            job_item_ids[job_id] = item_id
            jobs.append({
                "job_id": job_id,
                "item_id": item_id,
                "segment_index": segment["segment_index"],
                "text": segment["text"],
                "voice_key": segment["voice_key"],
                "speed": segment["speed"],
                "pitch": segment["pitch"],
                "volume": segment["volume"],
                "works_name": (
                    f"wordtts_{hashlib.sha1(job_id.encode('utf-8')).hexdigest()[:16]}"
                ),
            })
            resume_works_id = str(resume_works_ids.get(job_id) or "").strip()
            if resume_works_id:
                jobs[-1]["resume_works_id"] = resume_works_id
            ambiguous_name = str(resume_ambiguous_names.get(job_id) or "").strip()
            if ambiguous_name and not resume_works_id:
                jobs[-1]["ambiguous_works_name"] = ambiguous_name
        item_job_ids[item_id] = ids

    batch_results = {}
    progress_consumer = None
    progress_queue = None
    progress_futures = []
    progress_futures_lock = threading.Lock()

    if callable(progress_callback):
        loop = asyncio.get_running_loop()
        progress_queue = asyncio.Queue()
        submitted_jobs = {item_id: set() for item_id in item_job_ids}
        downloaded_jobs = {item_id: set() for item_id in item_job_ids}
        saved_jobs = {item_id: {} for item_id in item_job_ids}
        submitted_works = {item_id: {} for item_id in item_job_ids}
        ambiguous_works = {item_id: set() for item_id in item_job_ids}
        ambiguous_work_names = {item_id: {} for item_id in item_job_ids}
        invalid_works = {item_id: set() for item_id in item_job_ids}
        terminal_alert_sent = {item_id: set() for item_id in item_job_ids}
        final_progress_sent = set()

        def track_work_event(item_id, job_id, event):
            works_id = str(event.get("works_id") or "").strip()
            if event.get("works_id_invalid"):
                invalid_works[item_id].add(job_id)
                submitted_works[item_id].pop(job_id, None)
                ambiguous_works[item_id].discard(job_id)
                ambiguous_work_names[item_id].pop(job_id, None)
            elif event.get("ambiguous_works_id"):
                ambiguous_works[item_id].add(job_id)
                submitted_works[item_id].pop(job_id, None)
                works_name = str(event.get("works_name") or "").strip()
                if works_name:
                    ambiguous_work_names[item_id][job_id] = works_name
            elif works_id and job_id not in ambiguous_works[item_id]:
                ambiguous_works[item_id].discard(job_id)
                ambiguous_work_names[item_id].pop(job_id, None)
                submitted_works[item_id][job_id] = works_id

        def work_progress_snapshot(item_id):
            return {
                "works_ids": dict(submitted_works[item_id]),
                "ambiguous_works_ids": sorted(ambiguous_works[item_id]),
                "ambiguous_works_names": dict(ambiguous_work_names[item_id]),
                "invalid_works_ids": sorted(invalid_works[item_id]),
            }

        async def consume_batch_progress():
            while True:
                event = await progress_queue.get()
                if event is None:
                    return
                job_id = str(event.get("job_id") or "")
                item_id = job_item_ids.get(job_id)
                if not item_id:
                    continue
                stage = str(event.get("stage") or "saved")
                track_work_event(item_id, job_id, event)
                if stage == "submitted":
                    if job_id not in submitted_jobs[item_id]:
                        submitted_jobs[item_id].add(job_id)
                        try:
                            callback_result = progress_callback({
                                "item_id": item_id,
                                "status": "submitted",
                                "completed_segments": len(submitted_jobs[item_id]),
                                "total_segments": len(item_job_ids[item_id]),
                                "segment_id": job_id,
                                **work_progress_snapshot(item_id),
                            })
                            if inspect.isawaitable(callback_result):
                                await callback_result
                        except Exception as error:
                            _log(f"[xunfei] 题目提交进度回调异常（已忽略）: {error}")
                elif stage == "downloaded":
                    if job_id not in downloaded_jobs[item_id]:
                        downloaded_jobs[item_id].add(job_id)
                        try:
                            callback_result = progress_callback({
                                "item_id": item_id,
                                "status": "downloaded",
                                "completed_segments": len(downloaded_jobs[item_id]),
                                "total_segments": len(item_job_ids[item_id]),
                                "segment_id": job_id,
                                **work_progress_snapshot(item_id),
                            })
                            if inspect.isawaitable(callback_result):
                                await callback_result
                        except Exception as error:
                            _log(f"[xunfei] 题目下载进度回调异常（已忽略）: {error}")
                else:
                    saved_jobs[item_id][job_id] = bool(event.get("downloaded"))
                    if (
                        (event.get("ambiguous_works_id") or event.get("works_id_invalid"))
                        and job_id not in terminal_alert_sent[item_id]
                    ):
                        terminal_alert_sent[item_id].add(job_id)
                        try:
                            callback_result = progress_callback({
                                "item_id": item_id,
                                "status": "error",
                                "completed_segments": sum(
                                    1 for downloaded in saved_jobs[item_id].values()
                                    if downloaded
                                ),
                                "total_segments": len(item_job_ids[item_id]),
                                "segment_id": job_id,
                                "error": event.get("error") or (
                                    "讯飞已确认提交但作品 ID 不确定"
                                    if event.get("ambiguous_works_id")
                                    else "讯飞作品 ID 已失效"
                                ),
                                **work_progress_snapshot(item_id),
                            })
                            if inspect.isawaitable(callback_result):
                                await callback_result
                        except Exception as error:
                            _log(f"[tts] 题目不确定提交状态回调异常（已忽略）: {error}")
                    if (
                        len(saved_jobs[item_id]) == len(item_job_ids[item_id])
                        and item_id not in final_progress_sent
                    ):
                        final_progress_sent.add(item_id)
                        failures = [
                            job_key for job_key, downloaded in saved_jobs[item_id].items()
                            if not downloaded
                        ]
                        try:
                            callback_result = progress_callback({
                                "item_id": item_id,
                                "status": "ready" if not failures else "error",
                                "completed_segments": len(item_job_ids[item_id]) - len(failures),
                                "total_segments": len(item_job_ids[item_id]),
                                "error": event.get("error") if failures else None,
                                **work_progress_snapshot(item_id),
                            })
                            if inspect.isawaitable(callback_result):
                                await callback_result
                        except Exception as error:
                            _log(f"[xunfei] 题目保存进度回调异常（已忽略）: {error}")

        progress_consumer = asyncio.create_task(consume_batch_progress())

        def queue_batch_progress(event):
            if not isinstance(event, dict):
                return
            try:
                future = asyncio.run_coroutine_threadsafe(
                    progress_queue.put(dict(event)),
                    loop,
                )
            except RuntimeError:
                return
            with progress_futures_lock:
                progress_futures.append(future)

        try:
            batch_kwargs = {"progress_callback": queue_batch_progress}
            if cancel_check is not None:
                batch_kwargs["cancel_check"] = cancel_check
            batch_results = await _xunfei.synth_xunfei_batch(
                jobs,
                **batch_kwargs,
            )
        except Exception as error:
            cancelled_type = getattr(_xunfei, "XunfeiCancelled", None)
            if (
                cancelled_type is not None
                and isinstance(error, cancelled_type)
                and callable(cancel_check)
                and cancel_check()
            ):
                raise asyncio.CancelledError from error
            raise
        finally:
            # Sync Playwright 在专用线程中调用完所有回调后才会返回；等待
            # 已排队的跨线程 put 完成，再发送结束标记，防止最后几条进度丢失。
            with progress_futures_lock:
                queued_futures = list(progress_futures)
            if queued_futures:
                await asyncio.gather(
                    *(asyncio.wrap_future(future) for future in queued_futures),
                    return_exceptions=True,
                )
            progress_queue.put_nowait(None)
            await progress_consumer
    else:
        batch_kwargs = {}
        if cancel_check is not None:
            batch_kwargs["cancel_check"] = cancel_check
        try:
            batch_results = await _xunfei.synth_xunfei_batch(jobs, **batch_kwargs)
        except Exception as error:
            cancelled_type = getattr(_xunfei, "XunfeiCancelled", None)
            if (
                cancelled_type is not None
                and isinstance(error, cancelled_type)
                and callable(cancel_check)
                and cancel_check()
            ):
                raise asyncio.CancelledError from error
            raise

    item_results = {}
    for spec in item_specs:
        item_id = str(spec["item_id"])
        parts = []
        error = None
        for job_id in item_job_ids.get(item_id, []):
            result = batch_results.get(job_id) if isinstance(batch_results, dict) else None
            segment = result.get("segment") if isinstance(result, dict) else None
            if segment is None:
                error = (result or {}).get("error") if isinstance(result, dict) else None
                error = error or "讯飞批量下载未返回音频"
                break
            parts.append(segment)

        if error:
            item_results[item_id] = {"audio": None, "error": str(error)}
            continue
        if not parts:
            item_results[item_id] = {"audio": None, "error": "未生成任何音频段"}
            continue
        item_results[item_id] = {"audio": _concat_audio_segments(parts), "error": None}
    return item_results


async def _synth_items_batch_composite(
    item_specs,
    progress_callback=None,
    *,
    work_plan=None,
    resume=None,
    debug_dir=None,
    cancel_check=None,
):
    """一次提交多人配音作品，再按安全停顿恢复为题目音频。

    这里的“批量”单位是合并作品，而不是音色组。一个作品可包含多个音色
    和各自参数；只有网页字数上限或断点计划要求时才会有多个作品。
    """
    if not item_specs:
        return {}
    if not _XUNFEI_AVAILABLE or _xunfei is None:
        raise RuntimeError("讯飞配音引擎不可用（缺少 playwright）")
    if callable(cancel_check) and cancel_check():
        raise asyncio.CancelledError

    works = build_composite_work_plan(
        item_specs,
        existing_plan=work_plan,
    )
    if not works:
        return {}
    work_by_id = {str(work["work_id"]): work for work in works}
    resume_map = resume if isinstance(resume, dict) else {}
    request_works = []
    for index, work in enumerate(works, start=1):
        request = dict(work)
        request["job_id"] = str(work["work_id"])
        request["work_index"] = index
        request["work_total"] = len(works)
        request.setdefault(
            "works_name",
            f"wordtts_composite_{index:04d}_{uuid.uuid4().hex[:8]}",
        )
        request_works.append(request)

    submitted_work_ids = set()
    downloaded_work_ids = set()
    progress_consumer = None
    progress_queue = None
    progress_futures = []
    progress_futures_lock = threading.Lock()

    async def forward_progress(event):
        if not callable(progress_callback):
            return
        payload = dict(event or {})
        work_id = str(payload.get("work_id") or payload.get("job_id") or "")
        work = work_by_id.get(work_id) or {}
        stage = str(payload.get("stage") or "saved")
        if stage == "submitted":
            submitted_work_ids.add(work_id)
            status = "submitted"
        elif stage == "downloaded":
            downloaded_work_ids.add(work_id)
            status = "downloaded"
        elif stage == "cut":
            status = "cut"
        elif stage in {"cut_error", "error", "saved"} and payload.get("error"):
            status = "error"
        else:
            status = "downloaded" if work_id in downloaded_work_ids else "submitted"
        callback_payload = {
            "work_id": work_id,
            "job_id": work_id,
            "status": status,
            "stage": stage,
            "works_id": payload.get("works_id"),
            "works_name": payload.get("works_name") or work.get("works_name"),
            "item_count": int(work.get("item_count") or 0),
            "item_ids": list(work.get("item_ids") or []),
            "total_works": len(works),
            "submitted_works": len(submitted_work_ids),
            "downloaded_works": len(downloaded_work_ids),
            "error": payload.get("error"),
        }
        if payload.get("ambiguous_works_id"):
            callback_payload["ambiguous_works_id"] = True
        if payload.get("works_id_invalid"):
            callback_payload["works_id_invalid"] = True
        if isinstance(payload.get("cut_diagnostics"), dict):
            callback_payload["cut_diagnostics"] = dict(payload["cut_diagnostics"])
        callback_result = progress_callback(callback_payload)
        if inspect.isawaitable(callback_result):
            await callback_result

    if callable(progress_callback):
        loop = asyncio.get_running_loop()
        progress_queue = asyncio.Queue()

        async def consume_work_progress():
            while True:
                event = await progress_queue.get()
                if event is None:
                    return
                try:
                    await forward_progress(event)
                except Exception as error:
                    print(
                        f"[tts] 多人配音进度回调异常（已忽略）: {error}",
                        file=sys.stdout,
                    )

        progress_consumer = asyncio.create_task(consume_work_progress())

        def queue_work_progress(event):
            if not isinstance(event, dict):
                return
            try:
                future = asyncio.run_coroutine_threadsafe(
                    progress_queue.put(dict(event)),
                    loop,
                )
            except RuntimeError:
                return
            with progress_futures_lock:
                progress_futures.append(future)
    else:
        queue_work_progress = None

    try:
        composite_kwargs = {
            "progress_callback": queue_work_progress,
            "resume": resume_map,
        }
        if cancel_check is not None:
            composite_kwargs["cancel_check"] = cancel_check
        raw_results = await _xunfei.synth_xunfei_composite(
            request_works,
            **composite_kwargs,
        )
    except Exception as error:
        cancelled_type = getattr(_xunfei, "XunfeiCancelled", None)
        if (
            cancelled_type is not None
            and isinstance(error, cancelled_type)
            and callable(cancel_check)
            and cancel_check()
        ):
            raise asyncio.CancelledError from error
        raise
    finally:
        if progress_consumer is not None:
            with progress_futures_lock:
                queued_futures = list(progress_futures)
            if queued_futures:
                await asyncio.gather(
                    *(asyncio.wrap_future(future) for future in queued_futures),
                    return_exceptions=True,
                )
            progress_queue.put_nowait(None)
            await progress_consumer

    item_results = {}
    from pydub import AudioSegment

    for work in works:
        work_id = str(work["work_id"])
        raw = raw_results.get(work_id) if isinstance(raw_results, dict) else None
        audio = raw.get("audio") if isinstance(raw, dict) else None
        error = raw.get("error") if isinstance(raw, dict) else None
        if audio is None:
            message = str(error or "讯飞多人配音作品未返回音频")
            for item_id in work["item_ids"]:
                item_results[str(item_id)] = {"audio": None, "error": message}
            await forward_progress({
                "work_id": work_id,
                "stage": "error",
                "works_id": raw.get("works_id") if isinstance(raw, dict) else None,
                "ambiguous_works_id": (
                    bool(raw.get("ambiguous_works_id"))
                    if isinstance(raw, dict)
                    else False
                ),
                "works_id_invalid": (
                    bool(raw.get("works_id_invalid"))
                    if isinstance(raw, dict)
                    else False
                ),
                "works_name": (
                    raw.get("works_name") or work.get("works_name")
                    if isinstance(raw, dict)
                    else work.get("works_name")
                ),
                "error": message,
            })
            continue

        cut_diagnostics = {}
        try:
            pieces = cut_composite_audio(
                audio,
                work["item_count"],
                item_lengths=[
                    unit.get("char_count") for unit in work.get("items") or []
                ],
                diagnostics=cut_diagnostics,
            )
            if len(pieces) != len(work["item_ids"]):
                raise CompositeCutError("多人配音安全切割数量与题目数量不一致")
            for item_id, piece in zip(work["item_ids"], pieces):
                if not isinstance(piece, AudioSegment) or len(piece) < COMPOSITE_MIN_OUTPUT_MS:
                    raise CompositeCutError(f"{item_id} 切割后的音频过短")
                item_results[str(item_id)] = {"audio": piece, "error": None}
            diagnostic_text = format_composite_cut_diagnostics(cut_diagnostics)
            if diagnostic_text:
                print(
                    f"[tts] 合并作品 {work_id} {diagnostic_text}",
                    file=sys.stdout,
                )
            await forward_progress({
                "work_id": work_id,
                "stage": "cut",
                "works_id": raw.get("works_id") if isinstance(raw, dict) else None,
                "cut_item_count": len(pieces),
                "cut_diagnostics": cut_diagnostics,
            })
        except Exception as cut_error:
            message = str(cut_error)
            diagnostic_text = format_composite_cut_diagnostics(cut_diagnostics)
            if diagnostic_text:
                print(
                    f"[tts] 合并作品 {work_id} {diagnostic_text}",
                    file=sys.stdout,
                )
                message = f"{message}；{diagnostic_text}"
            if debug_dir:
                try:
                    os.makedirs(debug_dir, exist_ok=True)
                    debug_path = os.path.join(
                        debug_dir,
                        f"{re.sub(r'[^0-9A-Za-z_.-]+', '_', work_id)}.mp3",
                    )
                    await asyncio.to_thread(audio.export, debug_path, format="mp3")
                    message = f"{message}；合并音频已保留：{debug_path}"
                except Exception as save_error:
                    message = f"{message}；保留合并音频失败：{save_error}"
            for item_id in work["item_ids"]:
                item_results[str(item_id)] = {"audio": None, "error": message}
            await forward_progress({
                "work_id": work_id,
                "stage": "cut_error",
                "works_id": raw.get("works_id") if isinstance(raw, dict) else None,
                "ambiguous_works_id": (
                    bool(raw.get("ambiguous_works_id"))
                    if isinstance(raw, dict)
                    else False
                ),
                "works_id_invalid": (
                    bool(raw.get("works_id_invalid"))
                    if isinstance(raw, dict)
                    else False
                ),
                "error": message,
                "cut_diagnostics": cut_diagnostics,
            })
    return item_results


def generate_item_audio(text, rate, volume, pitch, default_voice=None,
                        female_voice=None, male_voice=None):
    """同步包装：为一条文本生成音频。"""
    return asyncio.run(_synth_item(text, rate, volume, pitch,
                                   default_voice=default_voice,
                                   female_voice=female_voice, male_voice=male_voice))
