"""
Background job registry.

Long operations (/scan, /fbatch, /process) must NEVER run inside a Telegram
command handler — that blocks the bot from answering anything else until the
job is done. Instead the handler registers a job here and returns instantly,
so the admin bot keeps responding to /status, /setdelay, /stop, … while the
job runs in the background.

Every job is a plain asyncio.Task, so cancellation is immediate and hard: no
restart needed to stop a running scan.
"""
import asyncio
import time
import uuid

_jobs: dict[str, "Job"] = {}


class Job:
    def __init__(self, name: str, detail: str, chat_id, user_id):
        self.id = uuid.uuid4().hex[:6]
        self.name = name
        self.detail = detail
        self.chat_id = chat_id
        self.user_id = user_id
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


def running_jobs() -> list[Job]:
    return [j for j in _jobs.values() if j.status == "running"]


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def start_job(name: str, detail: str, coro_factory, chat_id=None, user_id=None) -> Job:
    """
    Register and launch a background job.

    `coro_factory(job)` must return a coroutine. It receives the Job so it can
    publish progress via `job.progress = "…"`.
    """
    job = Job(name, detail, chat_id, user_id)
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
            # keep finished jobs around briefly for /jobs, then drop them
            asyncio.get_event_loop().call_later(300, _jobs.pop, job.id, None)

    job.task = asyncio.ensure_future(runner())
    return job


def cancel_job(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if job and job.task and not job.task.done():
        job.status = "cancelled"
        job.task.cancel()
        return True
    return False


def cancel_all() -> int:
    count = 0
    for job in list(_jobs.values()):
        if job.task and not job.task.done():
            job.status = "cancelled"
            job.task.cancel()
            count += 1
    return count
