from pathlib import Path
import csv
import json
import re
from datetime import datetime
from html import escape

from working_calendar import working_minutes_for_day

PROJECT_ROOT = Path(__file__).parent.parent

BENCHES_FILE = PROJECT_ROOT / "config" / "benches.csv"
NOTES_FILE = PROJECT_ROOT / "config" / "notes.json"
DASHBOARD_SETTINGS_FILE = PROJECT_ROOT / "config" / "dashboard_settings.json"
QUEUE_CONTROL_FILE = PROJECT_ROOT / "config" / "queue_control.json"
STATUS_FILE = PROJECT_ROOT / "data" / "processed" / "lineup_status.json"
SCHEDULED_FILE = PROJECT_ROOT / "data" / "processed" / "scheduled_orders.csv"
OVERFLOW_FILE = PROJECT_ROOT / "data" / "processed" / "overflow_orders.csv"
SPLIT_RECOMMENDATIONS_FILE = PROJECT_ROOT / "data" / "processed" / "split_recommendations.csv"
OUTPUT_FILE = PROJECT_ROOT / "output" / "dashboard.html"

ROTATION_SECONDS = 15
ENABLE_SCREEN_ROTATION = False
MAX_VISIBLE_QUEUE = 3
STALE_WARNING_MINUTES = 45


def read_csv(path):
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as infile:
        return list(csv.DictReader(infile))


def read_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as infile:
            data = json.load(infile)
    except (json.JSONDecodeError, OSError):
        return default
    return data if isinstance(data, dict) else default


def read_notes():
    data = read_json(NOTES_FILE, {"bench_notes": {}, "order_notes": {}})
    data.setdefault("bench_notes", {})
    data.setdefault("order_notes", {})
    return data


def read_dashboard_settings():
    data = read_json(DASHBOARD_SETTINGS_FILE, {"show_split_suggestions": True})
    data.setdefault("show_split_suggestions", True)
    return data


def read_queue_control():
    data = read_json(QUEUE_CONTROL_FILE, {"version": 2, "planning_pool": [], "manual_queues": {}, "held_orders": {}})
    data.setdefault("planning_pool", [])
    data.setdefault("manual_queues", {})
    data.setdefault("held_orders", {})
    return data


def read_lineup_status():
    data = read_json(STATUS_FILE, {})
    return {
        "last_run_status": data.get("last_run_status", "unknown"),
        "last_successful_run": data.get("last_successful_run", ""),
        "last_attempted_run": data.get("last_attempted_run", ""),
        "failure_step": data.get("failure_step", ""),
        "failure_message": data.get("failure_message", ""),
    }


def parse_int(value, default=0):
    try:
        text = str(value or "").strip()
        if not text:
            return default
        return int(float(text))
    except (TypeError, ValueError):
        return default


def parse_float(value, default=0.0):
    try:
        text = str(value or "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def format_duration_minutes(value):
    minutes = parse_int(value, None)
    if minutes is None:
        return "-"
    if minutes < 60:
        return f"{minutes} min"
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h" if remainder == 0 else f"{hours}h {remainder}m"


def format_started_at(value):
    text = str(value or "").strip()
    if not text:
        return "-"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%a %H:%M")
        except ValueError:
            pass
    return text


def format_refresh_at(value):
    text = str(value or "").strip()
    if not text:
        return "-"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%d/%m %H:%M")
        except ValueError:
            pass
    return text


def pool_title(pool):
    return {"CSKI_MVKI": "CSKI / MVKI", "LVPA": "Box / Bag"}.get(pool, pool)


def due_class(order):
    status = str(order.get("due_status") or "UNKNOWN").strip().upper()
    if status in ("EARLY", "AHEAD"):
        return "ahead"
    if status == "ON TARGET":
        return "on-target"
    if status == "LATE":
        return "late"
    return "unknown"


def display_due_status(order):
    status = str(order.get("due_status") or "UNKNOWN").strip().upper()
    return "AHEAD" if status == "EARLY" else status


def is_live_active(order):
    return str(order.get("live_active", "")).strip().lower() == "yes"


def assignment_source(order):
    source = str(order.get("assignment_source", "")).strip().upper()
    if source:
        return source
    override_type = str(order.get("override_type", "")).strip().upper()
    return {
        "PLANNER LOCK": "PLANNER",
        "MANUAL SPLIT": "SPLIT",
        "FORCED BENCH": "FORCED",
        "MAKE CURRENT": "FORCED",
        "AUTO LOCK": "STABLE",
    }.get(override_type, "LIVE" if is_live_active(order) else "OPTIMISED")


def source_badge(order):
    source = assignment_source(order)
    labels = {
        "LIVE": ("Live WIP", "source-live"),
        "PLANNER": ("Planner", "source-planner"),
        "SPLIT": ("Split", "source-split"),
        "FORCED": ("Forced", "source-forced"),
        "STABLE": ("Stable Queue", "source-stable"),
        "OPTIMISED": ("Optimised", "source-optimised"),
    }
    label, css = labels.get(source, (source.title(), "source-optimised"))
    return f'<span class="mini-tag {css}">{escape(label)}</span>'


def confidence_badge(order):
    confidence = str(order.get("schedule_confidence", "MEDIUM") or "MEDIUM").strip().upper()
    labels = {
        "HIGH": ("High confidence", "confidence-high"),
        "MEDIUM": ("Medium confidence", "confidence-medium"),
        "ATTENTION": ("Planner attention", "confidence-attention"),
    }
    label, css = labels.get(confidence, labels["MEDIUM"])
    return f'<span class="mini-tag {css}">{escape(label)}</span>'


def priority_tag(order):
    value = str(order.get("priority", "")).strip()
    return "" if value in ("", "0") else f'<span class="mini-tag priority-tag">P{escape(value)}</span>'


def australia_tag(order):
    return '<span class="mini-tag australia-tag">AU</span>' if str(order.get("is_australia", "")).strip().lower() == "yes" else ""


def parse_split_detail(order):
    if assignment_source(order) != "SPLIT":
        return None
    note = str(order.get("override_note", "") or "").strip()
    part_match = re.search(r"\bpart\s+([A-Z0-9]+)\b", note, re.IGNORECASE)
    qty_match = re.search(r"\bqty\s+([0-9]+)", note, re.IGNORECASE)
    minutes_match = re.search(r"/\s*([0-9]+)\s*min", note, re.IGNORECASE)
    return {
        "part": f"Part {part_match.group(1).upper()}" if part_match else "Split Part",
        "short": part_match.group(1).upper() if part_match else "",
        "quantity": qty_match.group(1) if qty_match else "-",
        "minutes": minutes_match.group(1) if minutes_match else str(order.get("adjusted_remaining_minutes") or order.get("bench_remaining_minutes") or "-"),
        "bench": str(order.get("bench_id", "")).strip(),
        "note": note,
    }


def split_tag(order):
    detail = parse_split_detail(order)
    if not detail:
        return ""
    suffix = f" {detail['short']}" if detail["short"] else ""
    return f'<span class="mini-tag source-split">Split{escape(suffix)}</span>'


def split_detail_panel(order):
    detail = parse_split_detail(order)
    if not detail:
        return ""
    note_html = f'<div class="split-operator-note">{escape(detail["note"])}</div>' if detail["note"] else ""
    return f'''
    <div class="split-detail-panel">
        <div><span>Part</span><strong>{escape(detail["part"])}</strong></div>
        <div><span>Quantity</span><strong>{escape(detail["quantity"])}</strong></div>
        <div><span>Adjusted</span><strong>{escape(format_duration_minutes(detail["minutes"]))}</strong></div>
        <div><span>Bench</span><strong>{escape(detail["bench"])}</strong></div>
    </div>{note_html}
    '''


def order_note_badge(order, notes):
    order_number = str(order.get("order_number", "")).strip()
    note = str(notes.get("order_notes", {}).get(order_number, "") or "").strip()
    return "" if not note else f'<div class="order-note"><span>Order Note</span>{escape(note)}</div>'


def work_start_form(bench_id, order, label="Start Tracking"):
    order_number = str(order.get("order_number", "")).strip()
    if not bench_id or not order_number:
        return ""
    return f'''
    <form method="post" action="/work/start" class="work-action-form">
        <input type="hidden" name="bench_id" value="{escape(bench_id)}">
        <input type="hidden" name="order_number" value="{escape(order_number)}">
        <button type="submit" class="work-button start">{escape(label)}</button>
    </form>
    '''


def work_stop_form(bench_id, order):
    order_number = str(order.get("order_number", "")).strip()
    if not bench_id or not order_number:
        return ""
    return f'''
    <form method="post" action="/work/stop" class="work-action-form">
        <input type="hidden" name="bench_id" value="{escape(bench_id)}">
        <input type="hidden" name="order_number" value="{escape(order_number)}">
        <button type="submit" class="work-button stop" onclick="return confirm('Stop live tracking for order {escape(order_number)}? This does not complete the order in SAP.');">Stop Tracking</button>
    </form>
    '''


def hold_buttons(bench_id, order):
    order_number = str(order.get("order_number", "")).strip()
    if not order_number:
        return ""
    buttons = []
    for reason in ("Shortage", "Quality", "People", "Space"):
        buttons.append(f'''
        <form method="post" action="/hold/add" class="work-action-form">
            <input type="hidden" name="source" value="dashboard">
            <input type="hidden" name="bench_id" value="{escape(bench_id)}">
            <input type="hidden" name="order_number" value="{escape(order_number)}">
            <input type="hidden" name="hold_reason" value="{escape(reason)}">
            <input type="hidden" name="hold_note" value="Held from dashboard: {escape(reason)}">
            <button type="submit" class="hold-reason-button" onclick="return confirm('Put order {escape(order_number)} on hold for {escape(reason)}?');">{escape(reason)}</button>
        </form>
        ''')
    return f'<div class="hold-action-group"><div class="hold-action-label">Hold Reason</div><div class="hold-reason-row">{"".join(buttons)}</div></div>'


def why_button(order):
    payload = {
        "order_number": str(order.get("order_number", "")),
        "material": str(order.get("material", "")),
        "bench_id": str(order.get("bench_id", "")),
        "source": str(order.get("assignment_source_label") or assignment_source(order).title()),
        "reason": str(order.get("assignment_reason", "") or "No detailed reason was recorded."),
        "confidence": str(order.get("schedule_confidence", "MEDIUM")),
        "confidence_reason": str(order.get("confidence_reason", "") or "No confidence explanation was recorded."),
        "queue_stability": str(order.get("queue_stability", "") or "Flexible"),
        "sap_remaining": format_duration_minutes(order.get("sap_remaining_minutes", "")),
        "adjusted_remaining": format_duration_minutes(order.get("adjusted_remaining_minutes", order.get("bench_remaining_minutes", ""))),
        "staffing_people": str(order.get("staffing_override_people", "") or "SAP normal"),
        "staffing_factor": str(order.get("staffing_factor", "1.0") or "1.0"),
        "finish": str(order.get("planned_finish_time", "")),
        "due": str(order.get("due_message", "")),
    }
    encoded = escape(json.dumps(payload, ensure_ascii=False), quote=True)
    return f'<button type="button" class="why-button" data-why="{encoded}" onclick="openWhyPanel(this)">Why?</button>'


def staffing_context(order):
    sap_minutes = parse_int(
        order.get("sap_remaining_minutes", order.get("bench_remaining_minutes", 0)),
        0,
    )
    adjusted_minutes = parse_int(
        order.get("adjusted_remaining_minutes", order.get("bench_remaining_minutes", 0)),
        0,
    )
    factor = parse_float(order.get("staffing_factor", 1.0), 1.0)
    override_people = str(order.get("staffing_override_people", "") or "").strip()

    if override_people and abs(factor - 1.0) > 0.001:
        return (
            f"SAP remaining: {format_duration_minutes(sap_minutes)}. "
            f"Adjusted for {override_people} people (duration factor {factor:.2f})."
        )

    return (
        f"Based on the latest SAP remaining capacity "
        f"({format_duration_minutes(sap_minutes)}). No staffing adjustment."
    )


def remaining_panel(order):
    adjusted_minutes = parse_int(
        order.get("adjusted_remaining_minutes", order.get("bench_remaining_minutes", 0)),
        0,
    )

    return f'''
    <div class="remaining-panel operational-panel">
        <div>
            <span>Remaining</span>
            <strong>{escape(format_duration_minutes(adjusted_minutes))}</strong>
        </div>
        <div>
            <span>Finish Estimate</span>
            <strong>{escape(str(order.get("planned_finish_time", "") or "-"))}</strong>
        </div>
    </div>
    <div class="calculation-note">{escape(staffing_context(order))}</div>
    '''


def live_work_panel(order):
    if not is_live_active(order):
        return ""

    remaining_minutes = parse_int(
        order.get("adjusted_remaining_minutes", order.get("bench_remaining_minutes", 0)),
        0,
    )
    started_at = str(order.get("live_started_at", "") or "").strip()
    generated_at = datetime.now().isoformat(timespec="seconds")

    return f'''
    <div
        class="live-work-panel live-dynamic-panel"
        data-live-started-at="{escape(started_at, quote=True)}"
        data-live-base-at="{escape(generated_at, quote=True)}"
        data-live-base-remaining="{remaining_minutes}"
    >
        <div>
            <span>Started</span>
            <strong>{escape(format_started_at(started_at))}</strong>
        </div>
        <div>
            <span>Time Worked</span>
            <strong class="live-time-worked">-</strong>
        </div>
        <div>
            <span>Remaining</span>
            <strong class="live-remaining">{escape(format_duration_minutes(remaining_minutes))}</strong>
        </div>
        <div>
            <span>Finish Estimate</span>
            <strong>{escape(str(order.get("planned_finish_time", "") or "-"))}</strong>
        </div>
    </div>
    <div class="calculation-note live-calculation-note">
        {escape(staffing_context(order))}
    </div>
    '''


def load_open_benches():
    benches = {}
    for row in read_csv(BENCHES_FILE):
        if str(row.get("is_open", "")).strip().lower() != "yes":
            continue
        bench_id = str(row.get("bench_id", "")).strip()
        if not bench_id:
            continue
        benches[bench_id] = {
            "bench_id": bench_id,
            "bench_name": str(row.get("bench_name", "")).strip(),
            "scheduling_pool": str(row.get("scheduling_pool", "")).strip(),
            "orders": [],
        }
    return benches


def add_orders_to_benches(benches, rows):
    for row in rows:
        bench_id = str(row.get("bench_id", "")).strip()
        if not bench_id:
            continue
        benches.setdefault(bench_id, {
            "bench_id": bench_id,
            "bench_name": str(row.get("bench_name", "")).strip(),
            "scheduling_pool": str(row.get("scheduling_pool", "")).strip(),
            "orders": [],
        })
        benches[bench_id]["orders"].append(row)
    for bench in benches.values():
        bench["orders"].sort(key=lambda row: parse_int(row.get("sequence", 0), 0))
    return benches


def bench_metrics(bench):
    total_minutes = sum(parse_int(order.get("adjusted_remaining_minutes", order.get("bench_remaining_minutes", 0)), 0) for order in bench["orders"])
    today_capacity = working_minutes_for_day(datetime.now().date())
    today_load = min(total_minutes, today_capacity)
    future_queue = max(0, total_minutes - today_capacity)
    utilisation = round((today_load / today_capacity) * 100) if today_capacity else 0
    late_count = sum(1 for order in bench["orders"] if due_class(order) == "late")
    attention_count = sum(1 for order in bench["orders"] if str(order.get("schedule_confidence", "")).upper() == "ATTENTION")
    if attention_count or late_count:
        confidence = "attention"
        confidence_text = "Planner attention"
    elif bench["orders"] and str(bench["orders"][0].get("schedule_confidence", "MEDIUM")).upper() == "HIGH":
        confidence = "high"
        confidence_text = "High confidence"
    elif bench["orders"]:
        confidence = "medium"
        confidence_text = "Medium confidence"
    else:
        confidence = "idle"
        confidence_text = "Available"
    return {
        "total_minutes": total_minutes,
        "today_capacity": today_capacity,
        "today_load": today_load,
        "future_queue": future_queue,
        "utilisation": utilisation,
        "late_count": late_count,
        "confidence": confidence,
        "confidence_text": confidence_text,
    }


def current_order_block(order, bench_id, notes):
    if not order:
        return '<div class="current-block empty-current"><div class="block-label">Current</div><div class="empty-text">No order currently queued</div></div>'
    status = due_class(order)
    action = work_stop_form(bench_id, order) if is_live_active(order) else work_start_form(bench_id, order)
    label = "Current - Live" if is_live_active(order) else "Current"
    return f'''
    <div class="current-block {status}">
        <div class="current-top"><div class="block-label">{escape(label)}</div><div class="tag-row">{source_badge(order)}{confidence_badge(order)}{split_tag(order)}{australia_tag(order)}{priority_tag(order)}</div></div>
        <div class="current-order-number">{escape(order.get("order_number", ""))}</div>
        <div class="current-material">{escape(order.get("material", ""))}</div>
        <div class="current-description">{escape(order.get("material_description", ""))}</div>
        <div class="current-time">{escape(order.get("planned_start_time", ""))} to {escape(order.get("planned_finish_time", ""))}</div>
        {live_work_panel(order) if is_live_active(order) else remaining_panel(order)}
        {split_detail_panel(order)}
        <div class="due-line">{escape(display_due_status(order))} - {escape(order.get("due_message", ""))}</div>
        <div class="why-row">{why_button(order)}</div>
        <div class="work-action-panel"><div class="main-work-action">{action}</div>{hold_buttons(bench_id, order)}</div>
        {order_note_badge(order, notes)}
    </div>
    '''


def queue_order_row(order, bench_id, position, notes, live_locked=False):
    label = "Next" if position == 1 else f"Queue {position}"
    action = '<div class="queue-lock-note">Live WIP locked</div>' if live_locked else work_start_form(bench_id, order, "Start This")
    return f'''
    <div class="queue-row {due_class(order)} {'next-row' if position == 1 else ''}">
        <div class="queue-position">{escape(label)}</div>
        <div class="queue-main">
            <div class="queue-order-number">{escape(order.get("order_number", ""))}</div>
            <div class="queue-material">{escape(order.get("material", ""))}</div>
            <div class="tag-row">{source_badge(order)}{confidence_badge(order)}{split_tag(order)}{australia_tag(order)}{priority_tag(order)}</div>
            <div class="queue-meta"><div class="queue-time"><div>{escape(order.get("planned_start_time", ""))}</div><span>to {escape(order.get("planned_finish_time", ""))}</span></div><div class="queue-status">{escape(display_due_status(order))}</div></div>
            <div class="queue-summary"><span>Remaining {escape(format_duration_minutes(order.get("adjusted_remaining_minutes", order.get("bench_remaining_minutes"))))}</span><span>Finish {escape(str(order.get("planned_finish_time", "") or "-"))}</span></div>
            <div class="why-row">{why_button(order)}</div>
            {split_detail_panel(order)}
            {order_note_badge(order, notes)}
            <div class="queue-action-panel compact"><div class="main-work-action">{action}</div>{hold_buttons(bench_id, order)}</div>
        </div>
    </div>
    '''


def bench_card(bench, notes):
    orders = bench["orders"]
    current = orders[0] if orders else None
    queue_orders = orders[1:1 + MAX_VISIBLE_QUEUE]
    hidden_count = max(0, len(orders) - 1 - len(queue_orders))
    metrics = bench_metrics(bench)
    queue_html = "".join(queue_order_row(order, bench["bench_id"], index, notes, bool(current and is_live_active(current))) for index, order in enumerate(queue_orders, start=1))
    if not queue_html:
        queue_html = '<div class="empty-queue">No further queue</div>'
    if hidden_count:
        queue_html += f'<div class="more-queue">+ {hidden_count} more queued</div>'
    bench_note = str(notes.get("bench_notes", {}).get(bench["bench_id"], "") or "").strip()
    bench_note_html = f'<div class="bench-note"><span>Note</span>{escape(bench_note)}</div>' if bench_note else ""
    return f'''
    <article class="bench-card {metrics['confidence']}">
        <div class="bench-head"><div><div class="bench-name">{escape(bench["bench_name"])}</div><div class="bench-id">{escape(bench["bench_id"])}</div></div><div class="confidence-pill {metrics['confidence']}">{escape(metrics['confidence_text'])}</div></div>
        <div class="utilisation-panel">
            <div><span>Today Load</span><strong>{format_duration_minutes(metrics['today_load'])}</strong></div>
            <div><span>Capacity</span><strong>{format_duration_minutes(metrics['today_capacity'])}</strong></div>
            <div><span>Utilisation</span><strong>{metrics['utilisation']}%</strong></div>
            <div><span>Future Queue</span><strong>{format_duration_minutes(metrics['future_queue'])}</strong></div>
        </div>
        {bench_note_html}
        {current_order_block(current, bench["bench_id"], notes)}
        <div class="queue-list">{queue_html}</div>
    </article>
    '''


def build_pool_screens(benches, notes):
    pools = {}
    for bench in benches.values():
        pools.setdefault(bench["scheduling_pool"], []).append(bench)
    screens, nav = [], []
    for index, pool in enumerate(sorted(pools)):
        pool_benches = sorted(pools[pool], key=lambda item: item["bench_id"])
        orders = [order for bench in pool_benches for order in bench["orders"]]
        active = "active" if index == 0 else ""
        screens.append(f'''
        <section class="screen {active}" data-screen-index="{index}">
            <div class="screen-title-row"><div><h2>{escape(pool_title(pool))}</h2><p>{len(pool_benches)} open benches</p></div><div class="pool-metrics"><div><span>Orders</span><strong>{len(orders)}</strong></div><div><span>Live</span><strong>{sum(1 for order in orders if is_live_active(order))}</strong></div><div><span>Late</span><strong>{sum(1 for order in orders if due_class(order) == 'late')}</strong></div><div><span>Attention</span><strong>{sum(1 for order in orders if str(order.get('schedule_confidence', '')).upper() == 'ATTENTION')}</strong></div></div></div>
            <div class="bench-board">{"".join(bench_card(bench, notes) for bench in pool_benches)}</div>
        </section>
        ''')
        nav.append(f'<button class="screen-dot {active}" data-screen-target="{index}">{escape(pool_title(pool))}</button>')
    return "".join(screens), "".join(nav)


def held_orders_panel(queue_control):
    held = queue_control.get("held_orders", {})
    if not held:
        return ""
    cards = []
    for order_number, data in held.items():
        cards.append(f'<div class="held-order-card"><strong>{escape(order_number)}</strong><span>{escape(str(data.get("reason", "On hold")))} - {escape(str(data.get("bench_id", "")))}</span><div class="held-note">{escape(str(data.get("note", "")))}</div></div>')
    return f'<section class="held-panel"><div class="held-panel-heading"><div><h2>Held Orders</h2><p>Removed from scheduling until released in the Control Centre.</p></div><strong>{len(held)}</strong></div><div class="held-card-grid">{"".join(cards)}</div></section>'


def split_recommendations_panel(rows):
    if not rows:
        return ""
    cards = []
    for index, row in enumerate(rows):
        hidden = "split-hidden-card" if index >= 4 else ""
        reason = str(row.get("recommendation_reason", "") or row.get("recommendation_message", ""))
        cards.append(f'''
        <div class="split-card {hidden}">
            <div class="split-card-top"><div><strong>{escape(row.get("order_number", ""))}</strong><span>{escape(row.get("material", ""))}</span></div><div class="split-status">{escape(row.get("recommendation_status", ""))}</div></div>
            <div class="split-message">{escape(reason)}</div>
            <div class="split-detail-grid">
                <div><span>Current Bench</span><strong>{escape(row.get("current_bench_id", ""))}</strong></div>
                <div><span>Second Bench</span><strong>{escape(row.get("recommended_second_bench_id", ""))}</strong></div>
                <div><span>Current Finish</span><strong>{escape(row.get("current_finish_time", ""))}</strong></div>
                <div><span>After Split</span><strong>{escape(row.get("recommended_finish_time", ""))}</strong></div>
                <div><span>Quantity Split</span><strong>{escape(str(row.get("current_bench_quantity", "")))} / {escape(str(row.get("second_bench_quantity", "")))}</strong></div>
                <div><span>Adjusted Time</span><strong>{escape(format_duration_minutes(row.get("current_bench_minutes")))} / {escape(format_duration_minutes(row.get("second_bench_minutes")))}</strong></div>
            </div>
            <form method="post" action="/split/apply" class="split-action-form"><input type="hidden" name="order_number" value="{escape(row.get("order_number", ""))}"><button type="submit">Apply Split</button></form>
        </div>
        ''')
    more = max(0, len(rows) - 4)
    more_button = f'<button type="button" class="split-more" id="split-show-more-button">+ {more} more recommendation(s)</button>' if more else ""
    return f'<section class="split-panel"><div class="split-panel-heading"><div><h2>Split Recommendations</h2><p>Only late automatic work due today or next working day.</p></div><div class="split-heading-actions"><strong>{len(rows)}</strong><button type="button" id="split-toggle-button">Hide</button></div></div><div class="split-card-grid">{"".join(cards)}{more_button}</div></section>'


def status_warning_panel(status):
    panels = []
    if status["last_run_status"] == "failed":
        panels.append(f'<div class="system-banner danger"><strong>Refresh failed</strong><span>Last success: {escape(status["last_successful_run"] or "unknown")} - Step: {escape(status["failure_step"] or "unknown")} - {escape(status["failure_message"])}</span></div>')
    elif status["last_run_status"] == "running":
        panels.append(f'<div class="system-banner warning"><strong>Refreshing</strong><span>Last attempt: {escape(status["last_attempted_run"] or "unknown")}</span></div>')
    panels.append('<div class="system-banner danger hidden" id="stale-warning"><strong>Dashboard may be stale</strong><span id="stale-warning-text"></span></div>')
    return "".join(panels)


def build_dashboard():
    scheduled_rows = read_csv(SCHEDULED_FILE)
    overflow_rows = read_csv(OVERFLOW_FILE)
    split_rows = read_csv(SPLIT_RECOMMENDATIONS_FILE)
    notes = read_notes()
    settings = read_dashboard_settings()
    queue_control = read_queue_control()
    status = read_lineup_status()

    benches = add_orders_to_benches(load_open_benches(), scheduled_rows)
    screens_html, nav_html = build_pool_screens(benches, notes)
    split_html = split_recommendations_panel(split_rows) if settings.get("show_split_suggestions", True) else ""
    held_html = held_orders_panel(queue_control)

    generated_at = datetime.now()
    generated_text = generated_at.strftime("%d/%m/%Y %H:%M:%S")
    generated_iso = generated_at.isoformat(timespec="seconds")
    late_total = sum(1 for row in scheduled_rows if due_class(row) == "late")
    attention_total = sum(1 for row in scheduled_rows if str(row.get("schedule_confidence", "")).upper() == "ATTENTION")
    live_total = sum(1 for row in scheduled_rows if is_live_active(row))

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>LINEUP Production Control Board</title> <style> *{{box-sizing:border-box}}:root{{--paper:#f7f3ec;--card:#fffdf8;--ink:#1c1917;--muted:#746d66;--line:#ded6ca;--fine:#e8dfd3;--accent:#b45309;--danger:#991b1b;--success:#166534;--warning:#b45309;--shadow:rgba(43,33,24,.08);--sans:Arial,Helvetica,sans-serif;--serif:Georgia,"Times New Roman",serif}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:var(--serif)}}.page{{min-height:100vh;padding:20px 26px}}.topbar{{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;padding-bottom:15px;border-bottom:2px solid var(--ink)}}.brand h1{{margin:0;font-size:44px;letter-spacing:4px}}.brand span{{font:700 14px var(--sans);color:var(--accent);letter-spacing:2px;text-transform:uppercase}}.timestamp-actions{{display:flex;align-items:flex-end;gap:12px}}.top-link{{display:inline-flex;align-items:center;min-height:36px;padding:0 13px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--ink);font:900 11px var(--sans);text-decoration:none;box-shadow:0 5px 14px var(--shadow)}}.top-link:hover{{border-color:var(--accent);color:var(--accent)}}.timestamp{{font:13px var(--sans);color:var(--muted);text-align:right}}.kpi-row,.pool-metrics{{display:flex;gap:20px;margin-top:12px;font-family:var(--sans)}}.kpi span,.pool-metrics span{{display:block;font-size:10px;text-transform:uppercase;color:var(--muted);letter-spacing:.8px}}.kpi strong,.pool-metrics strong{{display:block;font-size:21px;margin-top:2px}}.system-banner,.held-panel,.split-panel{{margin-top:12px;padding:12px 14px;border:1px solid var(--line);background:var(--card);box-shadow:0 8px 22px var(--shadow)}}.system-banner{{display:flex;justify-content:space-between;gap:15px;font:800 12px var(--sans)}}.system-banner.danger{{border-left:5px solid var(--danger);background:#fff5f5;color:#7f1d1d}}.system-banner.warning{{border-left:5px solid var(--warning);background:#fff7ed;color:#7c2d12}}.hidden{{display:none!important}}.held-panel{{border-left:5px solid var(--danger);background:#fff5f5}}.held-panel-heading,.split-panel-heading,.screen-title-row,.bench-head,.current-top,.split-card-top{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}.held-panel-heading h2,.split-panel-heading h2,.screen-title-row h2{{margin:0}}.held-panel-heading p,.split-panel-heading p,.screen-title-row p{{margin:3px 0 0;color:var(--muted);font:12px var(--sans)}}.held-card-grid,.split-card-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:9px;margin-top:10px}}.held-order-card,.split-card{{padding:10px;border:1px solid var(--fine);background:white;font-family:var(--sans)}}.held-order-card strong,.split-card strong{{display:block}}.held-order-card span,.held-note,.split-card span{{display:block;margin-top:3px;color:var(--muted);font-size:11px}}.split-heading-actions{{display:flex;gap:10px;align-items:center;font-family:var(--sans)}}.split-heading-actions button,.split-more{{border:1px solid var(--line);background:white;padding:7px 10px;border-radius:999px;font-weight:800;cursor:pointer}}.split-card-grid{{grid-template-columns:repeat(auto-fit,minmax(360px,1fr))}}.split-status{{font:900 10px var(--sans);color:var(--accent);text-transform:uppercase}}.split-message{{margin-top:8px;font:800 12px/1.4 var(--sans);color:#57534e}}.split-detail-grid,.split-detail-panel,.remaining-panel,.live-work-panel,.utilisation-panel{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:6px;margin-top:8px;font-family:var(--sans)}}.split-detail-grid div,.split-detail-panel div,.remaining-panel div,.live-work-panel div,.utilisation-panel div{{padding:7px;border:1px solid var(--fine);background:#fbf7f0}}.split-detail-grid span,.split-detail-panel span,.remaining-panel span,.live-work-panel span,.utilisation-panel span{{display:block;font-size:9px;text-transform:uppercase;color:var(--muted);font-weight:900;letter-spacing:.5px}}.split-detail-grid strong,.split-detail-panel strong,.remaining-panel strong,.live-work-panel strong,.utilisation-panel strong{{display:block;margin-top:2px;font-size:12px}}.split-action-form{{margin-top:9px}}.split-action-form button{{border:0;background:var(--ink);color:white;padding:8px 12px;border-radius:999px;font-weight:900;cursor:pointer}}.screens{{margin-top:16px}}.screen{{display:none}}.screen.active{{display:block}}.screen-title-row{{align-items:flex-end;margin-bottom:12px}}.bench-board{{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:14px}}.bench-card{{background:var(--card);border:1px solid var(--line);border-top:5px solid #57534e;padding:14px;box-shadow:0 8px 22px var(--shadow)}}.bench-card.attention{{border-top-color:var(--danger)}}.bench-card.high{{border-top-color:var(--success)}}.bench-card.medium{{border-top-color:var(--warning)}}.bench-card.idle{{opacity:.75;border-top-color:#a8a29e}}.bench-name{{font-size:22px;font-weight:700}}.bench-id{{font:11px var(--sans);color:var(--muted)}}.confidence-pill{{font:900 10px var(--sans);padding:6px 9px;border-radius:999px;border:1px solid var(--line)}}.confidence-pill.high{{color:var(--success);background:#ecfdf3;border-color:#bbf7d0}}.confidence-pill.medium{{color:#7c2d12;background:#fff7ed;border-color:#fed7aa}}.confidence-pill.attention{{color:var(--danger);background:#fff5f5;border-color:#fecaca}}.bench-note,.order-note,.split-operator-note{{margin-top:8px;padding:8px 9px;border:1px solid var(--fine);background:#fbf7f0;border-radius:8px;font:800 11px var(--sans)}}.bench-note span,.order-note span{{display:block;font-size:9px;text-transform:uppercase;color:var(--accent)}}.current-block{{border-left:4px solid var(--accent);padding:11px 0 11px 12px;margin-top:10px}}.current-block.late{{border-left-color:var(--danger)}}.block-label,.queue-position{{font:900 10px var(--sans);color:var(--accent);text-transform:uppercase;letter-spacing:.7px}}.current-order-number{{font:800 20px var(--sans);margin-top:5px}}.current-material{{font:800 26px var(--sans);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.current-description{{font:12px var(--sans);color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.current-time{{font:800 15px var(--sans);margin-top:7px}}.due-line{{font:11px var(--sans);color:var(--muted);margin-top:6px}}.tag-row{{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}}.mini-tag{{font:800 9px var(--sans);padding:4px 7px;border-radius:999px;border:1px solid var(--line);background:var(--paper)}}.source-live,.confidence-high{{color:var(--success);background:#ecfdf3;border-color:#bbf7d0}}.source-planner{{color:#1d4ed8;background:#eff6ff;border-color:#bfdbfe}}.source-split{{color:#7c2d12;background:#fff7ed;border-color:#fed7aa}}.source-forced{{color:#7e22ce;background:#faf5ff;border-color:#e9d5ff}}.source-stable,.confidence-medium{{color:#7c2d12;background:#fff7ed;border-color:#fed7aa}}.source-optimised{{color:#57534e}}.confidence-attention,.priority-tag{{color:var(--danger);background:#fff5f5;border-color:#fecaca}}.australia-tag{{background:#ead7bd}}.remaining-note{{grid-column:span 1}}.operational-panel div:first-child strong{{font-size:18px}}.calculation-note{{margin-top:6px;padding:7px 9px;border:1px solid var(--fine);background:#fbf7f0;border-radius:8px;font:800 10px/1.35 var(--sans);color:#57534e}}.live-calculation-note{{border-color:#bbf7d0;background:#f0fdf4;color:#166534}}.live-work-panel div{{background:#ecfdf3;border-color:#bbf7d0}}.split-detail-panel div,.split-operator-note{{background:#fff7ed;border-color:#fed7aa;color:#7c2d12}}.why-row{{margin-top:7px}}.why-button{{border:1px solid var(--line);background:white;padding:6px 10px;border-radius:999px;font:900 10px var(--sans);cursor:pointer}}.work-action-panel,.queue-action-panel{{margin-top:9px;padding:8px;border:1px solid var(--fine);background:#fbf7f0;border-radius:9px;display:grid;grid-template-columns:auto 1fr;gap:10px}}.work-action-form{{margin:0}}.work-button,.hold-reason-button{{border:0;border-radius:999px;padding:7px 10px;font:900 10px var(--sans);cursor:pointer}}.work-button.start{{background:var(--success);color:white}}.work-button.stop{{background:var(--danger);color:white}}.hold-action-label{{font:900 9px var(--sans);text-transform:uppercase;color:var(--muted);margin-bottom:4px}}.hold-reason-row{{display:flex;gap:5px;flex-wrap:wrap}}.hold-reason-button{{border:1px solid #fecaca;background:#fff5f5;color:var(--danger)}}.queue-list{{border-top:1px solid var(--fine);margin-top:9px;padding-top:8px}}.queue-row{{display:grid;grid-template-columns:62px 1fr;gap:9px;padding:8px 0;border-bottom:1px solid var(--fine)}}.next-row{{background:#fbf2e5;margin:0 -7px;padding:9px 7px;border-radius:8px}}.queue-order-number,.queue-material{{font:800 13px var(--sans)}}.queue-meta{{display:flex;justify-content:space-between;gap:8px;margin-top:4px;font:800 10px var(--sans)}}.queue-time span{{display:block;color:var(--muted)}}.queue-summary{{display:flex;gap:9px;margin-top:5px;font:800 10px var(--sans);color:#57534e}}.queue-lock-note{{display:inline-block;padding:6px 9px;border-radius:999px;background:#ecfdf3;color:var(--success);font:900 9px var(--sans)}}.empty-current,.empty-queue,.more-queue{{font:12px var(--sans);color:var(--muted);padding:8px 0}}.screen-nav{{display:flex;justify-content:center;gap:7px;margin-top:14px}}.screen-dot{{border:1px solid var(--line);background:white;padding:7px 11px;border-radius:999px;font:800 11px var(--sans);cursor:pointer}}.screen-dot.active{{background:var(--ink);color:white}}.why-backdrop{{display:none;position:fixed;inset:0;background:rgba(28,25,23,.45);z-index:100;justify-content:flex-end}}.why-backdrop.open{{display:flex}}.why-panel{{width:min(440px,100%);height:100%;background:var(--card);padding:20px;overflow:auto;box-shadow:-12px 0 30px rgba(0,0,0,.18)}}.why-panel-head{{display:flex;justify-content:space-between;gap:12px}}.why-panel h2{{margin:0}}.why-panel button{{border:1px solid var(--line);background:white;border-radius:999px;padding:6px 9px;cursor:pointer}}.why-section{{margin-top:15px;padding-top:12px;border-top:1px solid var(--fine);font-family:var(--sans)}}.why-section span{{display:block;font-size:9px;text-transform:uppercase;color:var(--muted);font-weight:900}}.why-section strong,.why-section p{{display:block;margin:4px 0 0;font-size:13px;line-height:1.45}}.split-hidden-card{{display:none}}.split-panel.show-all-splits .split-hidden-card{{display:block}}.split-panel.collapsed .split-card-grid{{display:none}}@media(max-width:900px){{.bench-board{{grid-template-columns:1fr}}.topbar,.screen-title-row{{display:block}}.timestamp{{text-align:left;margin-top:10px}}.work-action-panel{{grid-template-columns:1fr}}}}@media(max-width:600px){{.page{{padding:12px}}.queue-row{{grid-template-columns:1fr}}.kpi-row,.pool-metrics{{flex-wrap:wrap}}}}
</style>
</head>
<body data-generated-at="{generated_iso}" data-lineup-published-ms="{int(generated_at.timestamp() * 1000)}" data-stale-warning-minutes="{STALE_WARNING_MINUTES}">
<div class="page">
<header class="topbar"><div><div class="brand"><h1>LINEUP</h1><span>Production Control Board</span></div><div class="kpi-row"><div class="kpi"><span>Open Benches</span><strong>{len(benches)}</strong></div><div class="kpi"><span>Queued Orders</span><strong>{len(scheduled_rows)}</strong></div><div class="kpi"><span>Live</span><strong>{live_total}</strong></div><div class="kpi"><span>Held</span><strong>{len(queue_control.get("held_orders", {}))}</strong></div><div class="kpi"><span>Attention</span><strong>{attention_total}</strong></div><div class="kpi"><span>Late</span><strong>{late_total}</strong></div></div></div><div class="timestamp-actions"><a class="top-link" href="/logistics">Open Logistics</a><a class="top-link" href="/production-plans">Open Production Plans</a><a class="top-link" href="/control">Open Control Board</a><div class="timestamp">Generated<br><strong>{generated_text}</strong></div></div></header>
{status_warning_panel(status)}
{f'<div class="system-banner warning"><strong>Unassigned work</strong><span>{len(overflow_rows)} order(s) have no open compatible bench.</span></div>' if overflow_rows else ''} {held_html}{split_html} <main class="screens">{screens_html}</main><nav class="screen-nav">{nav_html}</nav>
</div>
<div class="why-backdrop" id="why-backdrop" onclick="closeWhyPanel(event)"><aside class="why-panel" onclick="event.stopPropagation()"><div class="why-panel-head"><div><h2 id="why-title">Why?</h2><div id="why-subtitle"></div></div><button type="button" onclick="closeWhyPanel()">Close</button></div><div class="why-section"><span>Assignment</span><strong id="why-source"></strong><p id="why-reason"></p></div><div class="why-section"><span>Queue Stability</span><strong id="why-stability"></strong></div><div class="why-section"><span>Confidence</span><strong id="why-confidence"></strong><p id="why-confidence-reason"></p></div><div class="why-section"><span>Remaining Time</span><p id="why-remaining"></p></div><div class="why-section"><span>Predicted Finish</span><strong id="why-finish"></strong><p id="why-due"></p></div></aside></div>
<script>
const screens=[...document.querySelectorAll('.screen')],dots=[...document.querySelectorAll('.screen-dot')];let currentScreen=0;function showScreen(index){{if(!screens.length)return;currentScreen=index%screens.length;screens.forEach((s,i)=>s.classList.toggle('active',i===currentScreen));dots.forEach((d,i)=>d.classList.toggle('active',i===currentScreen))}}dots.forEach(d=>d.addEventListener('click',()=>showScreen(parseInt(d.dataset.screenTarget,10))));if({str(ENABLE_SCREEN_ROTATION).lower()}&&screens.length>1)setInterval(()=>showScreen(currentScreen+1),{ROTATION_SECONDS*1000});
const splitPanel=document.querySelector('.split-panel'),splitToggle=document.getElementById('split-toggle-button'),splitMore=document.getElementById('split-show-more-button');if(splitPanel&&splitToggle){{if(localStorage.getItem('lineupSplitPanelCollapsed')==='yes'){{splitPanel.classList.add('collapsed');splitToggle.textContent='Show'}}splitToggle.addEventListener('click',()=>{{splitPanel.classList.toggle('collapsed');const collapsed=splitPanel.classList.contains('collapsed');splitToggle.textContent=collapsed?'Show':'Hide';localStorage.setItem('lineupSplitPanelCollapsed',collapsed?'yes':'no')}})}}if(splitPanel&&splitMore){{const original=splitMore.textContent;splitMore.addEventListener('click',()=>{{const all=splitPanel.classList.toggle('show-all-splits');splitMore.textContent=all?'Show fewer recommendation(s)':original}})}} function openWhyPanel(button){{let data={{}};try{{data=JSON.parse(button.dataset.why||'{{}}')}}catch(error){{console.error(error)}}document.getElementById('why-title').textContent='Order '+(data.order_number||'');document.getElementById('why-subtitle').textContent=(data.material||'')+' - '+(data.bench_id||'');document.getElementById('why-source').textContent=data.source||'-';document.getElementById('why-reason').textContent=data.reason||'-';document.getElementById('why-stability').textContent=data.queue_stability||'-';document.getElementById('why-confidence').textContent=data.confidence||'-';document.getElementById('why-confidence-reason').textContent=data.confidence_reason||'-';document.getElementById('why-remaining').textContent=(data.staffing_people&&data.staffing_people!=='SAP normal')?('Remaining '+(data.adjusted_remaining||'-')+'. SAP reported '+(data.sap_remaining||'-')+'. Staffing override: '+data.staffing_people+' people, factor '+(data.staffing_factor||'1.0')+'.'):('Remaining '+(data.adjusted_remaining||data.sap_remaining||'-')+'. Based on the latest SAP capacity with normal staffing.');document.getElementById('why-finish').textContent=data.finish||'-';document.getElementById('why-due').textContent=data.due||'-';document.getElementById('why-backdrop').classList.add('open')}}function closeWhyPanel(event){{if(event&&event.target.id!=='why-backdrop')return;document.getElementById('why-backdrop').classList.remove('open')}}
function formatMinutes(value){{const minutes=Math.max(0,Math.floor(Number(value)||0));if(minutes<60)return minutes+' min';const hours=Math.floor(minutes/60),remainder=minutes%60;return remainder?hours+'h '+remainder+'m':hours+'h'}} function workingBlocksForDate(date){{const day=date.getDay();if(day===0||day===6)return[];if(day===5)return[[7*60+45,10*60],[10*60+10,13*60+10]];return[[7*60+45,10*60],[10*60+10,12*60+30],[13*60+15,16*60+35]]}}
function workingMinutesBetween(start,end){{if(!start||!end||end<=start)return 0;let total=0;const cursor=new Date(start.getFullYear(),start.getMonth(),start.getDate());const finalDay=new Date(end.getFullYear(),end.getMonth(),end.getDate());while(cursor<=finalDay){{const blocks=workingBlocksForDate(cursor);const startMinute=(cursor.toDateString()===start.toDateString())?start.getHours()*60+start.getMinutes():0;const endMinute=(cursor.toDateString()===end.toDateString())?end.getHours()*60+end.getMinutes():24*60;blocks.forEach(([blockStart,blockEnd])=>{{const overlapStart=Math.max(startMinute,blockStart),overlapEnd=Math.min(endMinute,blockEnd);if(overlapEnd>overlapStart)total+=overlapEnd-overlapStart}});cursor.setDate(cursor.getDate()+1)}}return Math.max(0,total)}} function updateLivePanels(){{const now=new Date();document.querySelectorAll('.live-dynamic-panel').forEach(panel=>{{const started=new Date(panel.dataset.liveStartedAt||''),baseAt=new Date(panel.dataset.liveBaseAt||''),baseRemaining=parseInt(panel.dataset.liveBaseRemaining||'0',10);const workedElement=panel.querySelector('.live-time-worked'),remainingElement=panel.querySelector('.live-remaining');if(workedElement&&!Number.isNaN(started.getTime()))workedElement.textContent=formatMinutes(workingMinutesBetween(started,now));if(remainingElement&&!Number.isNaN(baseAt.getTime()))remainingElement.textContent=formatMinutes(Math.max(0,baseRemaining-workingMinutesBetween(baseAt,now)))}})}}
updateLivePanels();setInterval(updateLivePanels,60000);
function updateStaleWarning(){{const warning=document.getElementById('stale-warning'),text=document.getElementById('stale-warning-text');if(!warning||!text)return;const generated=new Date(document.body.dataset.generatedAt),limit=parseInt(document.body.dataset.staleWarningMinutes||'45',10);if(Number.isNaN(generated.getTime()))return;const age=Math.floor((Date.now()-generated.getTime())/60000);warning.classList.toggle('hidden',age<limit);if(age>=limit)text.textContent='Generated '+age+' minutes ago. Check the LINEUP update log before relying on it.'}}updateStaleWarning();setInterval(updateStaleWarning,60000);
</script>
<script src="/static/lineup_auto_refresh.js?v=1.0" defer></script>
</body></html>'''

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8", newline="") as outfile:
        outfile.write(html)

    print()
    print("===================================")
    print("LINEUP Dashboard Build Complete")
    print("===================================")
    print(f"Input       : {SCHEDULED_FILE.name}")
    print(f"Output      : {OUTPUT_FILE}")
    print("Insights    : assignment reasons and confidence")
    print("Remaining   : SAP versus adjusted")
    print("Utilisation : working-calendar aware")
    print()


build_dashboard()