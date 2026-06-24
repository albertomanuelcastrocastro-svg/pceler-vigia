"""
PCELER VIGÍA — Mensajero del Acelerómetro PALMERO
====================================================
Servicio que cada N minutos consulta a PCELER y envía las nuevas señales
detectadas a Telegram. Guarda log de señales enviadas en GitHub para
evitar duplicados y para que el bot principal las muestre en el panel.

Funcionamiento:
  1. Cada INTERVALO_SEGUNDOS pregunta a PCELER por señales en 15M
     (con filtro 4H, umbral 0.25) para XRP y SOL
  2. Compara con signals_log_pceler.json (en GitHub)
  3. Si hay señal NUEVA confirmada, envía a Telegram
  4. Actualiza signals_log_pceler.json en GitHub

Variables de entorno requeridas (Railway):
  - TELEGRAM_BOT_TOKEN: token de @PalmeroAgent_bot
  - TELEGRAM_CHAT_ID: 5448802464
  - GITHUB_TOKEN: PAT con permiso de contents:write sobre pceler-vigia
  - GITHUB_REPO: albertomanuelcastrocastro-svg/pceler-vigia
  - PCELER_URL: https://pceler-production.up.railway.app
  - INTERVALO_SEGUNDOS: 300 (5 minutos por defecto)
"""

import os
import time
import json
import base64
import requests
from datetime import datetime, timezone
from threading import Thread
from flask import Flask, jsonify

app = Flask(__name__)

# ============================================================
# CONFIGURACIÓN
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5448802464")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "albertomanuelcastrocastro-svg/pceler-vigia")
PCELER_URL = os.environ.get("PCELER_URL", "https://pceler-production.up.railway.app")
INTERVALO_SEGUNDOS = int(os.environ.get("INTERVALO_SEGUNDOS", "300"))

SIMBOLOS = ["XRPUSDT", "SOLUSDT"]
TF_OBJETIVO = "15m"
UMBRAL_OBJETIVO = "0.25"
LOG_FILENAME = "signals_log_pceler.json"

# Estado en memoria (cache del log)
_estado = {
    "ultima_consulta": None,
    "ultima_senal_enviada": None,
    "total_consultas": 0,
    "total_senales_enviadas": 0,
    "ultimos_errores": [],
}


# ============================================================
# UTILIDADES GITHUB
# ============================================================
def github_get_file(filename):
    """Lee un archivo del repo, devuelve (contenido_dict, sha) o (None, None)"""
    if not GITHUB_TOKEN:
        return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            sha = data.get("sha")
            content_b64 = data.get("content", "")
            content_str = base64.b64decode(content_b64).decode("utf-8")
            return json.loads(content_str), sha
        elif r.status_code == 404:
            return [], None
        else:
            return None, None
    except Exception as e:
        print(f"[github_get_file] Error: {e}")
        return None, None


def github_put_file(filename, content, sha=None, mensaje="actualizar log"):
    """Crea o actualiza un archivo del repo"""
    if not GITHUB_TOKEN:
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    content_b64 = base64.b64encode(json.dumps(content, indent=2).encode("utf-8")).decode("utf-8")
    body = {
        "message": mensaje,
        "content": content_b64,
    }
    if sha:
        body["sha"] = sha
    try:
        r = requests.put(url, headers=headers, json=body, timeout=15)
        return r.status_code in (200, 201)
    except Exception as e:
        print(f"[github_put_file] Error: {e}")
        return False


# ============================================================
# TELEGRAM
# ============================================================
def enviar_telegram(mensaje):
    """Envía un mensaje al chat de Telegram configurado"""
    if not TELEGRAM_BOT_TOKEN:
        print("[telegram] Sin token, no se envía")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=body, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[telegram] Error: {e}")
        return False


def formato_mensaje_telegram(senal):
    """Formato igual al de P15"""
    tipo = senal.get("tipo", "?")
    simbolo = senal.get("simbolo", "?")
    precio = senal.get("precio", "?")
    timestamp = senal.get("timestamp", "?")
    direccion_4h = senal.get("direccion_4h", "?")
    emoji = "🟢" if tipo == "LONG" else "🔴"
    return (
        f"{emoji} <b>PCELER {tipo}</b>\n"
        f"📊 <b>{simbolo}</b> @ {precio}\n"
        f"⏱ TF: 15M\n"
        f"🛡 Filtro 4H: {direccion_4h}\n"
        f"🕐 {timestamp}"
    )


# ============================================================
# LÓGICA DEL VIGÍA
# ============================================================
def obtener_senales_pceler(simbolo):
    """Consulta a PCELER las señales del umbral objetivo con filtro 4H"""
    url = f"{PCELER_URL}/senales/{simbolo}/{TF_OBJETIVO}"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
        umbrales = data.get("umbrales", {})
        bloque = umbrales.get(UMBRAL_OBJETIVO, {})
        filtro = bloque.get("filtro_4h", {})
        senales = filtro.get("senales", [])
        for s in senales:
            s["simbolo"] = simbolo
        return senales
    except Exception as e:
        print(f"[obtener_senales_pceler] Error: {e}")
        _estado["ultimos_errores"].append(f"{datetime.now(timezone.utc).isoformat()} - obtener {simbolo}: {e}")
        _estado["ultimos_errores"] = _estado["ultimos_errores"][-10:]
        return []


def es_senal_nueva(senal, log_existente):
    """Una señal es nueva si su timestamp + tipo + simbolo no está ya en el log"""
    clave = f"{senal.get('simbolo')}|{senal.get('tipo')}|{senal.get('timestamp')}"
    for entry in log_existente:
        clave_log = f"{entry.get('simbolo')}|{entry.get('tipo')}|{entry.get('timestamp')}"
        if clave == clave_log:
            return False
    return True


def ciclo_vigia():
    """Un ciclo completo: consulta, detecta, envía, actualiza log"""
    _estado["total_consultas"] += 1
    _estado["ultima_consulta"] = datetime.now(timezone.utc).isoformat()

    log_actual, sha = github_get_file(LOG_FILENAME)
    if log_actual is None:
        print("[ciclo] No se pudo leer log de GitHub. Saltando ciclo.")
        return

    senales_nuevas = []
    for simbolo in SIMBOLOS:
        senales = obtener_senales_pceler(simbolo)
        for s in senales:
            if es_senal_nueva(s, log_actual):
                senales_nuevas.append(s)

    if not senales_nuevas:
        print(f"[ciclo] Sin señales nuevas")
        return

    for s in senales_nuevas:
        mensaje = formato_mensaje_telegram(s)
        enviado = enviar_telegram(mensaje)
        if enviado:
            _estado["total_senales_enviadas"] += 1
            _estado["ultima_senal_enviada"] = datetime.now(timezone.utc).isoformat()
            s["enviado_telegram_utc"] = datetime.now(timezone.utc).isoformat()
            log_actual.append(s)
            print(f"[ciclo] Enviada: {s.get('simbolo')} {s.get('tipo')} {s.get('timestamp')}")

    ok = github_put_file(LOG_FILENAME, log_actual, sha=sha,
        mensaje=f"añadir {len(senales_nuevas)} señales nuevas")
    if not ok:
        print("[ciclo] ERROR actualizando log en GitHub")


def loop_vigia():
    """Hilo en bucle: ejecuta ciclo cada INTERVALO_SEGUNDOS"""
    print(f"[vigia] Iniciado. Intervalo: {INTERVALO_SEGUNDOS}s")
    while True:
        try:
            ciclo_vigia()
        except Exception as e:
            print(f"[vigia] Error en ciclo: {e}")
            _estado["ultimos_errores"].append(f"{datetime.now(timezone.utc).isoformat()} - ciclo: {e}")
            _estado["ultimos_errores"] = _estado["ultimos_errores"][-10:]
        time.sleep(INTERVALO_SEGUNDOS)


# ============================================================
# ENDPOINTS FLASK
# ============================================================
@app.route("/")
def home():
    return jsonify({
        "servicio": "PCELER VIGÍA",
        "version": "1.0",
        "descripcion": "Mensajero entre PCELER y Telegram",
        "configuracion": {
            "intervalo_segundos": INTERVALO_SEGUNDOS,
            "simbolos": SIMBOLOS,
            "tf_objetivo": TF_OBJETIVO,
            "umbral_objetivo": UMBRAL_OBJETIVO,
            "pceler_url": PCELER_URL,
            "github_repo": GITHUB_REPO,
            "log_filename": LOG_FILENAME,
            "telegram_configurado": bool(TELEGRAM_BOT_TOKEN),
            "github_configurado": bool(GITHUB_TOKEN),
        },
        "estado": _estado,
        "endpoints": [
            "/ — esta página",
            "/log — ver log de señales enviadas",
            "/ciclo — ejecutar un ciclo manualmente",
            "/test_telegram — enviar un mensaje de prueba a Telegram",
        ],
    })


@app.route("/log")
def ver_log():
    log_actual, _ = github_get_file(LOG_FILENAME)
    if log_actual is None:
        return jsonify({"error": "no se pudo leer el log"}), 500
    return jsonify({
        "n_senales_enviadas": len(log_actual),
        "senales": log_actual[-30:],
    })


@app.route("/ciclo")
def ejecutar_ciclo_manual():
    try:
        ciclo_vigia()
        return jsonify({"ok": True, "estado": _estado})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/test_telegram")
def test_telegram():
    mensaje = (
        f"🧪 <b>Test del PCELER VIGÍA</b>\n"
        f"Si ves esto, la conexión con Telegram funciona.\n"
        f"⏰ {datetime.now(timezone.utc).isoformat()}"
    )
    ok = enviar_telegram(mensaje)
    return jsonify({"telegram_ok": ok})


# ============================================================
# ARRANQUE: lanzar el hilo del vigía
# ============================================================
def arrancar_vigia():
    t = Thread(target=loop_vigia, daemon=True)
    t.start()

arrancar_vigia()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
