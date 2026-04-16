from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from typing import Dict, Set
import json
import uuid

app = FastAPI()

# Estructura: room_id -> {"websockets": {ws: user_id}, "user_ids": set()}
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
    user_id = uuid.uuid4().hex[:8]  # ID más corto

    # Crear sala si no existe
    if room_id not in rooms:
        rooms[room_id] = {"websockets": {}, "user_ids": set()}
    
    room = rooms[room_id]
    room["websockets"][websocket] = user_id
    room["user_ids"].add(user_id)

    # Enviar al nuevo usuario la lista de usuarios ya conectados
    other_users = [uid for uid in room["user_ids"] if uid != user_id]
    await websocket.send_text(json.dumps({
        "type": "existing-users",
        "users": other_users
    }))

    # Notificar a los demás que alguien se unió
    for client in room["websockets"]:
        if client != websocket:
            await client.send_text(json.dumps({
                "type": "user-joined",
                "userId": user_id
            }))

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            # Reenviar mensaje a todos los demás (o a un target específico)
            target = message.get("target")
            for client, uid in room["websockets"].items():
                if client != websocket:
                    if target is None or uid == target:
                        # Añadir el sender para que el frontend sepa quién envía
                        message["sender"] = user_id
                        await client.send_text(json.dumps(message))
    except WebSocketDisconnect:
        # Limpiar
        if websocket in room["websockets"]:
            del room["websockets"][websocket]
        room["user_ids"].discard(user_id)
        # Notificar salida
        for client in room["websockets"]:
            await client.send_text(json.dumps({
                "type": "user-left",
                "userId": user_id
            }))
        if len(room["websockets"]) == 0:
            del rooms[room_id]
