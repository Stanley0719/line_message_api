import base64
import datetime
import hashlib
import hmac
import os
import tempfile
import unittest

import server


class RestaurantBotTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = server.DATABASE_PATH
        server.DATABASE_PATH = os.path.join(self.temp_dir.name, "test.db")
        server.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(setattr, server, "DATABASE_PATH", self.original_path)
        self.connection = server.connect_database()
        self.addCleanup(self.connection.close)
        self.reservation_date = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

    def enter_booking_details(self):
        responses = [
            server.process_text(self.connection, "u1", "訂位"),
            server.process_text(self.connection, "u1", self.reservation_date),
            server.process_text(self.connection, "u1", "12:00"),
            server.process_text(self.connection, "u1", "3"),
            server.process_text(self.connection, "u1", "王小明"),
            server.process_text(self.connection, "u1", "0912345678"),
            server.process_text(self.connection, "u1", "靠窗"),
        ]
        return responses

    def test_collects_booking_details_and_assigns_a_four_person_table(self):
        responses = self.enter_booking_details()
        self.assertIn("特殊需求", responses[-2])
        self.assertIn("確認", responses[-1])
        confirmation = server.process_text(self.connection, "u1", "確認")
        self.assertIn("訂位成功", confirmation)
        self.assertIn("4人桌", confirmation)
        row = self.connection.execute(
            """SELECT reservation_date, reservation_time, party_size,
                      customer_name, contact, note, assigned_tables
               FROM reservations"""
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                self.reservation_date,
                "12:00",
                3,
                "王小明",
                "0912345678",
                "靠窗",
                "4人桌-1",
            ),
        )

    def test_parses_all_labeled_fields_and_asks_optional_note(self):
        response = server.process_text(
            self.connection,
            "u1",
            f"訂位 日期：{self.reservation_date} 時間：12:00 人數：2 "
            "姓名：林小姐 聯絡方式：0911222333",
        )
        self.assertIn("特殊需求", response)
        response = server.process_text(self.connection, "u1", "無")
        self.assertIn("確認", response)
        self.assertIn("訂位成功", server.process_text(self.connection, "u1", "確認"))
        row = self.connection.execute("SELECT note, assigned_tables FROM reservations").fetchone()
        self.assertEqual(tuple(row), ("無", "2人桌-1"))

    def test_rejects_out_of_hours_and_invalid_date(self):
        response = server.process_text(
            self.connection, "u1", f"訂位 日期：2026-99-99 時間：12:00"
        )
        self.assertIn("日期格式", response)

        server.process_text(self.connection, "u1", "取消")
        server.process_text(self.connection, "u1", "訂位")
        server.process_text(self.connection, "u1", self.reservation_date)
        response = server.process_text(self.connection, "u1", "14:00")
        self.assertIn("人數", response)
        for answer in ("2", "王小明", "0912345678", "無"):
            response = server.process_text(self.connection, "u1", answer)
        self.assertIn("確認", response)
        response = server.process_text(self.connection, "u1", "確認")
        self.assertIn("不在營業時段", response)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],
            0,
        )
        self.assertIn(
            "確認",
            server.process_text(self.connection, "u1", "12:00"),
        )

    def test_allocates_different_tables_for_overlapping_bookings(self):
        for user_id in ("u1", "u2"):
            server.process_text(
                self.connection,
                user_id,
                f"訂位 日期：{self.reservation_date} 時間：12:00 人數：2 "
                f"姓名：{user_id} 聯絡方式：0912345678 備註：無",
            )
            server.process_text(self.connection, user_id, "確認")
        tables = self.connection.execute(
            "SELECT assigned_tables FROM reservations ORDER BY id"
        ).fetchall()
        self.assertEqual([row[0] for row in tables], ["2人桌-1", "2人桌-2"])

    def test_full_capacity_reports_wait_and_does_not_confirm_booking(self):
        for index, table in enumerate(server.TABLES):
            self.connection.execute(
                """INSERT INTO reservations
                   (line_user_id, reservation_date, reservation_time, party_size,
                    customer_name, contact, note, assigned_tables, created_at)
                   VALUES (?, ?, '12:00', 2, '客人', '0912345678', '', ?, 0)""",
                (f"existing-{index}", self.reservation_date, table),
            )
        response = server.process_text(
            self.connection,
            "u1",
            f"訂位 日期：{self.reservation_date} 時間：12:00 人數：2 "
            "姓名：新客 聯絡方式：0912345678 備註：無",
        )
        self.assertIn("確認", response)
        response = server.process_text(self.connection, "u1", "確認")
        self.assertIn("等候約 60 分鐘", response)
        self.assertIn("尚未替您保留座位", response)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],
            12,
        )

    def test_table_availability_frees_tables_after_ninety_minutes(self):
        self.connection.execute(
            """INSERT INTO reservations
               (line_user_id, reservation_date, reservation_time, party_size,
                customer_name, contact, note, assigned_tables, created_at)
               VALUES ('u1', ?, '11:30', 2, '客人', '0912345678', '', '2人桌-1', 0)""",
            (self.reservation_date,),
        )
        free, groups = server.available_tables(
            self.connection, self.reservation_date, "13:00"
        )
        self.assertIn("2人桌-1", free)
        self.assertEqual(groups, 0)

    def test_admin_totals_and_basic_auth(self):
        self.connection.execute(
            """INSERT INTO reservations
               (line_user_id, reservation_date, reservation_time, party_size,
                customer_name, contact, note, assigned_tables, created_at)
               VALUES ('u1', ?, '12:00', 3, '客人', '0912345678', '靠窗', '4人桌-1', 0)""",
            (self.reservation_date,),
        )
        self.connection.commit()
        data = server.get_admin_data(self.reservation_date)
        self.assertEqual((data["reservation_count"], data["total_people"]), (1, 3))
        original_username = server.ADMIN_USERNAME
        original_password = server.ADMIN_PASSWORD
        server.ADMIN_USERNAME = "staff"
        server.ADMIN_PASSWORD = "strong-password"
        self.addCleanup(setattr, server, "ADMIN_USERNAME", original_username)
        self.addCleanup(setattr, server, "ADMIN_PASSWORD", original_password)
        credentials = base64.b64encode(b"staff:strong-password").decode("ascii")
        self.assertTrue(server.admin_authorized({"Authorization": f"Basic {credentials}"}))
        self.assertFalse(server.admin_authorized({"Authorization": "Basic invalid"}))

    def test_cancel_does_not_save_reservation(self):
        server.process_text(self.connection, "u1", "訂位")
        response = server.process_text(self.connection, "u1", "取消")
        self.assertIn("已取消", response)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],
            0,
        )

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
