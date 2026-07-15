#!/usr/bin/env python3
"""Patch analysis.py for disk session restore + safe cleanup; boot existing sessions."""
from pathlib import Path

analysis = Path("/home/ubuntu/analysis_server/analysis.py")
text = analysis.read_text(encoding="utf-8")

old_cleanup = '''def _cleanup_old_sessions() -> None:
    with _lock:
        items = sorted(
            ((sid, meta) for sid, meta in _sessions.items()),
            key=lambda item: float(item[1].get("created_at", 0)),
        )
        while len(items) > MAX_SESSIONS:
            sid, _ = items.pop(0)
            _sessions.pop(sid, None)
            path = _session_dir(sid)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)

        dirs = [p for p in UPLOAD_DIR.iterdir() if p.is_dir()]
        known = set(_sessions)
        for path in dirs:
            if path.name not in known:
                shutil.rmtree(path, ignore_errors=True)
'''

new_cleanup = '''def _cleanup_old_sessions() -> None:
    # Only drop oldest in-memory sessions. Never wipe upload dirs on unload/restart.
    with _lock:
        items = sorted(
            ((sid, meta) for sid, meta in _sessions.items()),
            key=lambda item: float(item[1].get("created_at", 0)),
        )
        while len(items) > MAX_SESSIONS:
            sid, _ = items.pop(0)
            _sessions.pop(sid, None)
'''

if old_cleanup not in text:
    raise SystemExit("cleanup block not found — abort")
text = text.replace(old_cleanup, new_cleanup, 1)

marker = '@app.get("/", response_class=HTMLResponse)'
if marker not in text:
    raise SystemExit("home marker not found")

restore_block = '''
def _restore_session_from_disk(session_id: str) -> dict[str, Any]:
    """Reload a finished upload using the 20Hz decimated cache (low RAM)."""
    session_id = (session_id or "").strip()
    if not session_id or "/" in session_id or "\\\\" in session_id:
        raise HTTPException(status_code=400, detail="잘못된 세션 ID")
    dest = _session_dir(session_id)
    csv_path = dest / "data.csv"
    videos = sorted(dest.glob("video.*"))
    if not csv_path.is_file() or not videos:
        raise HTTPException(status_code=404, detail="디스크에 세션 파일이 없습니다.")

    with _lock:
        existing = _sessions.get(session_id)
        if existing:
            return existing

    video_path = videos[0]
    video_ext = video_path.suffix.lower()
    decimated = load_decimated_cache(dest)
    if not isinstance(decimated, dict) or not decimated.get("times"):
        raise HTTPException(
            status_code=409,
            detail="후처리 캐시가 없습니다. 다시 업로드해야 합니다.",
        )

    from postprocess import load_events as _load_events
    from postprocess import load_kpi as _load_kpi
    from postprocess import load_segments as _load_segments

    kpi = _load_kpi(dest)
    events = _load_events(dest)
    segments = _load_segments(dest)

    try:
        pp = json.loads((dest / "postprocess.json").read_text(encoding="utf-8"))
    except Exception:
        pp = {"status": "ready"}

    times = decimated["times"]
    series = decimated.get("series") or {}
    columns = list(series.keys())
    t0 = float(times[0])
    t_end = float(times[-1])
    duration = float((kpi or {}).get("duration_sec") or max(0.0, t_end - t0))
    row_count = int((kpi or {}).get("row_count") or len(times))

    meta = {
        "id": session_id,
        "created_at": datetime.now().timestamp(),
        "csv_name": "data.csv",
        "video_name": video_path.name,
        "video_path": str(video_path),
        "video_media_type": {
            ".mp4": "video/mp4",
            ".avi": "video/x-msvideo",
            ".webm": "video/webm",
            ".mov": "video/quicktime",
            ".mkv": "video/x-matroska",
        }.get(video_ext, "application/octet-stream"),
        "bytes": csv_path.stat().st_size + video_path.stat().st_size,
        "t0": t0,
        "t_end": t_end,
        "duration_sec": duration,
        "row_count": row_count,
        "columns": columns,
        "times": times,
        "series": series,
        "offset_sec": 0.0,
        "sync": {
            "mode": "manual",
            "offset_sec": 0.0,
            "ok": False,
            "message": "디스크에서 복구됨",
        },
        "decimated": decimated,
        "postprocess": pp if isinstance(pp, dict) else {"status": "ready"},
        "kpi": kpi or {},
        "events": events if isinstance(events, list) else [],
        "segments": segments if isinstance(segments, list) else [],
        "restored": True,
    }
    with _lock:
        _sessions[session_id] = meta
    return meta


def _autoload_sessions() -> None:
    if not UPLOAD_DIR.is_dir():
        return
    for path in sorted(UPLOAD_DIR.iterdir()):
        if not path.is_dir():
            continue
        if not (path / "data.csv").is_file():
            continue
        if not list(path.glob("video.*")):
            continue
        try:
            _restore_session_from_disk(path.name)
            print(f"[analysis] restored session {path.name}")
        except Exception as exc:
            print(f"[analysis] restore skipped {path.name}: {exc}")


@app.on_event("startup")
def _on_startup() -> None:
    _autoload_sessions()


@app.get("/api/sessions")
def list_sessions() -> dict[str, Any]:
    with _lock:
        items = [
            {
                "session_id": sid,
                "csv_name": meta.get("csv_name"),
                "video_name": meta.get("video_name"),
                "row_count": meta.get("row_count"),
                "duration_sec": meta.get("duration_sec"),
                "restored": bool(meta.get("restored")),
                "postprocess": _session_postprocess(meta),
            }
            for sid, meta in _sessions.items()
        ]
    disk = []
    if UPLOAD_DIR.is_dir():
        for path in sorted(UPLOAD_DIR.iterdir()):
            if path.is_dir() and (path / "data.csv").is_file():
                disk.append(path.name)
    return {"ok": True, "sessions": items, "disk_sessions": disk}


@app.post("/api/session/{session_id}/restore")
def restore_session(session_id: str) -> dict[str, Any]:
    meta = _restore_session_from_disk(session_id)
    return {
        "ok": True,
        "session_id": session_id,
        "csv_name": meta["csv_name"],
        "video_name": meta["video_name"],
        "columns": meta["columns"],
        "row_count": meta["row_count"],
        "duration_sec": meta["duration_sec"],
        "t0": meta["t0"],
        "t0_label": "",
        "video_url": f"/api/session/{session_id}/video",
        "offset_sec": float(meta.get("offset_sec") or 0.0),
        "sync": meta.get("sync") or {},
        "postprocess": _session_postprocess(meta),
        "kpi": _session_kpi(meta, session_id),
        "chart_presets": CHART_PRESETS,
    }


'''

if "def _restore_session_from_disk" not in text:
    text = text.replace(marker, restore_block + "\n" + marker, 1)
else:
    print("restore already present")

old_win = '''    decimated = meta.get("decimated")
    if isinstance(decimated, dict) and decimated.get("times"):
        return _window_data_decimated(
            decimated,
            meta,
            video_t=video_t,
            offset_sec=offset_sec,
            window_sec=window_sec,
            cols=cols,
        )
'''
new_win = '''    decimated = meta.get("decimated")
    if not (isinstance(decimated, dict) and decimated.get("times")):
        loaded = _session_decimated(meta, str(meta.get("id") or ""))
        if isinstance(loaded, dict) and loaded.get("times"):
            meta["decimated"] = loaded
            decimated = loaded
    if isinstance(decimated, dict) and decimated.get("times"):
        return _window_data_decimated(
            decimated,
            meta,
            video_t=video_t,
            offset_sec=offset_sec,
            window_sec=window_sec,
            cols=cols,
        )
'''
if old_win in text and "loaded = _session_decimated" not in text:
    text = text.replace(old_win, new_win, 1)

analysis.write_text(text, encoding="utf-8")
print("patched analysis.py")

index = Path("/home/ubuntu/analysis_server/templates/index.html")
html = index.read_text(encoding="utf-8")
boot = '''
    async function openExistingSession(sid) {
      setStatus('세션 불러오는 중...');
      try {
        let res = await fetch('/api/session/' + sid + '/restore', { method: 'POST' });
        let data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
          res = await fetch('/api/session/' + sid, { cache: 'no-store' });
          data = await res.json().catch(() => ({}));
        }
        if (!res.ok || !data.ok) throw new Error(data.detail || data.message || 'session open failed');
        sessionId = data.session_id || sid;
        workspace.hidden = false;
        document.getElementById('clearBtn').disabled = false;
        document.getElementById('autosyncBtn').disabled = false;
        document.getElementById('metaCsv').textContent = 'CSV: ' + (data.csv_name || 'data.csv');
        document.getElementById('metaVideo').textContent = 'Video: ' + (data.video_name || 'video');
        document.getElementById('metaRows').textContent = 'rows: ' + (data.row_count || '-');
        document.getElementById('metaDur').textContent = 'csv span: ' + Number(data.duration_sec || 0).toFixed(1) + 's';
        document.getElementById('metaT0').textContent = 't0: ' + (data.t0_label || '-');
        renderColumns(data.columns || []);
        player.src = (data.video_url || ('/api/session/' + sessionId + '/video')) + '?_=' + Date.now();
        player.load();
        applySyncInfo(data.sync || {}, data.duration_sec);
        updatePpBadge(data.postprocess || { status: 'ready' });
        renderKpi(data.kpi || null);
        destroyVehicleViz();
        ensureVehicleViz();
        if (vehicleViz && vehicleViz.resetWorld) vehicleViz.resetWorld();
        if (typeof setVehicleUiEnabled === 'function') setVehicleUiEnabled(true);
        scheduleRefresh(true);
        setStatus('세션 복구 완료 · ' + sessionId, 'ok');
      } catch (err) {
        setStatus('세션 복구 실패: ' + (err.message || err), 'err');
      }
    }

    (async function bootSessionFromQuery() {
      const params = new URLSearchParams(location.search);
      const sid = params.get('session');
      if (sid) {
        await openExistingSession(sid);
        return;
      }
      try {
        const list = await fetch('/api/sessions', { cache: 'no-store' }).then((r) => r.json());
        if (list && list.ok && Array.isArray(list.sessions) && list.sessions.length === 1) {
          await openExistingSession(list.sessions[0].session_id);
        } else if (list && list.ok && Array.isArray(list.disk_sessions) && list.disk_sessions.length === 1) {
          await openExistingSession(list.disk_sessions[0]);
        }
      } catch (err) {}
    })();
'''

if "openExistingSession" not in html:
    html = html.replace("  </script>\n</body>", boot + "\n  </script>\n</body>")
    index.write_text(html, encoding="utf-8")
    print("patched index.html")
else:
    print("index already patched")

import py_compile

py_compile.compile(str(analysis), doraise=True)
print("syntax ok")
