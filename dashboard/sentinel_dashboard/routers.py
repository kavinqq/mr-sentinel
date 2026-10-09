"""Route the `reviews` app to sentinel.db and keep Django's hands off its schema."""

HISTORY_APP = "reviews"


class HistoryRouter:
    def db_for_read(self, model, **hints):
        return "history" if model._meta.app_label == HISTORY_APP else None

    def db_for_write(self, model, **hints):
        return "history" if model._meta.app_label == HISTORY_APP else None

    def allow_relation(self, obj1, obj2, **hints):
        if HISTORY_APP in (obj1._meta.app_label, obj2._meta.app_label):
            return obj1._meta.app_label == obj2._meta.app_label
        return None

    def allow_migrate(self, db, app_label, **hints):
        # the history package owns sentinel.db's schema; Django migrates only its own db
        if db == "history" or app_label == HISTORY_APP:
            return False
        return None
