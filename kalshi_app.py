# -*- coding: utf-8 -*-
"""
=============================================================
  KALSHI WEATHER SCANNER - App Web Local
=============================================================
USO:   python -X utf8 kalshi_app.py
ABRE:  http://localhost:5000
=============================================================
MEJORAS v2:
  - Win rate historico por ciudad+strike (calculado desde CSVs)
  - Senal BUY GANADORA cuando win rate >= 55% + edge >= umbral
  - Columna "Resultado" para marcar manualmente si acerto
  - Tabla de rendimiento historico por combinacion
  - Nueva pestana "Rendimiento" con estadisticas de acierto
MEJORAS v3:
  - Filtro de liquidez: ignora mercados < 15% o > 85%
    (spreads demasiado amplios, edge irreal)
  - Email solo cuando hay BUY GANADORA (no spam con BUYs normales)
=============================================================
"""

from flask import Flask, jsonify, render_template_string, request
import csv, math, base64, time, requests, smtplib, threading
from datetime import datetime
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict

app    = Flask(__name__)
FOLDER = Path(__file__).parent

# ── CONFIGURACION SCANNER ────────────────────────────────────
CONFIG = {
    "kalshi_key_id":          "4127d834-8e23-41eb-83a8-ef6b9b177d49",
    "kalshi_key_file":        "kalshi_private_key.pem",
    "kalshi_api_url":         "https://external-api.kalshi.com/trade-api/v2",
    "min_edge_buy":           5.0,
    "min_edge_watch":         2.0,
    "forecast_uncertainty_f": 5.0,  # fallback si ciudad no esta en la lista
    "forecast_days_ahead":    1,
    "uncertainty_por_ciudad": {
        # Ciudades originales
        "Miami":        3.0,
        "Dallas":       4.5,
        "Los Angeles":  3.5,
        "Phoenix":      3.0,
        "Chicago":      6.0,
        "New York":     5.5,
        "Seattle":      6.5,
        "Houston":      4.0,
        "Atlanta":      4.5,
        "Denver":       6.0,
        "Boston":       5.5,
        "Las Vegas":    3.0,
        # Ciudades nuevas
        "San Antonio":  4.0,
        "Minneapolis":  6.5,
        "Orlando":      3.5,
    },
    "win_rate_minimo":        55.0,   # % minimo para ser BUY GANADORA
    "muestras_minimas":       3,      # minimo de resultados resueltos para calcular win rate
    "liquidez_min":           15.0,   # precio minimo del mercado — por debajo el spread es enorme
    "liquidez_max":           85.0,   # precio maximo del mercado — por encima idem
    "cities": [
        {"name": "Miami",        "lat": 25.77,  "lon": -80.19,  "tz": "America/New_York"},
        {"name": "Los Angeles",  "lat": 34.05,  "lon": -118.24, "tz": "America/Los_Angeles"},
        {"name": "New York",     "lat": 40.71,  "lon": -74.01,  "tz": "America/New_York"},
        {"name": "Chicago",      "lat": 41.88,  "lon": -87.63,  "tz": "America/Chicago"},
        {"name": "Dallas",       "lat": 32.78,  "lon": -96.80,  "tz": "America/Chicago"},
        {"name": "Seattle",      "lat": 47.61,  "lon": -122.33, "tz": "America/Los_Angeles"},
        # ── Tier 1: maxima oportunidad ──
        {"name": "Phoenix",      "lat": 33.45,  "lon": -112.07, "tz": "America/Phoenix"},
        {"name": "Las Vegas",    "lat": 36.17,  "lon": -115.14, "tz": "America/Los_Angeles"},
        {"name": "Houston",      "lat": 29.76,  "lon": -95.37,  "tz": "America/Chicago"},
        {"name": "San Antonio",  "lat": 29.42,  "lon": -98.49,  "tz": "America/Chicago"},
        # ── Tier 2: buena oportunidad ──
        {"name": "Atlanta",      "lat": 33.75,  "lon": -84.39,  "tz": "America/New_York"},
        {"name": "Denver",       "lat": 39.74,  "lon": -104.98, "tz": "America/Denver"},
        {"name": "Minneapolis",  "lat": 44.98,  "lon": -93.27,  "tz": "America/Chicago"},
        {"name": "Orlando",      "lat": 28.54,  "lon": -81.38,  "tz": "America/New_York"},
    ],
    "strikes_f": [50, 60, 70, 75, 80, 90, 95],
}

# ── CONFIGURACION EMAIL ──────────────────────────────────────
EMAIL = {
    "activado":     True,
    "remitente":    "edmonar2002@gmail.com",
    "contrasena":   "qpzh xrfp ooru uxsf",
    "destinatario": "edmonar2002@gmail.com",
}

# ── AUTENTICACION RSA ────────────────────────────────────────
def cargar_clave():
    try:
        from cryptography.hazmat.primitives import serialization
        with open(FOLDER / CONFIG["kalshi_key_file"], "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    except Exception as e:
        print(f"  Clave no cargada: {e}")
        return None

def firmar(key_id, clave, metodo, ruta):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ts  = str(int(time.time() * 1000))
    sig = clave.sign(
        f"{ts}{metodo}{ruta}".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type":            "application/json",
    }

# ── PRONOSTICO ───────────────────────────────────────────────
def obtener_pronostico(ciudad, dias):
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=10, params={
            "latitude": ciudad["lat"], "longitude": ciudad["lon"],
            "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
            "forecast_days": dias + 1, "timezone": ciudad["tz"],
        })
        r.raise_for_status()
        d  = r.json()
        tf = d["daily"]["temperature_2m_max"][dias]
        return {"fecha": d["daily"]["time"][dias], "forecast_f": round(tf, 1),
                "forecast_c": round((tf - 32) * 5 / 9, 1), "ok": True}
    except Exception as e:
        print(f"  Error pronostico {ciudad['name']}: {e}")
        return {"ok": False}

# ── MODELO ESTADISTICO ───────────────────────────────────────
def prob_superar(fc_f, strike, sigma):
    z = (strike - fc_f) / (sigma * math.sqrt(2))
    return round(0.5 * math.erfc(z) * 100, 2)

# ── PRECIO KALSHI ────────────────────────────────────────────
def precio_kalshi(ciudad, strike, fecha, clave):
    if clave is None:
        return None
    prefijos = {
        "Miami":       "HIGHMIA",
        "Los Angeles": "HIGHLA",
        "New York":    "HIGHNYC",
        "Chicago":     "HIGHCHI",
        "Dallas":      "HIGHDAL",
        "Seattle":     "HIGHSEA",
        "Phoenix":     "HIGHPHX",
        "Las Vegas":   "HIGHLAS",
        "Houston":     "HIGHHOU",
        "San Antonio": "HIGHSAT",
        "Atlanta":     "HIGHATL",
        "Denver":      "HIGHDEN",
        "Minneapolis": "HIGHMSP",
        "Orlando":     "HIGHORL",
    }
    prefijo = prefijos.get(ciudad, "")
    if not prefijo:
        return None
    ruta = "/trade-api/v2/markets"
    try:
        headers = firmar(CONFIG["kalshi_key_id"], clave, "GET", ruta)
        r = requests.get(f"{CONFIG['kalshi_api_url']}/markets", headers=headers,
                         params={"series_ticker": prefijo, "status": "open"}, timeout=10)
        r.raise_for_status()
        for m in r.json().get("markets", []):
            if str(strike) in m.get("title", "") and fecha in m.get("close_time", ""):
                p = m.get("last_price") or m.get("yes_ask")
                if p is not None:
                    return round(float(p), 2)
    except Exception as e:
        print(f"  Error Kalshi {ciudad} {strike}F: {e}")
    return None

# ── WIN RATE HISTORICO ───────────────────────────────────────
def calcular_win_rates():
    """
    Lee todos los CSVs con columna 'resultado' (WIN/LOSS/PENDING)
    y calcula win rate por ciudad+strike.
    Devuelve dict: {("Dallas", 80): {"wins":3,"total":5,"win_rate":60.0}}
    """
    stats = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
    archivos = sorted(FOLDER.glob("scanner_*.csv"), reverse=True)

    for archivo in archivos:
        try:
            with open(archivo, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for fila in reader:
                    resultado = fila.get("resultado", "PENDING").strip().upper()
                    if resultado not in ("WIN", "LOSS"):
                        continue
                    ciudad = fila.get("ciudad", "").strip()
                    try:
                        strike = int(float(fila.get("strike", 0)))
                    except:
                        continue
                    key = (ciudad, strike)
                    stats[key]["total"] += 1
                    if resultado == "WIN":
                        stats[key]["wins"] += 1
                    else:
                        stats[key]["losses"] += 1
        except:
            pass

    resultado = {}
    for key, s in stats.items():
        if s["total"] >= CONFIG["muestras_minimas"]:
            resultado[key] = {
                "wins":     s["wins"],
                "losses":   s["losses"],
                "total":    s["total"],
                "win_rate": round(s["wins"] / s["total"] * 100, 1),
            }
    return resultado


def clasificar(edge, ciudad, strike, win_rates):
    """
    Logica de senal mejorada:
    - BUY GANADORA: edge >= umbral + win rate historico >= 55% (con suficientes muestras)
    - BUY:          edge >= umbral (sin historial suficiente o win rate ok)
    - WATCH:        edge >= umbral_watch
    - NO BET:       el resto
    """
    if edge >= CONFIG["min_edge_buy"]:
        wr = win_rates.get((ciudad, strike))
        if wr and wr["win_rate"] >= CONFIG["win_rate_minimo"]:
            return "BUY GANADORA"
        return "BUY"
    if edge >= CONFIG["min_edge_watch"]:
        return "WATCH"
    return "NO BET"


# ── EMAIL ALERTA ─────────────────────────────────────────────
KALSHI_PREFIJOS_URL = {
    "Miami":       "HIGHMIA",
    "Los Angeles": "HIGHLA",
    "New York":    "HIGHNYC",
    "Chicago":     "HIGHCHI",
    "Dallas":      "HIGHDAL",
    "Seattle":     "HIGHSEA",
    "Phoenix":     "HIGHPHX",
    "Las Vegas":   "HIGHLAS",
    "Houston":     "HIGHHOU",
    "San Antonio": "HIGHSAT",
    "Atlanta":     "HIGHATL",
    "Denver":      "HIGHDEN",
    "Minneapolis": "HIGHMSP",
    "Orlando":     "HIGHORL",
}

def calcular_apuesta(edge, mercado_pct, win_rate):
    """
    Calcula el tamaño de apuesta recomendado usando Kelly simplificado.
    Nunca mas del 5% del bankroll por apuesta.
    """
    p = (win_rate / 100) if win_rate else (mercado_pct + edge) / 100
    q = 1 - p
    b = (100 - mercado_pct) / mercado_pct  # ratio de beneficio
    kelly = (p * b - q) / b
    kelly_conservador = max(0, min(kelly * 0.25, 0.05))  # 25% Kelly, max 5%
    return round(kelly_conservador * 100, 1)  # como % del bankroll

def url_kalshi(ciudad, strike, fecha):
    prefijo = KALSHI_PREFIJOS_URL.get(ciudad, "")
    if not prefijo:
        return "https://kalshi.com/markets/climate"
    # Formato ticker Kalshi: HIGHDAL-26MAY13-T90
    try:
        dt = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_str = dt.strftime("%y%b%d").upper()
        return f"https://kalshi.com/markets/{prefijo}/{prefijo}-{fecha_str}-T{strike}"
    except:
        return f"https://kalshi.com/markets/{prefijo}"

def enviar_email(buys, fecha):
    if not EMAIL["activado"] or not buys:
        return
    ganadoras = [r for r in buys if r["senal"] == "BUY GANADORA"]

    # Hora local España (CET/CEST)
    hora_espana = datetime.utcnow()
    hora_txt = hora_espana.strftime("%H:%M UTC")

    tarjetas = ""
    for r in buys:
        es_ganadora = r["senal"] == "BUY GANADORA"
        color_borde = "#f59e0b" if es_ganadora else "#22c55e"
        wr_txt      = f"{r['win_rate']:.0f}%" if r.get("win_rate") is not None else "sin historial"
        apuesta_pct = calcular_apuesta(r["edge_pct"], r["mercado_pct"], r.get("win_rate"))
        url         = url_kalshi(r["ciudad"], r["strike"], r["fecha"])
        precio_max  = round(r["mercado_pct"] + 1.5, 1)  # no pagar mas de mercado + 1.5%

        estrella = "⭐ BUY GANADORA" if es_ganadora else "BUY"

        tarjetas += f"""
        <div style="background:#161b27;border:1.5px solid {color_borde};border-radius:10px;padding:18px 20px;margin-bottom:14px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <div>
              <span style="font-size:18px;font-weight:700;color:#fff">{r['ciudad']}</span>
              <span style="font-size:13px;color:#94a3b8;margin-left:8px">High Temp Above {r['strike']}°F</span>
            </div>
            <span style="background:{color_borde};color:#000;font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px">{estrella}</span>
          </div>

          <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:14px">
            <tr>
              <td style="color:#64748b;padding:4px 0">Forecast hoy</td>
              <td style="color:#fff;text-align:right;font-weight:600">{r['forecast_f']}°F</td>
            </tr>
            <tr>
              <td style="color:#64748b;padding:4px 0">Precio mercado</td>
              <td style="color:#fff;text-align:right">{r['mercado_pct']}%</td>
            </tr>
            <tr>
              <td style="color:#64748b;padding:4px 0">Probabilidad modelo</td>
              <td style="color:#22c55e;text-align:right;font-weight:600">{r['modelo_pct']}%</td>
            </tr>
            <tr>
              <td style="color:#64748b;padding:4px 0">Edge (ventaja)</td>
              <td style="color:#22c55e;text-align:right;font-weight:700">+{r['edge_pct']}%</td>
            </tr>
            <tr>
              <td style="color:#64748b;padding:4px 0">Win rate historico</td>
              <td style="color:#f59e0b;text-align:right">{wr_txt}</td>
            </tr>
          </table>

          <div style="background:#0f1117;border-radius:8px;padding:14px;margin-bottom:12px">
            <div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Instrucciones de apuesta</div>
            <div style="font-size:13px;color:#e0e0e0;line-height:1.8">
              <b style="color:#22c55e">1. Comprar: YES</b> (apostamos a que la temp supera {r['strike']}°F)<br>
              <b style="color:#22c55e">2. Precio maximo a pagar: {precio_max}¢</b> (no pagar mas)<br>
              <b style="color:#22c55e">3. Tamano sugerido: {apuesta_pct}% de tu bankroll</b><br>
              <b style="color:#94a3b8">4. Fecha cierre: {r['fecha']}</b>
            </div>
          </div>

          <a href="{url}" style="display:block;text-align:center;background:#3b82f6;color:#fff;text-decoration:none;padding:10px;border-radius:8px;font-weight:600;font-size:14px">
            Ir al mercado en Kalshi →
          </a>
        </div>"""

    resumen_ganadoras = ""
    if ganadoras:
        resumen_ganadoras = f"""
        <div style="background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);border-radius:8px;padding:12px 16px;margin-bottom:20px">
          <span style="color:#f59e0b;font-weight:700">⭐ {len(ganadoras)} señal(es) GANADORAS con historial confirmado — prioridad maxima</span>
        </div>"""

    html = f"""
    <div style="font-family:Arial,sans-serif;background:#0f1117;color:#e0e0e0;padding:28px;max-width:600px;margin:0 auto;border-radius:12px">
      <h2 style="color:#3b82f6;margin:0 0 4px">Kalshi Weather Scanner</h2>
      <p style="color:#64748b;font-size:12px;margin:0 0 16px">{hora_txt} · Fecha mercado: {fecha}</p>
      <p style="color:#94a3b8;margin:0 0 16px">
        Se encontraron <strong style="color:#22c55e">{len(buys)} señal(es) BUY</strong>
      </p>
      {resumen_ganadoras}
      {tarjetas}
      <p style="color:#475569;font-size:11px;margin:20px 0 0;line-height:1.6">
        El tamaño de apuesta es orientativo (Kelly 25%). Nunca arriesgues mas de lo que puedas permitirte perder.<br>
        Generado automaticamente por Kalshi Weather Scanner v3
      </p>
    </div>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"⚡ Kalshi Scanner — {len(buys)} BUY {'⭐' * len(ganadoras)} ({fecha})"
        msg["From"]    = EMAIL["remitente"]
        msg["To"]      = EMAIL["destinatario"]
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(EMAIL["remitente"], EMAIL["contrasena"])
            s.sendmail(EMAIL["remitente"], EMAIL["destinatario"], msg.as_string())
        print(f"  Email enviado ({len(buys)} BUY, {len(ganadoras)} GANADORAS)")
    except Exception as e:
        print(f"  Error email: {e}")

# ── SCANNER ──────────────────────────────────────────────────
def ejecutar_scan():
    clave      = cargar_clave()
    modo       = "REAL" if clave else "SIMULACION"
    win_rates  = calcular_win_rates()
    print(f"\n  Escaneando... modo {modo} | Win rates calculados: {len(win_rates)} combinaciones")
    resultados, row_id = [], 1

    for ciudad in CONFIG["cities"]:
        p = obtener_pronostico(ciudad, CONFIG["forecast_days_ahead"])
        if not p["ok"]:
            continue
        for strike in CONFIG["strikes_f"]:
            sigma  = CONFIG["uncertainty_por_ciudad"].get(
                        ciudad["name"], CONFIG["forecast_uncertainty_f"])
            modelo = prob_superar(p["forecast_f"], strike, sigma)
            mercado  = precio_kalshi(ciudad["name"], strike, p["fecha"], clave)
            simulado = mercado is None
            if simulado:
                ruido   = (hash(f"{ciudad['name']}{strike}") % 1000) / 100 - 5
                mercado = max(1, min(99, modelo + ruido))

            edge  = round(modelo - mercado, 2)

            # ── FILTRO DE LIQUIDEZ ──────────────────────────────
            # Mercados en extremos tienen spreads enormes — el edge es irreal
            fuera_de_rango = (mercado < CONFIG["liquidez_min"] or
                              mercado > CONFIG["liquidez_max"])
            if fuera_de_rango:
                senal = "ILIQUIDO"
            else:
                senal = clasificar(edge, ciudad["name"], strike, win_rates)

            # Win rate para mostrar en la tabla
            wr_data  = win_rates.get((ciudad["name"], strike))
            win_rate = wr_data["win_rate"] if wr_data else None
            wr_total = wr_data["total"]    if wr_data else 0

            resultados.append({
                "id":          row_id,
                "ciudad":      ciudad["name"],
                "mercado":     f"{ciudad['name']} High Temp Above {strike}F",
                "fecha":       p["fecha"],
                "forecast_c":  p["forecast_c"],
                "forecast_f":  p["forecast_f"],
                "strike":      strike,
                "mercado_pct": round(mercado, 2),
                "modelo_pct":  modelo,
                "edge_pct":    edge,
                "senal":       senal,
                "simulado":    simulado,
                "win_rate":    win_rate,
                "wr_total":    wr_total,
                "resultado":   "PENDING",   # se marca manualmente desde la UI
            })
            row_id += 1

    # Guardar CSV con columna resultado (para poder marcar WIN/LOSS despues)
    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = FOLDER / f"scanner_{ts}.csv"
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("id,ciudad,mercado,fecha,forecast_c,forecast_f,strike,"
                "mercado_pct,modelo_pct,edge_pct,senal,win_rate,wr_total,resultado\n")
        for r in resultados:
            wr_str = f"{r['win_rate']:.1f}" if r["win_rate"] is not None else ""
            f.write(f"{r['id']},{r['ciudad']},{r['mercado']},{r['fecha']},"
                    f"{r['forecast_c']},{r['forecast_f']},{r['strike']},"
                    f"{r['mercado_pct']},{r['modelo_pct']},{r['edge_pct']},"
                    f"{r['senal']},{wr_str},{r['wr_total']},{r['resultado']}\n")

    # Email solo si hay BUY GANADORAS — no molestar con BUYs normales
    ganadoras = [r for r in resultados if r["senal"] == "BUY GANADORA"]
    fecha = resultados[0]["fecha"] if resultados else datetime.now().strftime("%Y-%m-%d")
    enviar_email(ganadoras, fecha)

    return resultados, modo, csv_path.name, win_rates


def leer_historico():
    archivos  = sorted(FOLDER.glob("scanner_*.csv"), reverse=True)
    historial = []
    for archivo in archivos[:30]:
        try:
            with open(archivo, encoding="utf-8") as f:
                filas = list(csv.DictReader(f))
            buys      = [r for r in filas if "BUY" in r.get("senal", "")]
            ganadoras = [r for r in filas if r.get("senal") == "BUY GANADORA"]
            watchs    = [r for r in filas if r.get("senal") == "WATCH"]
            ts        = archivo.stem.replace("scanner_", "")
            fecha     = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}"
            historial.append({
                "archivo":    archivo.name,
                "fecha":      fecha,
                "total":      len(filas),
                "buys":       len(buys),
                "ganadoras":  len(ganadoras),
                "watchs":     len(watchs),
                "filas":      filas,
            })
        except:
            pass
    return historial


def leer_rendimiento():
    """Devuelve estadisticas de win rate por ciudad+strike para la pestana Rendimiento."""
    win_rates = calcular_win_rates()
    resultado = []
    for (ciudad, strike), wr in sorted(win_rates.items(),
                                        key=lambda x: x[1]["win_rate"], reverse=True):
        resultado.append({
            "ciudad":    ciudad,
            "strike":    strike,
            "wins":      wr["wins"],
            "losses":    wr["losses"],
            "total":     wr["total"],
            "win_rate":  wr["win_rate"],
            "ganadora":  wr["win_rate"] >= CONFIG["win_rate_minimo"],
        })
    return resultado


def verificar_resultados_historicos(archivo_nombre: str) -> dict:
    """
    Consulta Open-Meteo Historical para obtener la temperatura maxima real
    de la fecha del CSV y marca automáticamente WIN/LOSS en cada fila.
    Solo marca filas con senal BUY, BUY GANADORA o WATCH que estén PENDING.
    """
    archivo = FOLDER / archivo_nombre
    try:
        with open(archivo, encoding="utf-8") as f:
            filas = list(csv.DictReader(f))
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if not filas:
        return {"ok": False, "error": "CSV vacío"}

    # Obtener fecha del CSV
    fecha = filas[0].get("fecha", "")
    if not fecha:
        return {"ok": False, "error": "Sin fecha en el CSV"}

    # Obtener temperaturas reales por ciudad (una llamada por ciudad única)
    ciudades_unicas = list({f["ciudad"] for f in filas if f.get("ciudad")})
    temps_reales = {}

    for ciudad in ciudades_unicas:
        # Buscar coordenadas en CONFIG
        city_cfg = next((c for c in CONFIG["cities"] if c["name"] == ciudad), None)
        if not city_cfg:
            continue
        try:
            url = (
                f"https://archive-api.open-meteo.com/v1/archive"
                f"?latitude={city_cfg['lat']}&longitude={city_cfg['lon']}"
                f"&start_date={fecha}&end_date={fecha}"
                f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
                f"&timezone={city_cfg['tz']}"
            )
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
            temp_real = data["daily"]["temperature_2m_max"][0]
            temps_reales[ciudad] = round(temp_real, 1)
            print(f"  [VERIFICAR] {ciudad} {fecha}: {temp_real:.1f}°F real")
        except Exception as e:
            print(f"  [VERIFICAR] Error {ciudad}: {e}")

    if not temps_reales:
        return {"ok": False, "error": "No se pudieron obtener temperaturas históricas"}

    # Marcar WIN/LOSS en cada fila
    marcadas = 0
    for fila in filas:
        ciudad  = fila.get("ciudad", "")
        senal   = fila.get("senal", "")
        pending = fila.get("resultado", "PENDING") == "PENDING"

        # Solo marcar BUY/WATCH pendientes
        if not pending or senal not in ("BUY", "BUY GANADORA", "WATCH"):
            continue

        temp_real = temps_reales.get(ciudad)
        if temp_real is None:
            continue

        try:
            strike = int(float(fila.get("strike", 0)))
        except:
            continue

        fila["resultado"] = "WIN" if temp_real > strike else "LOSS"
        marcadas += 1

    # Reescribir CSV
    try:
        with open(archivo, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
            writer.writeheader()
            writer.writerows(filas)
    except Exception as e:
        return {"ok": False, "error": f"Error guardando: {e}"}

    return {
        "ok":           True,
        "marcadas":     marcadas,
        "temps_reales": temps_reales,
        "fecha":        fecha,
    }


# ── RUTAS API ────────────────────────────────────────────────
@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        resultados, modo, csv_name, win_rates = ejecutar_scan()
        wr_json = {f"{k[0]}|{k[1]}": v for k, v in win_rates.items()}
        return jsonify({
            "ok": True, "modo": modo, "csv": csv_name,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "resultados": resultados,
            "win_rates":  wr_json,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/historico")
def api_historico():
    return jsonify(leer_historico())

@app.route("/api/rendimiento")
def api_rendimiento():
    return jsonify(leer_rendimiento())

@app.route("/api/verificar_resultados", methods=["POST"])
def api_verificar_resultados():
    """
    Consulta Open-Meteo Historical y marca WIN/LOSS automáticamente.
    Body: { archivo }
    """
    try:
        data     = request.json
        archivo  = data.get("archivo", "")
        resultado = verificar_resultados_historicos(archivo)
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/marcar_resultado", methods=["POST"])
def api_marcar_resultado():
    """
    Marca una fila de un CSV como WIN o LOSS para alimentar el historial.
    Body: { archivo, ciudad, strike, resultado }
    """
    try:
        data      = request.json
        archivo   = FOLDER / data["archivo"]
        ciudad    = data["ciudad"]
        strike    = int(data["strike"])
        resultado = data["resultado"].upper()  # WIN o LOSS

        if resultado not in ("WIN", "LOSS", "PENDING"):
            return jsonify({"ok": False, "error": "resultado debe ser WIN, LOSS o PENDING"}), 400

        # Leer, modificar y reescribir el CSV
        with open(archivo, encoding="utf-8") as f:
            filas = list(csv.DictReader(f))

        campos = filas[0].keys() if filas else []
        for fila in filas:
            if fila.get("ciudad") == ciudad and int(float(fila.get("strike", 0))) == strike:
                fila["resultado"] = resultado

        with open(archivo, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(filas[0].keys()) if filas else campos)
            writer.writeheader()
            writer.writerows(filas)

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── INTERFAZ WEB ─────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi Weather Scanner</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e0e0e0;min-height:100vh}
  .header{background:#161b27;border-bottom:1px solid #2a2f3e;padding:16px 28px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
  .header h1{font-size:17px;font-weight:600;color:#fff;display:flex;align-items:center;gap:10px}
  .dot{width:8px;height:8px;border-radius:50%;background:#22c55e;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .badge{font-size:11px;padding:3px 10px;border-radius:20px;font-weight:500}
  .badge-real{background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.3)}
  .badge-sim{background:rgba(251,191,36,.15);color:#fbbf24;border:1px solid rgba(251,191,36,.3)}
  .tabs{display:flex;gap:2px;padding:0 28px;background:#161b27;border-bottom:1px solid #2a2f3e}
  .tab{padding:12px 20px;font-size:13px;cursor:pointer;color:#8892a4;border-bottom:2px solid transparent;transition:all .15s}
  .tab.active{color:#fff;border-bottom-color:#3b82f6}
  .content{padding:24px 28px;max-width:1200px;margin:0 auto}
  .stats{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:24px}
  .stat{background:#161b27;border:1px solid #2a2f3e;border-radius:10px;padding:16px 20px}
  .stat-label{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
  .stat-value{font-size:26px;font-weight:600;color:#fff}
  .stat-value.gold{color:#f59e0b}
  .hdr-right{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .ts{font-size:12px;color:#64748b}
  .scan-btn{display:flex;align-items:center;gap:8px;background:#3b82f6;color:#fff;border:none;padding:10px 22px;border-radius:8px;font-size:14px;font-weight:500;cursor:pointer;transition:background .15s}
  .scan-btn:hover{background:#2563eb}
  .scan-btn:disabled{background:#2a2f3e;color:#64748b;cursor:not-allowed}
  .spin{display:inline-block;animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .table-wrap{background:#161b27;border:1px solid #2a2f3e;border-radius:10px;overflow:hidden;overflow-x:auto}
  table{width:100%;border-collapse:collapse;font-size:13px}
  thead th{background:#1e2535;padding:10px 14px;text-align:left;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.05em;font-weight:500;white-space:nowrap}
  thead th.r{text-align:right}thead th.c{text-align:center}
  tbody tr{border-top:1px solid #1e2535;transition:background .1s}
  tbody tr:hover{background:#1a2030}
  tbody tr.ganadora{background:rgba(245,158,11,.05)}
  tbody tr.ganadora:hover{background:rgba(245,158,11,.1)}
  td{padding:10px 14px;white-space:nowrap}
  td.r{text-align:right;font-variant-numeric:tabular-nums}
  td.c{text-align:center}
  .sig{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}
  .sig-ganadora{background:rgba(245,158,11,.2);color:#f59e0b;border:1px solid rgba(245,158,11,.4)}
  .sig-buy{background:rgba(34,197,94,.15);color:#22c55e}
  .sig-watch{background:rgba(251,191,36,.15);color:#fbbf24}
  .sig-nobet{background:rgba(100,116,139,.15);color:#64748b}
  .sig-iliquido{background:rgba(51,65,85,.3);color:#334155;font-style:italic}
  .ep{color:#22c55e;font-weight:600}
  .en{color:#ef4444;font-weight:600}
  .bar-wrap{display:flex;align-items:center;gap:8px;justify-content:flex-end}
  .bar{width:50px;height:4px;border-radius:2px;background:#2a2f3e;overflow:hidden;flex-shrink:0}
  .bar-fill{height:100%;border-radius:2px}
  .sim{font-size:10px;color:#64748b}
  .wr-badge{font-size:11px;padding:2px 7px;border-radius:10px}
  .wr-good{background:rgba(34,197,94,.15);color:#22c55e}
  .wr-warn{background:rgba(251,191,36,.15);color:#fbbf24}
  .wr-none{color:#475569}
  /* Rendimiento */
  .perf-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px;margin-bottom:24px}
  .perf-card{background:#161b27;border:1px solid #2a2f3e;border-radius:10px;padding:16px}
  .perf-card.ganadora{border-color:rgba(245,158,11,.4);background:rgba(245,158,11,.05)}
  .perf-city{font-size:13px;font-weight:600;color:#e0e0e0;margin-bottom:4px}
  .perf-strike{font-size:11px;color:#64748b;margin-bottom:12px}
  .perf-wr{font-size:24px;font-weight:700;margin-bottom:8px}
  .perf-wr.good{color:#22c55e}.perf-wr.warn{color:#fbbf24}.perf-wr.bad{color:#ef4444}
  .perf-bar-bg{height:6px;background:#2a2f3e;border-radius:3px;margin-bottom:8px}
  .perf-bar-fill{height:100%;border-radius:3px}
  .perf-detail{font-size:11px;color:#64748b}
  /* Historial */
  .hist-item{background:#161b27;border:1px solid #2a2f3e;border-radius:10px;margin-bottom:10px;overflow:hidden}
  .hist-hdr{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;cursor:pointer}
  .hist-hdr:hover{background:#1a2030}
  .hist-fecha{font-size:13px;color:#cbd5e1;font-weight:500}
  .hist-stats{display:flex;gap:14px;font-size:12px}
  .hgan{color:#f59e0b;font-weight:600}
  .hbuy{color:#22c55e;font-weight:600}
  .hwatch{color:#fbbf24}
  .htotal{color:#64748b}
  .hist-body{display:none;border-top:1px solid #2a2f3e;overflow-x:auto}
  .hist-body.open{display:block}
  .empty{text-align:center;padding:48px;color:#64748b;font-size:14px}
  .overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center;flex-direction:column;gap:16px}
  .overlay.show{display:flex}
  .spinner{width:40px;height:40px;border:3px solid #2a2f3e;border-top-color:#3b82f6;border-radius:50%;animation:spin .8s linear infinite}
  .overlay-txt{color:#fff;font-size:15px}
  .resultado-sel{background:#0f1117;color:#e0e0e0;border:1px solid #2a2f3e;border-radius:6px;padding:3px 6px;font-size:11px;cursor:pointer}
  .info-box{background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.2);border-radius:8px;padding:14px 18px;margin-bottom:20px;font-size:13px;color:#94a3b8;line-height:1.6}
</style>
</head>
<body>

<div class="overlay" id="overlay">
  <div class="spinner"></div>
  <div class="overlay-txt">Escaneando mercados...</div>
</div>

<div class="header">
  <h1><div class="dot"></div>Kalshi Weather Scanner <span style="font-size:11px;color:#3b82f6;margin-left:4px">v2</span></h1>
  <div class="hdr-right">
    <span class="ts" id="ts">Sin datos</span>
    <span class="badge badge-sim" id="modo">SIMULACION</span>
    <span id="scheduler-info" style="font-size:12px;color:#64748b"></span>
    <button id="btn-scheduler" onclick="toggleScheduler()" style="background:transparent;border:1px solid #2a2f3e;color:#64748b;padding:6px 14px;border-radius:8px;font-size:12px;cursor:pointer">⏸ Pausar auto</button>
    <button class="scan-btn" id="btn" onclick="scan()">&#9654; Escanear ahora</button>
  </div>
</div>

<div class="tabs">
  <div class="tab active" onclick="tab('scanner',this)">Scanner en vivo</div>
  <div class="tab" onclick="tab('historico',this)">Historico</div>
  <div class="tab" onclick="tab('rendimiento',this)">Rendimiento ⭐</div>
</div>

<div class="content">

  <!-- SCANNER -->
  <div id="tab-scanner">
    <div class="stats">
      <div class="stat"><div class="stat-label">Senales BUY</div><div class="stat-value" id="s-buy">—</div></div>
      <div class="stat"><div class="stat-label">BUY Ganadoras ⭐</div><div class="stat-value gold" id="s-gan">—</div></div>
      <div class="stat"><div class="stat-label">Ventaja media %</div><div class="stat-value" id="s-edge">—</div></div>
      <div class="stat"><div class="stat-label">Modelo medio %</div><div class="stat-value" id="s-model">—</div></div>
      <div class="stat"><div class="stat-label">Filtrados (iliq.)</div><div class="stat-value" style="color:#334155" id="s-iliq">—</div></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>Ciudad</th><th>Mercado</th>
            <th class="r">Fc °F</th><th class="r">Mercado %</th>
            <th class="r">Modelo %</th><th class="r">Ventaja %</th>
            <th class="c">Win Rate</th><th>Senal</th>
          </tr>
        </thead>
        <tbody id="tbody">
          <tr><td colspan="9" class="empty">Pulsa "Escanear ahora" para obtener datos</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- HISTORICO -->
  <div id="tab-historico" style="display:none">
    <div class="info-box">
      Puedes marcar el <strong>resultado real</strong> de cada señal BUY una vez que el mercado cierre.<br>
      Selecciona <strong>WIN</strong> si la temperatura superó el strike, <strong>LOSS</strong> si no lo hizo.<br>
      Estos datos alimentan automáticamente el <strong>Win Rate</strong> de cada combinación ciudad+strike.
    </div>
    <div id="hist"></div>
  </div>

  <!-- RENDIMIENTO -->
  <div id="tab-rendimiento" style="display:none">
    <div class="info-box">
      Combinaciones con al menos <strong>3 resultados resueltos</strong> (WIN/LOSS).<br>
      Las marcadas en dorado tienen win rate ≥ 55% y se clasifican automáticamente como <strong>BUY GANADORA ⭐</strong>.
    </div>
    <div id="perf-grid" class="perf-grid"></div>
    <div id="perf-empty" class="empty" style="display:none">
      Aun no hay suficientes resultados marcados (WIN/LOSS) para calcular rendimiento.<br>
      Marca los resultados en la pestana Historico despues de que cierren los mercados.
    </div>
  </div>

</div>

<script>
let ultimoCsv = null;

function barColor(p){return p>=80?'#22c55e':p>=50?'#fbbf24':'#ef4444'}

function wrBadge(wr, total){
  if(wr===null||wr===undefined) return '<span class="wr-none">—</span>';
  const cls = wr>=55 ? 'wr-good' : 'wr-warn';
  return `<span class="wr-badge ${cls}">${wr.toFixed(1)}% <span style="opacity:.6">(${total})</span></span>`;
}

function sigClass(s){
  if(s==='BUY GANADORA') return 'sig-ganadora';
  if(s==='BUY')          return 'sig-buy';
  if(s==='WATCH')        return 'sig-watch';
  if(s==='ILIQUIDO')     return 'sig-iliquido';
  return 'sig-nobet';
}

function renderTabla(datos){
  const tbody=document.getElementById('tbody');
  if(!datos.length){tbody.innerHTML='<tr><td colspan="9" class="empty">Sin resultados</td></tr>';return}
  datos.sort((a,b)=>b.edge_pct-a.edge_pct);
  tbody.innerHTML=datos.map(r=>{
    const sc  = sigClass(r.senal);
    const ec  = r.edge_pct>=0?'ep':'en';
    const sign= r.edge_pct>=0?'+':'';
    const sim = r.simulado?'<span class="sim">~</span>':'';
    const mp  = Math.min(r.mercado_pct,100), mo=Math.min(r.modelo_pct,100);
    const rowCls = r.senal==='BUY GANADORA'?'ganadora':'';
    const wr  = wrBadge(r.win_rate, r.wr_total);
    return `<tr class="${rowCls}">
      <td style="color:#64748b">${r.id}</td>
      <td style="font-weight:500">${r.ciudad}</td>
      <td style="color:#94a3b8">${r.mercado}</td>
      <td class="r">${r.forecast_f}</td>
      <td class="r"><div class="bar-wrap">${r.mercado_pct.toFixed(2)}%<div class="bar"><div class="bar-fill" style="width:${mp}%;background:${barColor(mp)}"></div></div></div></td>
      <td class="r"><div class="bar-wrap">${r.modelo_pct.toFixed(2)}%<div class="bar"><div class="bar-fill" style="width:${mo}%;background:${barColor(mo)}"></div></div></div></td>
      <td class="r"><span class="${ec}">${sign}${r.edge_pct.toFixed(2)}%</span>${sim}</td>
      <td class="c">${wr}</td>
      <td><span class="sig ${sc}">${r.senal}</span></td>
    </tr>`;
  }).join('');
}

function actualizarStats(datos, winRates){
  const buys = datos.filter(r=>r.senal==='BUY'||r.senal==='BUY GANADORA');
  const gans = datos.filter(r=>r.senal==='BUY GANADORA');
  const iliq = datos.filter(r=>r.senal==='ILIQUIDO');
  document.getElementById('s-buy').textContent=buys.length;
  document.getElementById('s-gan').textContent=gans.length;
  document.getElementById('s-iliq').textContent=iliq.length;
  if(buys.length){
    document.getElementById('s-edge').textContent=(buys.reduce((s,r)=>s+r.edge_pct,0)/buys.length).toFixed(2)+'%';
    document.getElementById('s-model').textContent=(buys.reduce((s,r)=>s+r.modelo_pct,0)/buys.length).toFixed(2)+'%';
  }else{
    document.getElementById('s-edge').textContent='—';
    document.getElementById('s-model').textContent='—';
  }
}

async function scan(){
  const btn=document.getElementById('btn');
  btn.disabled=true; btn.innerHTML='<span class="spin">&#8635;</span> Escaneando...';
  document.getElementById('overlay').classList.add('show');
  try{
    const r=await fetch('/api/scan',{method:'POST'});
    const d=await r.json();
    if(d.ok){
      ultimoCsv=d.csv;
      renderTabla(d.resultados);
      actualizarStats(d.resultados, d.win_rates);
      document.getElementById('ts').textContent='Actualizado: '+d.timestamp;
      const badge=document.getElementById('modo');
      if(d.modo==='REAL'){badge.textContent='REAL';badge.className='badge badge-real';}
      else{badge.textContent='SIMULACION';badge.className='badge badge-sim';}
    }else{ alert('Error: '+d.error); }
  }catch(e){ alert('Error de conexion: '+e); }
  finally{
    btn.disabled=false; btn.innerHTML='&#9654; Escanear ahora';
    document.getElementById('overlay').classList.remove('show');
  }
}

function tab(name,el){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('tab-scanner').style.display=name==='scanner'?'':'none';
  document.getElementById('tab-historico').style.display=name==='historico'?'':'none';
  document.getElementById('tab-rendimiento').style.display=name==='rendimiento'?'':'none';
  if(name==='historico')   cargarHistorico();
  if(name==='rendimiento') cargarRendimiento();
}

async function cargarHistorico(){
  const c=document.getElementById('hist');
  c.innerHTML='<div class="empty">Cargando...</div>';
  try{
    const r=await fetch('/api/historico');
    const data=await r.json();
    if(!data.length){c.innerHTML='<div class="empty">No hay CSVs guardados aun</div>';return;}
    c.innerHTML=data.map((h,i)=>`
      <div class="hist-item">
        <div class="hist-hdr" onclick="toggleH(${i})">
          <div class="hist-fecha">${h.fecha}</div>
          <div class="hist-stats">
            ${h.ganadoras?`<span class="hgan">⭐ ${h.ganadoras}</span>`:''}
            <span class="hbuy">BUY: ${h.buys}</span>
            <span class="hwatch">WATCH: ${h.watchs}</span>
            <span class="htotal">Total: ${h.total}</span>
          </div>
          <div style="display:flex;align-items:center;gap:10px">
            <button onclick="event.stopPropagation();verificarResultados('${h.archivo}',${i})"
              id="btn-ver-${i}"
              style="font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid #3b82f6;background:transparent;color:#3b82f6;cursor:pointer">
              &#10003; Verificar resultados
            </button>
            <span style="color:#64748b;font-size:18px" id="chev-${i}">&#8250;</span>
          </div>
        </div>
        <div class="hist-body" id="hb-${i}">
          <table style="width:100%;border-collapse:collapse;font-size:12px">
            <thead><tr style="background:#1e2535">
              <th style="padding:8px 14px;text-align:left;color:#64748b;font-size:10px;text-transform:uppercase">Ciudad</th>
              <th style="padding:8px 14px;text-align:left;color:#64748b;font-size:10px;text-transform:uppercase">Mercado</th>
              <th style="padding:8px 14px;text-align:right;color:#64748b;font-size:10px;text-transform:uppercase">Mkt%</th>
              <th style="padding:8px 14px;text-align:right;color:#64748b;font-size:10px;text-transform:uppercase">Mod%</th>
              <th style="padding:8px 14px;text-align:right;color:#64748b;font-size:10px;text-transform:uppercase">Edge%</th>
              <th style="padding:8px 14px;text-align:center;color:#64748b;font-size:10px;text-transform:uppercase">Senal</th>
              <th style="padding:8px 14px;text-align:center;color:#64748b;font-size:10px;text-transform:uppercase">Resultado</th>
            </tr></thead>
            <tbody>${h.filas.sort((a,b)=>parseFloat(b.edge_pct)-parseFloat(a.edge_pct)).map(f=>{
              const sc=f.senal==='BUY GANADORA'?'sig-ganadora':f.senal==='BUY'?'sig-buy':f.senal==='WATCH'?'sig-watch':'sig-nobet';
              const ec=parseFloat(f.edge_pct)>=0?'#22c55e':'#ef4444';
              const sign=parseFloat(f.edge_pct)>=0?'+':'';
              const res=f.resultado||'PENDING';
              const resColor=res==='WIN'?'#22c55e':res==='LOSS'?'#ef4444':'#64748b';
              const showSel = (f.senal==='BUY'||f.senal==='BUY GANADORA'||f.senal==='WATCH');
              return `<tr style="border-top:1px solid #1e2535">
                <td style="padding:8px 14px;font-weight:500">${f.ciudad}</td>
                <td style="padding:8px 14px;color:#94a3b8">${f.mercado}</td>
                <td style="padding:8px 14px;text-align:right">${parseFloat(f.mercado_pct).toFixed(2)}%</td>
                <td style="padding:8px 14px;text-align:right">${parseFloat(f.modelo_pct).toFixed(2)}%</td>
                <td style="padding:8px 14px;text-align:right;color:${ec};font-weight:600">${sign}${parseFloat(f.edge_pct).toFixed(2)}%</td>
                <td style="padding:8px 14px;text-align:center"><span class="sig ${sc}">${f.senal}</span></td>
                <td style="padding:8px 14px;text-align:center">
                  ${showSel?`<select class="resultado-sel" onchange="marcarResultado('${h.archivo}','${f.ciudad}',${f.strike},this.value)" style="color:${resColor}">
                    <option value="PENDING" ${res==='PENDING'?'selected':''}>— Pendiente</option>
                    <option value="WIN"     ${res==='WIN'?'selected':''} style="color:#22c55e">WIN</option>
                    <option value="LOSS"    ${res==='LOSS'?'selected':''} style="color:#ef4444">LOSS</option>
                  </select>`:`<span style="color:#2a2f3e">—</span>`}
                </td>
              </tr>`;}).join('')}
            </tbody>
          </table>
        </div>
      </div>`).join('');
  }catch(e){ c.innerHTML='<div class="empty">Error cargando historial</div>'; }
}

async function marcarResultado(archivo, ciudad, strike, resultado){
  try{
    await fetch('/api/marcar_resultado',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({archivo,ciudad,strike,resultado})
    });
  }catch(e){ alert('Error al guardar resultado: '+e); }
}

async function cargarRendimiento(){
  const grid=document.getElementById('perf-grid');
  const empty=document.getElementById('perf-empty');
  grid.innerHTML='';
  try{
    const r=await fetch('/api/rendimiento');
    const data=await r.json();
    if(!data.length){grid.style.display='none';empty.style.display='block';return;}
    grid.style.display='grid';empty.style.display='none';
    grid.innerHTML=data.map(d=>{
      const cls     = d.win_rate>=55?'good':d.win_rate>=40?'warn':'bad';
      const barClr  = d.win_rate>=55?'#22c55e':d.win_rate>=40?'#fbbf24':'#ef4444';
      const cardCls = d.ganadora?'perf-card ganadora':'perf-card';
      const star    = d.ganadora?'⭐ ':'';
      return `<div class="${cardCls}">
        <div class="perf-city">${star}${d.ciudad}</div>
        <div class="perf-strike">High Temp Above ${d.strike}F</div>
        <div class="perf-wr ${cls}">${d.win_rate.toFixed(1)}%</div>
        <div class="perf-bar-bg"><div class="perf-bar-fill" style="width:${d.win_rate}%;background:${barClr}"></div></div>
        <div class="perf-detail">${d.wins} aciertos / ${d.losses} fallos / ${d.total} total</div>
      </div>`;
    }).join('');
  }catch(e){ grid.innerHTML='<div class="empty">Error cargando rendimiento</div>'; }
}

function toggleH(i){
  const b=document.getElementById('hb-'+i), c=document.getElementById('chev-'+i);
  c.innerHTML=b.classList.toggle('open')?'&#8964;':'&#8250;';
}

async function verificarResultados(archivo, i){
  const btn=document.getElementById('btn-ver-'+i);
  btn.textContent='Consultando...';
  btn.disabled=true;
  btn.style.color='#64748b';
  btn.style.borderColor='#64748b';
  try{
    const r=await fetch('/api/verificar_resultados',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({archivo})
    });
    const d=await r.json();
    if(d.ok){
      btn.textContent=`✓ ${d.marcadas} marcadas`;
      btn.style.color='#22c55e';
      btn.style.borderColor='#22c55e';
      // Mostrar temperaturas reales
      let info='';
      for(const [ciudad,temp] of Object.entries(d.temps_reales)){
        info+=`${ciudad}: ${temp}°F  `;
      }
      const hdr=btn.closest('.hist-hdr');
      let infoEl=hdr.querySelector('.temps-info');
      if(!infoEl){
        infoEl=document.createElement('div');
        infoEl.className='temps-info';
        infoEl.style.cssText='font-size:11px;color:#22c55e;margin-top:4px;width:100%';
        hdr.appendChild(infoEl);
      }
      infoEl.textContent='Temps reales: '+info;
      // Recargar el historico para ver los WIN/LOSS actualizados
      setTimeout(()=>cargarHistorico(), 500);
    } else {
      btn.textContent='Error: '+d.error;
      btn.style.color='#ef4444';
    }
  }catch(e){
    btn.textContent='Error conexion';
    btn.style.color='#ef4444';
  }
}

async function toggleScheduler(){
  const r=await fetch('/api/scheduler_toggle',{method:'POST'});
  const d=await r.json();
  const btn=document.getElementById('btn-scheduler');
  btn.textContent=d.activo?'⏸ Pausar auto':'▶ Reanudar auto';
  btn.style.color=d.activo?'#64748b':'#22c55e';
  actualizarScheduler();
}

async function actualizarScheduler(){
  try{
    const r=await fetch('/api/estado_scheduler');
    const d=await r.json();
    const info=document.getElementById('scheduler-info');
    if(d.activo && d.proximo_scan){
      info.textContent=`Auto-scan: proximo a las ${d.proximo_scan} · Total: ${d.total_scans}`;
      info.style.color='#22c55e';
    } else if(!d.activo){
      info.textContent='Auto-scan pausado';
      info.style.color='#64748b';
    }
  }catch(e){}
}

// Actualizar estado del scheduler cada 30 segundos
setInterval(actualizarScheduler, 30000);
actualizarScheduler();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/estado_scheduler")
def api_estado_scheduler():
    return jsonify({
        "activo":        SCHEDULER["activo"],
        "intervalo_h":   SCHEDULER["intervalo_horas"],
        "ultimo_scan":   SCHEDULER["ultimo_scan"],
        "proximo_scan":  SCHEDULER["proximo_scan"],
        "total_scans":   SCHEDULER["total_scans"],
    })

@app.route("/api/scheduler_toggle", methods=["POST"])
def api_scheduler_toggle():
    SCHEDULER["activo"] = not SCHEDULER["activo"]
    estado = "activado" if SCHEDULER["activo"] else "pausado"
    print(f"  Scheduler {estado}")
    return jsonify({"activo": SCHEDULER["activo"]})


# ── SCHEDULER ────────────────────────────────────────────────
SCHEDULER = {
    "activo":         True,
    "intervalo_horas": 1,
    "ultimo_scan":    None,
    "proximo_scan":   None,
    "total_scans":    0,
}

def loop_scheduler():
    """Hilo en segundo plano que escanea cada hora automáticamente."""
    intervalo_seg = SCHEDULER["intervalo_horas"] * 3600
    # Primer scan al arrancar (espera 5s para que Flask esté listo)
    time.sleep(5)
    while True:
        if SCHEDULER["activo"]:
            ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
            print(f"\n  [AUTO-SCAN] {ahora}")
            try:
                resultados, modo, csv_name, win_rates = ejecutar_scan()
                SCHEDULER["ultimo_scan"]  = ahora
                SCHEDULER["total_scans"] += 1
                proxima = datetime.fromtimestamp(
                    time.time() + intervalo_seg
                ).strftime("%H:%M")
                SCHEDULER["proximo_scan"] = proxima
                buys = [r for r in resultados if "BUY" in r["senal"]]
                gans = [r for r in resultados if r["senal"] == "BUY GANADORA"]
                print(f"  [AUTO-SCAN] BUYs: {len(buys)} | Ganadoras: {len(gans)} | Proximo: {proxima}")
            except Exception as e:
                print(f"  [AUTO-SCAN] Error: {e}")
        time.sleep(intervalo_seg)


if __name__ == "__main__":
    # Arrancar scheduler en hilo separado
    hilo = threading.Thread(target=loop_scheduler, daemon=True)
    hilo.start()

    threading.Thread(
        target=lambda: (time.sleep(1.5), webbrowser.open("http://localhost:5000")),
        daemon=True
    ).start()

    print("\n" + "="*55)
    print("  Kalshi Weather Scanner v3")
    print("  Abre en tu navegador: http://localhost:5000")
    print(f"  Escaner automatico: cada {SCHEDULER['intervalo_horas']}h")
    print("  Para cerrar: Ctrl + C")
    print("="*55 + "\n")

    import webbrowser
    app.run(host="127.0.0.1", port=5000, debug=False)