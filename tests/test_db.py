"""单元测试：SQLite 数据库持久化、认证、会话与环境变量同步"""
import os
import shutil
import tempfile
import unittest

os.environ["AB_DATA_DIR"] = tempfile.mkdtemp(prefix="ab_test_")
import gateway.db as db

class TestDB(unittest.TestCase):
    def setUp(self):
        db.init_db()

    def tearDown(self):
        shutil.rmtree(os.environ["AB_DATA_DIR"], ignore_errors=True)

    def test_default_user_and_verify(self):
        # 默认用户名 admin / 默认密码 password
        self.assertTrue(db.verify_user("admin", "password"))
        self.assertFalse(db.verify_user("admin", "wrong_pwd"))
        self.assertFalse(db.verify_user("nonexist", "password"))

    def test_session_lifecycle(self):
        token = db.create_session("admin")
        self.assertIsNotNone(token)
        self.assertEqual(db.validate_session(token), "admin")
        db.destroy_session(token)
        self.assertIsNone(db.validate_session(token))

    def test_password_change(self):
        self.assertTrue(db.change_password("admin", "new_secure_pwd!"))
        self.assertFalse(db.verify_user("admin", "password"))
        self.assertTrue(db.verify_user("admin", "new_secure_pwd!"))

    def test_system_config_kv(self):
        db.set_config("cf_mail_api", "https://mail.example.com/v1", category="mail")
        self.assertEqual(db.get_config("cf_mail_api"), "https://mail.example.com/v1")

if __name__ == "__main__":
    unittest.main()
