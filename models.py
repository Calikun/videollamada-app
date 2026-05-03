"""
Modelo SQLAlchemy para la tabla de usuarios con roles.
"""

from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from database import Base


class Usuario(Base):
    """Modelo de usuario que persiste nombre, rol y sala en PostgreSQL."""
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(String(20), unique=True, nullable=False, index=True,
                     comment="ID único generado por el servidor WebSocket")
    nombre = Column(String(100), nullable=False)
    rol = Column(String(50), nullable=False, default="")
    room_id = Column(String(100), nullable=True,
                     comment="Sala a la que está conectado actualmente")
    conectado = Column(String(10), nullable=False, default="si",
                       comment="Estado de conexión: 'si' o 'no'")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())

    def __repr__(self):
        return (
            f"<Usuario(id={self.id}, user_id='{self.user_id}', "
            f"nombre='{self.nombre}', rol='{self.rol}', "
            f"room_id='{self.room_id}', conectado='{self.conectado}')>"
        )
