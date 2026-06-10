import asyncio
import csv
import io
import openpyxl
import threading
import time
import traceback
from flask import Flask, jsonify, render_template, request, Response

import scraper
from scraper import scrape_channels, MAX_CHANNELS_PER_BATCH, COOLDOWN_SECONDS, discovery_queue

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global scrape state
# ---------------------------------------------------------------------------
_state = {
    "running": False,
    "progress": 0,
    "total": 0,
    "message": "",
    "results": [],
    "error": None,
    "cooldown_until": 0,
    "batches_done": 0,
    "auto_batch": False,
    "stop_requested": False,
    "activity_filter": "any",
}
_state_lock = threading.Lock()
_stop_event = threading.Event()

_EXPORT_HEADERS = [
    "Discovered Video Title", "Video URL",
    "Channel", "Handle", "Subscribers", "Location", "Upload Frequency",
    "Avg Views Videos", "Avg Views Shorts", "Avg Views Live", "Contact",
]


def _fmt(v):
    return "—" if v is None else v


def _progress_cb(done, total, msg):
    with _state_lock:
        _state["progress"] = done
        _state["total"] = total
        _state["message"] = msg


def _run_scrape(keyword, region, sub_tier, batch_size, target_category, activity_filter):
    _stop_event.clear()
    scraper.reset_session_state()
    with _state_lock:
        _state["error"] = None
        _state["results"] = []
        _state["progress"] = 0
        _state["total"] = batch_size
        _state["message"] = "Starting…"
        _state["stop_requested"] = False
        _state["auto_batch"] = True
        _state["batches_done"] = 0
        _state["cooldown_until"] = 0
        _state["activity_filter"] = activity_filter

    seen_handles: set = set()
    batch_num = 0
    _preserve_message = False

    try:
        while True:
            if _stop_event.is_set():
                break

            batch_num += 1
            scroll_passes = (batch_num - 1) * 3

            with _state_lock:
                _state["running"] = True
                _state["progress"] = 0
                _state["total"] = batch_size
                _state["message"] = f"Batch {batch_num} — scanning channels…"

            results = asyncio.run(
                scrape_channels(
                    keyword, region, sub_tier, batch_size,
                    _progress_cb,
                    seen_handles=seen_handles,
                    scroll_passes=scroll_passes,
                    target_category=target_category,
                    activity_filter=activity_filter,
                )
            )

            with _state_lock:
                _state["results"].extend(results)
                _state["batches_done"] += 1
                _state["running"] = False
                _state["cooldown_until"] = time.time() + COOLDOWN_SECONDS
                if results:
                    _state["message"] = (
                        f"Batch {batch_num} done — {len(results)} channels found. "
                        f"Cooling down before next batch…"
                    )
                else:
                    _state["message"] = (
                        f"Batch {batch_num} done — no new channels found. "
                        + ("Checking discovery queue next…" if discovery_queue else "Stopping.")
                    )

            if not results and not discovery_queue:
                break

            if _stop_event.wait(timeout=COOLDOWN_SECONDS):
                break

    except Exception as exc:
        with _state_lock:
            _state["error"] = str(exc)
            _state["message"] = f"Error: {exc}"
    finally:
        scraper.stop_requested = False
        with _state_lock:
            _state["running"] = False
            _state["auto_batch"] = False
            if not _state.get("error") and not _preserve_message:
                total_found = len(_state["results"])
                batches_word = "batch" if batch_num == 1 else "batches"
                channels_word = "channel" if total_found == 1 else "channels"
                _state["message"] = (
                    f"All done — {total_found} {channels_word} found "
                    f"across {batch_num} {batches_word}"
                )


def _run_scrape_thread(*args, **kwargs):
    try:
        _run_scrape(*args, **kwargs)
    except Exception as e:
        with _state_lock:
            _state["running"] = False
            _state["auto_batch"] = False
            _state["message"] = f'Scraper crashed: {str(e)}'
            _state["error"] = str(e)
        print(f'[scraper] FATAL ERROR: {e}')
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html",
                           max_batch=MAX_CHANNELS_PER_BATCH,
                           cooldown_secs=COOLDOWN_SECONDS)


@app.route("/start", methods=["POST"])
def start():
    with _state_lock:
        if _state["running"] or _state["auto_batch"]:
            return jsonify({"error": "Scrape already running — use Stop to cancel"}), 409

    data = request.json or {}
    raw_keyword = (data.get("keyword") or "").strip()
    if not raw_keyword:
        return jsonify({"error": "Please provide a keyword."}), 400
    # Split on comma to support multi-keyword input from UI
    keyword = [kw.strip() for kw in raw_keyword.split(',') if kw.strip()]
    if not keyword:
        keyword = ['']

    region = data.get("region", "TH")
    sub_tier = data.get("sub_tier", "any")
    if sub_tier not in ("under_5k", "5k_10k", "10k_100k", "100k_500k", "500k_1m", "over_1m", "any"):
        sub_tier = "any"
    batch_size = min(int(data.get("batch_size", 5)), MAX_CHANNELS_PER_BATCH)
    target_category = (data.get("target_category") or "").strip()
    activity_filter = data.get("activity_filter", "any")
    if activity_filter not in ("30", "60", "90", "any"):
        activity_filter = "any"

    t = threading.Thread(
        target=_run_scrape_thread,
        args=(keyword, region, sub_tier, batch_size, target_category, activity_filter),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True})


@app.route("/stop", methods=["POST"])
def stop():
    scraper.stop_requested = True
    _stop_event.set()
    with _state_lock:
        _state["stop_requested"] = True
        _state["message"] = "Stop requested — finishing current channel…"
    return jsonify({"status": "stop requested"})


@app.route("/status")
def status():
    with _state_lock:
        cooldown_left = max(0, _state["cooldown_until"] - time.time())
        return jsonify({
            "running": _state["running"],
            "progress": _state["progress"],
            "total": _state["total"],
            "message": _state["message"],
            "current_status": _state["message"],
            "error": _state["error"],
            "cooldown_left": int(cooldown_left),
            "results_count": len(_state["results"]),
            "auto_batch": _state["auto_batch"],
            "batches_done": _state["batches_done"],
            "discovery_queue_size": len(discovery_queue),
            "activity_filter": _state["activity_filter"],
        })


@app.route("/results")
def results():
    with _state_lock:
        return jsonify(_state["results"])


@app.route("/export/csv")
def export_csv():
    with _state_lock:
        rows = list(_state["results"])

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_EXPORT_HEADERS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(_flatten_row(row))

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=yt_scout_results.csv"},
    )


@app.route("/export/excel")
def export_excel():
    with _state_lock:
        rows = list(_state["results"])

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "YT Scout"
    ws.append(_EXPORT_HEADERS)
    for row in rows:
        flat = _flatten_row(row)
        ws.append([flat.get(f, "") for f in _EXPORT_HEADERS])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=yt_scout_results.xlsx"},
    )


def _flatten_row(r: dict) -> dict:
    contact_parts = list(r.get("emails") or []) + list(r.get("links") or [])
    return {
        "Discovered Video Title": _fmt(r.get("video_title")),
        "Video URL": _fmt(r.get("video_url")),
        "Channel": _fmt(r.get("name")),
        "Handle": _fmt(r.get("handle")),
        "Subscribers": _fmt(r.get("subs")),
        "Location": _fmt(r.get("location_badge")),
        "Upload Frequency": _fmt(r.get("upload_frequency")),
        "Avg Views Videos": _fmt(r.get("avg_views_videos")),
        "Avg Views Shorts": _fmt(r.get("avg_views_shorts")),
        "Avg Views Live": _fmt(r.get("avg_views_live")),
        "Contact": "; ".join(contact_parts) or "—",
    }


if __name__ == "__main__":
    app.run(host='127.0.0.1', port=8080, debug=False)
