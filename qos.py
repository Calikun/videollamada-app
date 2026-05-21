"""
Módulo de configuración de QoS (Quality of Service) por rol empresarial.
Define los roles corporativos predefinidos y sus niveles de prioridad
en la conexión WebRTC.
"""

import logging

logger = logging.getLogger("videollamada.qos")

# ═══════════════════════════════════════════════════════════════════════════
#  ROLES CORPORATIVOS CON PRIORIDAD QoS
# ═══════════════════════════════════════════════════════════════════════════

# Prioridad: 2 = Alta, 1 = Media, 0 = Normal
ROLES_CONFIG = {
    "Gerente": {
        "qos_priority": 2,
        "label": "Gerente",
        "description": "Máxima prioridad — Dirección general de la empresa",
        "color": "#ef4444",
        "icon": "👔"
    },
    "Director de Proyectos": {
        "qos_priority": 2,
        "label": "Director de Proyectos",
        "description": "Máxima prioridad — Lidera y supervisa proyectos estratégicos",
        "color": "#f97316",
        "icon": "📊"
    },
    "Jefe de Área": {
        "qos_priority": 2,
        "label": "Jefe de Área",
        "description": "Máxima prioridad — Responsable de un departamento completo",
        "color": "#a855f7",
        "icon": "🏢"
    },
    "Coordinador": {
        "qos_priority": 1,
        "label": "Coordinador",
        "description": "Prioridad media — Coordina equipos y actividades operativas",
        "color": "#eab308",
        "icon": "📋"
    },
    "Analista": {
        "qos_priority": 0,
        "label": "Analista",
        "description": "Prioridad estándar — Análisis de datos e información",
        "color": "#3b82f6",
        "icon": "📈"
    },
    "Empleado": {
        "qos_priority": 0,
        "label": "Empleado",
        "description": "Prioridad estándar — Personal operativo general",
        "color": "#22c55e",
        "icon": "💼"
    },
    "Pasante": {
        "qos_priority": 0,
        "label": "Pasante",
        "description": "Prioridad estándar — En formación o práctica profesional",
        "color": "#06b6d4",
        "icon": "🎓"
    },
    "Invitado": {
        "qos_priority": 0,
        "label": "Invitado",
        "description": "Prioridad estándar — Acceso temporal sin cuenta",
        "color": "#6b7280",
        "icon": "👤"
    }
}

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN DE BITRATE Y FRAMERATE POR PRIORIDAD
# ═══════════════════════════════════════════════════════════════════════════

QOS_PROFILES = {
    2: {  # Alta prioridad — Gerente, Director de Proyectos, Jefe de Área
        "maxBitrate": 2500000,      # 2.5 Mbps
        "maxFramerate": 30,
        "priority": "high",
        "networkPriority": "high",
        "scaleResolutionDownBy": 1,  # Sin reducción
        "label": "Alta",
        "description": "Video HD completo, sin restricciones de red"
    },
    1: {  # Prioridad media — Coordinador
        "maxBitrate": 1500000,      # 1.5 Mbps
        "maxFramerate": 25,
        "priority": "medium",
        "networkPriority": "medium",
        "scaleResolutionDownBy": 1,
        "label": "Media",
        "description": "Video de buena calidad con ligera optimización"
    },
    0: {  # Prioridad normal — Analista, Empleado, Pasante, Invitado
        "maxBitrate": 800000,       # 800 kbps
        "maxFramerate": 20,
        "priority": "low",
        "networkPriority": "low",
        "scaleResolutionDownBy": 1.5,
        "label": "Normal",
        "description": "Video estándar optimizado para ancho de banda"
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
