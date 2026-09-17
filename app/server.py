from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
MEETINGS_DIR = DATA_DIR / "meetings"
DB_PATH = DATA_DIR / "meetings.json"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a"}
ALLOWED_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

PIPELINE_STEPS = [
    ("uploaded", "アップロード"),
    ("audio_extracting", "音声抽出"),
    ("transcribing", "文字起こし"),
    ("diarizing", "話者分離"),
    ("summarizing", "要約"),
    ("completed", "完了"),
]

DB_LOCK = threading.Lock()


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"{path.name}:{line_number}: KEY=VALUE 形式で指定してください")
        try:
            parts = shlex.split(value, comments=True)
        except ValueError as exc:
            raise ValueError(f"{path.name}:{line_number}: 引用符を確認してください") from exc
        if len(parts) > 1:
            raise ValueError(f"{path.name}:{line_number}: 空白を含む値は引用符で囲んでください")
        os.environ.setdefault(key, parts[0] if parts else "")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(name: str) -> str:
    base = Path(name).name.strip() or "upload"
    return re.sub(r"[^A-Za-z0-9._ -]", "_", base)


def read_db() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"meetings": []}
    try:
        return json.loads(DB_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"meetings": []}


def write_db(db: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DB_PATH)


def get_meeting(meeting_id: str) -> dict[str, Any] | None:
    with DB_LOCK:
        for meeting in read_db()["meetings"]:
            if meeting["id"] == meeting_id:
                return meeting
    return None


def update_meeting(meeting_id: str, **changes: Any) -> dict[str, Any]:
    with DB_LOCK:
        db = read_db()
        for meeting in db["meetings"]:
            if meeting["id"] == meeting_id:
                meeting.update(changes)
                meeting["updatedAt"] = now_iso()
                write_db(db)
                return meeting
    raise KeyError(meeting_id)


def create_meeting(filename: str, stored_path: Path, media_type: str) -> dict[str, Any]:
    meeting_id = uuid.uuid4().hex
    meeting_dir = MEETINGS_DIR / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    final_path = meeting_dir / stored_path.name
    shutil.move(str(stored_path), final_path)

    meeting = {
        "id": meeting_id,
        "title": Path(filename).stem or "Untitled meeting",
        "filename": filename,
        "mediaType": media_type,
        "originalPath": str(final_path.relative_to(ROOT)),
        "preparedAudioPath": None,
        "status": "uploaded",
        "statusLabel": "アップロード",
        "stepIndex": 0,
        "progress": 5,
        "error": None,
        "transcript": [],
        "fullText": "",
        "summary": "",
        "decisions": [],
        "todos": [],
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }

    with DB_LOCK:
        db = read_db()
        db.setdefault("meetings", []).insert(0, meeting)
        write_db(db)
    return meeting


def set_step(meeting_id: str, status: str, progress: int) -> None:
    step_index = next((i for i, (key, _) in enumerate(PIPELINE_STEPS) if key == status), 0)
    label = PIPELINE_STEPS[step_index][1]
    update_meeting(
        meeting_id,
        status=status,
        statusLabel=label,
        stepIndex=step_index,
        progress=progress,
        error=None,
    )


def fail_meeting(meeting_id: str, message: str) -> None:
    update_meeting(meeting_id, status="failed", statusLabel="エラー", progress=100, error=message)


def run_command(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def prepare_audio(meeting: dict[str, Any], meeting_dir: Path) -> Path:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg が見つかりません。README の手順でインストールしてください。")

    source = ROOT / meeting["originalPath"]
    wav_path = meeting_dir / "audio_16k_mono.wav"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]
    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg の変換に失敗しました。\n{result.stderr.strip()}")
    return wav_path


def run_whisperx(audio_path: Path, meeting_dir: Path) -> dict[str, Any]:
    whisperx_bin = shutil.which("whisperx")
    if not whisperx_bin:
        if os.getenv("MEETING_MINUTES_MOCK") == "1":
            return mock_whisperx_result()
        raise RuntimeError("whisperx が見つかりません。README の手順で Python 環境を準備してください。")

    output_dir = meeting_dir / "whisperx"
    output_dir.mkdir(exist_ok=True)
    command = [
        whisperx_bin,
        str(audio_path),
        "--model",
        os.getenv("WHISPERX_MODEL", "large-v3"),
        "--language",
        "ja",
        "--device",
        os.getenv("WHISPERX_DEVICE", "cpu"),
        "--output_dir",
        str(output_dir),
        "--output_format",
        "json",
    ]

    token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
    command.extend(["--diarize", "--diarize_model", os.getenv("PYANNOTE_MODEL", "pyannote/speaker-diarization-3.1")])
    if token:
        command.extend(["--hf_token", token])

    result = run_command(command, cwd=meeting_dir)
    (meeting_dir / "whisperx.stdout.log").write_text(result.stdout, encoding="utf-8")
    (meeting_dir / "whisperx.stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"WhisperX の処理に失敗しました。\n{result.stderr.strip()}")

    json_files = sorted(output_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not json_files:
        raise RuntimeError("WhisperX の JSON 出力が見つかりませんでした。")
    return json.loads(json_files[0].read_text(encoding="utf-8"))


def mock_whisperx_result() -> dict[str, Any]:
    return {
        "segments": [
            {"start": 0.0, "end": 8.2, "speaker": "SPEAKER_00", "text": "本日の議題は新しい議事録アプリの進め方です。"},
            {"start": 8.4, "end": 17.0, "speaker": "SPEAKER_01", "text": "まずローカル保存と話者分離を優先して、来週までに試作版を確認しましょう。"},
            {"start": 17.3, "end": 26.0, "speaker": "SPEAKER_00", "text": "決定事項として、担当は佐藤さん、期限は金曜日でお願いします。"},
        ]
    }


def format_ts(seconds: float | int | None) -> str:
    value = max(float(seconds or 0), 0)
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    secs = int(value % 60)
    millis = int((value - int(value)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def normalize_transcript(whisperx_data: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    transcript = []
    for segment in whisperx_data.get("segments", []):
        speaker = segment.get("speaker") or "UNKNOWN"
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        item = {
            "start": float(segment.get("start") or 0),
            "end": float(segment.get("end") or 0),
            "startLabel": format_ts(segment.get("start")),
            "endLabel": format_ts(segment.get("end")),
            "speaker": speaker,
            "text": text,
        }
        transcript.append(item)

    full_text = "\n".join(
        f"[{item['startLabel']} -> {item['endLabel']}] [{item['speaker']}]: {item['text']}"
        for item in transcript
    )
    return transcript, full_text


def summarize_with_ai(full_text: str) -> dict[str, Any]:
    if not full_text.strip():
        return {"summary": "", "decisions": [], "todos": []}

    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if api_key:
        prompt = (
            "次の日本語会議文字起こしから、議事録を JSON で作ってください。"
            "必ず {\"summary\": string, \"decisions\": string[], "
            "\"todos\": [{\"task\": string, \"owner\": string, \"due\": string}]} の形式にしてください。\n\n"
            f"{full_text[:24000]}"
        )
        body = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": "あなたは日本語会議の議事録作成アシスタントです。JSON だけを返します。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return normalize_summary(parsed)
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
            return heuristic_summary(full_text, note=f"AI 要約に失敗したため簡易抽出に切り替えました: {exc}")

    return heuristic_summary(full_text, note="OPENAI_API_KEY 未設定のため簡易抽出で生成しました。")


def normalize_summary(data: dict[str, Any]) -> dict[str, Any]:
    todos = data.get("todos") or []
    return {
        "summary": str(data.get("summary") or "").strip(),
        "decisions": [str(item).strip() for item in data.get("decisions", []) if str(item).strip()],
        "todos": [
            {
                "task": str(item.get("task") or "").strip(),
                "owner": str(item.get("owner") or "").strip(),
                "due": str(item.get("due") or "").strip(),
            }
            for item in todos
            if isinstance(item, dict) and str(item.get("task") or "").strip()
        ],
    }


def heuristic_summary(full_text: str, note: str = "") -> dict[str, Any]:
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]
    important = [line for line in lines if re.search(r"決定|TODO|タスク|担当|期限|お願いします|やる|対応", line)]
    summary_lines = important[:5] or lines[:5]
    decisions = [line for line in important if "決定" in line][:8]
    todos = []
    for line in important:
        if re.search(r"TODO|タスク|担当|期限|お願いします|対応", line):
            owner = ""
            due = ""
            owner_match = re.search(r"担当(?:は|者は|:|：)?\s*([^、。\s]+)", line)
            due_match = re.search(r"期限(?:は|:|：)?\s*([^、。\s]+)", line)
            if owner_match:
                owner = owner_match.group(1)
            if due_match:
                due = due_match.group(1)
            todos.append({"task": line, "owner": owner, "due": due})
    return {
        "summary": "\n".join(([note] if note else []) + summary_lines),
        "decisions": decisions,
        "todos": todos[:12],
    }


def process_meeting(meeting_id: str) -> None:
    try:
        meeting = get_meeting(meeting_id)
        if not meeting:
            return
        meeting_dir = MEETINGS_DIR / meeting_id

        set_step(meeting_id, "audio_extracting", 20)
        audio_path = prepare_audio(meeting, meeting_dir)
        update_meeting(meeting_id, preparedAudioPath=str(audio_path.relative_to(ROOT)))

        set_step(meeting_id, "transcribing", 45)
        whisperx_data = run_whisperx(audio_path, meeting_dir)

        set_step(meeting_id, "diarizing", 70)
        transcript, full_text = normalize_transcript(whisperx_data)
        update_meeting(meeting_id, transcript=transcript, fullText=full_text)

        set_step(meeting_id, "summarizing", 85)
        summary = summarize_with_ai(full_text)
        update_meeting(
            meeting_id,
            summary=summary["summary"],
            decisions=summary["decisions"],
            todos=summary["todos"],
        )

        set_step(meeting_id, "completed", 100)
    except Exception as exc:  # The UI should receive a readable local processing error.
        fail_meeting(meeting_id, str(exc))


def parse_multipart_upload(handler: BaseHTTPRequestHandler) -> tuple[str, bytes]:
    content_type = handler.headers.get("Content-Type", "")
    boundary_match = re.search(r"boundary=(.+)", content_type)
    if not boundary_match:
        raise ValueError("multipart/form-data の boundary がありません。")
    boundary = boundary_match.group(1).strip('"').encode("utf-8")
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length)

    delimiter = b"--" + boundary
    for part in body.split(delimiter):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        headers_raw, _, content = part.partition(b"\r\n\r\n")
        headers = headers_raw.decode("utf-8", errors="replace")
        if 'name="file"' not in headers:
            continue
        filename_match = re.search(r'filename="([^"]+)"', headers)
        if not filename_match:
            raise ValueError("ファイル名が取得できませんでした。")
        if content.endswith(b"\r\n"):
            content = content[:-2]
        return filename_match.group(1), content
    raise ValueError("file フィールドが見つかりませんでした。")


class AppHandler(BaseHTTPRequestHandler):
    server_version = "MeetingMinutes/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/meetings":
            with DB_LOCK:
                meetings = read_db()["meetings"]
            compact = [{k: v for k, v in m.items() if k not in {"transcript", "fullText"}} for m in meetings]
            self.send_json({"meetings": compact})
            return

        meeting_match = re.fullmatch(r"/api/meetings/([a-f0-9]+)", self.path)
        if meeting_match:
            meeting = get_meeting(meeting_match.group(1))
            if not meeting:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json(meeting)
            return

        events_match = re.fullmatch(r"/api/meetings/([a-f0-9]+)/events", self.path)
        if events_match:
            self.stream_events(events_match.group(1))
            return

        self.serve_static()

    def do_HEAD(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            path = "/index.html"
        file_path = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
            file_path = STATIC_DIR / "index.html"
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/api/meetings":
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return

        try:
            filename, content = parse_multipart_upload(self)
            clean_name = safe_filename(filename)
            ext = Path(clean_name).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
                self.send_json({"error": f"未対応の拡張子です。対応形式: {allowed}"}, HTTPStatus.BAD_REQUEST)
                return

            tmp_path = DATA_DIR / f"upload-{uuid.uuid4().hex}-{clean_name}"
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(content)
            media_type = "video" if ext in VIDEO_EXTENSIONS else "audio"
            meeting = create_meeting(clean_name, tmp_path, media_type)
            threading.Thread(target=process_meeting, args=(meeting["id"],), daemon=True).start()
            self.send_json(meeting, HTTPStatus.CREATED)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def serve_static(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            path = "/index.html"
        file_path = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
            file_path = STATIC_DIR / "index.html"
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream_events(self, meeting_id: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_payload = ""
        for _ in range(60 * 60):
            meeting = get_meeting(meeting_id)
            if not meeting:
                break
            payload = json.dumps(meeting, ensure_ascii=False)
            if payload != last_payload:
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                last_payload = payload
            if meeting["status"] in {"completed", "failed"}:
                break
            time.sleep(1)


def main() -> None:
    load_env_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Local meeting minutes app")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        write_db({"meetings": []})

    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Meeting Minutes is running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
