# -*- coding: utf-8 -*-
"""持久化账号批量任务：AT 巡检、全量查套餐、全量查活刷新 AT。"""
from __future__ import annotations

import logging
import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from contextlib import contextmanager

from core.inspection_lease import acquire_inspection_lease

from core import db, live_check_service, plan_check_service
from core.chatgpt_plan import decode_jwt_payload_unverified, normalize_token

logger = logging.getLogger(__name__)
DEFAULT_WORKERS = 3
MAX_WORKERS = 32
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="account-inspection")


def token_needs_refresh(token: str, now: float | None = None) -> bool:
    """只读取 JWT exp，不访问 API；缺失/格式错误也按失效处理。"""
    if not isinstance(token, str):
        return True
    token = normalize_token(token)
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return True
    payload = decode_jwt_payload_unverified(token)
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return True
    try:
        return not math.isfinite(exp) or exp <= (time.time() if now is None else now)
    except (ValueError, OverflowError):
        return True


def _check_live(account: dict, executor: ThreadPoolExecutor, trigger: str = "inspection") -> dict:
    queued = live_check_service.enqueue_account_live_check(
        account_id=account["id"], email=account.get("email", ""),
        trigger=trigger, return_future=True, executor=executor,
    )
    if not queued.get("accepted"):
        return {"ok": False, "status": "rejected", "error": queued.get("error") or "查活入队被拒绝"}
    return queued["future"].result()


def _check_plan(account: dict, executor: ThreadPoolExecutor) -> dict:
    queued = plan_check_service.enqueue_account_plan_check(
        account_id=account["id"], email=account.get("email", ""),
        access_token=account.get("access_token", ""), trigger="one_click_plan",
        return_future=True, executor=executor,
    )
    if not queued.get("accepted"):
        return {"ok": False, "status": "rejected", "error": queued.get("error") or "套餐查询入队被拒绝"}
    return queued["future"].result()


def validate_workers(workers: int) -> int:
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"任务线程数必须为 1–{MAX_WORKERS} 的整数")
    return workers


class InspectionStopped(RuntimeError):
    """持久化任务已结束，旧 worker 不再执行后续阶段。"""


def _run(run: dict) -> None:
    # 每个 ID 只进入一次共享迭代器；领取与统计各自加锁。
    account_ids = list(dict.fromkeys(run["account_ids"]))
    run["account_ids"] = account_ids
    run["total_count"] = len(account_ids)
    run.setdefault("skipped_count", 0)
    run.setdefault("deactivated_skipped_count", 0)
    run.setdefault("processed_count", 0)
    run.setdefault("failure_count", 0)
    mode = run.get("mode", "inspection")
    pending = iter(account_ids)
    claim_lock = threading.Lock()
    stats_lock = threading.Lock()
    stopped = threading.Event()

    def persist(failure=None):
        saved = db.save_account_inspection(run, failure=failure) if failure is not None else db.save_account_inspection(run)
        if not saved:
            raise InspectionStopped("账号任务已结束或被中断")

    def record(*, failure=None, **counts):
        if failure is not None:
            counts["failure_count"] = 1
        with stats_lock:
            previous = {key: run[key] for key in counts}
            for key, value in counts.items():
                run[key] += value
            try:
                persist(failure)
            except BaseException:
                # 明细落库失败时不能让 finally 单独保存已经增加的失败计数。
                run.update(previous)
                raise

    def inspect_account(account_id, stage_executor):
        account = db.get_account(account_id)
        if account is None:
            record(skipped_count=1, processed_count=1)
            return
        if str(account.get("live_check_status") or "").strip().lower() == "deactivated":
            record(skipped_count=1, processed_count=1, deactivated_skipped_count=1, failure=dict(
                account_id=account_id, email=account.get("email", ""), status="deactivated_skipped",
                error="账号已标记为已废，本次任务跳过，不执行 AT 检查、查活及查套餐。" +
                      (" 原因：" + str(account["live_check_error"]) if account.get("live_check_error") else ""),
            ))
            return
        if run.get("selection") and (
            account.get("live_check_status") in {"queued", "running"}
            or account.get("plan_check_status") in {"queued", "running"}
            or not db.inspection_retry_still_eligible(account_id, run)
        ):
            record(skipped_count=1, processed_count=1)
            return
        if mode == "plan":
            try:
                result = _check_plan(account, stage_executor)
            except Exception as exc:
                logger.exception("批量查套餐异常 account_id=%s", account_id)
                result = {"ok": False, "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:1500]}"}
            ok = bool(result.get("ok"))
            record(checked_count=1, processed_count=1,
                   failure=None if ok else dict(account_id=account_id, email=account.get("email", ""),
                                               status="plan_failed", error=result.get("error") or "套餐查询失败"),
                   **{"plan_success_count" if ok else "plan_failed_count": 1})
            return
        if mode == "inspection":
            invalid = token_needs_refresh(account.get("access_token", ""))
            record(checked_count=1, invalid_count=int(invalid), processed_count=int(not invalid))
            if not invalid:
                return
        try:
            live_result = (_check_live(account, stage_executor, trigger="one_click_live") if mode == "live"
                           else _check_live(account, stage_executor))
        except Exception as exc:
            logger.exception("巡检查活异常 account_id=%s", account_id)
            live_result = {"ok": False, "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:1500]}"}
        live_ok = bool(live_result.get("ok"))
        counts = {"live_success_count" if live_ok else "live_failed_count": 1, "processed_count": 1}
        if mode == "live":
            counts["checked_count"] = 1
        if live_ok:
            plan_ok = bool((live_result.get("plan_result") or {}).get("ok"))
            counts["plan_success_count" if plan_ok else "plan_failed_count"] = 1
        failure = None if live_ok else dict(account_id=account_id, email=account.get("email", ""),
                                            status=live_result.get("status") or "failed",
                                            error=live_result.get("error") or "查活失败，未返回具体原因")
        if live_ok and not plan_ok:
            failure = dict(account_id=account_id, email=account.get("email", ""), status="plan_failed",
                           error=(live_result.get("plan_result") or {}).get("error") or "查活成功，自动查询套餐失败")
        record(failure=failure, **counts)

    def worker(stage_executor):
        while True:
            with claim_lock:
                if stopped.is_set():
                    return
                account_id = next(pending, None)
            if account_id is None:
                return
            try:
                inspect_account(account_id, stage_executor)
            except BaseException:
                # 一个 worker 发生存储/执行故障后，不应让其他 worker 继续领取整份清单。
                # 已在执行中的账号仍完成收尾，避免遗留会话和队列占用。
                stopped.set()
                raise

    try:
        workers = validate_workers(run.get("workers", DEFAULT_WORKERS))
        run["status"] = "running"
        persist()
        # 独立阶段线程池，不受手动查活固定线程数限制；仍复用统一账号占用与限流。
        # 阶段执行与等待它的巡检 worker 分池，避免线程池饥饿死锁。
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="inspection-stage") as stages:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="inspection-worker") as pool:
                futures = [pool.submit(worker, stages) for _ in range(min(workers, len(account_ids)))]
                for future in futures:
                    future.result()
        run["status"] = "completed"
    except InspectionStopped as exc:
        run.update(status="interrupted", error=str(exc))
    except Exception as exc:
        logger.exception("账号巡检异常")
        run.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        run["finished_at"] = datetime.now().isoformat(timespec="seconds")
        db.save_account_inspection(run)


@contextmanager
def startup_recovery_guard():
    """同库巡检仍持有进程锁时，其他实例不得把它及其阶段任务恢复为失败。"""
    lease = acquire_inspection_lease()
    try:
        yield lease is not None
    finally:
        if lease is not None:
            lease.close()


def _run_with_lease(run, lease):
    try:
        _run(run)
    finally:
        lease.close()


def _release_cancelled_inspection(future, run, lease):
    if not future.cancelled():
        return
    try:
        cancelled = dict(run, status="interrupted", finished_at=datetime.now().isoformat(timespec="seconds"),
                         error="巡检在开始执行前被取消")
        db.save_account_inspection(cancelled)
    except Exception:
        logger.exception("写入巡检取消状态失败")
    finally:
        lease.close()


def _recover_orphaned_inspections() -> None:
    # 调用方必须持有进程锁；此时不会有执行中的巡检，可以清理异常退出遗留状态。
    db.recover_interrupted_account_inspections(
        error="账号任务执行器已退出或任务异常中断，已释放任务占用；统计保留已完成部分")


def list_inspections(limit: int = 50, offset: int = 0) -> dict:
    # 不依赖重启：最后一次数据库写入失败时，下一次轮询也能解除虚假的忙碌状态。
    lease = acquire_inspection_lease()
    try:
        if lease is not None:
            _recover_orphaned_inspections()
        return db.list_account_inspections(limit=limit, offset=offset)
    finally:
        if lease is not None:
            lease.close()


def _validate_selection(*, workers=DEFAULT_WORKERS, archived="0", mode="inspection", scope="all",
                        account_ids=None, filters=None, source_inspection_id=None):
    workers = validate_workers(workers)
    if not isinstance(mode, str) or mode not in ("inspection", "plan", "live"):
        raise ValueError("任务类型必须为 inspection（巡检）、plan（查套餐）或 live（查活刷新 AT）")
    if type(archived) is bool:
        archived = "only" if archived else "0"
    if not isinstance(archived, str) or archived not in ("0", "only", "all"):
        raise ValueError("归档范围必须为 0（未归档）、only（已归档）或 all（全部）")
    if not isinstance(scope, str) or scope not in {"all", "filtered", "selected", "failed", "history_failed"}:
        raise ValueError("执行范围无效")
    if scope in {"failed", "history_failed"} and mode == "inspection":
        raise ValueError("重试失败项请选择查活或查套餐")
    selection = {"scope": scope}
    if scope == "selected":
        if not isinstance(account_ids, list) or not account_ids or len(account_ids) > 10000 or any(
            type(i) is not int or not 0 < i <= 2**63 - 1 for i in account_ids
        ):
            raise ValueError("已勾选账号必须是 1–10000 个有效账号 ID")
        selection["account_ids"] = list(dict.fromkeys(account_ids))
    elif account_ids is not None:
        raise ValueError("只有已勾选范围可以指定 account_ids")
    if scope == "filtered":
        allowed = {"q", "plan", "codex_status", "totp_status", "date_from", "date_to"}
        if not isinstance(filters, dict) or set(filters) - allowed or any(
            not isinstance(v, str) or len(v) > 500 for v in filters.values()
        ):
            raise ValueError("筛选条件无效")
        selection["filters"] = dict(filters)
    elif filters is not None:
        raise ValueError("只有当前筛选范围可以指定 filters")
    if scope == "history_failed":
        if type(source_inspection_id) is not int or not 0 < source_inspection_id <= 2**63 - 1:
            raise ValueError("来源任务 ID 无效")
        selection["source_inspection_id"] = source_inspection_id
    elif source_inspection_id is not None:
        raise ValueError("只有历史失败重试可以指定来源任务")
    return workers, archived, mode, selection


def preview_inspection(**options):
    _, archived, mode, selection = _validate_selection(**options)
    return db.preview_account_inspection(archived=archived, mode=mode, selection=selection)


def start_inspection(workers: int = DEFAULT_WORKERS, archived: str | bool = "0", mode: str = "inspection", *,
                     scope="all", account_ids=None, filters=None, source_inspection_id=None) -> tuple[dict, bool]:
    workers, archived, mode, selection = _validate_selection(
        workers=workers, archived=archived, mode=mode, scope=scope, account_ids=account_ids,
        filters=filters, source_inspection_id=source_inspection_id)
    lease = acquire_inspection_lease()
    if lease is None:
        active = db.list_account_inspections()["active"]
        return active or {"status": "queued", "error": "账号任务正在启动或收尾，请稍后重试"}, False
    submitted = False
    try:
        _recover_orphaned_inspections()
        run, created = db.create_account_inspection(workers=workers, archived=archived, mode=mode, selection=selection)
        if created:
            try:
                future = _EXECUTOR.submit(_run_with_lease, run.copy(), lease)
                submitted = True
                future.add_done_callback(lambda done: _release_cancelled_inspection(done, run, lease))
            except Exception as exc:
                run.update(status="failed", error=str(exc)[:200], finished_at=datetime.now().isoformat(timespec="seconds"))
                db.save_account_inspection(run)
                raise
        return {k: v for k, v in run.items() if k != "account_ids"}, created
    finally:
        if not submitted:
            lease.close()
