from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from typing import Dict, Set
import json
import uuid

app = FastAPI()

# Estructura: room_id -> {"websockets": {ws: user_id}, "user_ids": set(), "usernames": {ws: username}}
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
    
    # Recibir nombre de usuario (primer mensaje)
    try:
        init_msg = await websocket.receive_text()
        init_data = json.loads(init_msg)
        username = init_data.get("username", f"Usuario-{user_id[:4]}")
    except:
        username = f"Usuario-{user_id[:4]}"
    
    # Crear sala si no existe
    if room_id not in rooms:
        rooms[room_id] = {"websockets": {}, "user_ids": set(), "usernames": {}}
    
    room = rooms[room_id]
    room["websockets"][websocket] = user_id
    room["user_ids"].add(user_id)
    room["usernames"][user_id] = username
    
    # Enviar al nuevo usuario la lista de usuarios existentes (con nombres)
    other_users = []
    for uid in room["user_ids"]:
        if uid != user_id:
            other_users.append({"userId": uid, "username": room["usernames"][uid]})
    await websocket.send_text(json.dumps({
        "type": "existing-users",
        "users": other_users
    }))
    
    # Notificar a los demás que alguien se unió
    for client, uid in room["websockets"].items():
        if client != websocket:
            await client.send_text(json.dumps({
                "type": "user-joined",
                "userId": user_id,
                "username": username
            }))
            # También enviar mensaje de chat de sistema
            await client.send_text(json.dumps({
                "type": "chat",
                "sender": "system",
                "message": f"{username} se ha unido a la sala",
                "timestamp": None
            }))
    
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            message["senderId"] = user_id
            message["senderName"] = username
            
            # Reenviar a todos los demás (o a target específico)
            target = message.get("target")
            for client, uid in room["websockets"].items():
                if client != websocket:
                    if target is None or uid == target:
                        await client.send_text(json.dumps(message))
    except WebSocketDisconnect:
        # Limpiar
        if websocket in room["websockets"]:
            del room["websockets"][websocket]
        room["user_ids"].discard(user_id)
        if user_id in room["usernames"]:
            del room["usernames"][user_id]
        # Notificar salida
        for client in room["websockets"]:
            await client.send_text(json.dumps({
                "type": "user-left",
                "userId": user_id,
                "username": username
            }))
            await client.send_text(json.dumps({
                "type": "chat",
                "sender": "system",
                "message": f"{username} ha salido de la sala",
                "timestamp": None
            }))
        if len(room["websockets"]) == 0:
            del rooms[room_id]
