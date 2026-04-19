from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from typing import Dict, Optional
import json
import uuid
import logging
import time
import os
import psutil
from datetime import datetime, timedelta

from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from jose import jwt, JWTError
from passlib.hash import pbkdf2_sha256 as hasher

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ensigna")

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY", "ensigna-secret-key-change-in-production")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS_HASH = hasher.hash(os.environ.get("ADMIN_PASS", "admin123"))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 8

ROLE_PRIORITY = {
    "ceo": 100, "director": 90, "vicepresidente": 90, "vp": 90,
    "gerente": 70, "manager": 70, "jefe": 70,
    "coordinador": 50, "supervisor": 50, "líder": 50, "lider": 50,
    "analista": 30, "especialista": 30, "ingeniero": 30,
    "asistente": 10, "practicante": 10, "becario": 10,
}
DEFAULT_PRIORITY = 20

# ──────────────────────────────────────────────
# Prometheus Metrics
# ──────────────────────────────────────────────
active_rooms = Gauge("ensigna_active_rooms", "Active rooms")
active_participants = Gauge("ensigna_active_participants", "Total participants")
active_websockets = Gauge("ensigna_active_websockets", "Open WebSocket connections")
rooms_created_total = Counter("ensigna_rooms_created_total", "Total rooms created")
messages_sent_total = Counter("ensigna_messages_sent_total", "Chat messages sent")
webrtc_offers_total = Counter("ensigna_webrtc_offers_total", "WebRTC offers")
webrtc_answers_total = Counter("ensigna_webrtc_answers_total", "WebRTC answers")
ws_message_duration = Histogram("ensigna_ws_message_duration_seconds", "WS message processing time")
cpu_gauge = Gauge("ensigna_cpu_usage_percent", "CPU usage percent")
mem_gauge = Gauge("ensigna_memory_usage_percent", "Memory usage percent")
network_bandwidth = Gauge("ensigna_network_bandwidth_mbps", "Estimated network bandwidth usage in Mbps")
latency_gauge = Histogram("ensigna_webrtc_latency_ms", "WebRTC latency in milliseconds")
video_quality_gauge = Gauge("ensigna_video_quality_score", "Average video quality score (0-100)")
audio_quality_gauge = Gauge("ensigna_audio_quality_score", "Average audio quality score (0-100)")

app = FastAPI(title="Ensigna.corp", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")

start_time = time.time()
rooms: Dict[str, Dict] = {}

# ──────────────────────────────────────────────
# QoS Helpers
# ──────────────────────────────────────────────
def compute_role_priority(role: str) -> int:
    if not role:
        return DEFAULT_PRIORITY
    role_lower = role.lower().strip()
    for key, val in ROLE_PRIORITY.items():
        if key in role_lower:
            return val
    return DEFAULT_PRIORITY

def compute_room_priority(room: Dict) -> int:
    max_p = DEFAULT_PRIORITY
    for uid, udata in room.get("usernames", {}).items():
        p = compute_role_priority(udata.get("role", ""))
        if p > max_p:
            max_p = p
    override = room.get("priority_override")
    if override is not None:
        return override
    return max_p

def get_qos_params(priority: int, total_rooms: int) -> dict:
    if total_rooms <= 1:
        return {"qos": "high", "maxBitrate": 2500000, "maxFramerate": 30}
    if priority >= 70:
        return {"qos": "high", "maxBitrate": 2500000, "maxFramerate": 30}
    elif priority >= 40:
        return {"qos": "medium", "maxBitrate": 1200000, "maxFramerate": 24}
    else:
        return {"qos": "low", "maxBitrate": 500000, "maxFramerate": 15}

# ──────────────────────────────────────────────
# Auth Helpers
# ──────────────────────────────────────────────
def create_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm=JWT_ALGORITHM)

def verify_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None

def get_admin_user(request: Request) -> str:
    token = request.cookies.get("ensigna_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user

# ──────────────────────────────────────────────
# WebSocket Helpers
# ──────────────────────────────────────────────
async def safe_send(ws: WebSocket, data: str) -> bool:
    try:
        await ws.send_text(data)
        return True
    except Exception:
        return False

async def broadcast(room: Dict, message: dict, exclude_ws: WebSocket = None):
    data = json.dumps(message)
    disconnected = []
    for client, uid in list(room["websockets"].items()):
        if client != exclude_ws:
            if not await safe_send(client, data):
                disconnected.append(client)
    for client in disconnected:
        if client in room["websockets"]:
            uid = room["websockets"][client]
            del room["websockets"][client]
            room["user_ids"].discard(uid)
            room["usernames"].pop(uid, None)
            room["muted"].pop(uid, None)

async def broadcast_qos(room: Dict):
    priority = compute_room_priority(room)
    qos = get_qos_params(priority, len(rooms))
    await broadcast(room, {"type": "qos-update", "priority": priority, **qos})

async def cleanup_user(room: Dict, room_id: str, websocket: WebSocket, user_id: str, username: str):
    if websocket in room["websockets"]:
        del room["websockets"][websocket]
    room["user_ids"].discard(user_id)
    room["usernames"].pop(user_id, None)
    room["muted"].pop(user_id, None)

    active_participants.dec()
    active_websockets.dec()

    if room["host_id"] == user_id and room["user_ids"]:
        new_host_id = next(iter(room["user_ids"]))
        room["host_id"] = new_host_id
        disp = room["usernames"].get(new_host_id, {}).get("display", "")
        await broadcast(room, {"type": "host-info", "hostId": new_host_id})
        await broadcast(room, {"type": "chat", "sender": "system", "message": f"{disp} es ahora el anfitrion."})

    await broadcast(room, {"type": "user-left", "userId": user_id, "username": username})
    await broadcast(room, {"type": "chat", "sender": "system", "message": f"{username} ha salido de la sala"})

    if len(room["websockets"]) == 0 and room_id in rooms:
        del rooms[room_id]
        active_rooms.dec()
        logger.info(f"Room {room_id} removed (empty)")
    else:
        await broadcast_qos(room)

def update_system_metrics():
    cpu_gauge.set(psutil.cpu_percent(interval=None))
    mem_gauge.set(psutil.virtual_memory().percent)

# ──────────────────────────────────────────────
# Page Routes
# ──────────────────────────────────────────────
@app.get("/")
async def get_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/room/{room_id}")
async def get_room(room_id: str):
    with open("room.html", "r", encoding="utf-8") as f:
        html = f.read().replace("{{ROOM_ID}}", room_id)
    return HTMLResponse(content=html)

@app.get("/login")
async def get_login():
    with open("login.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/admin")
async def get_admin(request: Request):
    token = request.cookies.get("ensigna_token")
    if not token or not verify_token(token):
        return RedirectResponse(url="/login", status_code=302)
    with open("admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

# ──────────────────────────────────────────────
# Auth API
# ──────────────────────────────────────────────
@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")
    if username == ADMIN_USER and hasher.verify(password, ADMIN_PASS_HASH):
        token = create_token(username)
        response = JSONResponse({"ok": True})
        response.set_cookie("ensigna_token", token, httponly=True, samesite="lax", max_age=JWT_EXPIRE_HOURS*3600)
        return response
    raise HTTPException(status_code=401, detail="Invalid credentials")

@app.post("/api/logout")
async def api_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("ensigna_token")
    return response

# ──────────────────────────────────────────────
# Admin API
# ──────────────────────────────────────────────
@app.get("/api/admin/rooms")
async def admin_rooms(user: str = Depends(get_admin_user)):
    result = []
    for rid, room in rooms.items():
        users = []
        for uid, udata in room["usernames"].items():
            users.append({
                "userId": uid,
                "name": udata.get("name", ""),
                "role": udata.get("role", ""),
                "display": udata.get("display", ""),
                "isHost": uid == room["host_id"],
                "muted": room["muted"].get(uid, False),
                "priority": compute_role_priority(udata.get("role", ""))
            })
        result.append({
            "roomId": rid,
            "participants": len(room["user_ids"]),
            "users": users,
            "priority": compute_room_priority(room),
            "qos": get_qos_params(compute_room_priority(room), len(rooms)),
            "hostId": room["host_id"],
            "createdAt": room.get("created_at", "")
        })
    return result

@app.get("/api/admin/system")
async def admin_system(user: str = Depends(get_admin_user)):
    update_system_metrics()
    return {
        "cpu": psutil.cpu_percent(interval=0.1),
        "memory": psutil.virtual_memory().percent,
        "activeRooms": len(rooms),
        "activeParticipants": sum(len(r["user_ids"]) for r in rooms.values()),
        "activeWebsockets": sum(len(r["websockets"]) for r in rooms.values()),
        "uptime": int(time.time() - start_time),
        "totalRoomsCreated": rooms_created_total._value.get(),
        "totalMessages": messages_sent_total._value.get()
    }

@app.post("/api/admin/qos")
async def admin_qos(request: Request, user: str = Depends(get_admin_user)):
    body = await request.json()
    room_id = body.get("roomId")
    priority = body.get("priority")
    if room_id not in rooms:
        raise HTTPException(404, "Room not found")
    if priority is not None:
        rooms[room_id]["priority_override"] = int(priority)
    else:
        rooms[room_id].pop("priority_override", None)
    await broadcast_qos(rooms[room_id])
    return {"ok": True, "priority": compute_room_priority(rooms[room_id])}

@app.post("/api/metrics")
async def receive_metrics(request: Request):
    body = await request.json()
    video_quality = body.get("videoQuality", 0)
    audio_quality = body.get("audioQuality", 0)
    latency = body.get("latency", 0)
    bandwidth = body.get("bandwidth", 0)
    
    video_quality_gauge.set(video_quality)
    audio_quality_gauge.set(audio_quality)
    latency_gauge.observe(latency)
    network_bandwidth.set(bandwidth)
    
    return {"ok": True}

# ──────────────────────────────────────────────
# WebSocket Endpoint
# ──────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    user_id = uuid.uuid4().hex[:8]
    active_websockets.inc()

    try:
        init_msg = await websocket.receive_text()
        init_data = json.loads(init_msg)
        name = init_data.get("name", "Anonimo")
        role = init_data.get("role", "")
        username = f"{name} - {role}" if role else name
    except Exception as e:
        logger.warning(f"Init error: {e}")
        name, role, username = "Anonimo", "", "Anonimo"

    if room_id not in rooms:
        rooms[room_id] = {
            "host_id": user_id, "websockets": {}, "user_ids": set(),
            "usernames": {}, "muted": {}, "created_at": datetime.utcnow().isoformat()
        }
        active_rooms.inc()
        rooms_created_total.inc()
        logger.info(f"Room {room_id} created by {username}")

    room = rooms[room_id]
    room["websockets"][websocket] = user_id
    room["user_ids"].add(user_id)
    room["usernames"][user_id] = {"name": name, "role": role, "display": username}
    room["muted"][user_id] = False
    active_participants.inc()

    is_host = room["host_id"] == user_id
    user_priority = compute_role_priority(role)

    other_users = []
    for uid in list(room["user_ids"]):
        if uid != user_id:
            other_users.append({
                "userId": uid,
                "display": room["usernames"].get(uid, {}).get("display", ""),
                "isHost": uid == room["host_id"],
                "muted": room["muted"].get(uid, False),
                "role": room["usernames"].get(uid, {}).get("role", "")
            })

    room_priority = compute_room_priority(room)
    qos = get_qos_params(room_priority, len(rooms))

    await safe_send(websocket, json.dumps({
        "type": "existing-users", "users": other_users,
        "isHost": is_host, "hostId": room["host_id"], "myUserId": user_id,
        "priority": room_priority, **qos
    }))

    await broadcast(room, {
        "type": "user-joined", "userId": user_id, "display": username,
        "isHost": False, "muted": False, "role": role
    }, exclude_ws=websocket)
    await broadcast(room, {
        "type": "chat", "sender": "system", "message": f"{username} se ha unido"
    }, exclude_ws=websocket)

    await broadcast_qos(room)

    try:
        while True:
            t0 = time.time()
            data = await websocket.receive_text()
            message = json.loads(data)
            message["senderId"] = user_id
            message["senderName"] = username
            msg_type = message.get("type")

            if msg_type == "host-mute":
                if is_host and "targetId" in message:
                    tid = message["targetId"]
                    mute = message.get("mute", True)
                    if tid in room["muted"]:
                        room["muted"][tid] = mute
                        await broadcast(room, {"type": "user-muted", "userId": tid, "muted": mute})
                ws_message_duration.observe(time.time() - t0)
                continue

            if msg_type == "host-kick":
                if is_host and "targetId" in message:
                    tid = message["targetId"]
                    if tid == user_id:
                        continue
                    tws = None
                    tname = room["usernames"].get(tid, {}).get("display", "")
                    for c, u in list(room["websockets"].items()):
                        if u == tid:
                            tws = c
                            break
                    if tws:
                        await safe_send(tws, json.dumps({"type": "kicked", "reason": "Expulsado por el anfitrion."}))
                        try: await tws.close()
                        except: pass
                        await cleanup_user(room, room_id, tws, tid, tname)
                ws_message_duration.observe(time.time() - t0)
                continue

            if msg_type == "chat":
                messages_sent_total.inc()
            elif msg_type == "offer":
                webrtc_offers_total.inc()
            elif msg_type == "answer":
                webrtc_answers_total.inc()

            target = message.get("target")
            if target:
                for c, u in list(room["websockets"].items()):
                    if u == target:
                        await safe_send(c, json.dumps(message))
                        break
            else:
                await broadcast(room, message, exclude_ws=websocket)

            ws_message_duration.observe(time.time() - t0)

    except WebSocketDisconnect:
        logger.info(f"{username} disconnected from {room_id}")
        if room_id in rooms:
            await cleanup_user(room, room_id, websocket, user_id, username)
    except Exception as e:
        logger.error(f"Error for {username}: {e}")
        if room_id in rooms:
            await cleanup_user(room, room_id, websocket, user_id, username)
