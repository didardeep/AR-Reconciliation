"""Dev helper: create all tables (use Alembic for real migrations)."""
from app.db.base import Base
from app.db.session import engine
import app.db.models  # noqa: F401  (register models)

if __name__ == "__main__":
    Base.metadata.create_all(engine())
    print("tables created")
