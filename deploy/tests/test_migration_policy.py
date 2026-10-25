import unittest

from deploy.migration_policy import UnsafeMigration, check_changes, validate_added


def migration(operation):
    return "from django import migrations, models\nclass Migration(migrations.Migration):\n    dependencies = [('catalog', '0001_initial')]\n    operations = [" + operation + "]\n"


class MigrationPolicyTests(unittest.TestCase):
    def test_nullable_scalar_column_accepts_old_insert_shape(self):
        validate_added(migration("migrations.AddField(model_name='product', name='note', field=models.TextField(null=True))"))

    def test_new_table_and_non_unique_index_are_allowed(self):
        for op in (
            "migrations.CreateModel(name='Extra', fields=[('id', models.BigAutoField(primary_key=True))])",
            "migrations.AddIndex(model_name='product', index=models.Index(fields=['name'], name='product_name_idx'))",
        ):
            validate_added(migration(op))

    def test_python_default_is_not_a_database_default_for_old_writers(self):
        with self.assertRaises(UnsafeMigration):
            validate_added(migration("migrations.AddField(model_name='product', name='flag', field=models.BooleanField(default=False))"))

    def test_destructive_data_custom_and_constraint_operations_are_rejected(self):
        for op in (
            "migrations.RemoveField(model_name='product', name='name')",
            "migrations.AlterField(model_name='product', name='name', field=models.TextField(null=True))",
            "migrations.RunSQL('DELETE FROM catalog_product')",
            "migrations.RunPython(models.F) ;",  # Syntax errors also fail closed.
            "migrations.AddField(model_name='product', name='code', field=models.CharField(null=True, unique=True))",
            "migrations.AddField(model_name='product', name='owner', field=models.ForeignKey('auth.User', null=True, on_delete=models.CASCADE))",
        ):
            with self.subTest(op=op), self.assertRaises((UnsafeMigration, SyntaxError)):
                validate_added(migration(op))

    def test_existing_migrations_cannot_be_modified_removed_or_renamed(self):
        for change in ("modified", "removed", "renamed"):
            with self.assertRaises(UnsafeMigration):
                check_changes([(change, "catalog/migrations/0001_initial.py")], lambda p: "")

    def test_data_helpers_imports_and_squashes_are_rejected(self):
        for source in (
            "import os\n" + migration(""),
            migration("") + "print('executed')\n",
            migration("").replace("    operations", "    replaces = [('catalog', '0001_initial')]\n    operations"),
        ):
            with self.assertRaises(UnsafeMigration):
                validate_added(source)

    def test_previously_applied_name_cannot_be_reused_for_different_schema(self):
        path = "catalog/migrations/0002_note.py"
        source = migration("migrations.AddField(model_name='product', name='note', field=models.TextField(null=True))")
        hashes = check_changes([("added", path)], lambda p: source)
        self.assertEqual(check_changes([("added", path)], lambda p: source, hashes), hashes)
        with self.assertRaises(UnsafeMigration):
            check_changes([("added", path)], lambda p: source.replace("note", "other"), hashes)
