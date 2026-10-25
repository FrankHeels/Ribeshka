"""Conservative, syntax-only expand-migration gate. Does not execute PR code."""
import ast
import hashlib
import subprocess


class UnsafeMigration(ValueError):
    pass


def is_migration(path):
    parts = path.split("/")
    return len(parts) >= 3 and parts[-2] == "migrations" and path.endswith(".py")


def _name(node):
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return ""


def _literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def validate_added(source):
    """Only new tables, nullable scalar columns and ordinary indexes in phase 1.

    Python defaults do NOT make non-null columns safe for old INSERT statements.
    No arbitrary Python/SQL, imported helpers, inheritance, field alterations,
    constraints on existing tables, squashes or data migrations are accepted.
    """
    tree = ast.parse(source)
    classes = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "django" and all(
            alias.name in {"migrations", "models"} and alias.asname is None for alias in node.names
        ):
            continue
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            classes.append(node)
            continue
        raise UnsafeMigration("Only django imports and a literal Migration class are allowed")
    if (len(classes) != 1 or [_name(n) for n in classes[0].bases] != ["migrations.Migration"]
            or classes[0].decorator_list or classes[0].keywords):
        raise UnsafeMigration("Expected one standard Migration class")
    assignments = {}
    for node in classes[0].body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise UnsafeMigration("Executable migration class body is not allowed")
        key = node.targets[0].id
        if key not in {"initial", "dependencies", "operations"} or key in assignments:
            raise UnsafeMigration("Unsupported migration attribute")
        assignments[key] = node.value
    operations = assignments.get("operations")
    if not isinstance(operations, ast.List):
        raise UnsafeMigration("Operations must be a literal list")
    # Reject nested function calls except standard scalar fields and Index.
    scalar_fields = {
        "AutoField", "BigAutoField", "SmallAutoField", "BigIntegerField", "IntegerField",
        "PositiveIntegerField", "PositiveBigIntegerField", "SmallIntegerField",
        "PositiveSmallIntegerField", "BooleanField", "CharField", "TextField", "DateField",
        "DateTimeField", "TimeField", "DurationField", "DecimalField", "FloatField",
        "BinaryField", "UUIDField", "JSONField", "EmailField", "SlugField", "URLField",
        "GenericIPAddressField",
    }
    allowed_calls = {f"models.{n}" for n in scalar_fields | {"Index"}} | {
        "migrations.CreateModel", "migrations.AddField", "migrations.AddIndex",
    }
    for node in ast.walk(classes[0]):
        if isinstance(node, ast.Call) and (_name(node.func) not in allowed_calls or any(k.arg is None for k in node.keywords)):
            raise UnsafeMigration("Custom code, relationships and SQL require a separate rollout")
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.IfExp)):
            raise UnsafeMigration("Computed migration definitions are not allowed")
    for op in operations.elts:
        if not isinstance(op, ast.Call) or op.args:
            raise UnsafeMigration("Expected an operation with named arguments")
        kind = _name(op.func)
        values = {k.arg: k.value for k in op.keywords}
        if kind == "migrations.AddField":
            field = values.get("field")
            if not isinstance(field, ast.Call) or _name(field.func) not in {f"models.{n}" for n in scalar_fields}:
                raise UnsafeMigration("Only scalar nullable additions are allowed")
            flags = {k.arg: _literal(k.value) for k in field.keywords}
            if flags.get("null") is not True or flags.get("unique") or flags.get("primary_key"):
                raise UnsafeMigration("Added columns must be nullable and non-unique for old writers")
        elif kind == "migrations.CreateModel":
            if "bases" in values or "managers" in values or "options" in values and _literal(values["options"]) != {}:
                raise UnsafeMigration("Custom table options/bases/managers require a separate rollout")
        elif kind != "migrations.AddIndex":
            raise UnsafeMigration(f"Unsupported operation: {kind}")


def check_changes(changes, read, ledger=None):
    """changes is [(added|modified|removed, path)]; return content hashes."""
    hashes = {}
    for status, path in changes:
        if not is_migration(path):
            continue
        if status != "added":
            raise UnsafeMigration(f"Existing migrations are immutable: {path}")
        source = read(path)
        digest = hashlib.sha256(source.encode()).hexdigest()
        if ledger and path in ledger and ledger[path] != digest:
            raise UnsafeMigration(f"Previously applied migration name reused: {path}")
        if not path.endswith("/__init__.py"):
            validate_added(source)
        elif source.strip():
            raise UnsafeMigration("Migration package initializer must be empty")
        hashes[path] = digest
    return hashes


def check_git(base, head="HEAD"):
    raw = subprocess.check_output(["git", "diff", "--no-renames", "--name-status", base, head], text=True)
    statuses = {"A": "added", "M": "modified", "D": "removed"}
    changes = [(statuses.get(line.split("\t")[0], "modified"), line.split("\t")[1]) for line in raw.splitlines()]
    return check_changes(changes, lambda path: subprocess.check_output(["git", "show", f"{head}:{path}"], text=True))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    args = parser.parse_args()
    check_git(args.base)
    print("Expand-only migration policy passed")
