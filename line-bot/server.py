import base64
import datetime
import hashlib
import hmac
import itertools
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


DATABASE_PATH = os.environ.get(
    "DATABASE_PATH", os.path.join(os.path.dirname(__file__), "meetings.db")
)
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_REQUEST_BYTES = 1024 * 1024
DINING_MINUTES = 90
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
TABLES = tuple(
    [f"4人桌-{number}" for number in range(1, 7)]
    + [f"2人桌-{number}" for number in range(1, 7)]
)
FIELD_PATTERNS = {
    "date": r"(?:日期)\s*[：:]\s*(.*?)(?=\s*(?:時間|人數|姓名|名字|訂位人|聯絡方式|電話|手機|備註|特殊需求)\s*[：:]|$)",
    "time": r"(?:時間)\s*[：:]\s*(.*?)(?=\s*(?:日期|人數|姓名|名字|訂位人|聯絡方式|電話|手機|備註|特殊需求)\s*[：:]|$)",
    "party_size": r"(?:人數)\s*[：:]\s*(.*?)(?=\s*(?:日期|時間|姓名|名字|訂位人|聯絡方式|電話|手機|備註|特殊需求)\s*[：:]|$)",
    "name": r"(?:姓名|名字|訂位人)\s*[：:]\s*(.*?)(?=\s*(?:日期|時間|人數|聯絡方式|電話|手機|備註|特殊需求)\s*[：:]|$)",
    "contact": r"(?:聯絡方式|電話|手機)\s*[：:]\s*(.*?)(?=\s*(?:日期|時間|人數|姓名|名字|訂位人|備註|特殊需求)\s*[：:]|$)",
    "note": r"(?:備註|特殊需求)\s*[：:]\s*(.*?)(?=\s*(?:日期|時間|人數|姓名|名字|訂位人|聯絡方式|電話|手機)\s*[：:]|$)",
}
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("line-restaurant-bot")


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
    os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)
    with database_session() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                line_user_id TEXT NOT NULL,
                reservation_date TEXT NOT NULL,
                reservation_time TEXT NOT NULL,
                party_size INTEGER NOT NULL,
                customer_name TEXT NOT NULL,
                contact TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                assigned_tables TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed',
                estimated_wait_minutes INTEGER NOT NULL DEFAULT 0
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS reservation_conversations (
                line_user_id TEXT PRIMARY KEY,
                draft_date TEXT NOT NULL DEFAULT '',
                draft_time TEXT NOT NULL DEFAULT '',
                draft_party_size TEXT NOT NULL DEFAULT '',
                draft_name TEXT NOT NULL DEFAULT '',
                draft_contact TEXT NOT NULL DEFAULT '',
                draft_note TEXT NOT NULL DEFAULT '',
                draft_waitlist_accepted INTEGER NOT NULL DEFAULT 0,
                draft_estimated_wait_minutes INTEGER NOT NULL DEFAULT 0,
                stage TEXT NOT NULL DEFAULT ''
            )"""
        )
        migrations = {
            "reservations": (
                ("status", "TEXT NOT NULL DEFAULT 'confirmed'"),
                ("estimated_wait_minutes", "INTEGER NOT NULL DEFAULT 0"),
            ),
            "reservation_conversations": (
                ("draft_waitlist_accepted", "INTEGER NOT NULL DEFAULT 0"),
                ("draft_estimated_wait_minutes", "INTEGER NOT NULL DEFAULT 0"),
            ),
        }
        for table, columns in migrations.items():
            existing_columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for column, definition in columns:
                if column not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
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


def get_conversation(connection, line_user_id):
    row = connection.execute(
        "SELECT * FROM reservation_conversations WHERE line_user_id = ?",
        (line_user_id,),
    ).fetchone()
    if row:
        return dict(row)
    conversation = {"line_user_id": line_user_id, "stage": ""}
    for field in ("date", "time", "party_size", "name", "contact", "note"):
        conversation[f"draft_{field}"] = ""
    conversation["draft_waitlist_accepted"] = 0
    conversation["draft_estimated_wait_minutes"] = 0
    return conversation


def save_conversation(connection, conversation):
    fields = (
        "date",
        "time",
        "party_size",
        "name",
        "contact",
        "note",
        "waitlist_accepted",
        "estimated_wait_minutes",
    )
    columns = ", ".join(f"draft_{field}" for field in fields)
    values = ", ".join("?" for _ in fields)
    updates = ", ".join(
        f"draft_{field} = excluded.draft_{field}" for field in fields
    )
    connection.execute(
        f"""INSERT INTO reservation_conversations
            (line_user_id, {columns}, stage) VALUES (?, {values}, ?)
            ON CONFLICT(line_user_id) DO UPDATE SET {updates}, stage = excluded.stage""",
        (
            conversation["line_user_id"],
            *(conversation[f"draft_{field}"] for field in fields),
            conversation["stage"],
        ),
    )


def reset_draft(conversation):
    for field in ("date", "time", "party_size", "name", "contact", "note"):
        conversation[f"draft_{field}"] = ""
    conversation["draft_waitlist_accepted"] = 0
    conversation["draft_estimated_wait_minutes"] = 0
    conversation["stage"] = ""


def validate_field(field, value):
    if field == "date":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return "日期格式請使用 YYYY-MM-DD，例如 2026-12-08。"
        try:
            parsed = datetime.date.fromisoformat(value)
        except ValueError:
            return "日期格式請使用 YYYY-MM-DD，例如 2026-12-08。"
        if parsed < datetime.date.today():
            return "不能預訂已過去的日期，請重新輸入日期。"
    elif field == "time":
        if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value):
            return "時間格式請使用 24 小時制 HH:MM，例如 12:30。"
    elif field == "party_size":
        if not value.isdigit() or not 1 <= int(value) <= 24:
            return "人數請輸入 1 到 24 的整數。"
    elif field == "name" and not value:
        return "請提供訂位人姓名。"
    elif field == "contact" and not value:
        return "請提供電話或其他聯絡方式。"
    return ""


def next_prompt(conversation):
    fields = ("date", "time", "party_size", "name", "contact", "note")
    missing = [field for field in fields if not conversation[f"draft_{field}"]]
    if missing:
        field = missing[0]
        conversation["stage"] = field
        prompts = {
            "date": "請提供訂位日期（YYYY-MM-DD，例如 2026-12-08）。",
            "time": "請提供訂位時間（24 小時制 HH:MM，例如 12:30）。營業時間為 11:30–14:30、17:00–20:00。",
            "party_size": "請問用餐人數？（1–24 人）",
            "name": "請提供訂位人姓名。",
            "contact": "請提供聯絡電話或其他聯絡方式。",
            "note": "有特殊需求或備註嗎？若沒有請回覆「無」。",
        }
        return prompts[field]

    conversation["stage"] = "confirm"
    confirmation = (
        "請確認訂位資料：\n"
        f"日期：{conversation['draft_date']}\n"
        f"時間：{conversation['draft_time']}\n"
        f"人數：{conversation['draft_party_size']} 人\n"
        f"姓名：{conversation['draft_name']}\n"
        f"聯絡方式：{conversation['draft_contact']}\n"
        f"備註：{conversation['draft_note'] or '無'}\n\n"
        "回覆「確認」送出，或回覆「取消」放棄。"
    )
    if conversation["draft_waitlist_accepted"]:
        confirmation = (
            "目前該時段沒有空桌。請確認是否登記候補：\n"
            + confirmation.split("請確認訂位資料：\n", 1)[1]
        )
        confirmation = confirmation.replace(
            "回覆「確認」送出，或回覆「取消」放棄。",
            f"候補預估等候約 {conversation['draft_estimated_wait_minutes']} 分鐘。\n"
            "回覆「確認」登記候補，或回覆「取消」放棄。",
        )
    return confirmation


def reservation_interval(reservation_date, reservation_time):
    start = datetime.datetime.combine(
        datetime.date.fromisoformat(reservation_date),
        datetime.time.fromisoformat(reservation_time),
    )
    return start, start + datetime.timedelta(minutes=DINING_MINUTES)


def opening_window(start, end):
    windows = (
        (datetime.time(11, 30), datetime.time(14, 30)),
        (datetime.time(17, 0), datetime.time(20, 0)),
    )
    return any(start.time() >= opening and end.time() <= closing for opening, closing in windows)


def available_tables(connection, reservation_date, reservation_time):
    requested_start, requested_end = reservation_interval(
        reservation_date, reservation_time
    )
    if not opening_window(requested_start, requested_end):
        return None, 0

    occupied = set()
    overlapping_groups = 0
    rows = connection.execute(
        """SELECT reservation_time, assigned_tables FROM reservations
           WHERE reservation_date = ? AND status = 'confirmed'""",
        (reservation_date,),
    ).fetchall()
    for row in rows:
        existing_start, existing_end = reservation_interval(
            reservation_date, row["reservation_time"]
        )
        if existing_start < requested_end and existing_end > requested_start:
            occupied.update(row["assigned_tables"].split(","))
            overlapping_groups += 1

    free = [table for table in TABLES if table not in occupied]
    return free, overlapping_groups


def choose_tables(free_tables, party_size):
    candidates = []
    for count in range(1, len(free_tables) + 1):
        for selection in itertools.combinations(free_tables, count):
            seats = sum(4 if table.startswith("4人桌") else 2 for table in selection)
            if seats >= party_size:
                candidates.append((count, seats - party_size, selection))
        if candidates:
            break
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def create_reservation(connection, line_user_id, conversation):
    reservation_date = conversation["draft_date"]
    reservation_time = conversation["draft_time"]
    free_tables, overlapping_groups = available_tables(
        connection, reservation_date, reservation_time
    )
    if free_tables is None:
        conversation["draft_time"] = ""
        conversation["stage"] = "time"
        save_conversation(connection, conversation)
        return (
            "這個時間不在營業時段內，或 90 分鐘用餐時間會超過打烊時間。"
            "請直接輸入其他時間；可選 11:30–13:00 或 17:00–18:30。"
        )

    assigned = choose_tables(free_tables, int(conversation["draft_party_size"]))
    if not assigned:
        wait_minutes = max(10, ((overlapping_groups + 1) // 2) * 10)
        if not conversation["draft_waitlist_accepted"]:
            conversation["draft_estimated_wait_minutes"] = wait_minutes
            conversation["stage"] = "waitlist"
            save_conversation(connection, conversation)
            return (
                f"目前 {reservation_time} 沒有適合的空桌，預估等候約 "
                f"{wait_minutes} 分鐘（每 2 組約增加 10 分鐘）。\n"
                "願意登記候補嗎？回覆「願意」等候，或回覆「取消」放棄訂位。"
            )
        status = "waiting"
        assigned = ()
        wait_minutes = max(
            wait_minutes, int(conversation["draft_estimated_wait_minutes"])
        )
    else:
        status = "confirmed"
        wait_minutes = 0

    connection.execute(
        """INSERT INTO reservations
           (line_user_id, reservation_date, reservation_time, party_size,
            customer_name, contact, note, assigned_tables, created_at, status,
            estimated_wait_minutes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            line_user_id,
            reservation_date,
            reservation_time,
            int(conversation["draft_party_size"]),
            conversation["draft_name"],
            conversation["draft_contact"],
            conversation["draft_note"],
            ",".join(assigned),
            int(time.time()),
            status,
            wait_minutes,
        ),
    )
    party_size = int(conversation["draft_party_size"])
    reset_draft(conversation)
    save_conversation(connection, conversation)
    if status == "waiting":
        return (
            f"已登記候補：{reservation_date} {reservation_time}，{party_size} 人。"
            f"目前預估等候約 {wait_minutes} 分鐘；餐廳會以您提供的聯絡方式與您確認。"
        )
    return (
        f"訂位成功！{reservation_date} {reservation_time}，{party_size} 人。"
    )


def process_text(connection, line_user_id, text):
    message = text.strip()
    conversation = get_conversation(connection, line_user_id)

    if message in ("取消", "取消訂位"):
        reset_draft(conversation)
        save_conversation(connection, conversation)
        return "已取消，沒有送出訂位。"

    if message in ("訂位", "我要訂位", "開始訂位"):
        reset_draft(conversation)
        conversation["stage"] = "date"
        save_conversation(connection, conversation)
        return next_prompt(conversation)

    if message == "我的訂位":
        rows = connection.execute(
            """SELECT reservation_date, reservation_time, party_size,
                      customer_name, status, estimated_wait_minutes
               FROM reservations WHERE line_user_id = ?
               ORDER BY reservation_date, reservation_time LIMIT 10""",
            (line_user_id,),
        ).fetchall()
        if not rows:
            return "目前沒有您的訂位紀錄。傳送「訂位」即可開始預約。"
        return "您的訂位：\n" + "\n".join(
            f"{row['reservation_date']} {row['reservation_time']}｜"
            f"{row['party_size']} 人｜{row['customer_name']}｜"
            + (
                f"候補（預估等候 {row['estimated_wait_minutes']} 分鐘）"
                if row["status"] == "waiting"
                else "已確認"
            )
            for row in rows
        )

    if conversation["stage"] == "waitlist":
        if message in ("願意", "願意等", "候補", "接受"):
            conversation["draft_waitlist_accepted"] = 1
            conversation["stage"] = ""
            response = next_prompt(conversation)
            save_conversation(connection, conversation)
            return response
        return "請回覆「願意」登記候補，或回覆「取消」放棄訂位。"

    start_command = re.match(r"^(?:訂位|我要訂位)\s+(.+)$", message)
    if start_command:
        reset_draft(conversation)
        conversation["stage"] = "date"
        details = start_command.group(1)
    else:
        details = message

    if message in ("確認", "送出") and conversation["stage"] == "confirm":
        error = validate_field("date", conversation["draft_date"])
        if not error:
            error = validate_field("time", conversation["draft_time"])
        if error:
            return error
        return create_reservation(connection, line_user_id, conversation)

    if conversation["stage"] not in (
        "date", "time", "party_size", "name", "contact", "note", "confirm"
    ):
        return "歡迎使用餐廳訂位服務！傳送「訂位」開始預約，或傳送「我的訂位」查詢。"

    if not start_command and conversation["stage"] == "confirm":
        return "請回覆「確認」送出訂位，或回覆「取消」放棄。"

    fields = parse_labeled_fields(details)
    if fields:
        updates = fields
    else:
        updates = {conversation["stage"]: message}

    for field, value in updates.items():
        if field == "note" and value in ("無", "沒有", "不用"):
            conversation["draft_note"] = "無"
            continue
        error = validate_field(field, value)
        if error:
            save_conversation(connection, conversation)
            return error
        conversation[f"draft_{field}"] = value

    if (
        "time" in updates
        and conversation["draft_date"]
        and conversation["draft_time"]
    ):
        start, end = reservation_interval(
            conversation["draft_date"], conversation["draft_time"]
        )
        if not opening_window(start, end):
            conversation["draft_time"] = ""
            conversation["stage"] = "time"
            save_conversation(connection, conversation)
            return (
                "這個時間不在營業時段內，或 90 分鐘用餐時間會超過打烊時間。"
                "請重新輸入時間；可選 11:30–13:00 或 17:00–18:30。"
            )

    if (
        ({"time", "party_size"} & set(updates))
        and conversation["draft_date"]
        and conversation["draft_time"]
        and conversation["draft_party_size"]
    ):
        free_tables, overlapping_groups = available_tables(
            connection,
            conversation["draft_date"],
            conversation["draft_time"],
        )
        if free_tables is None:
            conversation["draft_time"] = ""
            conversation["stage"] = "time"
            save_conversation(connection, conversation)
            return "這個時間不在營業時段內，請重新輸入營業時間內的時間。"
        if not choose_tables(free_tables, int(conversation["draft_party_size"])):
            wait_minutes = max(10, ((overlapping_groups + 1) // 2) * 10)
            conversation["draft_waitlist_accepted"] = 0
            conversation["draft_estimated_wait_minutes"] = wait_minutes
            conversation["stage"] = "waitlist"
            save_conversation(connection, conversation)
            return (
                f"目前 {conversation['draft_time']} 沒有適合的空桌，預估等候約 "
                f"{wait_minutes} 分鐘（每 2 組約增加 10 分鐘）。\n"
                "願意登記候補嗎？回覆「願意」等候，或回覆「取消」放棄訂位。"
            )

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


def admin_authorized(headers):
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        return False
    value = headers.get("Authorization", "")
    if not value.startswith("Basic "):
        return False
    try:
        username, password = base64.b64decode(value[6:], validate=True).decode(
            "utf-8"
        ).split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(username, ADMIN_USERNAME) and hmac.compare_digest(
        password, ADMIN_PASSWORD
    )


def get_admin_data(reservation_date):
    with database_session() as connection:
        rows = connection.execute(
            """SELECT id, reservation_date, reservation_time, party_size,
                      customer_name, contact, note, assigned_tables, status,
                      estimated_wait_minutes
               FROM reservations WHERE reservation_date = ?
               ORDER BY reservation_time, id""",
            (reservation_date,),
        ).fetchall()
    reservations = [dict(row) for row in rows]
    return {
        "date": reservation_date,
        "total_people": sum(
            row["party_size"] for row in reservations if row["status"] == "confirmed"
        ),
        "reservation_count": sum(
            1 for row in reservations if row["status"] == "confirmed"
        ),
        "waiting_people": sum(
            row["party_size"] for row in reservations if row["status"] == "waiting"
        ),
        "waiting_count": sum(
            1 for row in reservations if row["status"] == "waiting"
        ),
        "reservations": reservations,
    }


class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.respond(200, b"ok", "text/plain; charset=utf-8")
            return
        if parsed.path in ("/", "/index.html"):
            self.serve_file("index.html")
            return
        if parsed.path in ("/admin", "/admin.html", "/api/reservations"):
            if not admin_authorized(self.headers):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Restaurant admin"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/api/reservations":
                date_value = parse_qs(parsed.query).get(
                    "date", [datetime.date.today().isoformat()]
                )[0]
                try:
                    datetime.date.fromisoformat(date_value)
                except ValueError:
                    self.respond_json(400, {"error": "date must use YYYY-MM-DD"})
                    return
                self.respond_json(200, get_admin_data(date_value))
                return
            self.serve_file("admin.html")
            return
        self.send_error(404)

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
        self.respond(200, b"ok", "text/plain; charset=utf-8")

    def handle_event(self, event):
        if event.get("type") != "message" or event.get("message", {}).get("type") != "text":
            return
        reply_token = event.get("replyToken")
        line_user_id = event.get("source", {}).get("userId")
        if not reply_token:
            return
        if not line_user_id:
            reply_to_line(reply_token, "請先將我加入好友，再使用餐廳訂位服務。")
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
            response_text = process_text(
                connection, line_user_id, event["message"]["text"]
            )
        reply_to_line(reply_token, response_text)

    def serve_file(self, filename):
        path = os.path.join(os.path.dirname(__file__), filename)
        try:
            with open(path, "rb") as file:
                content = file.read()
        except OSError:
            logger.exception("Could not read web file %s", filename)
            self.send_error(500)
            return
        self.respond(200, content, "text/html; charset=utf-8")

    def respond_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.respond(status, body, "application/json; charset=utf-8")

    def respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        logger.info("%s - %s", self.address_string(), format_string % args)


def main():
    initialize_database()
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), WebhookHandler)
    logger.info("LINE restaurant bot listening on port %s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
