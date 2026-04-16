from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from typing import Dict, List
import json

app = FastAPI()

# Servir el index.html
@app.get("/")
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

# Estructura de salas: { room_id: [websocket1, websocket2] }
rooms: Dict[str, List[WebSocket]] = {}

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    
    # Crear sala si no existe
    if room_id not in rooms:
        rooms[room_id] = []
    
    # Limitar a 2 participantes por sala (puedes aumentarlo si quieres)
    if len(rooms[room_id]) >= 2:
        await websocket.send_text(json.dumps({"type": "error", "message": "Sala llena"}))
        await websocket.close()
        return
    
    # Agregar el nuevo websocket a la sala
    rooms[room_id].append(websocket)
    
    # Notificar al otro usuario que alguien se ha conectado (opcional)
    if len(rooms[room_id]) == 2:
        for client in rooms[room_id]:
            await client.send_text(json.dumps({"type": "peer-connected"}))
    
    try:
        while True:
            # Recibir mensaje del cliente
            data = await websocket.receive_text()
            message = json.loads(data)
            
            # Reenviar el mensaje al otro cliente en la misma sala
            for client in rooms[room_id]:
                if client != websocket:
                    await client.send_text(json.dumps(message))
    except WebSocketDisconnect:
        # Eliminar el websocket de la sala
        if websocket in rooms[room_id]:
            rooms[room_id].remove(websocket)
        # Si la sala queda vacía, eliminarla
        if len(rooms[room_id]) == 0:
            del rooms[room_id]
        else:
            # Notificar al otro usuario que el peer se desconectó
            for client in rooms[room_id]:
                await client.send_text(json.dumps({"type": "hangup"}))
