from pathlib import Path
import sys
import csv
import json
import subprocess
from datetime import datetime

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    send_file,
    jsonify,
    make_response,
)

PROJECT_ROOT = Path(__file__).parent
APP_FOLDER = PROJECT_ROOT / "app"

if str(APP_FOLDER) not in sys.path:
    sys.path.insert(0, str(APP_FOLDER))

from overrides import load_overrides, save_overrides, prune_terminal_manual_splits
from live_work import (
    start_order_on_bench,
    stop_order_on_bench,
    clear_all_live_work,
    get_bench_live_state,
    get_active_benches,
)
from queue_control import (
    load_queue_control,
    save_queue_control,
    add_order_to_planning_pool,
    remove_order_from_planning_pool,
    clear_planning_pool,
    add_order_to_manual_queue,
    remove_order_from_manual_queue,
    move_order_in_manual_queue,
    clear_manual_queues,
    add_held_order,
    remove_held_order,
    release_held_order_to_queue,
)
from staffing_overrides import (
    load_staffing_overrides,
    set_staffing_override,
    clear_staffing_override,
    clear_all_staffing_overrides,
    staffing_factor_for_bench,
)
from planning_board_support import (
    build_order_board_metadata,
    validate_planning_board_payload,
    board_state_for_client,
)


from order_repository import (
    load_master_orders,
    load_ready_orders,
    load_awaiting_picking_orders,
    load_order_lookup as load_master_order_lookup,
)

from logistics.routes import logistics_bp
from production_plans.routes import production_plans_bp

app = Flask(
    __name__,
    template_folder=str(APP_FOLDER / "templates"),
    static_folder=str(APP_FOLDER / "static"),
)

app.register_blueprint(logistics_bp)
app.register_blueprint(production_plans_bp)

BENCHES_FILE = PROJECT_ROOT / "config" / "benches.csv"
NOTES_FILE = PROJECT_ROOT / "config" / "notes.json"
MASTER_FILE = PROJECT_ROOT / "data" / "processed" / "master_schedule.csv"
SCHEDULED_FILE = PROJECT_ROOT / "data" / "processed" / "scheduled_orders.csv"
SPLIT_RECOMMENDATIONS_FILE = PROJECT_ROOT / "data" / "processed" / "split_recommendations.csv"
DASHBOARD_FILE = PROJECT_ROOT / "output" / "dashboard.html"
MAIN_SCRIPT = PROJECT_ROOT / "app" / "main.py"


def parse_int(value, default=0):
    try:
        text = str(value or "").strip()
        return default if text == "" else int(float(text))
    except (ValueError, TypeError):
        return default


def status_tokens(value):
    return (
        str(value or "")
        .strip()
        .upper()
        .replace(",", " ")
        .replace(";", " ")
        .split()
    )


def is_delivered(system_status, user_status):
    return "DLV" in (status_tokens(system_status) + status_tokens(user_status))


def display_pool_name(pool):
    pool = str(pool or "").strip()
    if pool == "CSKI_MVKI":
        return "CSKI / MVKI"
    if pool == "LVPA":
        return "Box / Bag"
    return pool


def read_csv_rows(path):
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as infile:
        return list(csv.DictReader(infile))


def write_csv_rows(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_lineup():
    try:
        result = subprocess.run(
            [sys.executable, str(MAIN_SCRIPT)],
            cwd=str(APP_FOLDER),
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return False, f"Could not run LINEUP: {error}"

    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        return False, f"LINEUP failed. {output}"

    return True, "LINEUP recalculated."


def redirect_control(message=""):
    return redirect(url_for("control", message=message))


def redirect_after_action(message="Updated."):
    if "/dashboard" in (request.referrer or ""):
        return redirect(url_for("dashboard"))
    return redirect_control(message)


def read_notes():
    if not NOTES_FILE.exists():
        return {"bench_notes": {}, "order_notes": {}}

    try:
        with open(NOTES_FILE, "r", encoding="utf-8") as infile:
            data = json.load(infile)
    except (json.JSONDecodeError, OSError):
        data = {}

    if not isinstance(data, dict):
        data = {}

    data.setdefault("bench_notes", {})
    data.setdefault("order_notes", {})

    if not isinstance(data["bench_notes"], dict):
        data["bench_notes"] = {}
    if not isinstance(data["order_notes"], dict):
        data["order_notes"] = {}

    return data


def save_notes(notes):
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(NOTES_FILE, "w", encoding="utf-8") as outfile:
        json.dump(notes, outfile, indent=4)


def ensure_overrides(data):
    if not isinstance(data, dict):
        data = {}

    data.setdefault("excluded_orders", [])
    data.setdefault("forced_benches", {})
    data.setdefault("forced_next", {})
    data.setdefault("manual_splits", {})

    if not isinstance(data["excluded_orders"], list):
        data["excluded_orders"] = []
    if not isinstance(data["forced_benches"], dict):
        data["forced_benches"] = {}
    if not isinstance(data["forced_next"], dict):
        data["forced_next"] = {}
    if not isinstance(data["manual_splits"], dict):
        data["manual_splits"] = {}

    return data


def load_benches():
    staffing_state = load_staffing_overrides()
    benches = []

    for row in read_csv_rows(BENCHES_FILE):
        bench_id = str(row.get("bench_id", "")).strip()
        if not bench_id:
            continue

        standard_people = max(1, parse_int(row.get("standard_people", 1), 1))
        staffing_override = staffing_state.get("benches", {}).get(bench_id)
        override_people = ""

        if staffing_override:
            override_people = parse_int(staffing_override.get("people", 0), 0)

        benches.append({
            "bench_id": bench_id,
            "bench_name": str(row.get("bench_name", bench_id)).strip(),
            "scheduling_pool": str(row.get("scheduling_pool", "")).strip(),
            "standard_people": standard_people,
            "staffing_override_people": override_people,
            "staffing_factor": staffing_factor_for_bench(
                bench_id=bench_id,
                baseline_people=standard_people,
                state=staffing_state,
            ),
            "staffing_override_active": bool(override_people),
            "staffing_note": str(
                staffing_override.get("note", "") if staffing_override else ""
            ).strip(),
            "is_open": str(row.get("is_open", "yes")).strip().lower() or "yes",
        })

    benches.sort(key=lambda bench: (bench["scheduling_pool"], bench["bench_id"]))
    return benches


def save_bench_settings(form):
    rows = read_csv_rows(BENCHES_FILE)
    if not rows:
        raise ValueError("No benches found in config/benches.csv.")

    fieldnames = list(rows[0].keys())
    if "is_open" not in fieldnames:
        fieldnames.append("is_open")

    for row in rows:
        bench_id = str(row.get("bench_id", "")).strip()
        if bench_id:
            row["is_open"] = "yes" if form.get(f"open_{bench_id}") == "on" else "no"

    write_csv_rows(BENCHES_FILE, rows, fieldnames)


def calculate_remaining_quantity(row):
    target = parse_int(row.get("target_quantity", 0), 0)
    confirmed = parse_int(row.get("confirmed_quantity", 0), 0)
    remaining = max(0, target - confirmed)
    if remaining <= 0:
        remaining = target
    return target, confirmed, remaining


def find_scheduled_order(order_number, bench_id=""):
    order_number = str(order_number or "").strip()
    bench_id = str(bench_id or "").strip()

    for row in read_csv_rows(SCHEDULED_FILE):
        if str(row.get("order_number", "")).strip() != order_number:
            continue
        if bench_id and str(row.get("bench_id", "")).strip() != bench_id:
            continue
        return row
    return None


def get_order_for_action(order_number, bench_id=""):
    return find_scheduled_order(order_number, bench_id) or load_master_order_lookup().get(
        str(order_number or "").strip()
    )


def find_order_number_from_form(primary_name, manual_name):
    return (
        str(request.form.get(primary_name, "") or "").strip()
        or str(request.form.get(manual_name, "") or "").strip()
    )


def split_integer_total(total, parts):
    total = parse_int(total, 0)
    parts = parse_int(parts, 0)
    if parts <= 0:
        return []

    base = total // parts
    remainder = total % parts
    return [base + (1 if index < remainder else 0) for index in range(parts)]


def split_minutes_by_quantities(total_minutes, quantities):
    total_minutes = parse_int(total_minutes, 0)
    if total_minutes <= 0:
        return [0 for _ in quantities]

    quantity_total = sum(parse_int(quantity, 0) for quantity in quantities)
    if quantity_total <= 0:
        return split_integer_total(total_minutes, len(quantities))

    minutes = [
        round(total_minutes * parse_int(quantity, 0) / quantity_total)
        for quantity in quantities
    ]

    difference = total_minutes - sum(minutes)
    index = 0
    while difference != 0 and minutes:
        target = index % len(minutes)
        if difference > 0:
            minutes[target] += 1
            difference -= 1
        elif minutes[target] > 1:
            minutes[target] -= 1
            difference += 1

        index += 1
        if index > 10000:
            break

    return minutes


def alpha_label(index):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return f"Part {alphabet[index]}" if index < len(alphabet) else f"Part {index + 1}"


def remove_order_from_queue_control_everywhere(order_number):
    remove_order_from_planning_pool(str(order_number or "").strip())
    remove_order_from_manual_queue("", str(order_number or "").strip())


def create_multi_split(order_number, bench_ids):
    order_number = str(order_number or "").strip()
    bench_ids = list(dict.fromkeys(
        str(bench_id or "").strip()
        for bench_id in bench_ids
        if str(bench_id or "").strip()
    ))

    if not order_number:
        raise ValueError("Order number is required.")
    if len(bench_ids) < 2:
        raise ValueError("Select at least two benches for a split.")

    order = load_master_order_lookup().get(order_number)
    if order is None:
        raise ValueError("Order was not found in the released order list.")

    bench_lookup = {
        bench["bench_id"]: bench
        for bench in load_benches()
        if bench["is_open"] == "yes"
    }

    valid_benches = [
        bench_id
        for bench_id in bench_ids
        if bench_id in bench_lookup
        and bench_lookup[bench_id]["scheduling_pool"] == order["scheduling_pool"]
    ]

    if len(valid_benches) < 2:
        raise ValueError("Select at least two open compatible benches for this order.")

    quantities = split_integer_total(order["remaining_quantity"], len(valid_benches))
    minutes = split_minutes_by_quantities(order["bench_remaining_minutes"], quantities)

    parts = [
        {
            "part_label": alpha_label(index),
            "bench_id": bench_id,
            "quantity": quantities[index],
            "minutes": minutes[index],
        }
        for index, bench_id in enumerate(valid_benches)
    ]

    overrides = ensure_overrides(load_overrides())
    overrides["manual_splits"][order_number] = {
        "split_mode": "multi",
        "remaining_quantity": order["remaining_quantity"],
        "total_minutes": order["bench_remaining_minutes"],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "created_by": "planning_board",
        "parts": parts,
    }
    overrides["forced_benches"].pop(order_number, None)
    overrides["forced_next"].pop(order_number, None)
    overrides["excluded_orders"] = [
        value for value in overrides["excluded_orders"]
        if str(value).strip() != order_number
    ]

    save_overrides(overrides)
    remove_order_from_queue_control_everywhere(order_number)
    return parts


def create_legacy_two_bench_split(
    order_number,
    bench_a,
    bench_b,
    qty_a,
    qty_b,
    minutes_a,
    minutes_b,
    split_percent_a=50,
    split_percent_b=50,
    split_mode="manual",
):
    order_number = str(order_number or "").strip()
    bench_a = str(bench_a or "").strip()
    bench_b = str(bench_b or "").strip()

    if not order_number:
        raise ValueError("Order number is required.")
    if not bench_a or not bench_b:
        raise ValueError("Both benches are required.")
    if bench_a == bench_b:
        raise ValueError("Choose two different benches.")

    minutes_a = parse_int(minutes_a, 0)
    minutes_b = parse_int(minutes_b, 0)
    if minutes_a <= 0 or minutes_b <= 0:
        raise ValueError("Split minutes must be greater than zero.")

    overrides = ensure_overrides(load_overrides())
    overrides["manual_splits"][order_number] = {
        "bench_a": bench_a,
        "bench_b": bench_b,
        "qty_a": parse_int(qty_a, 0),
        "qty_b": parse_int(qty_b, 0),
        "minutes_a": minutes_a,
        "minutes_b": minutes_b,
        "split_percent_a": parse_int(split_percent_a, 50),
        "split_percent_b": parse_int(split_percent_b, 50),
        "split_mode": split_mode,
    }
    overrides["forced_benches"].pop(order_number, None)
    overrides["forced_next"].pop(order_number, None)
    overrides["excluded_orders"] = [
        value for value in overrides["excluded_orders"]
        if str(value).strip() != order_number
    ]

    save_overrides(overrides)
    remove_order_from_queue_control_everywhere(order_number)


def calculate_two_bench_percent_split(order, percent_a):
    percent_a = parse_int(percent_a, 50)
    if percent_a <= 0 or percent_a >= 100:
        raise ValueError("Split percentage must be between 1 and 99.")

    remaining = max(2, parse_int(order.get("remaining_quantity", 0), 0))
    qty_a = max(1, min(round(remaining * percent_a / 100), remaining - 1))
    qty_b = remaining - qty_a
    minutes_a, minutes_b = split_minutes_by_quantities(
        order.get("bench_remaining_minutes", 0),
        [qty_a, qty_b],
    )
    return qty_a, qty_b, minutes_a, minutes_b


def save_board_state_from_payload(payload):
    master_orders = load_master_orders()

    order_lookup = {
        order["order_number"]: order
        for order in master_orders
    }

    benches = load_benches()
    bench_lookup = {bench["bench_id"]: bench for bench in benches}

    clean_payload = validate_planning_board_payload(
        payload=payload,
        order_lookup=order_lookup,
        bench_lookup=bench_lookup,
        live_benches=get_active_benches(),
    )

    previous_state = load_queue_control()
    previous_notes = {}

    for item in previous_state.get("planning_pool", []):
        order_number = str(item.get("order_number", "")).strip()
        if order_number:
            previous_notes[order_number] = str(item.get("note", "")).strip()

    for items in previous_state.get("manual_queues", {}).values():
        for item in items:
            order_number = str(item.get("order_number", "")).strip()
            if order_number:
                previous_notes[order_number] = str(item.get("note", "")).strip()

    now_value = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    clean_pool = [
        {
            "order_number": item["order_number"],
            "added_at": now_value,
            "added_by": "planning_board",
            "note": previous_notes.get(item["order_number"], ""),
        }
        for item in clean_payload["planning_pool"]
    ]

    clean_queues = {}
    for bench_id, items in clean_payload["manual_queues"].items():
        clean_items = [
            {
                "order_number": item["order_number"],
                "added_at": now_value,
                "added_by": "planning_board",
                "note": previous_notes.get(item["order_number"], ""),
            }
            for item in items
        ]
        if clean_items:
            clean_queues[bench_id] = clean_items

    state = load_queue_control()
    state["version"] = 2
    state["planning_pool"] = clean_pool
    state["manual_queues"] = clean_queues

    board_orders = {item["order_number"] for item in clean_pool}
    for items in clean_queues.values():
        board_orders.update(item["order_number"] for item in items)

    for order_number in board_orders:
        state.get("held_orders", {}).pop(order_number, None)

    save_queue_control(state)
    return state


@app.route("/")
def index():
    return redirect(url_for("control"))


@app.route("/dashboard")
def dashboard():
    if not DASHBOARD_FILE.exists():
        return "Dashboard has not been generated yet. Run LINEUP first.", 404

    response = make_response(send_file(DASHBOARD_FILE))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.context_processor
def lineup_publication_context():
    """Attach the completed dashboard revision to server-rendered pages."""
    try:
        stamp = DASHBOARD_FILE.stat().st_mtime_ns // 1_000_000
    except OSError:
        stamp = 0
    return {"lineup_published_ms": stamp}


@app.route("/api/lineup-publication")
def lineup_publication():
    """Lightweight, non-cached freshness check for LINEUP browser pages.

    The final dashboard file is the publication marker: it is rebuilt only
    after the pipeline has produced its output files. We do not use raw SAP
    export timestamps here, because they can advance before LINEUP finishes.
    """
    if not DASHBOARD_FILE.exists():
        response = jsonify({"available": False})
        response.status_code = 503
    else:
        # A response generated while the background pipeline is still writing
        # datasets should not cause clients to reload halfway through a run.
        status_file = PROJECT_ROOT / "data" / "processed" / "lineup_status.json"
        try:
            status = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        published_stat = DASHBOARD_FILE.stat()
        published_at = datetime.fromtimestamp(
            published_stat.st_mtime
        ).isoformat(timespec="milliseconds")
        response = jsonify({
            "available": True,
            "published_at": published_at,
            "published_ms": published_stat.st_mtime_ns // 1_000_000,
            "processing": status.get("last_run_status", "unknown"),
        })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.route("/control", methods=["GET", "POST"])
def control():
    if request.method == "POST":
        try:
            save_bench_settings(request.form)
            ok, run_message = run_lineup()
            return redirect_control(
                "Bench setup saved and LINEUP recalculated."
                if ok
                else f"Bench setup saved, but recalculation failed: {run_message}"
            )
        except Exception as error:
            return redirect_control(f"Bench setup failed: {error}")

    benches = load_benches()

    master_orders = load_master_orders()

    available_orders = load_ready_orders()

    awaiting_picking_orders = load_awaiting_picking_orders()

    order_lookup = {
        order["order_number"]: order
        for order in master_orders
    }
    queue_control = load_queue_control()
    prune_terminal_manual_splits()
    overrides = ensure_overrides(load_overrides())
    notes = read_notes()
    live_benches = get_active_benches()
    board_metadata = build_order_board_metadata(available_orders, benches)
    now = datetime.now()
    message = request.args.get("message", "")

    return render_template(
        "control.html",
        message=message,
        benches=benches,
        available_orders=available_orders,
        awaiting_picking_orders=awaiting_picking_orders,
        order_lookup=order_lookup,
        queue_control=queue_control,
        overrides=overrides,
        notes=notes,
        live_benches=live_benches,
        board_metadata=board_metadata,
        current_date_input=now.strftime("%Y-%m-%d"),
        current_time_input=now.strftime("%H:%M"),
        display_pool_name=display_pool_name,
    )


@app.route("/planning-pool/add", methods=["POST"])
def planning_pool_add():
    order_number = find_order_number_from_form("pool_order_number", "pool_manual_order_number")
    note = str(request.form.get("pool_note", "") or "").strip()

    if not order_number:
        return redirect_control("Choose or type an order number first.")

    try:
        add_order_to_planning_pool(order_number, note=note)
        ok, run_message = run_lineup()
        return redirect_control(
            f"Order {order_number} added to the planning pool."
            if ok else f"Order added to pool, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_control(f"Could not add order to pool: {error}")


@app.route("/planning-pool/remove", methods=["POST"])
def planning_pool_remove():
    order_number = str(request.form.get("order_number", "") or "").strip()
    remove_order_from_planning_pool(order_number)
    ok, run_message = run_lineup()
    return redirect_control(
        f"Order {order_number} removed from the planning pool."
        if ok else f"Order removed from pool, but recalculation failed: {run_message}"
    )


@app.route("/planning-pool/clear", methods=["POST"])
def planning_pool_clear():
    removed_count = clear_planning_pool()
    ok, run_message = run_lineup()
    return redirect_control(
        f"Planning pool cleared. Removed {removed_count} order(s)."
        if ok else f"Planning pool cleared, but recalculation failed: {run_message}"
    )


@app.route("/planning-board/save", methods=["POST"])
def planning_board_save():
    try:
        save_board_state_from_payload(request.get_json(silent=True))
        ok, run_message = run_lineup()
        if ok:
            return jsonify(success=True, message="Planning board saved and LINEUP recalculated.")
        return jsonify(success=False, message=f"Planning board saved, but recalculation failed: {run_message}"), 500
    except Exception as error:
        return jsonify(success=False, message=str(error)), 400


@app.route("/queue/add", methods=["POST"])
def queue_add():
    order_number = find_order_number_from_form("queue_order_number", "queue_manual_order_number")
    bench_id = str(request.form.get("queue_bench_id", "") or "").strip()
    note = str(request.form.get("queue_note", "") or "").strip()

    try:
        add_order_to_manual_queue(bench_id, order_number, note=note)
        ok, run_message = run_lineup()
        return redirect_control(
            f"Order {order_number} added to {bench_id} queue."
            if ok else f"Order queued, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_control(f"Could not queue order: {error}")


@app.route("/queue/remove", methods=["POST"])
def queue_remove():
    order_number = str(request.form.get("order_number", "") or "").strip()
    bench_id = str(request.form.get("bench_id", "") or "").strip()
    remove_order_from_manual_queue(bench_id, order_number)
    ok, run_message = run_lineup()
    return redirect_after_action(
        f"Order {order_number} removed from queue."
        if ok else f"Order removed from queue, but recalculation failed: {run_message}"
    )


@app.route("/queue/move-up", methods=["POST"])
def queue_move_up():
    order_number = str(request.form.get("order_number", "") or "").strip()
    bench_id = str(request.form.get("bench_id", "") or "").strip()
    try:
        move_order_in_manual_queue(bench_id, order_number, "up")
        ok, run_message = run_lineup()
        return redirect_control(
            f"Order {order_number} moved up."
            if ok else f"Order moved, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_control(f"Could not move order: {error}")


@app.route("/queue/move-down", methods=["POST"])
def queue_move_down():
    order_number = str(request.form.get("order_number", "") or "").strip()
    bench_id = str(request.form.get("bench_id", "") or "").strip()
    try:
        move_order_in_manual_queue(bench_id, order_number, "down")
        ok, run_message = run_lineup()
        return redirect_control(
            f"Order {order_number} moved down."
            if ok else f"Order moved, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_control(f"Could not move order: {error}")


@app.route("/queue/clear", methods=["POST"])
def queue_clear():
    removed_count = clear_manual_queues()
    ok, run_message = run_lineup()
    return redirect_control(
        f"Manual bench queues cleared. Removed {removed_count} order(s)."
        if ok else f"Manual queues cleared, but recalculation failed: {run_message}"
    )


@app.route("/hold/add", methods=["POST"])
def hold_add():
    order_number = (
        str(request.form.get("order_number", "") or "").strip()
        or find_order_number_from_form("hold_order_number", "hold_manual_order_number")
    )
    bench_id = (
        str(request.form.get("bench_id", "") or "").strip()
        or str(request.form.get("hold_bench_id", "") or "").strip()
    )
    reason = str(request.form.get("hold_reason", "") or "").strip() or "On hold"
    note = str(request.form.get("hold_note", "") or "").strip()

    try:
        live_state = get_bench_live_state(bench_id) if bench_id else None
        if live_state and str(live_state.get("active_order_number", "")).strip() == order_number:
            stop_order_on_bench(bench_id, expected_order_number=order_number)

        add_held_order(order_number=order_number, bench_id=bench_id, reason=reason, note=note)
        ok, run_message = run_lineup()
        return redirect_after_action(
            f"Order {order_number} put on hold."
            if ok else f"Order held, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_after_action(f"Could not hold order: {error}")


@app.route("/hold/release", methods=["POST"])
def hold_release():
    order_number = str(request.form.get("order_number", "") or "").strip()
    bench_id = str(request.form.get("bench_id", "") or "").strip()
    note = str(request.form.get("release_note", "") or "").strip() or "Released from hold"

    try:
        release_held_order_to_queue(order_number, bench_id=bench_id, note=note)
        ok, run_message = run_lineup()
        return redirect_control(
            f"Order {order_number} released to {bench_id} queue."
            if ok else f"Order released, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_control(f"Could not release hold: {error}")


@app.route("/hold/remove", methods=["POST"])
def hold_remove():
    order_number = str(request.form.get("order_number", "") or "").strip()
    remove_held_order(order_number)
    ok, run_message = run_lineup()
    return redirect_control(
        f"Hold removed from order {order_number}."
        if ok else f"Hold removed, but recalculation failed: {run_message}"
    )


@app.route("/split/multi", methods=["POST"])
def split_multi():
    order_number = str(request.form.get("split_order_number", "") or "").strip()
    try:
        parts = create_multi_split(order_number, request.form.getlist("split_bench_ids"))
        ok, run_message = run_lineup()
        return redirect_control(
            f"Multi-bench split created for order {order_number} across {len(parts)} bench(es)."
            if ok else f"Split created, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_control(f"Could not create split: {error}")


@app.route("/split/apply", methods=["POST"])
def split_apply():
    order_number = str(request.form.get("order_number", "") or "").strip()
    recommendation = next(
        (
            row for row in read_csv_rows(SPLIT_RECOMMENDATIONS_FILE)
            if str(row.get("order_number", "")).strip() == order_number
        ),
        None,
    )

    if recommendation is None:
        return redirect_after_action(f"No split recommendation found for order {order_number}.")

    try:
        create_legacy_two_bench_split(
            order_number=order_number,
            bench_a=recommendation.get("current_bench_id", ""),
            bench_b=recommendation.get("recommended_second_bench_id", ""),
            qty_a=recommendation.get("current_bench_quantity", 0),
            qty_b=recommendation.get("second_bench_quantity", 0),
            minutes_a=recommendation.get("current_bench_base_minutes") or recommendation.get("current_bench_minutes", 0),
            minutes_b=recommendation.get("second_bench_base_minutes") or recommendation.get("second_bench_minutes", 0),
            split_percent_a=recommendation.get("split_percent_a", 50),
            split_percent_b=recommendation.get("split_percent_b", 50),
            split_mode="recommendation",
        )
        ok, run_message = run_lineup()
        return redirect_after_action(
            f"Split recommendation applied for order {order_number}."
            if ok else f"Split applied, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_after_action(f"Could not apply split: {error}")


@app.route("/split/remove", methods=["POST"])
def split_remove():
    order_number = str(request.form.get("order_number", "") or "").strip()
    overrides = ensure_overrides(load_overrides())
    overrides["manual_splits"].pop(order_number, None)
    save_overrides(overrides)

    ok, run_message = run_lineup()
    return redirect_control(
        f"Split removed for order {order_number}."
        if ok else f"Split removed, but recalculation failed: {run_message}"
    )


@app.route("/override/add", methods=["POST"])
def override_add():
    order_number = find_order_number_from_form("order_number", "manual_order_number")
    override_type = str(request.form.get("override_type", "") or "").strip()
    bench_id = str(request.form.get("bench_id", "") or "").strip()

    if not order_number:
        return redirect_control("Choose or type an order number first.")

    overrides = ensure_overrides(load_overrides())

    if override_type == "exclude":
        if order_number not in overrides["excluded_orders"]:
            overrides["excluded_orders"].append(order_number)
        overrides["forced_benches"].pop(order_number, None)
        overrides["forced_next"].pop(order_number, None)
    elif override_type == "force_bench":
        overrides["forced_benches"][order_number] = bench_id
        overrides["forced_next"].pop(order_number, None)
    elif override_type == "force_next":
        overrides["forced_next"][order_number] = bench_id
        overrides["forced_benches"].pop(order_number, None)
    else:
        return redirect_control("Unknown override type.")

    overrides["excluded_orders"] = [
        value for value in overrides["excluded_orders"]
        if override_type == "exclude" or str(value).strip() != order_number
    ]

    save_overrides(overrides)
    remove_order_from_queue_control_everywhere(order_number)
    ok, run_message = run_lineup()

    return redirect_control(
        f"Override saved for order {order_number}."
        if ok else f"Override saved, but recalculation failed: {run_message}"
    )


@app.route("/override/remove", methods=["POST"])
def override_remove():
    order_number = str(request.form.get("order_number", "") or "").strip()
    overrides = ensure_overrides(load_overrides())
    overrides["excluded_orders"] = [
        value for value in overrides["excluded_orders"]
        if str(value).strip() != order_number
    ]
    overrides["forced_benches"].pop(order_number, None)
    overrides["forced_next"].pop(order_number, None)
    overrides["manual_splits"].pop(order_number, None)
    save_overrides(overrides)

    ok, run_message = run_lineup()
    return redirect_control(
        f"Override removed from order {order_number}."
        if ok else f"Override removed, but recalculation failed: {run_message}"
    )


@app.route("/override/clear-all", methods=["POST"])
def override_clear_all():
    save_overrides({
        "excluded_orders": [],
        "forced_benches": {},
        "forced_next": {},
        "manual_splits": {},
    })
    ok, run_message = run_lineup()
    return redirect_control(
        "Advanced overrides cleared."
        if ok else f"Advanced overrides cleared, but recalculation failed: {run_message}"
    )


@app.route("/notes/benches", methods=["POST"])
def notes_benches():
    notes = read_notes()
    for bench in load_benches():
        note = str(request.form.get(f"note_{bench['bench_id']}", "") or "").strip()
        if note:
            notes["bench_notes"][bench["bench_id"]] = note
        else:
            notes["bench_notes"].pop(bench["bench_id"], None)

    save_notes(notes)
    ok, run_message = run_lineup()
    return redirect_control(
        "Bench notes saved."
        if ok else f"Bench notes saved, but recalculation failed: {run_message}"
    )


@app.route("/notes/orders", methods=["POST"])
def notes_orders():
    order_number = find_order_number_from_form("order_number", "manual_order_number")
    note = str(request.form.get("order_note", "") or "").strip()
    notes = read_notes()

    if note:
        notes["order_notes"][order_number] = note
    else:
        notes["order_notes"].pop(order_number, None)

    save_notes(notes)
    ok, run_message = run_lineup()
    return redirect_control(
        f"Order note saved for {order_number}."
        if ok else f"Order note saved, but recalculation failed: {run_message}"
    )


@app.route("/work/start", methods=["POST"])
def work_start():
    bench_id = str(request.form.get("bench_id", "") or "").strip()
    order_number = str(request.form.get("order_number", "") or "").strip()
    order = get_order_for_action(order_number, bench_id)

    if order is None:
        return redirect_after_action(f"Order {order_number} was not found.")

    try:
        start_order_on_bench(bench_id=bench_id, order=order, started_by="dashboard")
        ok, run_message = run_lineup()
        return redirect_after_action(
            f"Live tracking started for {order_number} on {bench_id}."
            if ok else f"Live tracking started, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_after_action(f"Could not start live tracking: {error}")


@app.route("/work/stop", methods=["POST"])
def work_stop():
    bench_id = str(request.form.get("bench_id", "") or "").strip()
    order_number = str(request.form.get("order_number", "") or "").strip()

    try:
        stop_order_on_bench(bench_id=bench_id, expected_order_number=order_number)
        ok, run_message = run_lineup()
        return redirect_after_action(
            f"Live tracking stopped on {bench_id}."
            if ok else f"Live tracking stopped, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_after_action(f"Could not stop live tracking: {error}")


@app.route("/work/set-start", methods=["POST"])
def work_set_start():
    bench_id = str(request.form.get("bench_id", "") or "").strip()
    order_number = str(request.form.get("order_number", "") or "").strip()
    started_at_text = (
        f"{str(request.form.get('started_date', '')).strip()} "
        f"{str(request.form.get('started_time', '')).strip()}:00"
    )
    order = get_order_for_action(order_number, bench_id)

    if order is None:
        return redirect_control(f"Order {order_number} was not found.")

    try:
        start_order_on_bench(
            bench_id=bench_id,
            order=order,
            started_by="manual_start_time",
            started_at_text=started_at_text,
        )
        ok, run_message = run_lineup()
        return redirect_control(
            f"Start time set for order {order_number} on {bench_id}."
            if ok else f"Start time set, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_control(f"Could not set start time: {error}")


@app.route("/work/clear-all", methods=["POST"])
def work_clear_all():
    removed_count = clear_all_live_work()
    ok, run_message = run_lineup()
    return redirect_control(
        f"Cleared live tracking from {removed_count} bench(es)."
        if ok else f"Live tracking cleared, but recalculation failed: {run_message}"
    )


@app.route("/staffing/set", methods=["POST"])
def staffing_set():
    bench_id = str(request.form.get("bench_id", "") or "").strip()
    people = parse_int(request.form.get("staffing_people", 0), 0)
    note = str(request.form.get("staffing_note", "") or "").strip()

    if not bench_id or people <= 0:
        return redirect_control("A bench and at least one person are required.")

    try:
        set_staffing_override(
            bench_id=bench_id,
            people=people,
            note=note,
            updated_by="control",
        )
        ok, run_message = run_lineup()
        return redirect_control(
            f"Staffing override set for {bench_id}: {people} people."
            if ok else f"Staffing override saved, but recalculation failed: {run_message}"
        )
    except Exception as error:
        return redirect_control(f"Could not set staffing override: {error}")


@app.route("/staffing/clear", methods=["POST"])
def staffing_clear():
    bench_id = str(request.form.get("bench_id", "") or "").strip()
    clear_staffing_override(bench_id)
    ok, run_message = run_lineup()
    return redirect_control(
        f"Staffing override cleared for {bench_id}."
        if ok else f"Override cleared, but recalculation failed: {run_message}"
    )


@app.route("/staffing/clear-all", methods=["POST"])
def staffing_clear_all():
    removed_count = clear_all_staffing_overrides()
    ok, run_message = run_lineup()
    return redirect_control(
        f"Cleared {removed_count} staffing override(s)."
        if ok else f"Overrides cleared, but recalculation failed: {run_message}"
    )


@app.route("/planning-board/state", methods=["GET"])
def planning_board_state():
    return jsonify({
        "success": True,
        "board": board_state_for_client(
            queue_control=load_queue_control(),
            benches=load_benches(),
            live_benches=get_active_benches(),
        ),
    })


@app.route("/planning-board/metadata", methods=["GET"])
def planning_board_metadata():
    return jsonify({
        "success": True,
        "orders": build_order_board_metadata(
            orders=load_ready_orders(),
            benches=load_benches(),
        ),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
