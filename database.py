"""
Configuración de conexión a PostgreSQL usando SQLAlchemy.

Para verificar la conexión manualmente con PSQL:
  psql -h dpg-d7ro8rpo3t8c73dbpd80-a.oregon-postgres.render.com -U videollamada_db_user -d videollamada_db -p 5432

Password: iuw2YP2z0P5FJh1QF8Ot7nuN84S7YM5j
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# URL externa para conectar desde fuera de Render
DATABASE_URL = (
    "postgresql://videollamada_db_user:iuw2YP2z0P5FJh1QF8Ot7nuN84S7YM5j"
    "@dpg-d7ro8rpo3t8c73dbpd80-a.oregon-postgres.render.com/videollamada_db"
)

# Si la app corre dentro de Render, usar la URL interna:
# DATABASE_URL = (
#     "postgresql://videollamada_db_user:iuw2YP2z0P5FJh1QF8Ot7nuN84S7YM5j"
#     "@dpg-d7ro8rpo3t8c73dbpd80-a/videollamada_db"
# )

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Generador de sesiones para usar como dependencia de FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
