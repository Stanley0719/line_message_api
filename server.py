import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DATABASE_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "meetings.db"))
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
MAX_REQUEST_BYTES = 1024 * 1024
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
FIELD_PATTERNS = {
    "name": r"(?:會議名稱|名稱|名字)\s*[：:]\s*(.*?)(?=\s*(?:時間|日期|地點)\s*[：:]|$)",
    "time": r"(?:時間|日期)\s*[：:]\s*(.*?)(?=\s*(?:會議名稱|名稱|名字|地點)\s*[：:]|$)",
    "location": r"(?:地點|位置)\s*[：:]\s*(.*?)(?=\s*(?:會議名稱|名稱|名字|時間|日期)\s*[：:]|$)",
}
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("line-meeting-bot")


def connect_database():
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def database_session():
    connection = connect_database()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database():
    directory = os.path.dirname(os.path.abspath(DATABASE_PATH))
    os.makedirs(directory, exist_ok=True)
    with database_session() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                name TEXT NOT NULL,
                meeting_time TEXT NOT NULL,
                location TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                owner_id TEXT PRIMARY KEY,
                draft_name TEXT NOT NULL DEFAULT '',
                draft_time TEXT NOT NULL DEFAULT '',
                draft_location TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT ''
            )"""
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY)"
        )


def parse_labeled_fields(text):
    fields = {}
    for field, pattern in FIELD_PATTERNS.items():
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            fields[field] = match.group(1).strip(" \t，,。；;")
    return fields


def get_conversation(connection, owner_id):
    row = connection.execute(
        "SELECT * FROM conversations WHERE owner_id = ?", (owner_id,)
    ).fetchone()
    if row:
        return dict(row)
    return {
        "owner_id": owner_id,
        "draft_name": "",
        "draft_time": "",
        "draft_location": "",
        "stage": "",
    }


def save_conversation(connection, conversation):
    connection.execute(
        """INSERT INTO conversations
           (owner_id, draft_name, draft_time, draft_location, stage)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(owner_id) DO UPDATE SET
             draft_name = excluded.draft_name,
             draft_time = excluded.draft_time,
             draft_location = excluded.draft_location,
             stage = excluded.stage""",
        (
            conversation["owner_id"],
            conversation["draft_name"],
            conversation["draft_time"],
            conversation["draft_location"],
            conversation["stage"],
        ),
    )


def next_prompt(conversation):
    missing = [
        key
        for key in ("name", "time", "location")
        if not conversation[f"draft_{key}"]
    ]
    if missing:
        field = missing[0]
        conversation["stage"] = field
        prompts = {
            "name": "請問會議名稱是什麼？",
            "time": "請提供會議時間，例如「明天下午 2 點」或「12/8 14:30」。",
            "location": "請提供會議地點，例如「三樓會議室」或「Google Meet」。",
        }
        return prompts[field]

    conversation["stage"] = "confirm"
    return (
        "請確認會議資訊：\n"
        f"名稱：{conversation['draft_name']}\n"
        f"時間：{conversation['draft_time']}\n"
        f"地點：{conversation['draft_location']}\n\n"
        "回覆「確認」儲存，或回覆「取消」放棄。"
    )


def reset_draft(conversation):
    conversation["draft_name"] = ""
    conversation["draft_time"] = ""
    conversation["draft_location"] = ""
    conversation["stage"] = ""


def process_text(connection, owner_id, text):
    message = text.strip()
    conversation = get_conversation(connection, owner_id)

    if message in ("取消", "取消記錄"):
        reset_draft(conversation)
        save_conversation(connection, conversation)
        return "已取消，沒有儲存會議。"

    if message in ("顯示會議", "我的會議", "列出會議"):
        rows = connection.execute(
            """SELECT name, meeting_time, location FROM meetings
               WHERE owner_id = ? ORDER BY id DESC LIMIT 10""",
            (owner_id,),
        ).fetchall()
        if not rows:
            return "目前沒有已記錄的會議。傳送「記錄會議」即可新增。"
        lines = [
            f"{index}. {row['name']}｜{row['meeting_time']}｜{row['location']}"
            for index, row in enumerate(rows, start=1)
        ]
        return "最近的會議：\n" + "\n".join(lines)

    if message in ("確認", "儲存") and conversation["stage"] == "confirm":
        connection.execute(
            """INSERT INTO meetings (owner_id, name, meeting_time, location, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                owner_id,
                conversation["draft_name"],
                conversation["draft_time"],
                conversation["draft_location"],
                int(time.time()),
            ),
        )
        meeting_name = conversation["draft_name"]
        reset_draft(conversation)
        save_conversation(connection, conversation)
        return f"已記錄會議「{meeting_name}」。"

    start_command = re.match(r"^記錄會議(?:\s|[：:，,]|$)(.*)$", message)
    if start_command:
        reset_draft(conversation)
        details = start_command.group(1).strip(" ：:,，")
    elif conversation["stage"]:
        details = message
    else:
        return "我可以幫你記錄會議。傳送「記錄會議」，再提供名稱、時間和地點。"

    fields = parse_labeled_fields(details)
    for field, value in fields.items():
        if value:
            conversation[f"draft_{field}"] = value

    if conversation["stage"] in ("name", "time", "location") and not fields:
        field = conversation["stage"]
        conversation[f"draft_{field}"] = message

    response = next_prompt(conversation)
    save_conversation(connection, conversation)
    return response


def verify_signature(body, signature):
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def reply_to_line(reply_token, text):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN is not configured")
    payload = json.dumps(
        {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        LINE_REPLY_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            if response.status >= 300:
                raise RuntimeError(f"LINE reply API returned HTTP {response.status}")
    except HTTPError as error:
        raise RuntimeError(f"LINE reply API returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError("Could not reach the LINE reply API") from error


class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        self.respond(200, b"ok")

    def do_POST(self):
        if self.path != "/callback":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self.send_error(413 if length > MAX_REQUEST_BYTES else 400)
            return

        body = self.rfile.read(length)
        if not verify_signature(body, self.headers.get("x-line-signature", "")):
            self.send_error(401)
            return

        try:
            payload = json.loads(body)
            for event in payload.get("events", []):
                self.handle_event(event)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.exception("Invalid LINE webhook payload")
            self.send_error(400)
            return
        except Exception:
            logger.exception("Failed to process LINE webhook event")
            self.send_error(500)
            return
        self.respond(200, b"ok")

    def handle_event(self, event):
        if event.get("type") != "message" or event.get("message", {}).get("type") != "text":
            return
        reply_token = event.get("replyToken")
        source = event.get("source", {})
        owner_id = source.get("userId")
        if not reply_token:
            return
        if not owner_id:
            reply_to_line(reply_token, "請先將我加入好友，再使用會議記錄功能。")
            return

        event_id = event.get("webhookEventId")
        with database_session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if event_id:
                try:
                    connection.execute(
                        "INSERT INTO processed_events (event_id) VALUES (?)",
                        (event_id,),
                    )
                except sqlite3.IntegrityError:
                    return
            response_text = process_text(connection, owner_id, event["message"]["text"])
        reply_to_line(reply_token, response_text)

    def respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        logger.info("%s - %s", self.address_string(), format_string % args)


def main():
    initialize_database()
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), WebhookHandler)
    logger.info("LINE meeting bot listening on port %s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
