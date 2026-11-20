import os
import tempfile
import unittest
import base64
import hashlib
import hmac

import server


class MeetingBotTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = server.DATABASE_PATH
        server.DATABASE_PATH = os.path.join(self.temp_dir.name, "test.db")
        server.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(setattr, server, "DATABASE_PATH", self.original_path)
        self.connection = server.connect_database()
        self.addCleanup(self.connection.close)

    def test_prompts_for_each_missing_field_then_saves(self):
        self.assertIn("名稱", server.process_text(self.connection, "u1", "記錄會議"))
        self.assertIn("時間", server.process_text(self.connection, "u1", "產品週會"))
        self.assertIn("地點", server.process_text(self.connection, "u1", "明天下午 2 點"))
        self.assertIn("確認", server.process_text(self.connection, "u1", "三樓會議室"))
        self.assertIn("已記錄", server.process_text(self.connection, "u1", "確認"))
        rows = self.connection.execute(
            "SELECT name, meeting_time, location FROM meetings WHERE owner_id = ?",
            ("u1",),
        ).fetchall()
        self.assertEqual(
            (rows[0]["name"], rows[0]["meeting_time"], rows[0]["location"]),
            ("產品週會", "明天下午 2 點", "三樓會議室"),
        )

    def test_parses_all_labeled_fields_from_one_message(self):
        server.process_text(
            self.connection,
            "u1",
            "記錄會議 名稱：產品週會，時間：12/8 14:30，地點：三樓會議室",
        )
        response = server.process_text(self.connection, "u1", "確認")
        self.assertIn("已記錄", response)

    def test_cancel_does_not_save_draft(self):
        server.process_text(self.connection, "u1", "記錄會議")
        response = server.process_text(self.connection, "u1", "取消")
        self.assertIn("已取消", response)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM meetings").fetchone()[0],
            0,
        )

    def test_meetings_are_private_to_each_line_user(self):
        server.process_text(
            self.connection,
            "u1",
            "記錄會議 名稱：週會 時間：星期五 地點：線上",
        )
        server.process_text(self.connection, "u1", "確認")
        self.assertIn("週會", server.process_text(self.connection, "u1", "顯示會議"))
        self.assertIn("沒有已記錄", server.process_text(self.connection, "u2", "顯示會議"))

    def test_signature_validation(self):
        original_secret = server.LINE_CHANNEL_SECRET
        server.LINE_CHANNEL_SECRET = "test-secret"
        self.addCleanup(setattr, server, "LINE_CHANNEL_SECRET", original_secret)
        body = b'{"events":[]}'
        self.assertFalse(server.verify_signature(body, "invalid"))
        digest = hmac.new(b"test-secret", body, hashlib.sha256).digest()
        signature = base64.b64encode(digest).decode("ascii")
        self.assertTrue(server.verify_signature(body, signature))


if __name__ == "__main__":
    unittest.main()
