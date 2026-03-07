"""Flask app bootstrap for the DayView dashboard."""

from __future__ import annotations

from flask import Flask

import activity_mapper
import projects_db
from daily_routes import register_daily_routes
from dashboard_routes import register_dashboard_routes
from project_routes import register_project_routes
from route_helpers import _pending_jobs


def create_app() -> Flask:
    app = Flask(__name__)

    projects_db.init_db()
    activity_mapper.init_activity_db()

    register_daily_routes(app)
    register_project_routes(app)
    register_dashboard_routes(app)

    return app


app = create_app()


if __name__ == "__main__":
    import os as _os

    app.run(
        host="127.0.0.1",
        port=5051,
        debug=_os.environ.get("FLASK_DEBUG", "1") == "1",
    )
