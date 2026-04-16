from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from typing import Dict, Set
import json
import uuid

app = FastAPI()

# Estructura de una sala:
# {
#   "host_id": str,
#   "websockets": { ws: user_id },
#   "user_ids": set(),
#   "usernames": { user_id: {"name": str, "role": str} },
#   "muted": { user_id: bool }   # silenciado por el host
# }
rooms: Dict[str, Dict] = {}

@app.get("/")
async def get_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/room/{room_id}")
async def get_room(room_id: str):
    with open("room.html", "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{{ROOM_ID}}", room_id)
    return HTMLResponse(content=html)

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    user_id = uuid.uuid4().hex[:8]
    
    # Recibir datos de identidad (nombre + cargo)
    try:
        init_msg = await websocket.receive_text()
        init_data = json.loads(init_msg)
        name = init_data.get("name", "Anónimo")
        role = init_data.get("role", "")
        username = f"{name} ({role})" if role else name
    except:
        name = "Anónimo"
        role = ""
        username = "Anónimo"
    
    # Crear sala si no existe
    if room_id not in rooms:
        rooms[room_id] = {
            "host_id": user_id,   # el primero en llegar es host
            "websockets": {},
            "user_ids": set(),
            "usernames": {},
            "muted": {}
        }
    
    room = rooms[room_id]
    room["websockets"][websocket] = user_id
    room["user_ids"].add(user_id)
    room["usernames"][user_id] = {"name": name, "role": role, "display": username}
    room["muted"][user_id] = False
    
    is_host = (room["host_id"] == user_id)
    
    # Enviar al nuevo usuario la lista de participantes existentes
    other_users = []
    for uid in room["user_ids"]:
        if uid != user_id:
            other_users.append({
                "userId": uid,
                "name": room["usernames"][uid]["name"],
                "role": room["usernames"][uid]["role"],
                "display": room["usernames"][uid]["display"],
                "isHost": (uid == room["host_id"]),
                "muted": room["muted"][uid]
            })
    
    await websocket.send_text(json.dumps({
        "type": "existing-users",
        "users": other_users,
        "isHost": is_host,
        "hostId": room["host_id"]
    }))
    
    # Notificar a todos (incluyendo el nuevo) quién es el host actual
    for client, uid in room["websockets"].items():
        await client.send_text(json.dumps({
            "type": "host-info",
            "hostId": room["host_id"]
        }))
    
    # Notificar a los demás que alguien se unió
    for client, uid in room["websockets"].items():
        if client != websocket:
            await client.send_text(json.dumps({
                "type": "user-joined",
                "userId": user_id,
                "name": name,
                "role": role,
                "display": username,
                "isHost": False,
                "muted": False
            }))
            await client.send_text(json.dumps({
                "type": "chat",
                "sender": "system",
                "message": f"{username} se ha unido a la sala"
            }))
    
    # Enviar mensaje de bienvenida al nuevo
    await websocket.send_text(json.dumps({
        "type": "chat",
        "sender": "system",
        "message": f"Bienvenido {username}. {'Eres el anfitrión de la sala.' if is_host else 'El anfitrión puede silenciar o expulsar participantes.'}"
    }))
    
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            message["senderId"] = user_id
            message["senderName"] = username
            
            # Comandos de host
            if message.get("type") == "host-mute":
                # Solo el host puede silenciar
                if is_host and "targetId" in message:
                    target_id = message["targetId"]
                    mute_state = message.get("mute", True)
                    if target_id in room["muted"]:
                        room["muted"][target_id] = mute_state
                        # Notificar a todos los participantes (incluido el objetivo)
                        for client, uid in room["websockets"].items():
                            await client.send_text(json.dumps({
                                "type": "user-muted",
                                "userId": target_id,
                                "muted": mute_state
                            }))
                        # También enviar un mensaje de chat del sistema
                        target_display = room["usernames"][target_id]["display"]
                        action = "silenciado" if mute_state else "reactivado"
                        for client, uid in room["websockets"].items():
                            await client.send_text(json.dumps({
                                "type": "chat",
                                "sender": "system",
                                "message": f"{target_display} ha sido {action} por el anfitrión."
                            }))
                continue
            
            if message.get("type") == "host-kick":
                if is_host and "targetId" in message:
                    target_id = message["targetId"]
                    if target_id == user_id:
                        continue  # no puede expulsarse a sí mismo
                    # Encontrar el websocket del objetivo y cerrarlo
                    for client, uid in room["websockets"].items():
                        if uid == target_id:
                            await client.send_text(json.dumps({
                                "type": "kicked",
                                "reason": "Has sido expulsado por el anfitrión."
                            }))
                            await client.close()
                            break
                    # El resto del cleanup se hará en el except WebSocketDisconnect
                continue
            
            # Reenviar mensajes normales (signalización, chat) a todos los demás
            target = message.get("target")
            for client, uid in room["websockets"].items():
                if client != websocket:
                    if target is None or uid == target:
                        # Añadir metadata útil
                        if message.get("type") == "chat":
                            message["senderDisplay"] = username
                        await client.send_text(json.dumps(message))
    except WebSocketDisconnect:
        # Limpiar usuario
        if websocket in room["websockets"]:
            del room["websockets"][websocket]
        room["user_ids"].discard(user_id)
        if user_id in room["usernames"]:
            del room["usernames"][user_id]
        if user_id in room["muted"]:
            del room["muted"][user_id]
        
        # Si era el host y aún quedan usuarios, asignar nuevo host
        if room["host_id"] == user_id and room["user_ids"]:
            new_host_id = next(iter(room["user_ids"]))  # cualquier otro
            room["host_id"] = new_host_id
            # Notificar cambio de host
            for client, uid in room["websockets"].items():
                await client.send_text(json.dumps({
                    "type": "host-info",
                    "hostId": new_host_id
                }))
                await client.send_text(json.dumps({
                    "type": "chat",
                    "sender": "system",
                    "message": f"{room['usernames'][new_host_id]['display']} es ahora el anfitrión."
                }))
        
        # Notificar salida
        for client, uid in room["websockets"].items():
            await client.send_text(json.dumps({
                "type": "user-left",
                "userId": user_id,
                "username": username
            }))
            await client.send_text(json.dumps({
                "type": "chat",
                "sender": "system",
                "message": f"{username} ha salido de la sala"
            }))
        
        if len(room["websockets"]) == 0:
            del rooms[room_id]
