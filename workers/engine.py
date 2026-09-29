# SPDX-License-Identifier: GPL-3.0-only
"""In-process worker engine.

One background thread polls the Store for work. A single global "GPU lease"
(threading.Lock) serializes rendering; run.state_version is used as a fencing
token so a stolen lease is detected before the next state transition. Every
state change goes through Store.transition_run (state machine + event +
outbox in one transaction).

Flow (generate):  QUEUED -> RENDERING -> GENERATED -> SCORING ->
                  accept: ACCEPTED | repair: REPAIRING -> invalidate_from |
                  else: HUMAN_REVIEW
Flow (reuse):     ASSET_READY -> NORMALIZING -> SCORING -> (same as above)
Infra errors:     -> RETRY_WAIT (not counted against repair budget), after
                  max_retries -> FAILED.
Cancel:           CANCEL_REQUESTED -> executor confirms at a safe point ->
                  CANCELLED; late results are discarded.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import traceback
from typing import Optional

from ..adapters.contracts import (
    AssetStore, ComfyRenderer, DecisionPort, TextModel, VisionJudge,
)
from ..domain.repositories.store import Store
from ..domain.schemas.core import (
    Asset, Event, RepairPlan, Run, ScoreReport, Shot, new_id, now_ts,
)
from ..domain.state_machine.machine import TERMINAL
from ..quality.checks import run_checks
from .compose import maybe_auto_compose

log = logging.getLogger(__name__)


class CancelRequested(Exception):
    """Raised inside the executor when a run hits a cancellation safe point."""


class StaleRun(Exception):
    """Fencing failure: state_version changed while we held the lease."""


def next_seed(seed: int, attempt: int) -> int:
    """修复/重试换种子：否则 ComfyUI 命中缓存，重渲染出来还是同一条。"""
    return (int(seed or 0) + 7919 * max(1, int(attempt))) % 2_147_483_647


class WorkerEngine:
    def __init__(self, store: Store, asset_store: AssetStore,
                 renderer: Optional[ComfyRenderer] = None,
                 judge: Optional[VisionJudge] = None,
                 decision: Optional[DecisionPort] = None,
                 text_model: Optional[TextModel] = None,
                 poll_interval_s: float = 0.5,
                 retry_backoff_s: float = 1.0,
                 max_retries: int = 3):
        self.store = store
        self.asset_store = asset_store
        self.renderer = renderer
        self.judge = judge
        self.decision = decision
        self.text_model = text_model
        self.poll_interval_s = poll_interval_s
        self.retry_backoff_s = retry_backoff_s
        self.max_retries = max_retries

        self._gpu_lock = threading.Lock()
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._executing: set[str] = set()
        self._retries: dict[str, int] = {}

    # ------------------------------------------------------------ lifecycle --
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="svf-worker",
                                        daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def pause_all(self) -> None:
        self._pause.set()

    def resume_all(self) -> None:
        self._pause.clear()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def reset_retries(self, run_id: str) -> None:
        self._retries.pop(run_id, None)

    def cancel_run(self, run_id: str) -> Run:
        run = self.store.get("runs", run_id)
        if run is None:
            raise KeyError(run_id)
        if run.state in TERMINAL:
            raise ValueError(f"run {run_id} already in terminal state {run.state}")
        if run.state == "HUMAN_REVIEW":
            return self.store.transition_run(
                run_id, "CANCELLED",
                self._ev(run, "run.cancelled", "orchestrator", "cancelled from review"))
        return self.store.transition_run(
            run_id, "CANCEL_REQUESTED",
            self._ev(run, "run.cancel_requested", "orchestrator", "cancel requested"))

    # ------------------------------------------------------------------ loop --
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("worker tick failed")
            self._stop.wait(self.poll_interval_s)

    def _tick(self) -> None:
        # finalize cancels for runs no executor is currently driving
        for run in self.store.all("runs", state="CANCEL_REQUESTED"):
            if run.run_id not in self._executing:
                self.store.transition_run(
                    run.run_id, "CANCELLED",
                    self._ev(run, "run.cancelled", "orchestrator", "cancel confirmed"))
        if self._pause.is_set():
            return
        # requeue infra-retry runs once their backoff elapsed
        for run in self.store.all("runs", state="RETRY_WAIT"):
            if run.run_id in self._executing:
                continue
            if now_ts() - run.updated_at >= self.retry_backoff_s:
                self.store.transition_run(
                    run.run_id, "QUEUED",
                    self._ev(run, "run.requeued", "orchestrator", "retry after backoff"),
                    patch={"attempt_id": new_id("att")})
        # rescue:生成模式的 run 不应停在 ASSET_READY(修复失效点会落在这里),
        # 续跑到 PROMPT_READY->QUEUED;reuse 模式由下面的 work 选择正常处理
        for run in self.store.all("runs", state="ASSET_READY"):
            if run.run_id in self._executing or not run.repair_count:
                continue
            if self._is_reuse(run):
                continue
            try:
                self.store.transition_run(
                    run.run_id, "PROMPT_READY",
                    self._ev(run, "run.invalidated", "orchestrator",
                             "resume stuck ASSET_READY"))
                self.store.transition_run(
                    run.run_id, "QUEUED",
                    self._ev(run, "step.queued", "orchestrator",
                             "requeued from ASSET_READY"))
            except Exception:
                log.warning("rescue stuck run %s failed", run.run_id,
                            exc_info=True)
        work = list(self.store.all("runs", state="QUEUED"))
        work += [r for r in self.store.all("runs", state="ASSET_READY")
                 if self._is_reuse(r)]
        if not work:
            return
        if not self._gpu_lock.acquire(blocking=False):
            return
        try:
            run = sorted(work, key=lambda r: r.created_at)[0]
            self._executing.add(run.run_id)
            try:
                self._process(run.run_id)
            except Exception:
                log.exception("run %s processing crashed", run.run_id)
            finally:
                self._executing.discard(run.run_id)
        finally:
            self._gpu_lock.release()

    def _is_reuse(self, run: Run) -> bool:
        shot = self.store.get("shots", run.shot_id)
        return bool(shot) and shot.spec.production_mode == "reuse"

    # -------------------------------------------------------------- process --
    def _process(self, run_id: str) -> None:
        run = self.store.get("runs", run_id)
        if run is None:
            return
        try:
            if run.state == "QUEUED":
                self._process_generate(run)
            elif run.state == "ASSET_READY":
                self._process_reuse(run)
        except CancelRequested:
            self._finalize_cancel(run_id)
        except Exception as exc:
            self._handle_infra_error(run_id, exc)
        else:
            self._retries.pop(run_id, None)

    def _process_generate(self, run: Run) -> None:
        shot = self._shot_of(run)
        spec = shot.spec
        if self.renderer is None:
            raise RuntimeError("renderer not configured")
        run = self._transition(run, "RENDERING", "step.started", "comfyui-adapter",
                               summary="render start")

        ref_keys = []
        for aid in spec.reference_assets:
            asset = self.store.get("assets", aid)
            if asset and asset.storage_key:
                ref_keys.append(asset.storage_key)

        def on_progress(current: int, total: int, summary: str) -> None:
            self._check_cancel(run.run_id)
            self.store.append_event(Event(
                project_id=run.project_id, run_id=run.run_id,
                attempt_id=run.attempt_id, type="tool.progress",
                actor="comfyui-adapter",
                progress={"current": current, "total": total}, summary=summary))

        video_key = self.renderer.render_shot(
            run.prompt_spec, ref_keys, run.seed,
            duration_s=spec.duration_s, aspect_ratio=spec.aspect_ratio,
            on_progress=on_progress)
        self._reject_if_late(run.run_id)   # cancelled while rendering: discard

        asset = self._register_video_asset(run, video_key, source="generated",
                                           parents=spec.reference_assets)
        run = self._transition(
            run, "GENERATED", "tool.completed", "comfyui-adapter",
            summary=f"rendered {video_key}",
            patch={"candidate_asset_ids": [asset.asset_id],
                   "artifact_ids": run.artifact_ids + [asset.asset_id]})
        run = self._transition(run, "SCORING", "step.started", "orchestrator",
                               summary="scoring start")
        self._score_and_decide(run, video_key, shot)

    def _process_reuse(self, run: Run) -> None:
        shot = self._shot_of(run)
        spec = shot.spec
        if self.renderer is None:
            raise RuntimeError("renderer not configured")
        binding = next((b for b in self.store.all("bindings", shot_id=shot.shot_id)
                        if b.role == "reuse_clip"), None)
        if binding is None:
            raise RuntimeError(f"reuse run {run.run_id} has no reuse_clip binding")
        source = self.store.get("assets", binding.asset_id)
        if source is None or not source.storage_key:
            raise RuntimeError(f"reuse_clip asset {binding.asset_id} unavailable")

        run = self._transition(run, "NORMALIZING", "step.started", "comfyui-adapter",
                               summary=f"normalize {source.storage_key}")
        video_key = self.renderer.normalize_clip(source.storage_key,
                                                 spec.duration_s, spec.aspect_ratio)
        self._reject_if_late(run.run_id)

        asset = self._register_video_asset(run, video_key, source="derived",
                                           parents=[source.asset_id])
        run = self._transition(
            run, "SCORING", "tool.completed", "comfyui-adapter",
            summary=f"normalized {video_key}",
            patch={"candidate_asset_ids": [asset.asset_id],
                   "artifact_ids": run.artifact_ids + [asset.asset_id]})
        self._score_and_decide(run, video_key, shot)

    # --------------------------------------------------------------- scoring --
    def _score_and_decide(self, run: Run, video_key: str, shot: Shot) -> None:
        spec = shot.spec
        checks = run_checks(self.asset_store.path_for(video_key),
                            expect_duration_s=spec.duration_s)
        if self.judge is not None:
            report = self.judge.score_video(video_key, spec)
        else:
            report = ScoreReport(run_id=run.run_id, verdict="accept",
                                 scores={"overall": 0.0},
                                 evidence=[{"kind": "degraded",
                                            "note": "no vision judge configured"}])
        report.run_id = run.run_id
        report.hard_checks = {**checks["hard_checks"], **report.hard_checks}
        report.evidence = checks["evidence"] + report.evidence
        if report.verdict in ("accept", "accept_with_deviation") \
                and not all(report.hard_checks.values()):
            report.verdict = "repair"
            report.evidence.append({"kind": "orchestrator",
                                    "note": "hard check failed; downgraded to repair"})
        self.store.put("score_reports", report)

        # shadow-mode routing advice: recorded, never blocks
        if self.decision is not None:
            try:
                advice = self.decision.advise(
                    {"run_state": "SCORING", "verdict": report.verdict,
                     "hard_checks": report.hard_checks,
                     "repair_count": run.repair_count,
                     "max_repairs": run.max_repairs},
                    ["accept", "repair", "human_review"])
                self.store.put("decisions", advice)
            except Exception:
                log.warning("decision advice failed", exc_info=True)

        fresh = self.store.get("runs", run.run_id) or run
        fresh.score = report.model_dump()
        fresh.updated_at = now_ts()
        self.store.put("runs", fresh)

        if report.verdict in ("accept", "accept_with_deviation"):
            if report.observation_confidence == "low":
                # 评审器看不清的不许自动通过，转人工
                self._transition(run, "HUMAN_REVIEW", "run.human_review",
                                 "vision-judge",
                                 summary=f"verdict={report.verdict} 但 "
                                         "observation_confidence=low，转人工复核")
                return
            summary = f"verdict={report.verdict} scores={report.scores}"
            if report.deviation:
                summary += f" deviation={report.deviation[:100]}"
            self._transition(run, "ACCEPTED", "run.accepted", "vision-judge",
                             summary=summary)
            shot = self.store.get("shots", shot.shot_id)
            shot.accepted_run_id = run.run_id
            # 评审观测到的实际末态写回 shot，供下一镜续接
            if report.observed_end_state:
                shot.spec.observed_end_state = report.observed_end_state
            self.store.put("shots", shot)
            # 一键出片收尾:最后一个镜头验收后自动拼接完整成片
            try:
                maybe_auto_compose(self.store, self.asset_store, run.project_id)
            except Exception:
                log.warning("auto compose trigger failed", exc_info=True)
            return

        if report.verdict in ("repair", "reject") and run.repair_count < run.max_repairs:
            run = self._transition(run, "REPAIRING", "run.repairing", "orchestrator",
                                   summary=f"verdict={report.verdict}")
            plan = self._make_repair_plan(run, report)
            self.store.put("repair_plans", plan)
            run = self._transition(
                run, plan.invalidate_from, "run.invalidated", "orchestrator",
                summary=f"repair: {plan.action}",
                patch={"repair_count": run.repair_count + 1,
                       "seed": next_seed(run.seed, run.repair_count + 1)})
            if run.state == "ASSET_READY" and not self._is_reuse(run):
                # 生成模式的修复失效点是 ASSET_READY 时,必须继续走到
                # PROMPT_READY->QUEUED,否则 run 会卡在 ASSET_READY 等不到执行
                run = self._transition(run, "PROMPT_READY", "run.invalidated",
                                       "orchestrator",
                                       summary="repair: resume from assets")
            if run.state == "PROMPT_READY":
                self._transition(run, "QUEUED", "step.queued", "orchestrator",
                                 summary="requeued after repair")
            return

        self._transition(run, "HUMAN_REVIEW", "run.human_review", "orchestrator",
                         summary=f"verdict={report.verdict}, repair budget "
                                 f"{run.repair_count}/{run.max_repairs}")

    def _make_repair_plan(self, run: Run, report: ScoreReport) -> RepairPlan:
        if self.text_model is not None:
            try:
                out = self.text_model.chat_json(
                    "你是修复 agent。根据评分报告给出最小修复计划，只输出 JSON。",
                    {"score_report": report.model_dump(),
                     "prompt_spec": run.prompt_spec},
                    '{"target":"prompt|reference_assets|shot_spec|infra",'
                    '"action":str,"detail":str,'
                    '"invalidate_from":"PROMPT_READY|ASSET_READY"}')
                target = out.get("target") or "prompt"
                if target not in ("prompt", "reference_assets", "shot_spec", "infra"):
                    target = "prompt"
                invalidate_from = out.get("invalidate_from") or "PROMPT_READY"
                if invalidate_from not in ("PROMPT_READY", "ASSET_READY"):
                    invalidate_from = "PROMPT_READY"
                return RepairPlan(run_id=run.run_id, target=target,
                                  action=str(out.get("action") or "rewrite prompt"),
                                  detail=str(out.get("detail") or ""),
                                  invalidate_from=invalidate_from)
            except Exception:
                log.warning("repair agent failed, using default plan", exc_info=True)
        return RepairPlan(run_id=run.run_id, target="prompt",
                          action="rewrite prompt with failing criteria",
                          detail="default plan (no repair agent configured)",
                          invalidate_from="PROMPT_READY")

    # ---------------------------------------------------------------- helpers --
    def _shot_of(self, run: Run) -> Shot:
        shot = self.store.get("shots", run.shot_id)
        if shot is None:
            raise KeyError(f"shot {run.shot_id} missing for run {run.run_id}")
        return shot

    def _register_video_asset(self, run: Run, storage_key: str, source: str,
                              parents: Optional[list[str]] = None) -> Asset:
        data = self.asset_store.read_bytes(storage_key)
        asset = Asset(project_id=run.project_id, source=source, media_type="video",
                      sha256=hashlib.sha256(data).hexdigest(),
                      storage_key=storage_key, status="READY",
                      metadata={"size": len(data)},
                      parent_asset_ids=list(parents or []))
        try:
            asset.preview_key = self.asset_store.make_preview(asset) or ""
        except Exception:
            log.debug("preview generation failed for %s", storage_key, exc_info=True)
        self.store.put("assets", asset)
        return asset

    def _ev(self, run: Run, type_: str, actor: str, summary: str = "") -> Event:
        return Event(project_id=run.project_id, run_id=run.run_id,
                     attempt_id=run.attempt_id, type=type_, actor=actor,
                     summary=summary)

    def _transition(self, run: Run, to_state: str, type_: str, actor: str,
                    summary: str = "", patch: Optional[dict] = None) -> Run:
        """Fenced transition: aborts when the run was cancelled or its
        state_version moved since we acquired the lease."""
        fresh = self.store.get("runs", run.run_id)
        if fresh is None:
            raise KeyError(run.run_id)
        if fresh.state in ("CANCEL_REQUESTED", "CANCELLED"):
            raise CancelRequested(run.run_id)
        if fresh.state_version != run.state_version:
            raise StaleRun(f"run {run.run_id} version {run.state_version} -> "
                           f"{fresh.state_version}")
        return self.store.transition_run(
            run.run_id, to_state, self._ev(fresh, type_, actor, summary), patch=patch)

    def _check_cancel(self, run_id: str) -> None:
        run = self.store.get("runs", run_id)
        if run is not None and run.state in ("CANCEL_REQUESTED", "CANCELLED"):
            raise CancelRequested(run_id)

    def _reject_if_late(self, run_id: str) -> None:
        """Called right after a blocking tool returns; discards the product
        when the run was cancelled underneath us."""
        self._check_cancel(run_id)

    def _finalize_cancel(self, run_id: str) -> None:
        run = self.store.get("runs", run_id)
        if run is not None and run.state == "CANCEL_REQUESTED":
            self.store.transition_run(
                run_id, "CANCELLED",
                self._ev(run, "run.cancelled", "orchestrator",
                         "executor confirmed cancel at safe point"))

    def _handle_infra_error(self, run_id: str, exc: Exception) -> None:
        run = self.store.get("runs", run_id)
        if run is None or run.state in TERMINAL:
            return
        if run.state in ("CANCEL_REQUESTED", "CANCELLED"):
            self._finalize_cancel(run_id)
            return
        n = self._retries.get(run_id, 0) + 1
        self._retries[run_id] = n
        failure = {"error": str(exc), "error_type": type(exc).__name__,
                   "infra_retries": n, "at": now_ts()}
        if n >= self.max_retries or "RETRY_WAIT" not in _retryable_from(run.state):
            self.store.transition_run(
                run_id, "FAILED",
                self._ev(run, "run.failed", "orchestrator", str(exc)[:300]),
                patch={"failure": failure})
            return
        log.warning("run %s infra error (%d/%d): %s", run_id, n, self.max_retries, exc)
        log.warning("run %s infra traceback:\n%s", run_id, traceback.format_exc())
        self.store.transition_run(
            run_id, "RETRY_WAIT",
            self._ev(run, "run.retry_wait", "orchestrator", str(exc)[:300]),
            patch={"failure": failure})


def _retryable_from(state: str) -> set[str]:
    from ..domain.state_machine.machine import TRANSITIONS
    return TRANSITIONS.get(state, set())
