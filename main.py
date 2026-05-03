"""
Servidor principal de la aplicación de videollamadas.
Incluye WebSocket para señalización WebRTC, gestión de salas,
persistencia de usuarios en PostgreSQL y panel de administración.
"""

import os
import sys
import json
import uuid
import logging
import secrets
import traceback
from typing import Dict, Optional
from datetime import datetime

# Asegurar que el directorio actual esté en el path de Python (necesario en Render)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    Depends, Request, HTTPException, status
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session

from database import engine, SessionLocal, Base
from models import Usuario

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
    title="VideoCall API",
    description="API de videollamadas con WebRTC, chat y gestión de roles",
    version="2.0.0"
)
security = HTTPBasic()

# ── Credenciales de administrador (configurables por env vars) ─────────────
ADMIN_USERNAME = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "clave")

# ── Estado en memoria de las salas activas ─────────────────────────────────
rooms: Dict[str, Dict] = {}


# ═══════════════════════════════════════════════════════════════════════════
#  ESQUEMAS DE VALIDACIÓN (Pydantic)
# ═══════════════════════════════════════════════════════════════════════════

class UsuarioUpdate(BaseModel):
    """Esquema de validación para actualizar un usuario."""
    nombre: Optional[str] = None
    rol: Optional[str] = None
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


# ═══════════════════════════════════════════════════════════════════════════
#  EVENTOS DE CICLO DE VIDA
# ═══════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    """Crea las tablas en PostgreSQL si no existen al iniciar."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("✅ Tablas creadas / verificadas en PostgreSQL")
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
        "rol": u.rol,
        "room_id": u.room_id,
        "conectado": u.conectado,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "updated_at": u.updated_at.isoformat() if u.updated_at else None,
    }


def registrar_usuario_db(user_id: str, nombre: str, rol: str, room_id: str):
    """Registra o actualiza un usuario en la base de datos."""
    db = _get_db()
    try:
        usuario = db.query(Usuario).filter(Usuario.user_id == user_id).first()
        if usuario:
            usuario.nombre = nombre
            usuario.rol = rol
            usuario.room_id = room_id
            usuario.conectado = "si"
            logger.info(f"[DB] Usuario actualizado: {nombre} ({user_id}) rol='{rol}' sala='{room_id}'")
        else:
            usuario = Usuario(
                user_id=user_id,
                nombre=nombre,
                rol=rol,
                room_id=room_id,
                conectado="si"
            )
            db.add(usuario)
            logger.info(f"[DB] Usuario creado: {nombre} ({user_id}) rol='{rol}' sala='{room_id}'")
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


# ═══════════════════════════════════════════════════════════════════════════
#  AUTENTICACIÓN DE ADMINISTRADOR
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
    logger.info(f"[AUTH] Acceso admin autorizado: {credentials.username}")
    return credentials.username


# ═══════════════════════════════════════════════════════════════════════════
#  ENDPOINTS REST — CRUD DE USUARIOS
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/usuarios")
async def listar_usuarios():
    """Retorna todos los usuarios registrados."""
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


@app.put("/api/usuarios/{user_id}")
async def actualizar_usuario(user_id: str, datos: UsuarioUpdate, admin: str = Depends(verificar_admin)):
    """Actualiza los campos de un usuario existente. Requiere autenticación admin."""
    db = _get_db()
    try:
        usuario = db.query(Usuario).filter(Usuario.user_id == user_id).first()
        if not usuario:
            return JSONResponse(status_code=404, content={"detail": "Usuario no encontrado"})

        campos_actualizados = []
        if datos.nombre is not None:
            usuario.nombre = datos.nombre
            campos_actualizados.append(f"nombre='{datos.nombre}'")
        if datos.rol is not None:
            usuario.rol = datos.rol
            campos_actualizados.append(f"rol='{datos.rol}'")
        if datos.conectado is not None:
            usuario.conectado = datos.conectado
            campos_actualizados.append(f"conectado='{datos.conectado}'")
            if datos.conectado == "no":
                usuario.room_id = None

        db.commit()
        db.refresh(usuario)
        logger.info(f"[ADMIN] Usuario {user_id} actualizado por {admin}: {', '.join(campos_actualizados)}")
        return _serializar_usuario(usuario)
    except Exception as e:
        db.rollback()
        logger.error(f"[ADMIN] Error actualizando usuario {user_id}: {e}")
        return JSONResponse(status_code=500, content={"detail": "Error al actualizar usuario"})
    finally:
        db.close()


@app.delete("/api/usuarios/{user_id}")
async def eliminar_usuario(user_id: str, admin: str = Depends(verificar_admin)):
    """Elimina un usuario de la base de datos. Requiere autenticación admin."""
    db = _get_db()
    try:
        usuario = db.query(Usuario).filter(Usuario.user_id == user_id).first()
        if not usuario:
            return JSONResponse(status_code=404, content={"detail": "Usuario no encontrado"})

        nombre = usuario.nombre
        db.delete(usuario)
        db.commit()
        logger.info(f"[ADMIN] Usuario eliminado por {admin}: {nombre} ({user_id})")
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

        # Persistir desconexión en la base de datos
        desconectar_usuario_db(user_id)

        # Transferir rol de host si es necesario
        if room["host_id"] == user_id and room["user_ids"]:
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
    """Página principal."""
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/admin")
async def get_admin(username: str = Depends(verificar_admin)):
    """Panel de administración protegido con autenticación básica."""
    with open("admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


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
    user_id = uuid.uuid4().hex[:8]
    logger.info(f"[WS] Nueva conexión WebSocket: user_id={user_id} sala={room_id}")

    # ── Recibir datos iniciales del cliente ──
    try:
        init_msg = await websocket.receive_text()
        init_data = json.loads(init_msg)
        name = init_data.get("name", "Anónimo").strip() or "Anónimo"
        role = init_data.get("role", "").strip()
        username = f"{name} ({role})" if role else name
        logger.info(f"[WS] Datos iniciales recibidos: nombre='{name}' rol='{role}' display='{username}'")
    except json.JSONDecodeError as e:
        logger.warning(f"[WS] JSON inválido en datos iniciales de {user_id}: {e}")
        name, role, username = "Anónimo", "", "Anónimo"
    except Exception as e:
        logger.warning(f"[WS] Error recibiendo datos iniciales de {user_id}: {e}")
        name, role, username = "Anónimo", "", "Anónimo"

    # ── Registrar en la base de datos ──
    registrar_usuario_db(user_id, name, role, room_id)

    # ── Crear o unirse a la sala ──
    if room_id not in rooms:
        rooms[room_id] = {
            "host_id": user_id,
            "websockets": {},
            "user_ids": set(),
            "usernames": {},
            "muted": {}
        }
        logger.info(f"[SALA {room_id}] Sala creada por {username} (host)")

    room = rooms[room_id]
    room["websockets"][websocket] = user_id
    room["user_ids"].add(user_id)
    room["usernames"][user_id] = {"name": name, "role": role, "display": username}
    room["muted"][user_id] = False

    is_host = (room["host_id"] == user_id)

    # ── Enviar lista de participantes existentes al nuevo usuario ──
    other_users = []
    for uid in list(room["user_ids"]):
        if uid != user_id:
            other_users.append({
                "userId": uid,
                "display": room["usernames"].get(uid, {}).get("display", "Desconocido"),
                "isHost": (uid == room["host_id"]),
                "muted": room["muted"].get(uid, False)
            })

    await safe_send(websocket, json.dumps({
        "type": "existing-users",
        "users": other_users,
        "isHost": is_host,
        "hostId": room["host_id"],
        "myUserId": user_id
    }))

    # ── Notificar a los demás ──
    await broadcast(room, {
        "type": "user-joined",
        "userId": user_id,
        "display": username,
        "isHost": False,
        "muted": False
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
