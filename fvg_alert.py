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


def telegram_photo(path, caption):
    """Envoie une image (avec légende) à Telegram."""
    if not TOKEN or not CHAT_ID:
        print("[Telegram non configuré] image :", path, "|", caption)
        return
    boundary = "----fvgboundary7d1f"

    def part(name, value):
        return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()

    with open(path, "rb") as fh:
        img = fh.read()
    body = part("chat_id", CHAT_ID) + part("caption", caption)
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="chart.png"\r\n'
             "Content-Type: image/png\r\n\r\n").encode() + img + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendPhoto", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=30).read()


def make_chart(window, fvgs, highlight=None, path="/tmp/chart.png"):
    """Dessine les bougies M30 de la fenêtre et colore les zones FVG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    UP, DN, BG, FG = "#26a69a", "#ef5350", "#0f1419", "#c9d1d9"
    fig, ax = plt.subplots(figsize=(8, 5), dpi=130)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    n = len(window)
    index = {c["epoch"]: i for i, c in enumerate(window)}

    for f in fvgs:                                   # zones FVG (derrière les bougies)
        i3 = index.get(f["epoch"])
        if i3 is None:
            continue
        color = UP if f["type"] == "HAUSSIER" else DN
        strong = f["key"] == highlight
        x0, x1 = i3 - 2 - 0.4, n - 0.5
        ax.add_patch(Rectangle((x0, f["low"]), x1 - x0, f["high"] - f["low"], facecolor=color,
                               alpha=0.45 if strong else 0.18, edgecolor=color if strong else "none",
                               linewidth=1.2, zorder=1))
        ax.text(x1 - 0.1, (f["low"] + f["high"]) / 2, "FVG", color=color, fontsize=8,
                ha="right", va="center", zorder=5)

    for i, c in enumerate(window):                   # bougies
        col = UP if c["close"] >= c["open"] else DN
        ax.plot([i, i], [c["low"], c["high"]], color=col, lw=1.5, zorder=3)
        h = max(abs(c["close"] - c["open"]), 0.02)
        ax.add_patch(Rectangle((i - 0.3, min(c["open"], c["close"])), 0.6, h, color=col, zorder=4))

    lo = min([c["low"] for c in window] + [f["low"] for f in fvgs])
    hi = max([c["high"] for c in window] + [f["high"] for f in fvgs])
    pad = (hi - lo) * 0.08 or 1
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(-0.7, n - 0.3)
    ax.set_xticks(range(n))
    ax.set_xticklabels([hhmm(c["epoch"]) for c in window], rotation=45, fontsize=8)
    ax.tick_params(colors=FG, labelsize=8)
    ax.grid(axis="y", color="#2a323c", linewidth=0.5)
    for s in ax.spines.values():
        s.set_color("#2a323c")
    ax.set_title(f"Step Index 100 - M30 (UTC{TZ_HOURS:+g})", color=FG, fontsize=11)
    fig.tight_layout()
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    return path


def send_alert(window, fvgs, fvg, text):
    """Envoie le graphique + le texte ; si le graphique échoue, envoie le texte seul."""
    try:
        telegram_photo(make_chart(window, fvgs, fvg["key"] if fvg else None), text)
    except Exception as e:
        print("Graphique impossible, envoi du texte seul :", e)
        telegram(text)


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
        send_alert(window, fvgs, None, "\n".join(lines))
        return

    seen, first_run = load_state()
    if first_run:
        telegram("✅ Surveillance démarrée : Step Index 100, M30. Tu seras alerté à chaque nouveau FVG.")
        seen = {f["key"]: f["epoch"] for f in fvgs}   # on ne réalerte pas l'ancien
    else:
        for f in fvgs:
            if f["key"] not in seen:
                send_alert(window, fvgs, f, describe(f))
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
