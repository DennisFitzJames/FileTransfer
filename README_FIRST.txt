SHARED SAP DATA HUB -> LINEUP SCHEDULING PILOT

START HERE: INSTALL_AND_TEST.md

Copy the DataHub/ script to your Data Hub root and the LINEUP/ scripts
to your LINEUP root. Do NOT replace existing config, data, or exporters.

1) Enable the four LINEUP exports in Data Hub; pause/resume using dashboard.
2) In LINEUP run check_lineup_schedule.cmd and prime_lineup_schedule.cmd.
3) Refresh the 4 Data Hub exports; run run_lineup_scheduled.cmd twice.
   First should run LINEUP, second should skip.
4) Only after success create two Windows Scheduled Tasks as in guide.

The scheduling bridge uses file-based completion markers, not a LAN-exposed
Data Hub API; it works across machines that share the same Live dataset root.

This is a tested *offline* pilot, not a guarantee of unattended SAP GUI operation.
