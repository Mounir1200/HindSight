from importlib.resources import files
from pathlib import Path
from typing import Any


def apply_migrations(connection: Any, directory: Path | None = None) -> list[str]:
    migrations = _migration_files(directory)
    if not migrations:
        raise FileNotFoundError("no SQL migrations found")

    for migration in migrations:
        with connection.transaction():
            connection.execute(migration.read_text(encoding="utf-8"))
    return [migration.name for migration in migrations]


def _migration_files(directory: Path | None) -> list[Any]:
    if directory is not None:
        return sorted(directory.glob("*.sql"))

    packaged = files("hindsight").joinpath("migrations")
    if packaged.is_dir():
        return sorted(
            (item for item in packaged.iterdir() if item.name.endswith(".sql")),
            key=lambda item: item.name,
        )
    project_migrations = Path(__file__).resolve().parents[3] / "migrations"
    return sorted(project_migrations.glob("*.sql"))
