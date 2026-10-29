"""Safely run LINEUP only after all four Data Hub Live CSVs advance.

Additive scheduling bridge: does not modify Data Hub, LINEUP business logic,
configurations, or existing local production plans. Python stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import datahub_adapter as adapter

RUNTIME = ROOT / "runtime"
STATE = RUNTIME / "lineup_datahub_schedule.json"
LOG = RUNTIME / "lineup_datahub_schedule.log"
LOCK = RUNTIME / "lineup_datahub_schedule.lock"
REQUIRED_JOBS = tuple(adapter.SOURCE_NAMES)


def log(message: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    line = dt.datetime.now().astimezone().isoformat(timespec="seconds") + "  " + message
    with LOG.open("a", encoding="utf-8") as output:
        output.write(line + "\n")
    print(line, flush=True)


def load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        content = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(content, dict):
            raise ValueError("state is not an object")
        return content
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Schedule state is unreadable ({STATE}): {exc}") from exc


def save_state(content: dict) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=RUNTIME, prefix=".lineup_schedule_",
        suffix=".tmp", delete=False
    ) as out:
        json.dump(content, out, indent=2)
        out.write("\n")
        temp = Path(out.name)
    os.replace(temp, STATE)


@contextlib.contextmanager
def exclusive_run():
    """An OS-level advisory lock, automatically released even after a crash."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+b") as handle:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("A scheduled LINEUP refresh is already running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("A scheduled LINEUP refresh is already running") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def source_status() -> tuple[dict, dict]:
    if not adapter.is_enabled():
        raise RuntimeError("LINEUP config/datahub_sources.json must have enabled=true")
    paths, signatures = {}, {}
    for job in REQUIRED_JOBS:
        csv = adapter.source_path(job)
        schema = csv.with_name(f"{job}_schema.json")
        if not csv.is_file() or not schema.is_file():
            raise RuntimeError(f"Missing completed CSV/schema pair for {job}: {csv}")
        c, s = csv.stat(), schema.stat()
        if not c.st_size or not s.st_size:
            raise RuntimeError(f"Empty CSV/schema pair for {job}")
        # Hub publishes schema JSON *last* as the per-report completion marker.
        if s.st_mtime_ns < c.st_mtime_ns:
            raise RuntimeError(f"{job} publish is not complete: schema is older than CSV")
        signatures[job] = {"mtime_ns": c.st_mtime_ns, "size": c.st_size}
        paths[job] = (csv, schema)
    # Reuse the real LINEUP migration adapter's strict row/schema/freshness checks.
    adapter.validate()
    return paths, signatures


def is_new_cycle(current: dict, previous: dict) -> bool:
    last = previous.get("sources")
    return not last or all(current.get(job) != last.get(job) for job in REQUIRED_JOBS)


@contextlib.contextmanager
def staged_inputs(paths: dict, signatures: dict):
    """Create a fixed four-report snapshot for the whole LINEUP processing run.

    Copy metadata too, and verify the sources have not changed during copying.
    mtime is preserved so the existing adapter's freshness checks still apply.
    """
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.TemporaryDirectory(prefix="lineup_hub_cycle_", dir=RUNTIME)
    root = Path(tmp.name)
    try:
        for job, (csv, schema) in paths.items():
            dest = root / "Live" / job
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(csv, dest / csv.name)
            shutil.copy2(schema, dest / schema.name)
        for job, (csv, schema) in paths.items():
            stat = csv.stat()
            actual = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
            if actual != signatures[job] or schema.stat().st_mtime_ns < stat.st_mtime_ns:
                raise RuntimeError(f"Data Hub changed {job} while copying; retry next scheduled check")
        yield root
    finally:
        tmp.cleanup()


def execute_lineup(snapshot_root: Path) -> int:
    batch = ROOT / "run_lineup_background.bat"
    if not batch.is_file():
        raise RuntimeError(f"LINEUP launcher not found: {batch}")
    if os.name != "nt":
        raise RuntimeError("Executing run_lineup_background.bat requires Windows")
    env = os.environ.copy()
    env["LINEUP_DATAHUB_ROOT"] = str(snapshot_root)
    log("Starting LINEUP with a validated, fixed snapshot of all four reports")
    return subprocess.call(["cmd.exe", "/d", "/c", str(batch)], cwd=str(ROOT), env=env)


def run(mode: str) -> int:
    with exclusive_run():
        paths, current = source_status()
        previous = load_state()
        if mode == "check":
            log("Inputs valid; " + ("new four-report cycle available" if is_new_cycle(current, previous)
                                     else "waiting for all four reports to refresh"))
            return 0
        if mode == "prime":
            save_state({"sources": current, "primed_at": dt.datetime.now().astimezone().isoformat()})
            log("Baseline saved. Next automatic LINEUP run will require all four exports to refresh.")
            return 0
        if not is_new_cycle(current, previous):
            log("Skipped: waiting until ALL four Live CSVs have refreshed since the last successful LINEUP run")
            return 0
        with staged_inputs(paths, current) as snapshot:
            original_env = os.environ.get("LINEUP_DATAHUB_ROOT")
            os.environ["LINEUP_DATAHUB_ROOT"] = str(snapshot)
            try:
                # Check the snapshot as well, not only the live sources.
                adapter.validate()
            finally:
                if original_env is None:
                    os.environ.pop("LINEUP_DATAHUB_ROOT", None)
                else:
                    os.environ["LINEUP_DATAHUB_ROOT"] = original_env
            code = execute_lineup(snapshot)
        if code:
            log(f"LINEUP failed with exit code {code}; keeping prior schedule baseline for retry")
            return 1
        save_state({
            "sources": current,
            "last_success": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        log("LINEUP processing succeeded; four-report baseline advanced")
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Validate shared inputs and report if a refresh is ready")
    group.add_argument("--prime", action="store_true", help="Mark existing exports as already consumed; wait for next cycle")
    group.add_argument("--run", action="store_true", help="Run LINEUP once when all four sources are newer")
    opts = parser.parse_args()
    mode = "check" if opts.check else "prime" if opts.prime else "run"
    try:
        return run(mode)
    except Exception as exc:
        log(f"SKIPPED/FAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
