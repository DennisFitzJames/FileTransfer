# Shared SAP Data Hub → LINEUP: scheduled deployment kit

**Version:** rollout pilot 1.0. **Purpose:** add Windows scheduling without changing the working Data Hub exporters, parser or LINEUP business calculations. Contains *new files only*, suitable for committing to the appropriate GitHub repositories.

## Files and where to copy them

**Data Hub project root** (folder containing `app.py` and `config.json`):
- `DataHub/start_datahub_scheduled.cmd` → `start_datahub_scheduled.cmd`.

**LINEUP project root** (folder containing `run_lineup_background.bat`):
- `LINEUP/app/scheduled_datahub_refresh.py` → `app/scheduled_datahub_refresh.py`.
- `LINEUP/run_lineup_scheduled.cmd` → `run_lineup_scheduled.cmd`.
- `LINEUP/check_lineup_schedule.cmd` → `check_lineup_schedule.cmd`.
- `LINEUP/prime_lineup_schedule.cmd` → `prime_lineup_schedule.cmd`.

**Do not replace:** `config.json`, `config/datahub_sources.json`, any SAP script, `run_lineup_background.bat`, processed files, or project-specific user settings. Back up both projects before changes.

## Why a bridge is necessary

The Data Hub already has a single SAP worker and a built-in per-report scheduler. Do **not** create 17 Windows tasks for each SAP export. Windows Task Scheduler should launch Data Hub once; the Data Hub schedules SAP jobs itself.

LINEUP's new bridge runs as a separate periodic Windows task. It checks that:
1. The four Live datasets and schema completion markers are present and valid.
2. All four datasets have changed **since the previous successful scheduled LINEUP processing run**.
3. Every source passes LINEUP's existing strict validation and configured freshness limit.
4. No other scheduled LINEUP bridge instance is running.

It then copies a **stable snapshot** of the four sources into a local temporary folder, revalidates that snapshot and runs the **existing** `run_lineup_background.bat` with a temporary `LINEUP_DATAHUB_ROOT` override. The snapshot is deleted afterwards. If processing fails, the bridge does **not** mark the cycle consumed, so the next scheduled attempt can retry. This implementation works even if Data Hub and LINEUP later run on **different PCs**, provided both can read the shared folder.

**Known limitation:** The lock prevents overlapping *scheduled bridge* runs; manual recalculations triggered through LINEUP's web interface are not yet covered by the same lock. Avoid simultaneous manual processing during the initial pilot; synchronise these paths before broad deployment.

## Stage A: shared folder configuration

1. In **Data Hub `config.json`**, check `storage.publish_root` is the *actual* accessible folder containing `Live`, `Raw`, `History`. `storage.requested_share` is only a requested destination and does not make publishing happen there.
2. In **LINEUP `config/datahub_sources.json`**, `publish_root` must match it; keep `enabled: true` and `max_age_minutes: 120` for the initial trial.
3. Confirm all four locations exist:
   - `Live/coois_production_lineup/coois_production_lineup_latest.csv`
   - `Live/coois_capacities_lineup/coois_capacities_lineup_latest.csv`
   - `Live/lx02/lx02_latest.csv`
   - `Live/lrf2/lrf2_latest.csv`
   Each has a sibling `<job_id>_schema.json` written last by the Data Hub parser.
4. For Windows Task Scheduler, **UNC paths are preferable** (`\\server\share\...`). A mapped `W:` drive might be unavailable to a task running under a different account or logon session. Confirm read/write access with the scheduled account. Never substitute an invented server/share name.

## Stage B: Data Hub automatic refresh (pilot the four LINEUP sources)

1. Stop any existing Data Hub controller so that only one controller uses local port 8765.
2. Start the controller manually and use the dashboard at `http://127.0.0.1:8765`.
3. Enable these **four** Data Hub jobs: `coois_production_lineup`, `coois_capacities_lineup`, `lx02`, `lrf2`.
4. Set each to **30 minutes**, starting with the four-source pilot. Keep the remaining 13 jobs disabled temporarily (you may enable them gradually after monitoring runtimes).
5. Make sure Python `pywin32` works, SAP GUI is running and logged into the correct system, and `storage.publish_root` is accessible.
6. Click **Resume automatic scheduling** in the Data Hub dashboard. Note: pause/resume state persists in `runtime/state.json`; editing only `config.json` may not override a previously saved pause.
7. Check each job reaches `success` and a fresh CSV/schema pair appears. If an exporter fails, the Data Hub retains the previously validated CSV and retries according to the configured policy; LINEUP will not run until all four have advanced.

## Stage C: LINEUP first-run test

1. Copy the four new LINEUP-side files to the working LINEUP directory.
2. Open Command Prompt in the LINEUP project root. If you use a virtual environment, the supplied `.cmd` files select `venv\Scripts\python.exe`; otherwise they use `py -3`.
3. Run `check_lineup_schedule.cmd`. It should say inputs are valid, and indicate whether a new four-report cycle is available.
4. Run **`prime_lineup_schedule.cmd` once**. This records the four *current* source versions as already consumed. No SAP or LINEUP processing runs during prime.
5. Manually refresh the four jobs in Data Hub (or wait for one complete scheduled cycle). Verify all four are `success`.
6. Double-click `run_lineup_scheduled.cmd`. Expected: it starts LINEUP once using four fixed input copies, and logs `LINEUP processing succeeded; four-report baseline advanced`.
7. Double-click `run_lineup_scheduled.cmd` again *without another SAP cycle*. Expected: it **skips** with `waiting until ALL four Live CSVs have refreshed`, rather than running LINEUP again.
8. Compare `data/processed/production_list_clean.csv`, `capacities_clean.csv`, `master_schedule.csv`, planning-board counts and logistics views with the successful manual migration test.
9. On the first week, monitor `runtime/lineup_datahub_schedule.log` and the existing `logs/lineup_run_YYYY-MM-DD.log`. An error does not overwrite the schedule baseline; an unsuccessful SAP run doesn't count as a new completed input.

## Stage D: create two Windows Scheduled Tasks

Use **Task Scheduler** (`taskschd.msc`) → **Create Task** (not Basic Task). Tasks should run under the **same ordinary Windows account** used for the interactive SAP GUI session and allowed to access the share; do not use SYSTEM and do not store SAP credentials in scripts.

### Task 1: Shared SAP Data Hub Controller

- **General:** name `Shared SAP Data Hub Controller`; choose **Run only when user is logged on**. Do not request highest privileges unless IT specifically requires them.
- **Triggers:** **At log on** of the appropriate user; optionally **delay 5 minutes** so the network and SAP GUI are ready. The session must actually be logged in to SAP; task registration cannot log into SAP automatically.
- **Actions:** Start a program; Program/script `C:\Windows\System32\cmd.exe`; Add arguments `/d /c ""C:\ACTUAL\PATH\TO\DataHub\start_datahub_scheduled.cmd""`; Start in `C:\ACTUAL\PATH\TO\DataHub` (no quotes). Replace the example path with the real folder.
- **Conditions:** do not require idle; only start on AC power if appropriate for the dedicated laptop.
- **Settings:** **If already running: Do not start a new instance**; disable the default maximum runtime limit for this long-lived controller; enable restart on task failure if permitted.
- The controller logs detailed events to `logs/controller.log` and Python startup errors to `logs/controller_session.log`.

### Task 2: LINEUP Data Hub Scheduled Refresh

- **General:** name `LINEUP Data Hub Refresh`; **Run only when user is logged on** for the first pilot (allows the same mapped-drive/session access; it does not itself interact with SAP).
- **Triggers:** Daily, start at a convenient time; **Repeat task every 10 minutes** for a duration of **1 day** (a daily trigger renews the period). If the UI offers *indefinitely*, that also works.
- **Actions:** Program/script `C:\Windows\System32\cmd.exe`; Add arguments `/d /c ""C:\ACTUAL\PATH\TO\LINEUP\run_lineup_scheduled.cmd""`; Start in `C:\ACTUAL\PATH\TO\LINEUP` (no quotes).
- **Settings:** **If already running: Do not start a new instance**. Do not run when the computer is idle only; set a sensible limit such as 30 minutes if LINEUP normally finishes much sooner. Enable **Run task as soon as possible after a scheduled start is missed**.
- This task does **not** run SAP and does **not** process LINEUP every 10 minutes: it checks every 10 minutes, *processing only once when all four files have changed since its last success*.

**Important:** The Data Hub task must run continuously; do not configure Windows to relaunch `app.py` every 30 minutes. It has its own scheduler and owns the single SAP session.

## Stage E: operational acceptance on the dedicated laptop

- Test manual task **Run** in Task Scheduler for each task; inspect **Last Run Result** and both applications' own logs.
- Confirm the Data Hub remains accessible at `http://127.0.0.1:8765`, and LINEUP at its existing URL after starting its existing web-server launcher separately.
- Confirm automatic refreshes complete after **sign out / log in** (with SAP re-authentication if needed), after a brief network outage, and after a failed SAP job.
- Test **screen locked**, **Remote Desktop disconnect**, **sleep**, and **Windows restart** separately. SAP GUI scripting often requires an active interactive desktop and a logged-on SAP session; don't assume every combination will work. Follow corporate IT policy for sleep/session handling.
- Check the live CSV filenames/mtime, controller state, LINEUP's schedule log, and displayed board results during a working day.
- Keep old LINEUP VBS scripts and the previous deployment available for rollback during the pilot. Only disable the old LINEUP SAP scheduled tasks once the new pipeline runs reliably; never run both export schedules concurrently.
- Before expanding access to LINEUP's browser, check authentication and network exposure: its existing Flask launch currently binds `0.0.0.0:5000` with write-capable endpoints. This is not suitable for unrestricted network exposure without IT-approved access controls.

## Stage F: the remaining 13 Data Hub reports / historical exports

Add the FOOT and Logistics KPI jobs progressively, watching the *total cycle runtime* of the single SAP worker. With all 17 jobs set to 30 minutes, a slow COOIS report or daily reconciliation/backfill may create a queue. Set priorities/longer intervals based on actual runtimes, rather than enabling everything at once. Enable `history.enabled` and automatic previous-day reconciliation only after confirming day-scoped SAP exports on the production laptop and validating resulting dated CSVs. Note that `History` is currently for the six configured Logistics transaction reports, not all snapshots.

## Recovery / rollback

- **Pause SAP jobs:** Data Hub dashboard → Pause automatic scheduling. It persists even across controller restarts.
- **Pause LINEUP:** Disable `LINEUP Data Hub Refresh` in Task Scheduler; manual LINEUP use remains available.
- **Return LINEUP to old exporters:** After a backup, set `config/datahub_sources.json` `enabled: false` and use the prior working scripts as necessary. Do not run both exporter suites simultaneously.
- **Reset the scheduling baseline** if required: rerun `prime_lineup_schedule.cmd` on the already validated CSVs. Do not delete the schedule state as the normal way of pausing; that would allow another run using existing current files.
- **Troubleshooting:** `LINEUP/runtime/lineup_datahub_schedule.log`; `LINEUP/logs/lineup_run_YYYY-MM-DD.log`; `DataHub/logs/controller.log`; `DataHub/logs/controller_session.log`; `DataHub/logs/<job>_last_run.log`.

## Source-control hygiene

Keep live SAP exports, CSVs, source files, logs, runtime state, and backups out of GitHub, even for a private repository where possible. LINEUP's `.gitignore` already ignores many datasets but doesn't cover `/runtime/`; add `/runtime/`. Data Hub v0.6 may not include a `.gitignore`; add `Raw/`, `Live/`, `History/`, `runtime/`, `logs/`, `venv/`, Python caches and temporary files as appropriate. If real SAP files have already been committed, simply adding `.gitignore` does not remove them from existing Git history. Confirm repository visibility and follow company data-handling policy.

## Offline tests included

`python -m unittest discover -s tests -v` runs 8 checks for complete publication, missing schema, schema ordering, four-source advancement, first-run prime, consistent snapshot, failure recovery and successful baseline advancement. These do not test Windows Task Scheduler or SAP GUI; the acceptance checks above are still required.
