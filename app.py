import asyncio
import csv
import io
import threading
import time
from flask import Flask, jsonify, render_template, request, Response

from scraper import scrape_channels, MAX_CHANNELS_PER_BATCH, COOLDOWN_SECONDS

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
    "cooldown_until": 0,   # epoch seconds
    "batches_done": 0,
    "auto_batch": False,   # True while the multi-batch loop is alive
    "stop_requested": False,
}
_state_lock = threading.Lock()


def _update(key, value):
    with _state_lock:
        _state[key] = value


def _progress_cb(done, total, msg):
    with _state_lock:
        _state["progress"] = done
        _state["total"] = total
        _state["message"] = msg


def _run_scrape(keyword, region, min_subs, max_subs, batch_size):
    """
    Multi-batch loop: scrapes one batch, waits out the cooldown, then scrapes
    the next batch using deeper search-page scrolling.  Stops when no new
    channels are found or a stop is requested.
    """
    # Reset state for a fresh run
    with _state_lock:
        _state["running"] = False
        _state["error"] = None
        _state["results"] = []
        _state["progress"] = 0
        _state["total"] = batch_size
        _state["message"] = "Starting…"
        _state["stop_requested"] = False
        _state["auto_batch"] = True
        _state["batches_done"] = 0
        _state["cooldown_until"] = 0

    seen_handles: set = set()
    batch_num = 0

    try:
        while True:
            with _state_lock:
                if _state["stop_requested"]:
                    break

            batch_num += 1
            scroll_passes = (batch_num - 1) * 3  # go deeper each batch

            with _state_lock:
                _state["running"] = True
                _state["progress"] = 0
                _state["total"] = batch_size
                _state["message"] = f"Batch {batch_num} — scanning channels…"

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                results = loop.run_until_complete(
                    scrape_channels(
                        keyword, region, min_subs, max_subs, batch_size,
                        _progress_cb,
                        seen_handles=seen_handles,
                        scroll_passes=scroll_passes,
                    )
                )
            finally:
                loop.close()

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
                        f"Batch {batch_num} done — no new channels found. Stopping."
                    )

            if not results:
                break

            # Wait out cooldown, bailing early if stop is requested
            stop_early = False
            for _ in range(COOLDOWN_SECONDS):
                with _state_lock:
                    if _state["stop_requested"]:
                        stop_early = True
                        break
                time.sleep(1)

            if stop_early:
                break

    except Exception as exc:
        with _state_lock:
            _state["error"] = str(exc)
            _state["message"] = f"Error: {exc}"
    finally:
        with _state_lock:
            _state["running"] = False
            _state["auto_batch"] = False
            if not _state.get("error"):
                total_found = len(_state["results"])
                batches_word = "batch" if batch_num == 1 else "batches"
                channels_word = "channel" if total_found == 1 else "channels"
                _state["message"] = (
                    f"All done — {total_found} {channels_word} found "
                    f"across {batch_num} {batches_word}"
                )


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
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"error": "keyword required"}), 400

    region = data.get("region", "TH")
    min_subs = max(0, int(data.get("min_subs", 1000)))
    max_subs = max(min_subs, int(data.get("max_subs", 10_000_000)))
    batch_size = min(int(data.get("batch_size", 5)), MAX_CHANNELS_PER_BATCH)

    t = threading.Thread(
        target=_run_scrape,
        args=(keyword, region, min_subs, max_subs, batch_size),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True})


@app.route("/stop", methods=["POST"])
def stop():
    with _state_lock:
        _state["stop_requested"] = True
        _state["message"] = "Stop requested — finishing current channel…"
    return jsonify({"ok": True})


@app.route("/status")
def status():
    with _state_lock:
        cooldown_left = max(0, _state["cooldown_until"] - time.time())
        return jsonify({
            "running": _state["running"],
            "progress": _state["progress"],
            "total": _state["total"],
            "message": _state["message"],
            "error": _state["error"],
            "cooldown_left": int(cooldown_left),
            "results_count": len(_state["results"]),
            "auto_batch": _state["auto_batch"],
            "batches_done": _state["batches_done"],
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
    writer = csv.DictWriter(output, fieldnames=_csv_fields(), extrasaction="ignore")
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
    import openpyxl
    with _state_lock:
        rows = list(_state["results"])

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "YT Scout"
    fields = _csv_fields()
    ws.append(fields)
    for row in rows:
        flat = _flatten_row(row)
        ws.append([flat.get(f, "") for f in fields])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=yt_scout_results.xlsx"},
    )


def _csv_fields():
    return [
        "name", "url", "subscribers", "location_badge", "active", "upload_freq",
        "score", "country", "avg_video_views", "avg_short_views",
        "avg_live_views", "emails", "links",
    ]


def _flatten_row(r: dict) -> dict:
    def _fmt(v):
        if v is None:
            return "—"
        return v

    return {
        "name": _fmt(r.get("name")),
        "url": _fmt(r.get("url")),
        "subscribers": _fmt(r.get("subs")),
        "location_badge": _fmt(r.get("location_badge")),
        "active": "Yes" if r.get("active") else "No",
        "upload_freq": _fmt(r.get("upload_freq")),
        "score": _fmt(r.get("score")),
        "country": _fmt(r.get("country")),
        "avg_video_views": _fmt(r.get("avg_video_views")),
        "avg_short_views": _fmt(r.get("avg_short_views")),
        "avg_live_views": _fmt(r.get("avg_live_views")),
        "emails": "; ".join(r.get("emails") or []),
        "links": "; ".join(r.get("links") or []),
    }


if __name__ == "__main__":
    app.run(host='127.0.0.1', port=8080, debug=False)
