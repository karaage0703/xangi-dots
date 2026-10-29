import hashlib
from pathlib import Path
import tempfile
import unittest

from xangi_dots.store import Store


class StorageLimitTests(unittest.TestCase):
    def test_default_custom_and_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            default = Store(Path(tmp) / "default.sqlite")
            custom = Store(Path(tmp) / "custom.sqlite", max_storage_bytes=3)
            try:
                self.assertEqual(default.health()["storage"]["max_bytes"], 20 * 1024**3)
                args = ("one", "a", "text/plain", 3, hashlib.sha256(b"abc").hexdigest())
                row = custom.create_file(*args)
                self.assertEqual(
                    custom.health()["storage"],
                    {
                        "max_bytes": 3,
                        "reserved_bytes": 3,
                        "available_bytes": 0,
                    },
                )
                self.assertEqual(custom.create_file(*args)["id"], row["id"])
                with self.assertRaisesRegex(ValueError, "storage quota"):
                    custom.create_file(
                        "two", "b", "text/plain", 1, hashlib.sha256(b"x").hexdigest()
                    )
                custom.delete_file(row["id"])
                self.assertEqual(custom.health()["storage"]["available_bytes"], 3)
                self.assertEqual(default.health()["storage"]["reserved_bytes"], 0)
            finally:
                default.db.close()
                custom.db.close()

    def test_invalid_limits_before_database_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.sqlite"
            for value in [0, -1, True, False, 1.5, "20", None]:
                with (
                    self.subTest(value=value),
                    self.assertRaisesRegex(ValueError, "positive integer"),
                ):
                    Store(path, max_storage_bytes=value)
            self.assertFalse(path.exists())

    def test_lowering_limit_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.sqlite"
            store = Store(path)
            row = store.create_file("one", "a", "text/plain", 3, hashlib.sha256(b"abc").hexdigest())
            store.write_file(row["id"], 0, "abc", "utf-8")
            store.finish_file(row["id"])
            store.db.close()
            store = Store(path, max_storage_bytes=1)
            try:
                self.assertEqual(store.file_row(row["id"])["status"], "ready")
                self.assertEqual(store.health()["storage"]["available_bytes"], 0)
                with self.assertRaisesRegex(ValueError, "storage quota"):
                    store.create_file("two", "b", "text/plain", 1, hashlib.sha256(b"x").hexdigest())
            finally:
                store.db.close()
