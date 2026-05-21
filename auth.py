"""
Módulo de autenticación para la plataforma de videollamadas.
Maneja JWT tokens, hashing de contraseñas y gestión de sesiones.
"""

import os
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional

from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi import Request, Response, HTTPException, status

logger = logging.getLogger("videollamada.auth")

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════

SECRET_KEY = os.environ.get("SECRET_KEY", "v1d30ll4m4d4_s3cr3t_k3y_pr0duct10n_2024_ensigan")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24
COOKIE_NAME = "session_token"

# Contexto de hashing con bcrypt
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ═══════════════════════════════════════════════════════════════════════════
#  HASHING DE CONTRASEÑAS
# ═══════════════════════════════════════════════════════════════════════════

def hash_password(password: str) -> str:
    """Genera un hash bcrypt de la contraseña."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica una contraseña contra su hash bcrypt."""
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception as e:
        logger.error(f"[AUTH] Error verificando contraseña: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
#  JWT TOKENS
# ═══════════════════════════════════════════════════════════════════════════

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Crea un JWT token con los datos del usuario.
    
    Args:
        data: Diccionario con datos del usuario (user_id, nombre, rol, etc.)
        expires_delta: Tiempo de expiración opcional
    
    Returns:
        Token JWT firmado
    """
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS))
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Optional[dict]:
    """
    Decodifica y valida un JWT token.
    
    Returns:
        Diccionario con los datos del usuario, o None si es inválido/expirado
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError as e:
        logger.debug(f"[AUTH] Token inválido: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  SESIONES CON COOKIES
# ═══════════════════════════════════════════════════════════════════════════

def set_session_cookie(response: Response, token: str) -> None:
    """Establece la cookie de sesión en la respuesta HTTP."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        secure=os.environ.get("ENVIRONMENT", "development") == "production",
        path="/"
    )
    logger.info("[AUTH] Cookie de sesión establecida")


def clear_session_cookie(response: Response) -> None:
    """Elimina la cookie de sesión."""
    response.delete_cookie(key=COOKIE_NAME, path="/")
    logger.info("[AUTH] Cookie de sesión eliminada")


def get_session_from_request(request: Request) -> Optional[dict]:
    """
    Extrae y valida la sesión del usuario desde la cookie.
    
    Returns:
        Diccionario con datos del usuario o None si no hay sesión válida
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return decode_token(token)


def require_session(request: Request) -> dict:
    """
    Requiere una sesión válida. Lanza HTTPException si no existe.
    Para usar como dependencia de FastAPI.
    """
    session = get_session_from_request(request)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión inválida o expirada. Inicia sesión nuevamente."
        )
    return session


def require_admin(request: Request) -> dict:
    """
    Requiere una sesión de administrador. Lanza HTTPException si no es admin.
    Para usar como dependencia de FastAPI.
    """
    session = require_session(request)
    if not session.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado. Se requieren permisos de administrador."
        )
    return session


# ═══════════════════════════════════════════════════════════════════════════
#  UTILIDADES
# ═══════════════════════════════════════════════════════════════════════════

def generate_user_id() -> str:
    """Genera un ID único para un usuario."""
    return uuid.uuid4().hex[:12]


def generate_guest_name() -> str:
    """Genera un nombre para un invitado."""
    return f"Invitado-{uuid.uuid4().hex[:6]}"
