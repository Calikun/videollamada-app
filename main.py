"""
Servidor principal de la plataforma de videollamadas Ensigan.corp.
Incluye: Autenticación JWT, WebSocket para señalización WebRTC,
gestión de salas, QoS por roles, CRUD de usuarios y panel de administración.
"""

import os
import sys
import json
import uuid
import logging
import secrets
import traceback
from typing import Dict, Optional, List
from datetime import datetime

# Asegurar que el directorio actual esté en el path de Python (necesario en Render)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    Depends, Request, HTTPException, status
)
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session

from database import engine, SessionLocal, Base, get_db
from models import Usuario, Sala
from auth import (
    hash_password, verify_password, create_access_token,
    set_session_cookie, clear_session_cookie, get_session_from_request,
    require_session, require_admin, generate_user_id, COOKIE_NAME
)
from qos import (
    get_qos_priority, get_roles_list, get_role_names,
    get_qos_profiles_for_client, ROLES_CONFIG
)


# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN Y LOGGING
# ═══════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("videollamada")

app = FastAPI(
    title="Ensigan.corp VideoCall API",
    description="Plataforma de videollamadas profesional con WebRTC, autenticación, QoS y panel de administración",
    version="3.0.0"
)
security = HTTPBasic()


@app.get("/logo.jpeg")
async def get_logo():
    """Sirve el logo de la aplicación."""
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.jpeg")
    if os.path.exists(logo_path):
        return FileResponse(logo_path, media_type="image/jpeg")
    return JSONResponse(status_code=404, content={"detail": "Logo no encontrado"})

# ── Credenciales de administrador de emergencia (configurables por env vars) ─
ADMIN_USERNAME = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "clave")

# ── Estado en memoria de las salas activas ─────────────────────────────────
rooms: Dict[str, Dict] = {}

# ── Rate limiting simple en memoria ────────────────────────────────────────
ws_message_counts: Dict[str, list] = {}  # user_id -> [timestamps]
MAX_MESSAGES_PER_SECOND = 15


# ═══════════════════════════════════════════════════════════════════════════
#  ESQUEMAS DE VALIDACIÓN (Pydantic)
# ═══════════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    """Esquema para registro de usuario."""
    nombre: str
    email: str
    password: str
    cargo: Optional[str] = None
    rol: str = "Estudiante"

    @validator("nombre")
    def nombre_no_vacio(cls, v):
        if not v or len(v.strip()) < 2:
            raise ValueError("El nombre debe tener al menos 2 caracteres")
        return v.strip()

    @validator("email")
    def email_valido(cls, v):
        if not v or "@" not in v or "." not in v:
            raise ValueError("El email no es válido")
        return v.strip().lower()

    @validator("password")
    def password_segura(cls, v):
        if not v or len(v) < 6:
            raise ValueError("La contraseña debe tener al menos 6 caracteres")
        return v

    @validator("rol")
    def rol_valido(cls, v):
        valid_roles = get_role_names()
        if v not in valid_roles:
            raise ValueError(f"Rol inválido. Opciones: {', '.join(valid_roles)}")
        return v


class LoginRequest(BaseModel):
    """Esquema para inicio de sesión."""
    email: str
    password: str

    @validator("email")
    def email_format(cls, v):
        return v.strip().lower()


class GuestRequest(BaseModel):
    """Esquema para ingreso como invitado."""
    nombre: str

    @validator("nombre")
    def nombre_no_vacio(cls, v):
        if not v or len(v.strip()) < 2:
            raise ValueError("El nombre debe tener al menos 2 caracteres")
        return v.strip()


class UsuarioUpdate(BaseModel):
    """Esquema de validación para actualizar un usuario."""
    nombre: Optional[str] = None
    email: Optional[str] = None
    cargo: Optional[str] = None
    rol: Optional[str] = None
    is_admin: Optional[bool] = None
    conectado: Optional[str] = None

    @validator("nombre")
    def nombre_no_vacio(cls, v):
        if v is not None and len(v.strip()) == 0:
            raise ValueError("El nombre no puede estar vacío")
        return v.strip() if v else v

    @validator("conectado")
    def conectado_valido(cls, v):
        if v is not None and v not in ("si", "no"):
            raise ValueError("El estado debe ser 'si' o 'no'")
        return v


class UsuarioCreate(BaseModel):
    """Esquema para crear usuario desde el admin."""
    nombre: str
    email: Optional[str] = None
    password: Optional[str] = None
    cargo: Optional[str] = None
    rol: str = "Estudiante"
    is_admin: bool = False

    @validator("nombre")
    def nombre_no_vacio(cls, v):
        if not v or len(v.strip()) < 2:
            raise ValueError("El nombre debe tener al menos 2 caracteres")
        return v.strip()


# ═══════════════════════════════════════════════════════════════════════════
#  EVENTOS DE CICLO DE VIDA
# ═══════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    """Crea las tablas en PostgreSQL y el admin por defecto al iniciar."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("✅ Tablas creadas / verificadas en PostgreSQL")

        # Crear admin por defecto si no existe
        db = SessionLocal()
        try:
            admin = db.query(Usuario).filter(Usuario.email == "admin@ensigan.corp").first()
            if not admin:
                admin = Usuario(
                    user_id=generate_user_id(),
                    nombre="Administrador",
                    email="admin@ensigan.corp",
                    password_hash=hash_password("admin123"),
                    cargo="Administrador del Sistema",
                    rol="Director",
                    is_admin=True,
                    is_guest=False,
                    qos_priority=2,
                    conectado="no"
                )
                db.add(admin)
                db.commit()
                logger.info("✅ Admin por defecto creado: admin@ensigan.corp / admin123")
            else:
                logger.info("ℹ️ Admin por defecto ya existe")
        except Exception as e:
            db.rollback()
            logger.error(f"❌ Error creando admin por defecto: {e}")
        finally:
            db.close()

    except Exception as e:
        logger.error(f"❌ Error al crear tablas en PostgreSQL: {e}")
        logger.error(traceback.format_exc())


# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS DE BASE DE DATOS
# ═══════════════════════════════════════════════════════════════════════════

def _get_db():
    """Crea y retorna una sesión de base de datos con manejo seguro."""
    db = SessionLocal()
    return db


def _serializar_usuario(u: Usuario) -> dict:
    """Convierte un objeto Usuario a diccionario serializable."""
    return {
        "id": u.id,
        "user_id": u.user_id,
        "nombre": u.nombre,
        "email": u.email,
        "cargo": u.cargo,
        "rol": u.rol,
        "is_admin": u.is_admin,
        "is_guest": u.is_guest,
        "qos_priority": u.qos_priority,
        "room_id": u.room_id,
        "conectado": u.conectado,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "updated_at": u.updated_at.isoformat() if u.updated_at else None,
    }


def registrar_usuario_db(user_id: str, nombre: str, rol: str, room_id: str, qos_priority: int = 0):
    """Registra o actualiza un usuario en la base de datos."""
    db = _get_db()
    try:
        usuario = db.query(Usuario).filter(Usuario.user_id == user_id).first()
        if usuario:
            usuario.nombre = nombre
            usuario.rol = rol
            usuario.room_id = room_id
            usuario.conectado = "si"
            usuario.qos_priority = qos_priority
            logger.info(f"[DB] Usuario actualizado: {nombre} ({user_id}) rol='{rol}' sala='{room_id}' qos={qos_priority}")
        else:
            usuario = Usuario(
                user_id=user_id,
                nombre=nombre,
                rol=rol,
                room_id=room_id,
                conectado="si",
                qos_priority=qos_priority,
                is_guest=True
            )
            db.add(usuario)
            logger.info(f"[DB] Usuario creado: {nombre} ({user_id}) rol='{rol}' sala='{room_id}' qos={qos_priority}")
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Error registrando usuario {user_id}: {e}")
    finally:
        db.close()


def desconectar_usuario_db(user_id: str):
    """Marca un usuario como desconectado en la base de datos."""
    db = _get_db()
    try:
        usuario = db.query(Usuario).filter(Usuario.user_id == user_id).first()
        if usuario:
            usuario.conectado = "no"
            usuario.room_id = None
            db.commit()
            logger.info(f"[DB] Usuario desconectado: {user_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Error desconectando usuario {user_id}: {e}")
    finally:
        db.close()


def registrar_sala_db(room_id: str, created_by: str = None):
    """Registra una nueva sala en la base de datos."""
    db = _get_db()
    try:
        sala = db.query(Sala).filter(Sala.room_id == room_id).first()
        if not sala:
            sala = Sala(room_id=room_id, created_by=created_by, is_active=True)
            db.add(sala)
            db.commit()
            logger.info(f"[DB] Sala registrada: {room_id} por {created_by}")
    except Exception as e:
        db.rollback()
        logger.error(f"[DB] Error registrando sala {room_id}: {e}")
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════
#  AUTENTICACIÓN — HTTP BASIC (LEGADO / EMERGENCIA)
# ═══════════════════════════════════════════════════════════════════════════

def verificar_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Verifica credenciales de administrador con comparación segura."""
    username_ok = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    password_ok = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (username_ok and password_ok):
        logger.warning(f"[AUTH] Intento de acceso admin fallido: usuario='{credentials.username}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas",
            headers={"WWW-Authenticate": "Basic"},
        )
    logger.info(f"[AUTH] Acceso admin autorizado via HTTP Basic: {credentials.username}")
    return credentials.username


# ═══════════════════════════════════════════════════════════════════════════
#  ENDPOINTS DE AUTENTICACIÓN
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/auth/register")
async def register(data: RegisterRequest):
    """Registro de un nuevo usuario con email y contraseña."""
    db = _get_db()
    try:
        # Verificar si el email ya existe
        existing = db.query(Usuario).filter(Usuario.email == data.email).first()
        if existing:
            return JSONResponse(
                status_code=400,
                content={"detail": "Ya existe una cuenta con este correo electrónico"}
            )

        # Crear usuario
        user_id = generate_user_id()
        qos = get_qos_priority(data.rol)
        usuario = Usuario(
            user_id=user_id,
            nombre=data.nombre,
            email=data.email,
            password_hash=hash_password(data.password),
            cargo=data.cargo,
            rol=data.rol,
            is_admin=False,
            is_guest=False,
            qos_priority=qos,
            conectado="no"
        )
        db.add(usuario)
        db.commit()

        # Crear token de sesión
        token = create_access_token({
            "user_id": user_id,
            "nombre": data.nombre,
            "email": data.email,
            "cargo": data.cargo,
            "rol": data.rol,
            "is_admin": False,
            "is_guest": False,
            "qos_priority": qos
        })

        response = JSONResponse(content={
            "detail": "Cuenta creada exitosamente",
            "user_id": user_id,
            "nombre": data.nombre,
            "rol": data.rol,
            "qos_priority": qos
        })
        set_session_cookie(response, token)
        logger.info(f"[AUTH] Registro exitoso: {data.nombre} ({data.email}) rol={data.rol}")
        return response

    except Exception as e:
        db.rollback()
        logger.error(f"[AUTH] Error en registro: {e}")
        logger.error(traceback.format_exc())
        return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})
    finally:
        db.close()


@app.post("/api/auth/login")
async def login(data: LoginRequest):
    """Inicio de sesión con email y contraseña."""
    db = _get_db()
    try:
        usuario = db.query(Usuario).filter(Usuario.email == data.email).first()
        if not usuario or not usuario.password_hash:
            return JSONResponse(
                status_code=401,
                content={"detail": "Correo electrónico o contraseña incorrectos"}
            )

        if not verify_password(data.password, usuario.password_hash):
            logger.warning(f"[AUTH] Login fallido para: {data.email}")
            return JSONResponse(
                status_code=401,
                content={"detail": "Correo electrónico o contraseña incorrectos"}
            )

        # Crear token
        token = create_access_token({
            "user_id": usuario.user_id,
            "nombre": usuario.nombre,
            "email": usuario.email,
            "cargo": usuario.cargo,
            "rol": usuario.rol,
            "is_admin": usuario.is_admin,
            "is_guest": False,
            "qos_priority": usuario.qos_priority
        })

        response = JSONResponse(content={
            "detail": "Sesión iniciada",
            "user_id": usuario.user_id,
            "nombre": usuario.nombre,
            "email": usuario.email,
            "cargo": usuario.cargo,
            "rol": usuario.rol,
            "is_admin": usuario.is_admin,
            "qos_priority": usuario.qos_priority
        })
        set_session_cookie(response, token)
        logger.info(f"[AUTH] Login exitoso: {usuario.nombre} ({data.email})")
        return response

    except Exception as e:
        logger.error(f"[AUTH] Error en login: {e}")
        return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})
    finally:
        db.close()


@app.post("/api/auth/guest")
async def guest_login(data: GuestRequest):
    """Ingreso como invitado (sin cuenta)."""
    user_id = generate_user_id()
    token = create_access_token({
        "user_id": user_id,
        "nombre": data.nombre,
        "email": None,
        "cargo": None,
        "rol": "Invitado",
        "is_admin": False,
        "is_guest": True,
        "qos_priority": 0
    })

    response = JSONResponse(content={
        "detail": "Ingreso como invitado",
        "user_id": user_id,
        "nombre": data.nombre,
        "rol": "Invitado",
        "qos_priority": 0
    })
    set_session_cookie(response, token)
    logger.info(f"[AUTH] Ingreso como invitado: {data.nombre} ({user_id})")
    return response


@app.get("/api/auth/me")
async def get_me(request: Request):
    """Retorna los datos del usuario actual basado en la cookie de sesión."""
    session = get_session_from_request(request)
    if not session:
        return JSONResponse(status_code=401, content={"detail": "No hay sesión activa"})
    return {
        "user_id": session.get("user_id"),
        "nombre": session.get("nombre"),
        "email": session.get("email"),
        "cargo": session.get("cargo"),
        "rol": session.get("rol"),
        "is_admin": session.get("is_admin", False),
        "is_guest": session.get("is_guest", False),
        "qos_priority": session.get("qos_priority", 0)
    }


@app.post("/api/auth/logout")
async def logout():
    """Cierra la sesión del usuario."""
    response = JSONResponse(content={"detail": "Sesión cerrada"})
    clear_session_cookie(response)
    return response


# ═══════════════════════════════════════════════════════════════════════════
#  ENDPOINTS DE SALAS
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/rooms/{room_id}/exists")
async def room_exists(room_id: str):
    """Verifica si una sala existe (en memoria o en BD)."""
    # Verificar en memoria (salas activas)
    if room_id in rooms:
        participants = len(rooms[room_id]["user_ids"])
        return {"exists": True, "active": True, "participants": participants}

    # Verificar en BD
    db = _get_db()
    try:
        sala = db.query(Sala).filter(Sala.room_id == room_id, Sala.is_active == True).first()
        if sala:
            return {"exists": True, "active": False, "participants": 0}
        return {"exists": False}
    finally:
        db.close()


@app.post("/api/rooms/create")
async def create_room(request: Request):
    """Crea una nueva sala y retorna su ID."""
    session = get_session_from_request(request)
    room_id = uuid.uuid4().hex[:8]
    created_by = session.get("user_id") if session else None
    registrar_sala_db(room_id, created_by)
    logger.info(f"[SALA] Sala creada vía API: {room_id} por {created_by}")
    return {"room_id": room_id}


# ═══════════════════════════════════════════════════════════════════════════
#  ENDPOINTS DE ROLES Y QoS
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/roles")
async def listar_roles():
    """Retorna la lista de roles predefinidos con su configuración QoS."""
    return get_roles_list()


@app.get("/api/qos/profiles")
async def qos_profiles():
    """Retorna los perfiles QoS para el cliente WebRTC."""
    return get_qos_profiles_for_client()


# ═══════════════════════════════════════════════════════════════════════════
#  ENDPOINTS REST — CRUD DE USUARIOS
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/usuarios")
async def listar_usuarios(request: Request):
    """Retorna todos los usuarios registrados. Requiere sesión admin."""
    session = get_session_from_request(request)
    # Permitir acceso si es admin por sesión o HTTP Basic
    if not session or not session.get("is_admin", False):
        # Intentar HTTP Basic como fallback
        try:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Basic "):
                import base64
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                if not (secrets.compare_digest(username, ADMIN_USERNAME) and
                        secrets.compare_digest(password, ADMIN_PASSWORD)):
                    return JSONResponse(status_code=403, content={"detail": "Acceso denegado"})
            else:
                return JSONResponse(status_code=403, content={"detail": "Acceso denegado"})
        except Exception:
            return JSONResponse(status_code=403, content={"detail": "Acceso denegado"})

    db = _get_db()
    try:
        usuarios = db.query(Usuario).order_by(Usuario.id.desc()).all()
        return [_serializar_usuario(u) for u in usuarios]
    except Exception as e:
        logger.error(f"[API] Error listando usuarios: {e}")
        return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})
    finally:
        db.close()


@app.get("/api/usuarios/conectados")
async def listar_usuarios_conectados():
    """Retorna solo los usuarios actualmente conectados."""
    db = _get_db()
    try:
        usuarios = db.query(Usuario).filter(Usuario.conectado == "si").all()
        return [_serializar_usuario(u) for u in usuarios]
    except Exception as e:
        logger.error(f"[API] Error listando usuarios conectados: {e}")
        return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})
    finally:
        db.close()


@app.get("/api/usuarios/{user_id}")
async def obtener_usuario(user_id: str):
    """Retorna un usuario específico por su user_id."""
    db = _get_db()
    try:
        usuario = db.query(Usuario).filter(Usuario.user_id == user_id).first()
        if not usuario:
            return JSONResponse(status_code=404, content={"detail": "Usuario no encontrado"})
        return _serializar_usuario(usuario)
    except Exception as e:
        logger.error(f"[API] Error obteniendo usuario {user_id}: {e}")
        return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})
    finally:
        db.close()


@app.post("/api/usuarios")
async def crear_usuario(data: UsuarioCreate, request: Request):
    """Crea un nuevo usuario desde el panel admin."""
    # Verificar permisos de admin
    session = get_session_from_request(request)
    is_admin = False
    if session and session.get("is_admin"):
        is_admin = True
    if not is_admin:
        try:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Basic "):
                import base64
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD):
                    is_admin = True
        except Exception:
            pass
    if not is_admin:
        return JSONResponse(status_code=403, content={"detail": "Acceso denegado"})

    db = _get_db()
    try:
        # Verificar email duplicado si se proporcionó
        if data.email:
            existing = db.query(Usuario).filter(Usuario.email == data.email.strip().lower()).first()
            if existing:
                return JSONResponse(status_code=400, content={"detail": "Ya existe un usuario con este email"})

        user_id = generate_user_id()
        qos = get_qos_priority(data.rol)
        usuario = Usuario(
            user_id=user_id,
            nombre=data.nombre,
            email=data.email.strip().lower() if data.email else None,
            password_hash=hash_password(data.password) if data.password else None,
            cargo=data.cargo,
            rol=data.rol,
            is_admin=data.is_admin,
            is_guest=False,
            qos_priority=qos,
            conectado="no"
        )
        db.add(usuario)
        db.commit()
        db.refresh(usuario)
        logger.info(f"[ADMIN] Usuario creado: {data.nombre} ({user_id}) rol={data.rol}")
        return _serializar_usuario(usuario)
    except Exception as e:
        db.rollback()
        logger.error(f"[ADMIN] Error creando usuario: {e}")
        return JSONResponse(status_code=500, content={"detail": "Error al crear usuario"})
    finally:
        db.close()


@app.put("/api/usuarios/{user_id}")
async def actualizar_usuario(user_id: str, datos: UsuarioUpdate, request: Request):
    """Actualiza los campos de un usuario existente. Requiere autenticación admin."""
    # Verificar permisos
    session = get_session_from_request(request)
    is_admin = False
    admin_name = "unknown"
    if session and session.get("is_admin"):
        is_admin = True
        admin_name = session.get("nombre", "admin")
    if not is_admin:
        try:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Basic "):
                import base64
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD):
                    is_admin = True
                    admin_name = username
        except Exception:
            pass
    if not is_admin:
        return JSONResponse(status_code=403, content={"detail": "Acceso denegado"})

    db = _get_db()
    try:
        usuario = db.query(Usuario).filter(Usuario.user_id == user_id).first()
        if not usuario:
            return JSONResponse(status_code=404, content={"detail": "Usuario no encontrado"})

        campos_actualizados = []
        if datos.nombre is not None:
            usuario.nombre = datos.nombre
            campos_actualizados.append(f"nombre='{datos.nombre}'")
        if datos.email is not None:
            usuario.email = datos.email
            campos_actualizados.append(f"email='{datos.email}'")
        if datos.cargo is not None:
            usuario.cargo = datos.cargo
            campos_actualizados.append(f"cargo='{datos.cargo}'")
        if datos.rol is not None:
            usuario.rol = datos.rol
            usuario.qos_priority = get_qos_priority(datos.rol)
            campos_actualizados.append(f"rol='{datos.rol}' qos={usuario.qos_priority}")
        if datos.is_admin is not None:
            usuario.is_admin = datos.is_admin
            campos_actualizados.append(f"is_admin={datos.is_admin}")
        if datos.conectado is not None:
            usuario.conectado = datos.conectado
            campos_actualizados.append(f"conectado='{datos.conectado}'")
            if datos.conectado == "no":
                usuario.room_id = None

        db.commit()
        db.refresh(usuario)
        logger.info(f"[ADMIN] Usuario {user_id} actualizado por {admin_name}: {', '.join(campos_actualizados)}")
        return _serializar_usuario(usuario)
    except Exception as e:
        db.rollback()
        logger.error(f"[ADMIN] Error actualizando usuario {user_id}: {e}")
        return JSONResponse(status_code=500, content={"detail": "Error al actualizar usuario"})
    finally:
        db.close()


@app.delete("/api/usuarios/{user_id}")
async def eliminar_usuario(user_id: str, request: Request):
    """Elimina un usuario de la base de datos. Requiere autenticación admin."""
    session = get_session_from_request(request)
    is_admin = False
    admin_name = "unknown"
    if session and session.get("is_admin"):
        is_admin = True
        admin_name = session.get("nombre", "admin")
    if not is_admin:
        try:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Basic "):
                import base64
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD):
                    is_admin = True
                    admin_name = username
        except Exception:
            pass
    if not is_admin:
        return JSONResponse(status_code=403, content={"detail": "Acceso denegado"})

    db = _get_db()
    try:
        usuario = db.query(Usuario).filter(Usuario.user_id == user_id).first()
        if not usuario:
            return JSONResponse(status_code=404, content={"detail": "Usuario no encontrado"})

        nombre = usuario.nombre
        db.delete(usuario)
        db.commit()
        logger.info(f"[ADMIN] Usuario eliminado por {admin_name}: {nombre} ({user_id})")
        return {"detail": f"Usuario '{nombre}' eliminado correctamente", "user_id": user_id}
    except Exception as e:
        db.rollback()
        logger.error(f"[ADMIN] Error eliminando usuario {user_id}: {e}")
        return JSONResponse(status_code=500, content={"detail": "Error al eliminar usuario"})
    finally:
        db.close()


@app.get("/api/stats")
async def obtener_estadisticas():
    """Retorna estadísticas generales del sistema."""
    db = _get_db()
    try:
        total = db.query(Usuario).count()
        conectados = db.query(Usuario).filter(Usuario.conectado == "si").count()
        desconectados = total - conectados
        salas_activas = len(rooms)
        admins = db.query(Usuario).filter(Usuario.is_admin == True).count()
        invitados = db.query(Usuario).filter(Usuario.is_guest == True).count()

        # Roles únicos
        roles_query = db.query(Usuario.rol).distinct().all()
        roles = [r[0] for r in roles_query if r[0]]

        return {
            "total_usuarios": total,
            "conectados": conectados,
            "desconectados": desconectados,
            "salas_activas": salas_activas,
            "salas_en_memoria": list(rooms.keys()),
            "roles_registrados": roles,
            "total_admins": admins,
            "total_invitados": invitados,
        }
    except Exception as e:
        logger.error(f"[API] Error obteniendo estadísticas: {e}")
        return JSONResponse(status_code=500, content={"detail": "Error interno"})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════
#  UTILIDADES WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════

async def safe_send(ws: WebSocket, data: str) -> bool:
    """Envía un mensaje a un WebSocket de forma segura. Retorna False si falla."""
    try:
        await ws.send_text(data)
        return True
    except Exception as e:
        logger.debug(f"[WS] Error enviando mensaje: {type(e).__name__}")
        return False


async def broadcast(room: Dict, message: dict, exclude_ws: WebSocket = None):
    """Envía un mensaje JSON a todos los WebSockets de la sala, excepto al excluido."""
    data = json.dumps(message)
    disconnected = []

    for client, uid in list(room["websockets"].items()):
        if client != exclude_ws:
            success = await safe_send(client, data)
            if not success:
                disconnected.append(client)

    # Limpiar WebSockets desconectados detectados durante el broadcast
    for client in disconnected:
        if client in room["websockets"]:
            uid = room["websockets"][client]
            del room["websockets"][client]
            room["user_ids"].discard(uid)
            if uid in room["usernames"]:
                del room["usernames"][uid]
            if uid in room["muted"]:
                del room["muted"][uid]
            logger.info(f"[WS] Cliente zombi eliminado durante broadcast: {uid}")


def check_rate_limit(user_id: str) -> bool:
    """Verifica rate limiting para mensajes WebSocket. Retorna True si está permitido."""
    import time
    now = time.time()
    if user_id not in ws_message_counts:
        ws_message_counts[user_id] = []

    # Limpiar timestamps viejos (más de 1 segundo)
    ws_message_counts[user_id] = [t for t in ws_message_counts[user_id] if now - t < 1.0]

    if len(ws_message_counts[user_id]) >= MAX_MESSAGES_PER_SECOND:
        return False

    ws_message_counts[user_id].append(now)
    return True


async def cleanup_user(room: Dict, room_id: str, websocket: WebSocket, user_id: str, username: str):
    """Limpia completamente el estado de un usuario que se desconecta o es expulsado."""
    try:
        # Limpiar del estado en memoria
        if websocket in room["websockets"]:
            del room["websockets"][websocket]
        room["user_ids"].discard(user_id)
        if user_id in room["usernames"]:
            del room["usernames"][user_id]
        if user_id in room["muted"]:
            del room["muted"][user_id]

        # Limpiar rate limiting
        if user_id in ws_message_counts:
            del ws_message_counts[user_id]

        # Persistir desconexión en la base de datos
        desconectar_usuario_db(user_id)

        # Transferir rol de host si es necesario
        if room.get("host_id") == user_id and room["user_ids"]:
            new_host_id = next(iter(room["user_ids"]))
            room["host_id"] = new_host_id
            new_host_display = room["usernames"].get(new_host_id, {}).get("display", "Desconocido")
            await broadcast(room, {"type": "host-info", "hostId": new_host_id})
            await broadcast(room, {
                "type": "chat",
                "sender": "system",
                "message": f"{new_host_display} es ahora el anfitrión."
            })
            logger.info(f"[SALA {room_id}] Host transferido a {new_host_display} ({new_host_id})")

        # Notificar salida a los demás participantes
        await broadcast(room, {"type": "user-left", "userId": user_id, "username": username})
        await broadcast(room, {
            "type": "chat",
            "sender": "system",
            "message": f"{username} ha salido de la sala"
        })

        # Eliminar sala si quedó vacía
        if len(room["websockets"]) == 0 and room_id in rooms:
            del rooms[room_id]
            logger.info(f"[SALA {room_id}] Sala eliminada (vacía)")

    except Exception as e:
        logger.error(f"[WS] Error en cleanup_user para {username} ({user_id}): {e}")
        logger.error(traceback.format_exc())


# ═══════════════════════════════════════════════════════════════════════════
#  RUTAS HTML
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/")
async def get_index():
    """Página principal con lobby de autenticación."""
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/admin")
async def get_admin(request: Request):
    """Panel de administración protegido con autenticación por sesión o HTTP Basic."""
    # Verificar sesión JWT primero
    session = get_session_from_request(request)
    if session and session.get("is_admin"):
        with open("admin.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())

    # Fallback a HTTP Basic
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            import base64
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
            if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD):
                with open("admin.html", "r", encoding="utf-8") as f:
                    return HTMLResponse(content=f.read())
        except Exception:
            pass

    # Solicitar credenciales HTTP Basic
    return HTMLResponse(
        status_code=401,
        content="<html><body><h1>Acceso denegado</h1><p>Inicia sesión como administrador.</p></body></html>",
        headers={"WWW-Authenticate": "Basic realm='Admin Panel'"}
    )


@app.get("/room/{room_id}")
async def get_room(room_id: str):
    """Sala de videollamada."""
    with open("room.html", "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{{ROOM_ID}}", room_id)
    return HTMLResponse(content=html)


# ═══════════════════════════════════════════════════════════════════════════
#  WEBSOCKET PRINCIPAL — SEÑALIZACIÓN WEBRTC
# ═══════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    """Endpoint WebSocket para señalización WebRTC, chat y gestión de sala."""
    await websocket.accept()
    user_id = generate_user_id()
    logger.info(f"[WS] Nueva conexión WebSocket: user_id={user_id} sala={room_id}")

    # ── Recibir datos iniciales del cliente ──
    try:
        init_msg = await websocket.receive_text()
        init_data = json.loads(init_msg)
        name = init_data.get("name", "Anónimo").strip() or "Anónimo"
        role = init_data.get("role", "").strip()
        qos_priority = int(init_data.get("qos_priority", 0))

        # Si viene con session user_id, usar ese
        if init_data.get("session_user_id"):
            # Verificar si el usuario existe en la BD
            db = _get_db()
            try:
                existing = db.query(Usuario).filter(
                    Usuario.user_id == init_data["session_user_id"]
                ).first()
                if existing:
                    user_id = existing.user_id
                    name = existing.nombre
                    role = existing.rol
                    qos_priority = existing.qos_priority
            finally:
                db.close()

        # Si no tiene QoS definido, calcularlo del rol
        if qos_priority == 0 and role:
            qos_priority = get_qos_priority(role)

        username = f"{name} ({role})" if role else name
        logger.info(f"[WS] Datos iniciales: nombre='{name}' rol='{role}' qos={qos_priority} display='{username}'")
    except json.JSONDecodeError as e:
        logger.warning(f"[WS] JSON inválido en datos iniciales de {user_id}: {e}")
        name, role, username, qos_priority = "Anónimo", "", "Anónimo", 0
    except Exception as e:
        logger.warning(f"[WS] Error recibiendo datos iniciales de {user_id}: {e}")
        name, role, username, qos_priority = "Anónimo", "", "Anónimo", 0

    # ── Registrar en la base de datos ──
    registrar_usuario_db(user_id, name, role, room_id, qos_priority)
    registrar_sala_db(room_id, user_id)

    # ── Crear o unirse a la sala ──
    if room_id not in rooms:
        rooms[room_id] = {
            "host_id": user_id,
            "websockets": {},
            "user_ids": set(),
            "usernames": {},
            "muted": {},
            "qos": {}
        }
        logger.info(f"[SALA {room_id}] Sala creada por {username} (host)")

    room = rooms[room_id]
    room["websockets"][websocket] = user_id
    room["user_ids"].add(user_id)
    room["usernames"][user_id] = {"name": name, "role": role, "display": username}
    room["muted"][user_id] = False
    room["qos"][user_id] = qos_priority

    is_host = (room["host_id"] == user_id)

    # ── Enviar lista de participantes existentes al nuevo usuario ──
    other_users = []
    for uid in list(room["user_ids"]):
        if uid != user_id:
            other_users.append({
                "userId": uid,
                "display": room["usernames"].get(uid, {}).get("display", "Desconocido"),
                "isHost": (uid == room["host_id"]),
                "muted": room["muted"].get(uid, False),
                "qos_priority": room["qos"].get(uid, 0)
            })

    await safe_send(websocket, json.dumps({
        "type": "existing-users",
        "users": other_users,
        "isHost": is_host,
        "hostId": room["host_id"],
        "myUserId": user_id,
        "myQosPriority": qos_priority,
        "qosProfiles": get_qos_profiles_for_client()
    }))

    # ── Notificar a los demás ──
    await broadcast(room, {
        "type": "user-joined",
        "userId": user_id,
        "display": username,
        "isHost": False,
        "muted": False,
        "qos_priority": qos_priority
    }, exclude_ws=websocket)

    await broadcast(room, {
        "type": "chat",
        "sender": "system",
        "message": f"{username} se ha unido a la sala"
    }, exclude_ws=websocket)

    participant_count = len(room["user_ids"])
    logger.info(f"[SALA {room_id}] {username} ({user_id}) se unió. Participantes: {participant_count}")

    # ── Loop de mensajes ──
    try:
        while True:
            raw_data = await websocket.receive_text()

            # Rate limiting
            if not check_rate_limit(user_id):
                await safe_send(websocket, json.dumps({
                    "type": "error",
                    "message": "Demasiados mensajes. Espera un momento."
                }))
                continue

            try:
                message = json.loads(raw_data)
            except json.JSONDecodeError:
                logger.warning(f"[WS] JSON inválido recibido de {username} ({user_id})")
                continue

            message["senderId"] = user_id
            message["senderName"] = username
            msg_type = message.get("type")

            # ── Comando: Silenciar usuario (solo host) ──
            if msg_type == "host-mute":
                if not is_host:
                    logger.warning(f"[WS] {username} intentó silenciar sin ser host")
                    continue
                target_id = message.get("targetId")
                if target_id and target_id in room["muted"]:
                    mute_state = message.get("mute", True)
                    room["muted"][target_id] = mute_state
                    await broadcast(room, {
                        "type": "user-muted",
                        "userId": target_id,
                        "muted": mute_state
                    })
                    action = "silenció" if mute_state else "activó micrófono de"
                    logger.info(f"[SALA {room_id}] Host {username} {action} {target_id}")
                continue

            # ── Comando: Expulsar usuario (solo host) ──
            if msg_type == "host-kick":
                if not is_host:
                    logger.warning(f"[WS] {username} intentó expulsar sin ser host")
                    continue
                target_id = message.get("targetId")
                if not target_id or target_id == user_id:
                    continue

                target_name = room["usernames"].get(target_id, {}).get("display", "Desconocido")
                target_ws = None
                for client, uid in list(room["websockets"].items()):
                    if uid == target_id:
                        target_ws = client
                        break

                if target_ws:
                    await safe_send(target_ws, json.dumps({
                        "type": "kicked",
                        "reason": "Has sido expulsado por el anfitrión."
                    }))
                    try:
                        await target_ws.close()
                    except Exception:
                        pass
                    await cleanup_user(room, room_id, target_ws, target_id, target_name)
                    logger.info(f"[SALA {room_id}] Host {username} expulsó a {target_name}")
                continue

            # ── Señalización WebRTC y chat ──
            target = message.get("target")
            if target:
                for client, uid in list(room["websockets"].items()):
                    if uid == target:
                        await safe_send(client, json.dumps(message))
                        break
            else:
                await broadcast(room, message, exclude_ws=websocket)

    except WebSocketDisconnect:
        logger.info(f"[WS] Desconexión normal: {username} ({user_id}) de sala {room_id}")
        if room_id in rooms:
            await cleanup_user(room, room_id, websocket, user_id, username)
    except Exception as e:
        logger.error(f"[WS] Error inesperado para {username} ({user_id}): {e}")
        logger.error(traceback.format_exc())
        if room_id in rooms:
            await cleanup_user(room, room_id, websocket, user_id, username)
