"""设置持久化测试：数据目录 + API Key 保存/恢复/文件权限。"""
import os
import tempfile
import unittest
from pathlib import Path

from mainrise import gui_pyqt as g


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig_path = g.SETTINGS_PATH
        g.SETTINGS_PATH = self.tmp / "settings.json"
        os.environ.pop("MAINRISE_HOME", None)
        os.environ.pop("MAINRISE_API_KEY", None)

    def tearDown(self):
        g.SETTINGS_PATH = self.orig_path
        os.environ.pop("MAINRISE_HOME", None)
        os.environ.pop("MAINRISE_API_KEY", None)

    def test_save_and_restore(self):
        os.environ["MAINRISE_HOME"] = "/tmp/foo"
        g._save_settings("sk-test-123")
        g._apply_saved_settings()
        self.assertEqual(os.environ.get("MAINRISE_API_KEY"), "sk-test-123")
        self.assertEqual(os.environ.get("MAINRISE_HOME"), "/tmp/foo")

    def test_file_permission_600(self):
        g._save_settings("sk-test-456")
        mode = g.SETTINGS_PATH.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_empty_settings_no_error(self):
        g._apply_saved_settings()  # 文件不存在时不应报错
        self.assertIsNone(os.environ.get("MAINRISE_API_KEY"))


if __name__ == "__main__":
    unittest.main()
