from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse  # 👈 Importante
from typing import Dict, List

app = FastAPI()

# 👇 Esto sirve para mostrar el index.html cuando entres a la web
@app.get("/")
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

# Estructura de salas
rooms: Dict[str, List[WebSocket]] = {}

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()

    if room_id not in rooms:
        rooms[room_id] = []

    if len(rooms[room_id]) >= 2:
        await websocket.send_text("Sala llena")
        await websocket.close()
        return

    rooms[room_id].append(websocket)

    try:
        while True:
            data = await websocket.receive_text()
            for client in rooms[room_id]:
                if client != websocket:
                    await client.send_text(data)
    except WebSocketDisconnect:
        rooms[room_id].remove(websocket)
        if len(rooms[room_id]) == 0:
            del rooms[room_id]