"""Новые сценарии v2.2: проверка ввода и границ операций записи."""
import contextlib
from dataclasses import replace
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_cyberwall_master_manager import app, ACCOUNT, Cursor, connection, pymysql
from modules.account_fields import parse_column, validate_name, editable_columns


def column(name, sql_type="varchar(255)", nullable=True, default=None, extra=""):
    return dict(Field=name, Type=sql_type, Null="YES" if nullable else "NO", Default=default, Extra=extra, Key="")


SCHEMA = [column("name"), column("password"), column("money", "int", default="0"),
          column("losa", "int", default="0"), column("netrunner_lvl", "tinyint unsigned", default="0"),
          column("police", "tinyint(1)", default="0"), column("occupancy"),
          column("joinrpg_id", "int unsigned")]
VALUES = dict(name="Банк Востока", password="00123", money=0, losa=100, netrunner_lvl=0,
              police=None, occupancy=None, joinrpg_id=None)
ROW = dict(id=123456789, card_id=75, name="Банк Востока", money=0, losa=100, netrunner_lvl=0, police=None)


class WriteCursor(Cursor):
    def __init__(self, row=None, fail=None):
        super().__init__(row)
        self.lastrowid = 75
        self.rowcount = 1
        self.fail = fail

    def execute(self, sql, args=()):
        super().execute(sql, args)
        if self.fail and sql.startswith(self.fail):
            raise pymysql.MySQLError(1142, "permission denied")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.console_patch = patch.object(app, "CONSOLE", None)
        self.console_patch.start()

    def tearDown(self):
        self.console_patch.stop()
        self.redirect.__exit__(None, None, None)

    def fake_db(self):
        db = Mock()
        db.refresh.side_effect = lambda value: value
        db.account_schema.return_value = SCHEMA
        db.create_account.return_value = app.account_from_row(ROW)
        return db

    def test_name_rules_identify_each_violation(self):
        with self.assertRaises(ValueError) as caught:
            validate_name("Первый Второй Третий Оченьдлинноеслово")
        self.assertIn("4 слов", str(caught.exception))
        self.assertIn("Слово №4", str(caught.exception))
        validate_name("А" * 11 + " " + "Б" * 11 + " " + "В" * 11)

    def test_name_edit_reprompts_until_valid(self):
        db = self.fake_db()
        db.field_value.return_value = "Старое имя"
        with patch("builtins.input", side_effect=["Один Два Три Четыре", "А" * 12, "Банк Востока", "да"]):
            app.edit_account_field(db, ACCOUNT, "name")
        db.set_field.assert_called_once_with(ACCOUNT.card_id, "name", "Старое имя", "Банк Востока")
        self.assertIn("Слово №1", self.output.getvalue())

    def test_police_null_and_zero_display_identically(self):
        self.assertEqual(app.entity_values(replace(ACCOUNT, police=None)), app.entity_values(replace(ACCOUNT, police=0)))
        self.assertEqual(app.display_field("police", None), "0")

    def test_password_cyrillic_retry_happens_before_connect(self):
        with patch.object(app, "getpass", side_effect=["пароль", "mixЯ", "secret"]), patch.object(app, "read_input", return_value=""), patch.object(app.pymysql, "connect", create=True) as connect:
            app.connect_interactively(store=Mock(load=Mock(return_value=None)))
        connect.assert_called_once()
        self.assertEqual(connect.call_args.kwargs["password"], "secret")
        self.assertEqual(self.output.getvalue().count("Пароль введён кириллицей"), 2)
        self.assertNotIn("secret", self.output.getvalue())

    def test_schema_skips_generated_identifiers_and_rejects_unknown_types(self):
        columns = [column("id", "int unsigned"), column("card_id", "bigint unsigned", extra="auto_increment"), *SCHEMA]
        self.assertEqual(editable_columns(columns), SCHEMA)
        with self.assertRaises(ValueError):
            editable_columns([*SCHEMA, column("new_field", "json")])

    def test_creation_fields_preserve_password_and_validate_ranges(self):
        self.assertEqual(parse_column(column("password"), " 00123 "), " 00123 ")
        self.assertIsNone(parse_column(column("joinrpg_id", "int unsigned"), ""))
        self.assertIsNone(parse_column(column("police", "tinyint(1)", default="0"), "0"))
        for col, value in [(column("netrunner_lvl", "tinyint unsigned"), "256"),
                           (column("joinrpg_id", "int unsigned"), "-1"),
                           (column("police", "tinyint(1)"), "2"),
                           (column("occupancy", "varchar(4)"), "Пять!")]:
            with self.assertRaises(ValueError):
                parse_column(col, value)

    def test_create_wizard_all_fields_and_sticker_reminder(self):
        db = self.fake_db()
        db.account_schema.return_value = [
            {**col, "Default": "12345"} if col["Field"] == "joinrpg_id" else col
            for col in SCHEMA
        ]
        with patch("builtins.input", side_effect=["Банк Востока", "00123", "0", "100", "0", "0", "", "да"]) as prompt:
            result = app.create_account(db)
        db.create_account.assert_called_once_with(VALUES)
        self.assertEqual(prompt.call_count, 8)  # Семь полей и подтверждение; JoinRPG не запрашивается.
        self.assertIn("принадлежность (occupancy)", self.output.getvalue())
        self.assertNotIn("занятие", self.output.getvalue())
        self.assertNotIn("ID заявки JoinRPG (joinrpg_id)", self.output.getvalue())
        self.assertEqual(result.card_id, 75)
        self.assertIn("123 456 789", self.output.getvalue())
        self.assertIn("Напиши на наклейке", self.output.getvalue())

    def test_create_cancel_never_inserts(self):
        db = self.fake_db()
        with patch("builtins.input", side_effect=["Банк Востока", ":b"]):
            self.assertIsNone(app.create_account(db))
        db.create_account.assert_not_called()

    def test_create_insert_omits_ids_and_reads_generated_values(self):
        cursor = WriteCursor(ROW)
        conn = connection(cursor)
        db = app.Database(conn)
        db.account_schema = Mock(return_value=SCHEMA)
        result = db.create_account(VALUES)
        insert = next((sql, args) for sql, args in cursor.calls if sql.startswith("INSERT"))
        self.assertNotIn("`id`", insert[0])
        self.assertNotIn("`card_id`", insert[0])
        self.assertNotIn("Банк", insert[0])
        self.assertIn("00123", insert[1])
        self.assertIsNone(insert[1][[col["Field"] for col in SCHEMA].index("joinrpg_id")])
        self.assertEqual(cursor.calls[-1][1], (75,))
        self.assertEqual(result.number, 123456789)
        conn.commit.assert_called_once()

    def test_create_bad_trigger_rolls_back(self):
        cursor = WriteCursor({**ROW, "id": 0})
        conn = connection(cursor)
        db = app.Database(conn)
        db.account_schema = Mock(return_value=SCHEMA)
        with self.assertRaises(ValueError):
            db.create_account(VALUES)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_create_duplicate_is_not_retried(self):
        cursor = WriteCursor(ROW)
        original = cursor.execute
        def execute(sql, args=()):
            original(sql, args)
            if sql.startswith("INSERT"):
                raise pymysql.MySQLError(1062, "Duplicate entry")
        cursor.execute = execute
        conn = connection(cursor)
        db = app.Database(conn)
        db.account_schema = Mock(return_value=SCHEMA)
        with self.assertRaises(pymysql.MySQLError):
            db.create_account(VALUES)
        self.assertEqual(sum(sql.startswith("INSERT") for sql, _ in cursor.calls), 1)
        conn.rollback.assert_called_once()

    def test_search_new_returns_created_account(self):
        db = self.fake_db()
        with patch.object(app, "read_input", side_effect=[":menu", "1"]), patch.object(app, "create_account", return_value=ACCOUNT):
            self.assertEqual(app.choose_candidate(db), ACCOUNT)
        db.search.assert_not_called()

    def test_delete_confirmation_rejects_card_id_and_accepts_display_id(self):
        db = self.fake_db()
        with patch.object(app, "read_input", side_effect=[str(ACCOUNT.card_id), "123 456 789"]):
            self.assertTrue(app.delete_entity(db, ACCOUNT))
        self.assertIn("неправильный ID", self.output.getvalue())
        db.delete_entity.assert_called_once_with(ACCOUNT)

    def test_delete_cancel_does_not_delete(self):
        db = self.fake_db()
        with patch.object(app, "read_input", side_effect=["ошибка", ""]):
            self.assertFalse(app.delete_entity(db, ACCOUNT))
        db.delete_entity.assert_not_called()

    def test_delete_account_atomic_cleanup_without_registry_or_logs(self):
        cursor = WriteCursor({"id": ACCOUNT.number})
        conn = connection(cursor)
        app.Database(conn).delete_entity(ACCOUNT)
        self.assertEqual([sql.split()[0] for sql, _ in cursor.calls], ["SELECT", "DELETE", "DELETE"])
        self.assertIn("FOR UPDATE", cursor.calls[0][0])
        self.assertEqual(cursor.calls[1], ("DELETE FROM lock_access WHERE card_id = %s", (ACCOUNT.card_id,)))
        self.assertFalse(any("logs" in sql or "id_registry" in sql for sql, _ in cursor.calls))
        conn.commit.assert_called_once()

    def test_delete_concurrent_id_change_prevents_any_deletes(self):
        cursor = WriteCursor({"id": ACCOUNT.number + 1})
        conn = connection(cursor)
        with self.assertRaises(app.SelectionChanged):
            app.Database(conn).delete_entity(ACCOUNT)
        self.assertEqual(len(cursor.calls), 1)
        conn.rollback.assert_called_once()

    def test_delete_failure_rolls_back_rights_cleanup(self):
        cursor = WriteCursor({"id": ACCOUNT.number}, fail="DELETE FROM main")
        conn = connection(cursor)
        with self.assertRaises(pymysql.MySQLError):
            app.Database(conn).delete_entity(ACCOUNT)
        self.assertTrue(any(sql.startswith("DELETE FROM lock_access") for sql, _ in cursor.calls))
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()

    def test_lock_delete_uses_lock_id(self):
        lock = app.Entity("lock", 9, "Склад")
        cursor = WriteCursor({"description": lock.label})
        conn = connection(cursor)
        app.Database(conn).delete_entity(lock)
        self.assertEqual(cursor.calls[-1], ("DELETE FROM locks WHERE lock_id = %s", (9,)))
        conn.commit.assert_called_once()

    def test_lock_description_ignores_account_name_limits(self):
        lock = app.Entity("lock", 9, "Склад")
        db = self.fake_db()
        db.lock_description_schema.return_value = column("description")
        text = "Оченьдлинноеописание замка из нескольких длинных слов"
        with patch.object(app, "read_input", side_effect=[text, "да"]):
            app.edit_lock_description(db, lock)
        db.set_lock_description.assert_called_once_with(lock, text)

    def test_lock_update_detects_concurrent_edit(self):
        cursor = WriteCursor({"description": "Изменено"})
        conn = connection(cursor)
        db = app.Database(conn)
        db.lock_description_schema = Mock(return_value=column("description"))
        with self.assertRaises(app.SelectionChanged):
            db.set_lock_description(app.Entity("lock", 9, "Склад"), "Новое")
        self.assertFalse(any(sql.startswith("UPDATE") for sql, _ in cursor.calls))
        conn.rollback.assert_called_once()

    def test_successful_card_write_reminds_sticker(self):
        db = self.fake_db()
        console = Mock()
        console.write_number.return_value = (ACCOUNT.card_id, None)
        with patch.object(app, "CONSOLE", console), patch.object(app, "read_input", return_value="да"):
            app.write_account_card(db, ACCOUNT)
        self.assertIn("на наклейке карты написан правильный ID: 123 456 789", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
