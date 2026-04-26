#!/usr/bin/env python3
"""
VPVoAe Batch Orchestrator
=========================

Хост-сайд оркестратор для масштабной обработки сайтов (500–1000+ в сутки).

Что делает:
  • Читает очередь URL из файла (или stdin).
  • Запускает N Docker-контейнеров параллельно (worker pool).
  • Каждому контейнеру передаёт свой TARGET_URL и изолированный output-том.
  • Перезапускает упавшие задачи (configurable retry + exponential backoff).
  • Ведёт persistent state (jobs.json) — можно прервать и продолжить.
  • Пишет structured-логи и итоговый отчёт.

Запуск:
  python3 orchestrator.py --urls urls.txt --workers 4 --output-root /srv/vpvoae-output
  python3 orchestrator.py --urls urls.txt --workers 6 --retries 2 --resume

Зависимости: только stdlib + установленный docker CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("orchestrator")


# ---------------------------------------------------------------------------
# Модели данных
# ---------------------------------------------------------------------------

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"


@dataclass
class Job:
    url: str
    slug: str
    status: str = JOB_PENDING
    attempts: int = 0
    last_error: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    container_name: str = ""
    output_dir: str = ""
    exit_code: Optional[int] = None
    duration_s: float = 0.0


@dataclass
class BatchState:
    started_at: float = field(default_factory=time.time)
    jobs: Dict[str, Job] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "jobs": {k: asdict(v) for k, v in self.jobs.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BatchState":
        st = cls(started_at=data.get("started_at", time.time()))
        for k, v in (data.get("jobs") or {}).items():
            st.jobs[k] = Job(**v)
        return st


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def url_to_slug(url: str) -> str:
    """Превращает URL в безопасный идентификатор для имён контейнера/папки."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "site").lower()
    path = (parsed.path or "").strip("/").lower()
    raw = f"{host}-{path}" if path else host
    slug = _SLUG_RE.sub("-", raw).strip("-")
    return (slug or "site")[:60]


def load_urls(path: Path) -> List[str]:
    urls: List[str] = []
    seen: Set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Поддерживаем CSV: "url,note" — берём только первую колонку
            url = line.split(",", 1)[0].strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return urls


def ensure_unique_slug(slug: str, used: Set[str]) -> str:
    if slug not in used:
        used.add(slug)
        return slug
    n = 2
    while f"{slug}-{n}" in used:
        n += 1
    final = f"{slug}-{n}"
    used.add(final)
    return final


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def load_state(state_file: Path) -> Optional[BatchState]:
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return BatchState.from_dict(data)
    except Exception as e:
        logger.warning(f"⚠️  Не удалось прочитать state-файл {state_file}: {e}")
        return None


def save_state(state: BatchState, state_file: Path) -> None:
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_file)


# ---------------------------------------------------------------------------
# Docker runner
# ---------------------------------------------------------------------------

async def run_docker_job(
    job: Job,
    image: str,
    output_root: Path,
    job_timeout_s: int,
    extra_env: Dict[str, str],
    cpu_shares: int,
    mem_limit: str,
    shm_size: str,
) -> int:
    """Запускает один контейнер для одного URL. Возвращает exit code."""
    out_dir = output_root / job.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    job.output_dir = str(out_dir)

    # Уникальное имя контейнера — slug + attempt, чтобы избежать коллизий при ретраях.
    container_name = f"vpvoae-{job.slug}-{job.attempts}"
    job.container_name = container_name

    # Преварительно зачищаем тёзку (если остался от прошлого аварийного запуска).
    await _docker_rm(container_name)

    cmd: List[str] = [
        "docker", "run", "--rm",
        "--name", container_name,
        "-e", f"TARGET_URL={job.url}",
        "-e", "OUTPUT_PATH=/app/output",
        "-v", f"{out_dir}:/app/output",
        "--shm-size", shm_size,
        "--cpu-shares", str(cpu_shares),
        "--memory", mem_limit,
        "--log-driver", "json-file",
        "--log-opt", "max-size=50m",
        "--log-opt", "max-file=2",
    ]
    for k, v in extra_env.items():
        cmd.extend(["-e", f"{k}={v}"])
    cmd.append(image)

    logger.info(f"▶️  [{job.slug}] attempt={job.attempts} → {job.url}")
    logger.debug(f"   cmd: {' '.join(cmd)}")

    job.started_at = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        job.last_error = "docker CLI не найден в PATH"
        logger.error(f"❌ {job.last_error}")
        return 127

    log_file = out_dir / f"run_{job.attempts}.log"
    exit_code = 1
    try:
        with log_file.open("wb") as lf:
            try:
                async def _drain():
                    assert proc.stdout is not None
                    while True:
                        chunk = await proc.stdout.read(4096)
                        if not chunk:
                            break
                        lf.write(chunk)
                        lf.flush()

                await asyncio.wait_for(asyncio.gather(_drain(), proc.wait()), timeout=job_timeout_s)
                exit_code = proc.returncode if proc.returncode is not None else 1
            except asyncio.TimeoutError:
                job.last_error = f"timeout after {job_timeout_s}s"
                logger.warning(f"⏱️  [{job.slug}] {job.last_error} — kill container")
                await _docker_kill(container_name)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=15)
                except asyncio.TimeoutError:
                    proc.kill()
                exit_code = 124
    finally:
        job.finished_at = time.time()
        job.duration_s = round(job.finished_at - (job.started_at or job.finished_at), 2)
        job.exit_code = exit_code
        # На всякий случай — удалить контейнер, если --rm не сработал.
        await _docker_rm(container_name)

    return exit_code


async def _docker_rm(name: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except FileNotFoundError:
        pass


async def _docker_kill(name: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Worker pool
# ---------------------------------------------------------------------------

class Orchestrator:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.output_root = Path(args.output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.state_file = self.output_root / "jobs.json"
        self.state: BatchState = BatchState()
        self._save_lock = asyncio.Lock()
        self._stopping = False
        self.extra_env: Dict[str, str] = {}
        if args.env:
            for pair in args.env:
                if "=" not in pair:
                    continue
                k, v = pair.split("=", 1)
                self.extra_env[k.strip()] = v.strip()

    # ---------------- queue management ----------------

    def init_queue(self, urls: List[str], resume: bool) -> None:
        existing = load_state(self.state_file) if resume else None
        if existing:
            self.state = existing
            logger.info(f"🔁 Resume: загружено {len(self.state.jobs)} задач из {self.state_file}")
            # Сбрасываем зависшие RUNNING (видимо, прервали оркестратор) → в pending для повтора.
            for job in self.state.jobs.values():
                if job.status == JOB_RUNNING:
                    job.status = JOB_PENDING
                    job.last_error = "interrupted, requeued"

        used_slugs: Set[str] = {j.slug for j in self.state.jobs.values()}
        added = 0
        for url in urls:
            # Дедупликация по URL.
            if any(j.url == url for j in self.state.jobs.values()):
                continue
            slug = ensure_unique_slug(url_to_slug(url), used_slugs)
            self.state.jobs[slug] = Job(url=url, slug=slug)
            added += 1
        if added:
            logger.info(f"➕ Добавлено {added} новых URL в очередь")

    def pending_jobs(self) -> List[Job]:
        return [j for j in self.state.jobs.values() if j.status == JOB_PENDING]

    async def _persist(self) -> None:
        async with self._save_lock:
            save_state(self.state, self.state_file)

    # ---------------- main loop ----------------

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except NotImplementedError:
                # Windows — тут не критично, оркестратор предназначен для Linux-сервера.
                pass

        queue: asyncio.Queue[Job] = asyncio.Queue()
        for job in self.pending_jobs():
            await queue.put(job)

        total = len(self.state.jobs)
        pending = queue.qsize()
        logger.info("=" * 70)
        logger.info(f"🚀 VPVoAe Orchestrator — старт")
        logger.info(f"   Всего задач: {total} | в очереди: {pending} | workers: {self.args.workers}")
        logger.info(f"   Output root: {self.output_root}")
        logger.info(f"   Image: {self.args.image} | retries: {self.args.retries} | timeout: {self.args.job_timeout}s")
        logger.info("=" * 70)

        await self._persist()

        workers = [
            asyncio.create_task(self._worker(i + 1, queue), name=f"worker-{i+1}")
            for i in range(self.args.workers)
        ]

        # Ждём опустошения + завершения всех воркеров.
        await queue.join()
        for w in workers:
            w.cancel()
        for w in workers:
            try:
                await w
            except asyncio.CancelledError:
                pass

        await self._persist()
        self._print_report()
        failed = sum(1 for j in self.state.jobs.values() if j.status == JOB_FAILED)
        return 0 if failed == 0 else 2

    def _request_stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        logger.warning("🛑 Получен сигнал остановки — дорабатываем активные задачи и выходим…")

    async def _worker(self, wid: int, queue: asyncio.Queue) -> None:
        while True:
            try:
                job = await queue.get()
            except asyncio.CancelledError:
                return
            try:
                if self._stopping:
                    # Возвращаем задачу в pending — её подхватят при resume.
                    job.status = JOB_PENDING
                    queue.task_done()
                    continue
                await self._process_job(wid, job, queue)
            finally:
                queue.task_done()

    async def _process_job(self, wid: int, job: Job, queue: asyncio.Queue) -> None:
        job.attempts += 1
        job.status = JOB_RUNNING
        await self._persist()

        try:
            exit_code = await run_docker_job(
                job=job,
                image=self.args.image,
                output_root=self.output_root,
                job_timeout_s=self.args.job_timeout,
                extra_env=self.extra_env,
                cpu_shares=self.args.cpu_shares,
                mem_limit=self.args.mem_limit,
                shm_size=self.args.shm_size,
            )
        except Exception as e:
            exit_code = 1
            job.last_error = f"unexpected: {e}"
            logger.exception(f"💥 [w{wid}] {job.slug}: исключение в раннере")

        if exit_code == 0:
            job.status = JOB_DONE
            job.last_error = ""
            logger.info(f"✅ [w{wid}] {job.slug} done за {job.duration_s}s → {job.output_dir}")
        else:
            if not job.last_error:
                job.last_error = f"exit code {exit_code}"
            if job.attempts <= self.args.retries:
                # Экспоненциальный backoff: 5s, 15s, 45s, …
                delay = min(5 * (3 ** (job.attempts - 1)), 300)
                logger.warning(
                    f"🔁 [w{wid}] {job.slug} fail ({job.last_error}), "
                    f"retry {job.attempts}/{self.args.retries} через {delay}s"
                )
                job.status = JOB_PENDING
                await self._persist()
                await asyncio.sleep(delay)
                await queue.put(job)
            else:
                job.status = JOB_FAILED
                logger.error(
                    f"❌ [w{wid}] {job.slug} FAILED после {job.attempts} попыток: {job.last_error}"
                )

        await self._persist()

    # ---------------- report ----------------

    def _print_report(self) -> None:
        done = [j for j in self.state.jobs.values() if j.status == JOB_DONE]
        failed = [j for j in self.state.jobs.values() if j.status == JOB_FAILED]
        pending = [j for j in self.state.jobs.values() if j.status == JOB_PENDING]
        total = len(self.state.jobs)
        elapsed = time.time() - self.state.started_at
        avg = (sum(j.duration_s for j in done) / len(done)) if done else 0.0

        logger.info("=" * 70)
        logger.info("📊 Итоги батча")
        logger.info(f"   Всего:    {total}")
        logger.info(f"   ✅ Done:   {len(done)}")
        logger.info(f"   ❌ Failed: {len(failed)}")
        logger.info(f"   ⏳ Pending:{len(pending)}")
        logger.info(f"   ⏱  Wall time:   {elapsed:.1f}s")
        logger.info(f"   ⏱  Avg per job: {avg:.1f}s")
        if failed:
            logger.info("   Список упавших:")
            for j in failed:
                logger.info(f"     - {j.slug} ({j.url}) — {j.last_error}")
        logger.info("=" * 70)

        # CSV-отчёт рядом со state.
        report = self.output_root / "report.csv"
        with report.open("w", encoding="utf-8") as f:
            f.write("slug,url,status,attempts,duration_s,exit_code,output_dir,error\n")
            for j in self.state.jobs.values():
                err = (j.last_error or "").replace(",", ";").replace("\n", " ")
                f.write(
                    f"{j.slug},{j.url},{j.status},{j.attempts},{j.duration_s},"
                    f"{j.exit_code if j.exit_code is not None else ''},{j.output_dir},{err}\n"
                )
        logger.info(f"📝 CSV-отчёт: {report}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="VPVoAe batch orchestrator — параллельная обработка очереди URL.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--urls", required=True, type=Path,
                   help="Файл со списком URL (один в строке, # — комментарий).")
    p.add_argument("--workers", type=int, default=int(os.getenv("ORCH_WORKERS", "4")),
                   help="Сколько контейнеров крутить параллельно.")
    p.add_argument("--retries", type=int, default=int(os.getenv("ORCH_RETRIES", "2")),
                   help="Сколько повторных попыток на упавший URL.")
    p.add_argument("--image", default=os.getenv("ORCH_IMAGE", "vpvoae-renderer:latest"),
                   help="Docker-образ рендерера (собирается из ./Dockerfile).")
    p.add_argument("--output-root", type=Path,
                   default=Path(os.getenv("ORCH_OUTPUT_ROOT", "./batch-output")),
                   help="Корень для подпапок-результатов и state-файла.")
    p.add_argument("--job-timeout", type=int, default=int(os.getenv("ORCH_JOB_TIMEOUT", "1800")),
                   help="Жёсткий таймаут одной задачи (секунды).")
    p.add_argument("--shm-size", default=os.getenv("ORCH_SHM_SIZE", "2gb"),
                   help="--shm-size для контейнера (Chrome любит много).")
    p.add_argument("--mem-limit", default=os.getenv("ORCH_MEM_LIMIT", "4g"),
                   help="--memory лимит на контейнер.")
    p.add_argument("--cpu-shares", type=int, default=int(os.getenv("ORCH_CPU_SHARES", "1024")),
                   help="--cpu-shares на контейнер.")
    p.add_argument("--env", action="append", default=[],
                   help="Доп. env для контейнера, формат KEY=VALUE (можно несколько раз). "
                        "Пробрасывает любые SMART_CURSOR_*/FFMPEG_* и т.п.")
    p.add_argument("--resume", action="store_true",
                   help="Продолжить предыдущий батч из jobs.json в --output-root.")
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG логи.")
    return p.parse_args(argv)


async def _amain(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if not args.urls.exists():
        logger.error(f"❌ Файл с URL не найден: {args.urls}")
        return 1
    if not shutil.which("docker"):
        logger.error("❌ docker CLI не найден в PATH")
        return 1

    urls = load_urls(args.urls)
    if not urls and not args.resume:
        logger.error("❌ В файле нет URL")
        return 1
    logger.info(f"📥 Загружено {len(urls)} URL из {args.urls}")

    orch = Orchestrator(args)
    orch.init_queue(urls, resume=args.resume)
    return await orch.run()


def main() -> None:
    try:
        sys.exit(asyncio.run(_amain()))
    except KeyboardInterrupt:
        logger.warning("⛔ Прервано пользователем")
        sys.exit(130)


if __name__ == "__main__":
    main()
