from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.test.runner import DiscoverRunner

from .test_safety import validate_database, validate_live_connection


class IsolatedTestRunner(DiscoverRunner):
    def __init__(self, *args, **kwargs):
        if settings.SETTINGS_MODULE != "fishsite.settings_test":
            raise ImproperlyConfigured("Tests require fishsite.settings_test.")
        super().__init__(*args, **kwargs)
        if self.parallel > 1:
            raise ImproperlyConfigured("Parallel database clones are not supported by the test guard.")
        if set(settings.DATABASES) != {"default"}:
            raise ImproperlyConfigured("Only the isolated default database is allowed.")
        validate_database(connections["default"].settings_dict, maintenance=True)

    def setup_databases(self, **kwargs):
        validate_database(connections["default"].settings_dict, maintenance=True)
        return super().setup_databases(**kwargs)

    def teardown_databases(self, old_config, **kwargs):
        for connection, _old_name, destroy in old_config:
            if destroy:
                validate_database(connection.settings_dict)
                connection.ensure_connection()
                validate_live_connection(connection.connection)
        return super().teardown_databases(old_config, **kwargs)
