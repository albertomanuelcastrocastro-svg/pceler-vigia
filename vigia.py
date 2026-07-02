"""

PCELER VIGÍA — Mensajero del Acelerómetro PALMERO (v1.2)

====================================================

Servicio que cada N minutos consulta a PCELER y envía las nuevas señales

detectadas a Telegram.

v1.3: añade ping a PCELER en cada ciclo para evitar idle de Railway

v1.2: cambia a lógica SIMPLIFICADA (cambio de signo) en vez de percentiles.

  - Usa /senales_simple en vez de /senales (percentiles).

  - Las señales aparecen inmediatamente al cierre de vela (no con retraso).

  - Esto permite que el filtro de frescura funcione correctamente.

v1.1: corrige el problema de la inundación de mensajes en el primer arranque.

  - Al arrancar por primera vez (log vacío), marca TODAS las señales históricas

    como ya conocidas SIN enviarlas. El Vigía solo vigila desde su nacimiento.

  - Filtro temporal: solo se consideran "nuevas" las señales cuyo timestamp

    sea de los últimos N minutos (configurable). Las históricas se descartan.

  - Notificación de arranque en Telegram para confirmar inicio.

"""

import os

import time

import json

import base64

import requests

from datetime import datetime, timezone, timedelta

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

VENTANA_FRESCURA_MINUTOS = int(os.environ.get("VENTANA_FRESCURA_MINUTOS", "20"))

SIMBOLOS = ["XRPUSDT", "SOLUSDT"]

TF_OBJETIVO = "15m"

UMBRAL_OBJETIVO = "0.25"

LOG_FILENAME = "signals_log_pceler.json"

_estado = {

    "ultima_consulta": None,

    "ultima_senal_enviada": None,

    "total_consultas": 0,

    "total_senales_enviadas": 0,

    "primer_arranque": True,

    "ultimos_errores": [],

}

# ============================================================

# UTILIDADES GITHUB

# ============================================================

def github_get_file(filename):

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

    if not GITHUB_TOKEN:

        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"

    headers = {

        "Authorization": f"Bearer {GITHUB_TOKEN}",

        "Accept": "application/vnd.github+json",

    }

    content_b64 = base64.b64encode(json.dumps(content, indent=2).encode("utf-8")).decode("utf-8")

    body = {"message": mensaje, "content": content_b64}

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

    if not TELEGRAM_BOT_TOKEN:

        print("[telegram] Sin token, no se envía")

        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    body = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}

    try:

        r = requests.post(url, json=body, timeout=10)

        return r.status_code == 200

    except Exception as e:

        print(f"[telegram] Error: {e}")

        return False

def formato_mensaje_telegram(senal):

    tipo = senal.get("tipo", "?")

    simbolo = senal.get("simbolo", "?")

    precio = senal.get("precio", "?")

    timestamp = senal.get("timestamp", "?")

    direccion_4h = senal.get("direccion_4h", "?")

    emoji = "🟢" if tipo == "LONG" else "🔴"

    return (

        f"{emoji} <b>PCELER {tipo}</b> (simple)\n"

        f"📊 <b>{simbolo}</b> @ {precio}\n"

        f"⏱ TF: 15M\n"

        f"🛡 Filtro 4H: {direccion_4h}\n"

        f"🕐 {timestamp}"

    )

# ============================================================

# LÓGICA DEL VIGÍA

# ============================================================

def es_senal_fresca(senal, ventana_minutos=VENTANA_FRESCURA_MINUTOS):

    """Una señal es fresca si su timestamp es de los últimos N minutos"""

    ts_str = senal.get("timestamp")

    if not ts_str:

        return False

    try:

        ts_dt = datetime.fromisoformat(ts_str)

        ahora = datetime.now(timezone.utc)

        edad = ahora - ts_dt

        return edad <= timedelta(minutes=ventana_minutos)

    except Exception as e:

        print(f"[es_senal_fresca] Error parseando timestamp '{ts_str}': {e}")

        return False

def obtener_senales_pceler(simbolo):

    url = f"{PCELER_URL}/senales_simple/{simbolo}/{TF_OBJETIVO}"

    try:

        r = requests.get(url, timeout=20)

        if r.status_code != 200:

            return []

        data = r.json()

        filtro = data.get("filtro_4h", {})

        senales = filtro.get("senales", [])

        for s in senales:

            s["simbolo"] = simbolo

        return senales

    except Exception as e:

        print(f"[obtener_senales_pceler] Error: {e}")

        _estado["ultimos_errores"].append(f"{datetime.now(timezone.utc).isoformat()} - obtener {simbolo}: {e}")

        _estado["ultimos_errores"] = _estado["ultimos_errores"][-10:]

        return []

def es_senal_ya_registrada(senal, log_existente):

    clave = f"{senal.get('simbolo')}|{senal.get('tipo')}|{senal.get('timestamp')}"

    for entry in log_existente:

        clave_log = f"{entry.get('simbolo')}|{entry.get('tipo')}|{entry.get('timestamp')}"

        if clave == clave_log:

            return True

    return False

def primer_arranque_marcar_historico():

    print("[primer_arranque] Comprobando log existente...")

    log_actual, sha = github_get_file(LOG_FILENAME)

    if log_actual is None:

        print("[primer_arranque] No se pudo leer el log. Reintentando en próximo ciclo.")

        return False

    if len(log_actual) > 0:

        print(f"[primer_arranque] Log ya tiene {len(log_actual)} señales. No es primer arranque.")

        _estado["primer_arranque"] = False

        return True

    print("[primer_arranque] Log vacío. Marcando histórico como conocido...")

    historico = []

    for simbolo in SIMBOLOS:

        senales = obtener_senales_pceler(simbolo)

        for s in senales:

            entry = {

                "simbolo": s.get("simbolo"),

                "tipo": s.get("tipo"),

                "timestamp": s.get("timestamp"),

                "precio": s.get("precio"),

                "marcado_como_historico_utc": datetime.now(timezone.utc).isoformat(),

            }

            historico.append(entry)

    ok = github_put_file(LOG_FILENAME, historico, sha=sha,

        mensaje=f"primer arranque: marcadas {len(historico)} señales como histórico")

    if ok:

        print(f"[primer_arranque] OK. Marcadas {len(historico)} señales como histórico.")

        _estado["primer_arranque"] = False

        mensaje_arranque = (

            f"🟠 <b>PCELER VIGÍA iniciado</b>\n"

            f"⚙️ Monitorizando XRP y SOL en 15M\n"

            f"⏱ Intervalo: {INTERVALO_SEGUNDOS//60} min\n"

            f"📦 {len(historico)} señales históricas marcadas (no se envían)\n"

            f"🟢 A partir de ahora solo señales nuevas\n"

            f"🕐 {datetime.now(timezone.utc).isoformat()}"

        )

        enviar_telegram(mensaje_arranque)

        return True

    else:

        print("[primer_arranque] ERROR guardando histórico.")

        return False

def ping_pceler():
    """Mantiene PCELER despierto con un ping silencioso."""
    try:
        requests.get(f"{PCELER_URL}/paper", timeout=5)
        print("[ping] PCELER despertado")
    except:
        pass

def ciclo_vigia():

    _estado["total_consultas"] += 1

    ping_pceler()

    _estado["ultima_consulta"] = datetime.now(timezone.utc).isoformat()

    if _estado["primer_arranque"]:

        ok = primer_arranque_marcar_historico()

        if not ok:

            return

    log_actual, sha = github_get_file(LOG_FILENAME)

    if log_actual is None:

        print("[ciclo] No se pudo leer log. Saltando.")

        return

    senales_nuevas = []

    for simbolo in SIMBOLOS:

        senales = obtener_senales_pceler(simbolo)

        for s in senales:

            if not es_senal_fresca(s):

                continue

            if es_senal_ya_registrada(s, log_actual):

                continue

            senales_nuevas.append(s)

    if not senales_nuevas:

        print(f"[ciclo] Sin señales nuevas frescas")

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

    print(f"[vigia] Iniciado. Intervalo: {INTERVALO_SEGUNDOS}s")

    time.sleep(10)

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

        "version": "1.3",

        "descripcion": "Mensajero entre PCELER y Telegram (lógica simplificada)",

        "correccion_v12": "cambio a lógica simple (cambio de signo) para señales inmediatas",

        "configuracion": {

            "intervalo_segundos": INTERVALO_SEGUNDOS,

            "ventana_frescura_minutos": VENTANA_FRESCURA_MINUTOS,

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

            "/log — ver log de señales",

            "/ciclo — ejecutar un ciclo manualmente",

            "/test_telegram — enviar mensaje de prueba",

            "/reset_historico — borra el log y marca histórico actual (uso solo manual)",

        ],

    })

@app.route("/log")

def ver_log():

    log_actual, _ = github_get_file(LOG_FILENAME)

    if log_actual is None:

        return jsonify({"error": "no se pudo leer el log"}), 500

    return jsonify({

        "n_entradas": len(log_actual),

        "ultimas_30": log_actual[-30:],

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

@app.route("/reset_historico")

def reset_historico():

    _, sha = github_get_file(LOG_FILENAME)

    historico = []

    for simbolo in SIMBOLOS:

        senales = obtener_senales_pceler(simbolo)

        for s in senales:

            entry = {

                "simbolo": s.get("simbolo"),

                "tipo": s.get("tipo"),

                "timestamp": s.get("timestamp"),

                "precio": s.get("precio"),

                "marcado_como_historico_utc": datetime.now(timezone.utc).isoformat(),

            }

            historico.append(entry)

    ok = github_put_file(LOG_FILENAME, historico, sha=sha,

        mensaje=f"RESET: marcadas {len(historico)} señales como histórico")

    return jsonify({"ok": ok, "n_marcadas": len(historico)})

# ============================================================

# ARRANQUE

# ============================================================

def arrancar_vigia():

    t = Thread(target=loop_vigia, daemon=True)

    t.start()

arrancar_vigia()

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 8080))

    app.run(host="0.0.0.0", port=port)
