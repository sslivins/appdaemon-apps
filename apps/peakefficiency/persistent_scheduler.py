from datetime import datetime, timedelta, timezone
from diskcache import Cache
import uuid
import os
import json

class PersistentScheduler:
    def __init__(self, app, cache_path="/config/appdaemon_cache/scheduler"):
        self.app = app
        self.cache = Cache(cache_path)
        self.jobs = {}  # in-memory reference to active job handles
        self.restore_jobs()

    def _job_key(self, job_id):
        return f"job:{job_id}"

    def _serialize(self, func_name, run_time, kwargs):
        return {
            "func": func_name,
            "run_time": run_time.isoformat(),
            "kwargs": kwargs
        }

    def _deserialize(self, data):
        return data["func"], datetime.fromisoformat(data["run_time"]), data.get("kwargs", {})

    def schedule(self, func, run_time: datetime, kwargs=None, job_id=None):
        """Schedule a function to run at a specific time."""
        kwargs = kwargs or {}
        job_id = job_id or str(uuid.uuid4())

        # Save to cache
        self.cache.set(self._job_key(job_id), self._serialize(func.__name__, run_time, kwargs))

        # Schedule the job
        now = datetime.now()
        delay = max((run_time - now).total_seconds(), 0)
        handle = self.app.run_in(self._run_job, delay, job_id=job_id)

        self.jobs[job_id] = handle
        self.app.log(f"[Scheduler] Scheduled job '{job_id}' to run in {delay:.0f} seconds.")
        return job_id

    def _run_job(self, kwargs):
        job_id = kwargs["job_id"]
        job_data = self.cache.get(self._job_key(job_id))

        if not job_data:
            self.app.log(f"[Scheduler] Job '{job_id}' not found in cache, skipping.", level="WARNING")
            return

        func_name, _, job_kwargs = self._deserialize(job_data)
        func = getattr(self.app, func_name, None)
        if callable(func):
            self.app.log(f"[Scheduler] Running job '{job_id}' -> {func_name}")
            func(job_kwargs)
        else:
            self.app.log(f"[Scheduler] Function '{func_name}' not found in app!", level="ERROR")

        self.cache.delete(self._job_key(job_id))
        self.jobs.pop(job_id, None)

    def restore_jobs(self):
        now = datetime.now()
        for key in self.cache.iterkeys():
            if not key.startswith("job:"):
                continue

            job_id = key.split("job:")[1]
            data = self.cache.get(key)
            if not data:
                continue

            func_name, run_time, kwargs = self._deserialize(data)
            if run_time > now:
                delay = (run_time - now).total_seconds()
                handle = self.app.run_in(self._run_job, delay, job_id=job_id)
                self.jobs[job_id] = handle
                self.app.log(f"[Scheduler] Restored job '{job_id}' to run in {delay:.0f} seconds.")
            else:
                self.app.log(f"[Scheduler] Skipping expired job '{job_id}'.")

    def cancel_job(self, job_id):
        """Cancel a scheduled job."""
        if job_id in self.jobs:
            self.app.cancel_timer(self.jobs[job_id])
            self.jobs.pop(job_id)
            self.app.log(f"[Scheduler] Cancelled job '{job_id}'.")
        self.cache.delete(self._job_key(job_id))
