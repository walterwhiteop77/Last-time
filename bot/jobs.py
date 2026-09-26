"""
Background job registry — scoped per user (workspace).

Long operations (/scan, /fbatch, /process) never run inside a Telegram command
handler: the handler registers a job here and returns instantly, so the bot
keeps answering everyone while jobs run.

Each job belongs to the admin who started it. /stop, /jobs and /cancel only
ever touch that admin's own jobs, so two people can run their own jobs at the
same time without interfering.
"""
import asyncio
import time
import uuid

_jobs: dict[str, "Job"] = {}


class Job:
    def __init__(self, name: str, detail: str, chat_id, user_id, ws=None):
        self.id = uuid.uuid4().hex[:6]
        self.name = name
        self.detail = detail
        self.chat_id = chat_id
        self.user_id = user_id
        self.ws = int(ws if ws is not None else (user_id or 0))
        self.started = time.time()
        self.task: asyncio.Task | None = None
        self.status = "running"
        self.progress = ""

    @property
    def runtime(self) -> int:
        return int(time.time() - self.started)

    def describe(self) -> str:
        icon = {"running": "🟢", "cancelled": "⛔", "done": "✅", "error": "⚠️"}.get(self.status, "•")
        line = f"{icon} `{self.id}` *{self.name}* — {self.detail} ({self.runtime}s)"
        if self.progress:
            line += f"\n   ↳ {self.progress}"
        return line


def running_jobs(ws=None) -> list:
    out = [j for j in _jobs.values() if j.status == "running"]
    if ws is not None:
        out = [j for j in out if j.ws == int(ws)]
    return out


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def start_job(name: str, detail: str, coro_factory, chat_id=None, user_id=None, ws=None) -> Job:
    """
    Register and launch a background job.

    `coro_factory(job)` must return a coroutine. It receives the Job so it can
    publish progress via `job.progress = "…"`.
    """
    job = Job(name, detail, chat_id, user_id, ws)
    _jobs[job.id] = job

    async def runner():
        try:
            await coro_factory(job)
            if job.status == "running":
                job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as e:
            job.status = "error"
            job.progress = str(e)[:200]
            print(f"[jobs] Job {job.id} ({job.name}) failed: {e}")
        finally:
            asyncio.get_event_loop().call_later(300, _jobs.pop, job.id, None)

    # ensure_future copies the current context, so the job keeps running
    # against the workspace of the admin who started it.
    job.task = asyncio.ensure_future(runner())
    return job


def cancel_job(job_id: str, ws=None) -> bool:
    job = _jobs.get(job_id)
    if job is None:
        return False
    if ws is not None and job.ws != int(ws):
        return False
    if job.task and not job.task.done():
        job.status = "cancelled"
        job.task.cancel()
        return True
    return False


def cancel_all(ws=None) -> int:
    count = 0
    for job in list(_jobs.values()):
        if ws is not None and job.ws != int(ws):
            continue
        if job.task and not job.task.done():
            job.status = "cancelled"
            job.task.cancel()
            count += 1
    return count
