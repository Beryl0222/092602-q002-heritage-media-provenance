import unittest

from heritage_media_provenance.service import Service
from heritage_media_provenance.store import Store


class 基础行为测试(unittest.TestCase):
    def test_health(self):
        self.assertEqual(Service(Store()).health()["status"], "ok")

    def test_register_and_find(self):
        service = Service(Store())
        saved = service.register({"record_id": "asset-001", "owner_id": "editorial-board", "state": "collected", "revision": 1})
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(service.find(saved["record_id"])["owner_id"], saved["owner_id"])

    def test_duplicate_is_rejected(self):
        service = Service(Store())
        payload = {"record_id": "asset-001", "owner_id": "editorial-board", "state": "collected", "revision": 1}
        service.register(payload)
        with self.assertRaises(Exception):
            service.register(payload)


if __name__ == "__main__":
    unittest.main()
