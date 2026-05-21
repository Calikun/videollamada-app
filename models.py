"""
Modelos SQLAlchemy para la plataforma de videollamadas.
Incluye Usuario (con autenticación y QoS) y Sala.
"""

from sqlalchemy import Column, Integer, String, DateTime, Boolean
from sqlalchemy.sql import func
from database import Base


class Usuario(Base):
    """
    Modelo de usuario con soporte para:
    - Autenticación (email + password hash)
    - Roles y cargos profesionales
    - Prioridad QoS por rol
    - Estado de conexión en tiempo real
    """
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(String(50), unique=True, nullable=False, index=True,
                     comment="ID único generado por el servidor")
    nombre = Column(String(100), nullable=False)
    email = Column(String(150), unique=True, nullable=True, index=True,
                   comment="Email para login (nullable para invitados)")
    password_hash = Column(String(255), nullable=True,
                           comment="Hash bcrypt de la contraseña")
    cargo = Column(String(100), nullable=True,
                   comment="Cargo o puesto de trabajo")
    rol = Column(String(50), nullable=False, default="Invitado",
                 comment="Rol del sistema: Director, Gerente, Docente, etc.")
    is_admin = Column(Boolean, default=False, nullable=False,
                      comment="Si el usuario tiene acceso al panel admin")
    is_guest = Column(Boolean, default=False, nullable=False,
                      comment="Si el usuario ingresó como invitado")
    qos_priority = Column(Integer, default=0, nullable=False,
                          comment="Prioridad QoS: 0=normal, 1=media, 2=alta")
    room_id = Column(String(100), nullable=True,
                     comment="Sala a la que está conectado actualmente")
    conectado = Column(String(10), nullable=False, default="no",
                       comment="Estado de conexión: 'si' o 'no'")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())

    def __repr__(self):
        return (
            f"<Usuario(id={self.id}, user_id='{self.user_id}', "
            f"nombre='{self.nombre}', email='{self.email}', "
            f"rol='{self.rol}', qos={self.qos_priority}, "
            f"admin={self.is_admin}, conectado='{self.conectado}')>"
        )


class Sala(Base):
    """
    Modelo de sala de videollamada.
    Permite validar que una sala existe antes de unirse.
    """
    __tablename__ = "salas"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    room_id = Column(String(50), unique=True, nullable=False, index=True,
                     comment="ID único de la sala")
    nombre = Column(String(100), nullable=True,
                    comment="Nombre descriptivo de la sala")
    created_by = Column(String(50), nullable=True,
                        comment="user_id del creador")
    is_active = Column(Boolean, default=True, nullable=False,
                       comment="Si la sala está activa")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return (
            f"<Sala(id={self.id}, room_id='{self.room_id}', "
            f"created_by='{self.created_by}', active={self.is_active})>"
        )
