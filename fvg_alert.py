#!/usr/bin/env python3
"""Alerte FVG - Step Index 100 (M30) via l'API publique Deriv + Telegram.

Usage :
  python fvg_alert.py        -> surveillance normale (lancée par GitHub Actions)
  python fvg_alert.py test   -> envoie un message de test avec les dernières bougies réelles
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
DEFAULT_SYMBOL = "stpRNG"      # Step Index 100
GRAN = 1800                    # M30 en secondes
KEEP = 12                      # 12 bougies M30 = 6 heures d'historique
STATE_FILE = "state.json"

MIN_GAP = float(os.environ.get("MIN_GAP") or 0)       # taille minimale du FVG (en points)
TZ_HOURS = float(os.environ.get("TZ_HOURS") or 0)     # décalage horaire pour l'affichage
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def hhmm(epoch):
    t = time.gmtime(epoch + TZ_HOURS * 3600)
    return f"{t.tm_hour:02d}:{t.tm_min:02d}"


def telegram(text):
    if not TOKEN or not CHAT_ID:
        print("[Telegram non configuré] " + text)
        return
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data)
    urllib.request.urlopen(req, timeout=20).read()


def call(ws, payload, expect):
    ws.send(json.dumps(payload))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("error"):
            raise RuntimeError(f"Erreur Deriv : {msg['error']}")
        if msg.get("msg_type") == expect:
            return msg


def resolve_symbol(ws):
    """Cherche le symbole du Step Index 100 dans la liste officielle de Deriv."""
    try:
        res = call(ws, {"active_symbols": "brief"}, "active_symbols")
        syms = res.get("active_symbols", [])
        ids = [s.get("underlying_symbol") or s.get("symbol") for s in syms]
        if DEFAULT_SYMBOL in ids:
            return DEFAULT_SYMBOL
        for s in syms:
            name = (s.get("underlying_symbol_name") or s.get("display_name") or "").lower()
            if "step" in name and ("100" in name or name.strip() == "step index"):
                return s.get("underlying_symbol") or s.get("symbol")
        steps = [(i, (s.get("underlying_symbol_name") or s.get("display_name"))) for i, s in zip(ids, syms)
                 if "step" in str(s).lower()]
        print("Step Index 100 introuvable. Symboles 'step' vus :", steps)
    except Exception as e:  # on retombe sur le symbole par défaut
        print("Résolution du symbole impossible :", e)
    return DEFAULT_SYMBOL


def fetch_candles(symbol):
    from websocket import create_connection
    last_err = None
    for _ in range(3):
        try:
            ws = create_connection(WS_URL, timeout=20)
            try:
                if symbol is None:
                    symbol = resolve_symbol(ws)
                res = call(ws, {
                    "ticks_history": symbol, "adjust_start_time": 1,
                    "count": KEEP + 2, "end": "latest",
                    "granularity": GRAN, "style": "candles",
                }, "candles")
                return symbol, res["candles"]
            finally:
                ws.close()
        except Exception as e:
            last_err = e
            time.sleep(3)
    raise RuntimeError(f"Connexion à Deriv impossible : {last_err}")


def closed_window(candles, now=None):
    """Garde uniquement les bougies CLÔTURÉES, sur une fenêtre glissante de 6 h."""
    now = now or int(time.time())
    done = sorted((c for c in candles if c["epoch"] + GRAN <= now), key=lambda c: c["epoch"])
    return done[-KEEP:]


def find_fvgs(candles, min_gap=0.0):
    out = []
    for i in range(2, len(candles)):
        c1, c3 = candles[i - 2], candles[i]
        if c3["low"] > c1["high"] and c3["low"] - c1["high"] >= min_gap:
            out.append({"key": f"UP-{c3['epoch']}", "type": "HAUSSIER", "epoch": c3["epoch"],
                        "low": c1["high"], "high": c3["low"]})
        elif c3["high"] < c1["low"] and c1["low"] - c3["high"] >= min_gap:
            out.append({"key": f"DN-{c3['epoch']}", "type": "BAISSIER", "epoch": c3["epoch"],
                        "low": c3["high"], "high": c1["low"]})
    return out


def describe(f):
    icon = "🟢" if f["type"] == "HAUSSIER" else "🔴"
    return (f"{icon} FVG {f['type']} - Step Index 100 (M30)\n"
            f"Zone : {f['low']:.2f} -> {f['high']:.2f} (taille {f['high'] - f['low']:.2f})\n"
            f"Bougie clôturée à {hhmm(f['epoch'] + GRAN)}")


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh), False
    except Exception:
        return {}, True


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    symbol, raw = fetch_candles(None)
    window = closed_window(raw)
    fvgs = find_fvgs(window, MIN_GAP)
    print(f"Symbole : {symbol} | bougies clôturées : {len(window)} | FVG : {len(fvgs)}")

    if mode == "test":
        lines = [f"✅ Test réussi - données réelles Deriv ({symbol}, M30)", "Dernières bougies clôturées :"]
        for c in window[-3:]:
            lines.append(f"{hhmm(c['epoch'])}  O {c['open']}  H {c['high']}  L {c['low']}  C {c['close']}")
        lines.append(f"FVG dans les 6 dernières heures : {len(fvgs)}")
        telegram("\n".join(lines))
        return

    seen, first_run = load_state()
    if first_run:
        telegram("✅ Surveillance démarrée : Step Index 100, M30. Tu seras alerté à chaque nouveau FVG.")
        seen = {f["key"]: f["epoch"] for f in fvgs}   # on ne réalerte pas l'ancien
    else:
        for f in fvgs:
            if f["key"] not in seen:
                telegram(describe(f))
                seen[f["key"]] = f["epoch"]
    limit = int(time.time()) - 12 * 3600
    seen = {k: v for k, v in seen.items() if v >= limit}
    with open(STATE_FILE, "w") as fh:
        json.dump(seen, fh)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERREUR :", e)
        sys.exit(1)
