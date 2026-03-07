import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class DayViewRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

        import projects_db

        self.projects_db = projects_db
        self.original_db_path = projects_db.DB_PATH
        projects_db.DB_PATH = self.db_path
        projects_db.init_db()

        import activity_mapper
        import app as dayview_app

        self.activity_mapper = activity_mapper
        self.dayview_app = dayview_app
        self.activity_mapper.init_activity_db()
        self.dayview_app._pending_jobs.clear()
        self.client = self.dayview_app.app.test_client()

    def tearDown(self) -> None:
        self.projects_db.DB_PATH = self.original_db_path
        self.tmpdir.cleanup()

    def test_delete_project_removes_activity_rows_first(self) -> None:
        project_id = self.projects_db.create_project("Delete Me")

        with self.projects_db.get_db() as conn:
            conn.execute(
                """INSERT INTO project_activity
                   (project_id, date, minutes, app_breakdown, frame_count)
                   VALUES (?, ?, ?, ?, ?)""",
                (project_id, "2026-03-01", 5.0, "{}", 60),
            )

        response = self.client.delete(f"/api/projects/{project_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})
        self.assertIsNone(self.projects_db.get_project(project_id))

    def test_historical_activity_uses_cached_rows_without_remap(self) -> None:
        project_id = self.projects_db.create_project("Cached Project")
        past_date = (date.today() - timedelta(days=1)).isoformat()

        with self.projects_db.get_db() as conn:
            conn.execute(
                """INSERT INTO project_activity
                   (project_id, date, minutes, app_breakdown, frame_count)
                   VALUES (?, ?, ?, ?, ?)""",
                (project_id, past_date, 12.5, '{"Cursor": 3}', 150),
            )

        with patch.object(
            self.dayview_app.activity_mapper,
            "map_activity_for_date",
            side_effect=AssertionError("historical activity should use cached rows"),
        ):
            response = self.client.get(f"/api/activity/{past_date}")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["cached"])
        self.assertEqual(payload["date"], past_date)
        self.assertEqual(len(payload["projects"]), 1)
        self.assertEqual(payload["projects"][0]["project_name"], "Cached Project")
        self.assertEqual(payload["projects"][0]["minutes"], 12.5)

    def test_shipped_keeps_same_item_visible_on_multiple_days(self) -> None:
        project_id = self.projects_db.create_project("Repeat Project")
        today = date.today()

        for offset in (0, 1):
            self.projects_db.add_entry(
                project_id,
                (today - timedelta(days=offset)).isoformat(),
                achievements=["Ship widget"],
                source=f"manual-{offset}",
            )

        response = self.client.get("/api/shipped?days=2")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload["days"]), 2)

        item_counts = []
        for day in payload["days"]:
            projects = day["projects"]
            self.assertEqual(len(projects), 1)
            item_counts.append(len(projects[0]["items"]))
            self.assertEqual(projects[0]["items"][0]["text"], "Ship widget")

        self.assertEqual(item_counts, [1, 1])

    def test_create_project_applies_default_tag_mapping(self) -> None:
        project_id = self.projects_db.create_project("DayView")
        project = self.projects_db.get_project(project_id)

        self.assertIsNotNone(project)
        self.assertEqual(project["tag"], "Product")


if __name__ == "__main__":
    unittest.main()
