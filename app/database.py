import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Resolve .env relative to the PROJECT ROOT (parent of this app/ package), not the
# current working directory. The MCP server and the native Hermes plugin import
# this module from arbitrary cwds; a cwd-relative load_dotenv() silently missed
# the .env and fell back to a bad default URL (role "postgres" does not exist),
# which crashed the MCP server on startup.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
load_dotenv()  # also honor a cwd .env / already-exported vars if present

# Engine database (Postgres — the engine relies on ARRAY columns). Default
# matches the bundled docker-compose Postgres service so `docker compose up` is
# plug-and-play. Override with DATABASE_URL. See forge_config.database_url().
import forge_config

DATABASE_URL = forge_config.database_url()

# For SQLAlchemy 1.4/2.0, postgresql needs to use the correct dialect
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
