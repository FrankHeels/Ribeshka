from django.db.backends.postgresql.base import DatabaseWrapper as PostgreSQLWrapper
from django.db.backends.postgresql.operations import DatabaseOperations

from fishsite.test_safety import validate_database, validate_live_connection


class GuardedOperations(DatabaseOperations):
    def sql_flush(self, style, tables, *, reset_sequences=False, allow_cascade=False):
        validate_database(self.connection.settings_dict)
        self.connection.ensure_connection()
        validate_live_connection(self.connection.connection)
        return super().sql_flush(
            style, tables, reset_sequences=reset_sequences, allow_cascade=allow_cascade
        )


class DatabaseWrapper(PostgreSQLWrapper):
    ops_class = GuardedOperations

    def get_connection_params(self):
        config = self.settings_dict.copy()
        # Django's maintenance connection uses NAME=None for postgres.
        if self.alias == "__no_db__" and config["NAME"] is None:
            config["NAME"] = "postgres"
        validate_database(config, maintenance=self.alias == "__no_db__")
        return super().get_connection_params()

    def get_new_connection(self, conn_params):
        raw = super().get_new_connection(conn_params)
        try:
            # Validate in autocommit so no transaction leaks into Django setup.
            raw.autocommit = True
            validate_live_connection(raw, maintenance=self.alias == "__no_db__")
            raw.autocommit = False
        except BaseException:
            raw.close()
            raise
        return raw
