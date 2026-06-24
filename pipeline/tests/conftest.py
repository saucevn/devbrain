import os

# pipeline.py reads DATABASE_URL at import time. These unit tests never open a
# pool, so a placeholder is enough to make the module importable.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
