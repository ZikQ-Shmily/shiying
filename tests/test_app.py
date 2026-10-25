"""Behavior and security regressions using a disposable database only.

Run from the project root with: .venv/bin/python -m unittest discover -s tests -v
"""

import copy
import importlib.util
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch


class MediaTrackerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="media-tracker-tests-")
        cls.password = "OnlyForTests!LongPassword123"
        overrides = {
            "DATA_DIR": cls.temp.name,
            "SECRET_KEY": secrets.token_urlsafe(48),
            "ADMIN_USERNAME": "test-admin",
            "ADMIN_PASSWORD": cls.password,
            "COOKIE_SECURE": "false",
        }
        previous = {key: os.environ.get(key) for key in overrides}
        try:
            os.environ.update(overrides)
            spec = importlib.util.spec_from_file_location(
                "media_tracker_test_app", Path(__file__).parents[1] / "app.py"
            )
            cls.backend = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.backend)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        cls.app = cls.backend.app
        cls.app.config.update(TESTING=True)
        cls.initial_hash = cls.backend.setting("password")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        worker = patch.object(self.backend, 'start_source_refresh', return_value=True)
        worker.start()
        self.addCleanup(worker.stop)
        with self.backend.database() as db:
            db.execute("DELETE FROM items")
            db.execute("DELETE FROM attempts")
            db.execute("DELETE FROM sessions")
            db.execute("UPDATE settings SET value=? WHERE key='password'", (self.initial_hash,))
            db.execute("UPDATE settings SET value=? WHERE key='auth_version'", (secrets.token_hex(16),))
        self.client = self.app.test_client()
        self.csrf = self.login(self.client)

    def login(self, client, password=None):
        result = client.post("/api/login", json={
            "username": "test-admin", "password": password or self.password,
        })
        self.assertEqual(result.status_code, 200, result.get_json())
        return result.get_json()["csrf"]

    def headers(self, csrf=None):
        return {"X-CSRF-Token": csrf or self.csrf}

    def item(self, **changes):
        result = {
            "title": "追更测试 第一季", "kind": "tv", "total_episodes": 12,
            "library_episodes": "1-8,10", "finished": False, "completed": False,
            "sources": [{"id": "source-a", "sharer": "分享者 A", "episodes": "1-12",
                         "post_url": "https://example.com/post/1",
                         "share_url": "https://115.com/s/test", "version": "4K 国语"}],
            "active_source_id": "source-a", "next_check": "2020-01-01",
        }
        result.update(changes)
        return result

    def create(self, item=None):
        result = self.client.post("/api/items", json=item or self.item(), headers=self.headers())
        self.assertEqual(result.status_code, 201, result.get_json())
        return result.get_json()["item"]

    def test_anonymous_cannot_read_or_mutate_private_data(self):
        other = self.app.test_client()
        self.assertFalse(other.get("/api/session").get_json()["authenticated"])
        for url in ("/api/items", "/api/items?trash=1", "/api/backup"):
            self.assertEqual(other.get(url).status_code, 401)
        self.assertEqual(other.post("/api/items", json=self.item()).status_code, 401)

    def test_wanted_item_accepts_empty_source_from_editor(self):
        item = self.create(self.item(sources=[], active_source_id=None,
                                    library_episodes="", total_episodes=None))
        self.assertEqual(item["status"], "wanted")
        self.assertEqual(item["sources"], [])
        self.assertEqual(item["active_source_id"], "")

    def test_csrf_missing_wrong_and_cross_origin_are_rejected(self):
        for headers in ({}, {"X-CSRF-Token": "wrong"},
                        {**self.headers(), "Origin": "https://attacker.example"}):
            response = self.client.post("/api/items", json=self.item(), headers=headers)
            self.assertEqual(response.status_code, 403)
        other = self.app.test_client()
        other_csrf = self.login(other)
        self.assertEqual(self.client.post("/api/items", json=self.item(),
                         headers=self.headers(other_csrf)).status_code, 403)
        self.assertEqual(self.client.post("/api/items", json=self.item(), headers={
            **self.headers(), "Origin": "http://localhost"}).status_code, 201)

    def test_login_rate_limit_and_cross_origin_login(self):
        other = self.app.test_client()
        response = other.post("/api/login", json={"username": "test-admin", "password": self.password},
                              headers={"Origin": "https://attacker.example"})
        self.assertEqual(response.status_code, 403)
        for _ in range(10):
            self.assertEqual(other.post("/api/login", json={
                "username": "test-admin", "password": "incorrect"}).status_code, 401)
        self.assertEqual(other.post("/api/login", json={
            "username": "test-admin", "password": self.password}).status_code, 429)

    def test_episode_holes_are_detected(self):
        item = self.create()
        self.assertEqual(item["missing_episodes"], "9,11-12")
        self.assertEqual(item["library_count"], 9)
        self.assertEqual(item["available_count"], 12)
        self.assertEqual(item["status"], "missing")
        self.assertTrue(item["due"])

    def test_single_episode_number_is_not_interpreted_as_range(self):
        item = self.create(self.item(library_episodes="12"))
        self.assertEqual(item["library_count"], 1)
        self.assertEqual(item["missing_episodes"], "1-11")

    def test_invalid_episode_ranges_are_rejected(self):
        for episodes in ("0", "5-1", "5001", "1-", "1,", "1.5", "S01E01"):
            response = self.client.post("/api/items", json=self.item(library_episodes=episodes),
                                        headers=self.headers())
            self.assertEqual(response.status_code, 400, episodes)

    def test_movie_progress_and_invalid_episode_are_handled(self):
        movie = self.item(kind="movie", total_episodes=None, library_episodes="",
                          sources=[{"id": "movie-source", "episodes": "1"}],
                          active_source_id="movie-source")
        item = self.create(movie)
        self.assertEqual(item["total_episodes"], 1)
        self.assertEqual(item["status"], "pending")
        movie["library_episodes"] = "1"
        response = self.client.put("/api/items/" + item["id"], json=movie, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["item"]["status"], "complete")
        movie["library_episodes"] = "2"
        self.assertEqual(self.client.put("/api/items/" + item["id"], json=movie,
                                       headers=self.headers()).status_code, 400)

    def test_changing_active_source_preserves_other_source_details(self):
        payload = self.item()
        payload["sources"].append({"id": "source-b", "sharer": "分享者 B", "episodes": "1-10",
                                   "post_url": "https://example.org/post/2", "version": "1080P"})
        item = self.create(payload)
        original_sources = copy.deepcopy(item["sources"])
        item["active_source_id"] = "source-b"
        response = self.client.put("/api/items/" + item["id"], json=item, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        updated = response.get_json()["item"]
        self.assertEqual(updated["sources"], original_sources)
        self.assertEqual(updated["active_source_id"], "source-b")
        self.assertEqual(updated["missing_episodes"], "9")

    def test_unknown_total_requires_explicit_complete_and_no_missing(self):
        item = self.create(self.item(total_episodes=None, library_episodes="1-12"))
        self.assertEqual(item["status"], "updating")
        item["finished"] = True
        response = self.client.put("/api/items/" + item["id"], json=item, headers=self.headers())
        self.assertEqual(response.get_json()["item"]["status"], "waiting_source")
        item["completed"] = True
        response = self.client.put("/api/items/" + item["id"], json=item, headers=self.headers())
        self.assertEqual(response.get_json()["item"]["status"], "complete")
        item["sources"][0]["episodes"] = "1-13"
        response = self.client.put("/api/items/" + item["id"], json=item, headers=self.headers())
        self.assertEqual(response.get_json()["item"]["status"], "missing")

    def test_soft_delete_and_restore(self):
        item = self.create()
        url = "/api/items/" + item["id"]
        self.assertEqual(self.client.delete(url, headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get("/api/items").get_json()["items"], [])
        self.assertEqual(self.client.get("/api/items?trash=1").get_json()["items"][0]["id"], item["id"])
        self.assertEqual(self.client.put(url, json=item, headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.post(url + "/restore", headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get("/api/items").get_json()["items"][0]["id"], item["id"])
        self.assertEqual(self.client.get("/api/items?trash=1").get_json()["items"], [])

    def test_backup_restore_to_empty_library_includes_trash_and_progress(self):
        item = self.create()
        deleted = self.create(self.item(title="回收站记录"))
        self.client.delete("/api/items/" + deleted["id"], headers=self.headers())
        backup_response = self.client.get("/api/backup")
        self.assertIn("attachment", backup_response.headers["Content-Disposition"])
        backup = backup_response.get_json()
        self.assertNotIn("password", backup)
        self.assertEqual(backup["schema_version"], 1)
        with self.backend.database() as db:
            db.execute("DELETE FROM items")
        response = self.client.post("/api/import", json=backup, headers=self.headers())
        self.assertEqual(response.get_json(), {"imported": 2, "skipped": 0})
        restored = self.client.get("/api/items").get_json()["items"]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["id"], item["id"])
        self.assertEqual(restored[0]["sources"], item["sources"])
        self.assertEqual(restored[0]["library_episodes"], item["library_episodes"])
        self.assertEqual(len(self.client.get("/api/items?trash=1").get_json()["items"]), 1)
        self.assertEqual(self.client.post("/api/import", json=backup, headers=self.headers()).get_json(),
                         {"imported": 0, "skipped": 2})

    def test_backup_restores_original_timestamps(self):
        item = self.create()
        backup = self.client.get("/api/backup").get_json()
        backup["items"][0].update(created_at="2024-01-01T08:00:00+08:00",
                                   updated_at="2024-02-02T09:00:00+08:00",
                                   last_check="2024-02-01T10:00:00+08:00")
        with self.backend.database() as db:
            db.execute("DELETE FROM items")
        response = self.client.post("/api/import", json=backup, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.get_json())
        restored = self.client.get("/api/items").get_json()["items"][0]
        for field in ("created_at", "updated_at", "last_check"):
            self.assertEqual(restored[field], backup["items"][0][field], field)

    def test_invalid_backup_is_atomic(self):
        item = self.create()
        backup = self.client.get("/api/backup").get_json()
        backup["items"].append({"id": "bad-id", "title": "invalid"})
        with self.backend.database() as db:
            db.execute("DELETE FROM items")
        self.assertEqual(self.client.post("/api/import", json=backup,
                                        headers=self.headers()).status_code, 400)
        self.assertEqual(self.client.get("/api/items").get_json()["items"], [])

    def test_executable_credential_and_relative_links_are_rejected(self):
        for bad_link in ("javascript:alert(1)", "data:text/html,hello", "//example.com", "/local",
                         "file:///etc/passwd", "https://user:pass@example.com/", "https://"):
            item = self.item()
            item["sources"][0]["share_url"] = bad_link
            response = self.client.post("/api/items", json=item, headers=self.headers())
            self.assertEqual(response.status_code, 400, bad_link)

    def test_password_change_invalidates_old_sessions_and_csrf(self):
        other = self.app.test_client()
        other_csrf = self.login(other)
        old_csrf = self.csrf
        new_password = "ChangedForTests!LongPassword456"
        response = self.client.post("/api/password", json={
            "old_password": self.password, "new_password": new_password}, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.get_json())
        self.csrf = response.get_json()["csrf"]
        self.assertNotEqual(old_csrf, self.csrf)
        self.assertEqual(other.get("/api/items").status_code, 401)
        self.assertEqual(other.post("/api/items", json=self.item(),
                                   headers=self.headers(other_csrf)).status_code, 401)
        self.assertEqual(self.client.post("/api/items", json=self.item(),
                                         headers=self.headers(old_csrf)).status_code, 403)
        self.assertEqual(self.client.get("/api/items").status_code, 200)
        self.assertEqual(other.post("/api/login", json={
            "username": "test-admin", "password": self.password}).status_code, 401)
        self.login(other, new_password)

    def test_logout_revokes_copied_session_cookie(self):
        copied_session = self.app.test_client()
        copied_session.set_cookie("session", self.client.get_cookie("session").value)
        self.assertEqual(copied_session.get("/api/items").status_code, 200)
        response = self.client.post("/api/logout", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.get("/api/session").get_json()["authenticated"])
        self.assertEqual(copied_session.get("/api/items").status_code, 401)


if __name__ == "__main__":
    unittest.main()
