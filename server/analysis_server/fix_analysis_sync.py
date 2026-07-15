#!/usr/bin/env python3
from pathlib import Path
import py_compile
import subprocess
import sys

root = Path("/home/ubuntu/analysis_server")
bak = root / "analysis.py.bak_restore"
target = root / "analysis.py"

if bak.is_file():
    target.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
    print("restored from bak_restore")

# apply restore helpers
subprocess.check_call([sys.executable, str(root / "patch_restore.py")])

text = target.read_text(encoding="utf-8")
text = text.replace(
    "_start_postprocess(session_id)",
    "pass  # postprocess disabled for sync-check UI",
)
old = "    sync = _detect_video_clock_sync(parsed, video_path)"
new = (
    "    sync = {\n"
    '        "mode": "manual",\n'
    '        "offset_sec": 0.0,\n'
    '        "ok": False,\n'
    '        "message": "manual offset sync check",\n'
    "    }"
)
if old in text:
    text = text.replace(old, new, 1)
    print("disabled OCR autosync")

target.write_text(text, encoding="utf-8")
py_compile.compile(str(target), doraise=True)
print("compile ok")
