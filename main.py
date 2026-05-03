import os
import sys

# Asegurar que el directorio actual esté en el path de Python (necesario en Render)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
from typing import Dict
import json
import uuid
import logging

from database import engine, SessionLocal, Base
from models import Usuario
from sqlalchemy.orm import Session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("videollamada")

app = FastAPI()
security = HTTPBasic()

# ── Credenciales de administrador ──────────────────────────────────────────
ADMIN_USERNAME = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "clave")

rooms: Dict[str, Dict] = {}


# ── Crear las tablas al iniciar la aplicación ──────────────────────────────
@app.on_event("startup")
async def startup():
    """Crea las tablas en PostgreSQL si no existen."""
    Base.metadata.create_all(bind=engine)
    logger.info("Tablas creadas / verificadas en PostgreSQL.")


# ── Helpers de base de datos ───────────────────────────────────────────────
def registrar_usuario_db(user_id: str, nombre: str, rol: str, room_id: str):
    """Registra o actualiza un usuario en la base de datos."""
    db = SessionLocal()
    try:
        usuario = db.query(Usuario).filter(Usuario.user_id == user_id).first()
        if usuario:
            usuario.nombre = nombre
            usuario.rol = rol
            usuario.room_id = room_id
            usuario.conectado = "si"
        else:
            usuario = Usuario(
                user_id=user_id,
                nombre=nombre,
                rol=rol,
                room_id=room_id,
                conectado="si"
            )
            db.add(usuario)
        db.commit()
        logger.info(f"Usuario {nombre} ({user_id}) registrado en BD con rol '{rol}'")
    except Exception as e:
        db.rollback()
        logger.error(f"Error registrando usuario en BD: {e}")
    finally:
        db.close()


def desconectar_usuario_db(user_id: str):
    """Marca un usuario como desconectado en la base de datos."""
    db = SessionLocal()
    try:
        usuario = db.query(Usuario).filter(Usuario.user_id == user_id).first()
        if usuario:
            usuario.conectado = "no"
            usuario.room_id = None
            db.commit()
            logger.info(f"Usuario {user_id} marcado como desconectado en BD")
    except Exception as e:
        db.rollback()
        logger.error(f"Error desconectando usuario en BD: {e}")
    finally:
        db.close()


# ── Endpoints REST para consultar usuarios ─────────────────────────────────
@app.get("/api/usuarios")
async def listar_usuarios():
    """Retorna todos los usuarios registrados en la base de datos."""
    db = SessionLocal()
    try:
        usuarios = db.query(Usuario).all()
        return [
            {
                "id": u.id,
                "user_id": u.user_id,
                "nombre": u.nombre,
                "rol": u.rol,
                "room_id": u.room_id,
                "conectado": u.conectado,
                "created_at": str(u.created_at) if u.created_at else None,
                "updated_at": str(u.updated_at) if u.updated_at else None,
            }
            for u in usuarios
        ]
    finally:
        db.close()


@app.get("/api/usuarios/conectados")
async def listar_usuarios_conectados():
    """Retorna solo los usuarios actualmente conectados."""
    db = SessionLocal()
    try:
        usuarios = db.query(Usuario).filter(Usuario.conectado == "si").all()
        return [
            {
                "id": u.id,
                "user_id": u.user_id,
                "nombre": u.nombre,
                "rol": u.rol,
                "room_id": u.room_id,
            }
            for u in usuarios
        ]
    finally:
        db.close()


@app.get("/api/usuarios/{user_id}")
async def obtener_usuario(user_id: str):
    """Retorna un usuario específico por su user_id."""
    db = SessionLocal()
    try:
        usuario = db.query(Usuario).filter(Usuario.user_id == user_id).first()
        if not usuario:
            return JSONResponse(status_code=404, content={"detail": "Usuario no encontrado"})
        return {
            "id": usuario.id,
            "user_id": usuario.user_id,
            "nombre": usuario.nombre,
            "rol": usuario.rol,
            "room_id": usuario.room_id,
            "conectado": usuario.conectado,
            "created_at": str(usuario.created_at) if usuario.created_at else None,
            "updated_at": str(usuario.updated_at) if usuario.updated_at else None,
        }
    finally:
        db.close()


# ── Utilidades WebSocket ───────────────────────────────────────────────────
async def safe_send(ws: WebSocket, data: str) -> bool:
    """Envía un mensaje a un WebSocket. Retorna False si falla."""
    try:
        await ws.send_text(data)
        return True
    except Exception:
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
    # Limpiar WebSockets desconectados
    for client in disconnected:
        if client in room["websockets"]:
            uid = room["websockets"][client]
            del room["websockets"][client]
            room["user_ids"].discard(uid)
            if uid in room["usernames"]:
                del room["usernames"][uid]
            if uid in room["muted"]:
                del room["muted"][uid]


async def cleanup_user(room: Dict, room_id: str, websocket: WebSocket, user_id: str, username: str):
    """Limpia el estado de un usuario que se desconecta o es expulsado."""
    if websocket in room["websockets"]:
        del room["websockets"][websocket]
    room["user_ids"].discard(user_id)
    if user_id in room["usernames"]:
        del room["usernames"][user_id]
    if user_id in room["muted"]:
        del room["muted"][user_id]

    # Marcar como desconectado en la base de datos
    desconectar_usuario_db(user_id)

    # Transferir host si es necesario
    if room["host_id"] == user_id and room["user_ids"]:
        new_host_id = next(iter(room["user_ids"]))
        room["host_id"] = new_host_id
        new_host_display = room["usernames"].get(new_host_id, {}).get("display", "Desconocido")
        await broadcast(room, {
            "type": "host-info",
            "hostId": new_host_id
        })
        await broadcast(room, {
            "type": "chat",
            "sender": "system",
            "message": f"{new_host_display} es ahora el anfitrión."
        })

    # Notificar salida a los demás
    await broadcast(room, {
        "type": "user-left",
        "userId": user_id,
        "username": username
    })
    await broadcast(room, {
        "type": "chat",
        "sender": "system",
        "message": f"{username} ha salido de la sala"
    })

    # Eliminar sala vacía
    if len(room["websockets"]) == 0 and room_id in rooms:
        del rooms[room_id]
        logger.info(f"Sala {room_id} eliminada (vacía)")


# ── Verificación de credenciales de admin ──────────────────────────────────
def verificar_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Verifica que las credenciales sean las del administrador."""
    username_ok = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    password_ok = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (username_ok and password_ok):
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ── Rutas HTML ─────────────────────────────────────────────────────────────
@app.get("/")
async def get_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/admin")
async def get_admin(username: str = Depends(verificar_admin)):
    """Panel de administración protegido con autenticación básica."""
    with open("admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/room/{room_id}")
async def get_room(room_id: str):
    with open("room.html", "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{{ROOM_ID}}", room_id)
    return HTMLResponse(content=html)


# ── WebSocket principal ───────────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    user_id = uuid.uuid4().hex[:8]

    try:
        init_msg = await websocket.receive_text()
        init_data = json.loads(init_msg)
        name = init_data.get("name", "Anónimo")
        role = init_data.get("role", "")
        username = f"{name} ({role})" if role else name
    except Exception as e:
        logger.warning(f"Error recibiendo datos iniciales: {e}")
        name = "Anónimo"
        role = ""
        username = "Anónimo"

    # ── Registrar usuario con su rol en la base de datos ──
    registrar_usuario_db(user_id, name, role, room_id)

    if room_id not in rooms:
        rooms[room_id] = {
            "host_id": user_id,
            "websockets": {},
            "user_ids": set(),
            "usernames": {},
            "muted": {}
        }
        logger.info(f"Sala {room_id} creada por {username}")

    room = rooms[room_id]
    room["websockets"][websocket] = user_id
    room["user_ids"].add(user_id)
    room["usernames"][user_id] = {"name": name, "role": role, "display": username}
    room["muted"][user_id] = False

    is_host = (room["host_id"] == user_id)

    # Enviar lista de participantes existentes al nuevo
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

    # Notificar a los demás que alguien se unió
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

    logger.info(f"{username} ({user_id}) se unió a sala {room_id}")

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            message["senderId"] = user_id
            message["senderName"] = username

            msg_type = message.get("type")

            # Comandos de host
            if msg_type == "host-mute":
                if is_host and "targetId" in message:
                    target_id = message["targetId"]
                    mute_state = message.get("mute", True)
                    if target_id in room["muted"]:
                        room["muted"][target_id] = mute_state
                        await broadcast(room, {
                            "type": "user-muted",
                            "userId": target_id,
                            "muted": mute_state
                        })
                        logger.info(f"Host {username} {'silenció' if mute_state else 'activó'} a {target_id}")
                continue

            if msg_type == "host-kick":
                if is_host and "targetId" in message:
                    target_id = message["targetId"]
                    if target_id == user_id:
                        continue  # No puede expulsarse a sí mismo
                    # Buscar el WebSocket del usuario a expulsar
                    target_ws = None
                    target_name = room["usernames"].get(target_id, {}).get("display", "Desconocido")
                    for client, uid in list(room["websockets"].items()):
                        if uid == target_id:
                            target_ws = client
                            break
                    if target_ws:
                        # Notificar al expulsado
                        await safe_send(target_ws, json.dumps({
                            "type": "kicked",
                            "reason": "Has sido expulsado por el anfitrión."
                        }))
                        # Cerrar su WebSocket
                        try:
                            await target_ws.close()
                        except Exception:
                            pass
                        # Limpiar estado del usuario expulsado
                        await cleanup_user(room, room_id, target_ws, target_id, target_name)
                        logger.info(f"Host {username} expulsó a {target_name}")
                continue

            # Reenviar señalización WebRTC y chat a los destinatarios
            target = message.get("target")
            if target:
                # Enviar solo al destinatario específico
                for client, uid in list(room["websockets"].items()):
                    if uid == target:
                        await safe_send(client, json.dumps(message))
                        break
            else:
                # Broadcast a todos excepto al remitente
                await broadcast(room, message, exclude_ws=websocket)

    except WebSocketDisconnect:
        logger.info(f"{username} ({user_id}) se desconectó de sala {room_id}")
        if room_id in rooms:
            await cleanup_user(room, room_id, websocket, user_id, username)
    except Exception as e:
        logger.error(f"Error inesperado para {username} ({user_id}): {e}")
        if room_id in rooms:
            await cleanup_user(room, room_id, websocket, user_id, username)
