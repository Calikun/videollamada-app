"""
Módulo de configuración de QoS (Quality of Service) por rol.
Define los roles predefinidos y sus niveles de prioridad en la conexión WebRTC.
"""

import logging

logger = logging.getLogger("videollamada.qos")

# ═══════════════════════════════════════════════════════════════════════════
#  ROLES PREDEFINIDOS CON PRIORIDAD QoS
# ═══════════════════════════════════════════════════════════════════════════

# Prioridad: 2 = Alta, 1 = Media, 0 = Normal
ROLES_CONFIG = {
    "Director": {
        "qos_priority": 2,
        "label": "Director",
        "description": "Máxima prioridad en calidad de conexión",
        "color": "#ef4444",  # Rojo premium
        "icon": "👔"
    },
    "Gerente": {
        "qos_priority": 2,
        "label": "Gerente",
        "description": "Máxima prioridad en calidad de conexión",
        "color": "#f97316",  # Naranja
        "icon": "📊"
    },
    "Coordinador": {
        "qos_priority": 1,
        "label": "Coordinador",
        "description": "Prioridad media en calidad de conexión",
        "color": "#eab308",  # Amarillo
        "icon": "📋"
    },
    "Docente": {
        "qos_priority": 1,
        "label": "Docente",
        "description": "Prioridad media en calidad de conexión",
        "color": "#3b82f6",  # Azul
        "icon": "🎓"
    },
    "Estudiante": {
        "qos_priority": 0,
        "label": "Estudiante",
        "description": "Prioridad estándar en calidad de conexión",
        "color": "#22c55e",  # Verde
        "icon": "📚"
    },
    "Invitado": {
        "qos_priority": 0,
        "label": "Invitado",
        "description": "Prioridad estándar en calidad de conexión",
        "color": "#6b7280",  # Gris
        "icon": "👤"
    }
}

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN DE BITRATE Y FRAMERATE POR PRIORIDAD
# ═══════════════════════════════════════════════════════════════════════════

QOS_PROFILES = {
    2: {  # Alta prioridad
        "maxBitrate": 2500000,      # 2.5 Mbps
        "maxFramerate": 30,
        "priority": "high",
        "networkPriority": "high",
        "scaleResolutionDownBy": 1,  # Sin reducción
        "label": "Alta",
        "description": "Video HD completo, sin restricciones"
    },
    1: {  # Prioridad media
        "maxBitrate": 1500000,      # 1.5 Mbps
        "maxFramerate": 25,
        "priority": "medium",
        "networkPriority": "medium",
        "scaleResolutionDownBy": 1,
        "label": "Media",
        "description": "Video de buena calidad"
    },
    0: {  # Prioridad normal
        "maxBitrate": 800000,       # 800 kbps
        "maxFramerate": 20,
        "priority": "low",
        "networkPriority": "low",
        "scaleResolutionDownBy": 1.5,
        "label": "Normal",
        "description": "Video estándar optimizado"
    }
}

# ═══════════════════════════════════════════════════════════════════════════
#  FUNCIONES DE UTILIDAD
# ═══════════════════════════════════════════════════════════════════════════

def get_qos_priority(rol: str) -> int:
    """
    Retorna la prioridad QoS para un rol dado.
    Si el rol no existe en la configuración, retorna 0 (normal).
    """
    config = ROLES_CONFIG.get(rol)
    if config:
        return config["qos_priority"]
    # Intentar búsqueda case-insensitive
    for key, value in ROLES_CONFIG.items():
        if key.lower() == rol.lower():
            return value["qos_priority"]
    logger.warning(f"[QoS] Rol desconocido '{rol}', asignando prioridad normal")
    return 0


def get_qos_profile(priority: int) -> dict:
    """Retorna el perfil QoS completo para un nivel de prioridad."""
    return QOS_PROFILES.get(priority, QOS_PROFILES[0])


def get_roles_list() -> list:
    """Retorna la lista de roles disponibles con su configuración."""
    return [
        {
            "nombre": key,
            "qos_priority": value["qos_priority"],
            "label": value["label"],
            "description": value["description"],
            "color": value["color"],
            "icon": value["icon"]
        }
        for key, value in ROLES_CONFIG.items()
    ]


def get_role_names() -> list:
    """Retorna solo los nombres de roles disponibles."""
    return list(ROLES_CONFIG.keys())


def get_qos_profiles_for_client() -> dict:
    """Retorna los perfiles QoS en formato para enviar al cliente."""
    return {
        str(k): {
            "maxBitrate": v["maxBitrate"],
            "maxFramerate": v["maxFramerate"],
            "priority": v["priority"],
            "networkPriority": v["networkPriority"],
            "scaleResolutionDownBy": v["scaleResolutionDownBy"],
            "label": v["label"]
        }
        for k, v in QOS_PROFILES.items()
    }
