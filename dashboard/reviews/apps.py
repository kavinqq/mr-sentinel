import os
import sys
import threading

from django.apps import AppConfig


class ReviewsConfig(AppConfig):
    name = "reviews"
    verbose_name = "Review 歷史"

    def ready(self):
        # Warm the 個人軌跡 analysis (a ~2 s Monte-Carlo) in the background when the
        # server starts, so the first click is not the slow one. Only for the serving
        # process (runserver's reloader child / a WSGI server), never in tests or commands.
        serving = os.environ.get("RUN_MAIN") == "true" or "gunicorn" in sys.argv[0] or "uwsgi" in sys.argv[0]
        if serving:
            threading.Thread(target=_warm, daemon=True).start()


def _warm():
    try:
        from . import services
        services.warm_trajectory()
    except Exception:          # a warm-up must never break the server
        pass
