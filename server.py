#!/usr/bin/env python3
"""TV Cast - servidor + controlador (roda no PC).

A TV (APK) abre http://IP-DO-PC:8080/tv e mostra o que o painel
(http://localhost:8080) mandar: tela inteira, janela, vídeo ou imagem.
"""
import asyncio
import io
import ipaddress
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlparse

from aiohttp import WSMsgType, web

PORT = int(os.environ.get("TVCAST_PORT", "8765"))
ALLOW_REMOTE_PANEL = os.environ.get("TVCAST_ALLOW_REMOTE") == "1"
BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
MEDIA = BASE / "media"
MEDIA.mkdir(exist_ok=True)

IS_WIN = sys.platform == "win32"

try:
    import windows_capture  # noqa: F401  (captura direta de janela, estilo OBS)
    WGC_OK = IS_WIN
except Exception:
    WGC_OK = False

VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ogv", ".ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# preset: (largura máx, qualidade JPEG, fps)
PRESETS = {"minima": (640, 45, 10), "leve": (960, 55, 12), "normal": (1280, 65, 15), "alta": (1920, 78, 20)}

state = {
    "mode": "blank",       # blank | screen | window | video | image
    "source": {},          # {"kind": "screen", "monitor": 1} | {"kind": "window", "id": ...}
    "name": "",
    "file_id": None,
    "rev": 0,
    "quality": "normal",
    "bg": None,            # {"file_id", "name", "muted"}: vídeo de fundo em loop
    "lyrics": {"text": "", "size": 1.0},   # texto por cima de tudo (letras)
    "page": "",            # URL mostrada na TV (modo "url")
    "css": "",             # CSS personalizado (página + letra)
    "badge": "auto",       # selo TV CAST + status na TV: auto | always | never
    "wmethod": "direct",   # captura de janela: direct (estilo OBS) | region (cópia da tela, mais leve)
}
FILES = {}                 # id -> Path (só o que foi escolhido no painel é servido)
clients = {"tv": set(), "panel": set()}
progress = {}

CAPTURE_POOL = ThreadPoolExecutor(max_workers=1)   # mss exige sempre a mesma thread
PICK_POOL = ThreadPoolExecutor(max_workers=1)

# ---------------------------------------------------------------- Windows
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    dwm = ctypes.windll.dwmapi
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


def _win_rect(hwnd):
    r = wintypes.RECT()
    if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def list_windows():
    out = []
    if IS_WIN:
        def cb(hwnd, _):
            try:
                if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                    return True
                cloaked = wintypes.DWORD(0)
                dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
                if cloaked.value:
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                rect = _win_rect(hwnd)
                if not rect or rect[2] < 120 or rect[3] < 80:
                    return True
                if buf.value in ("Program Manager", "Settings"):
                    return True
                out.append({"id": int(hwnd), "title": buf.value})
            except Exception:
                pass
            return True
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    elif sys.platform.startswith("linux") and shutil.which("wmctrl"):
        try:
            txt = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True, timeout=3).stdout
            for line in txt.splitlines():
                p = line.split(None, 8)
                if len(p) == 9 and p[8].strip():
                    out.append({"id": p[0], "title": p[8].strip()})
        except Exception:
            pass
    out.sort(key=lambda w: w["title"].lower())
    return out


def window_rect(wid):
    try:
        if IS_WIN:
            return _win_rect(int(wid))
        txt = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True, timeout=3).stdout
        for line in txt.splitlines():
            p = line.split(None, 8)
            if len(p) == 9 and p[0] == str(wid):
                return int(p[2]), int(p[3]), int(p[4]), int(p[5])
    except Exception:
        pass
    return None


def window_title(wid):
    try:
        if IS_WIN:
            n = user32.GetWindowTextLengthW(int(wid))
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(int(wid), buf, n + 1)
            return buf.value
    except Exception:
        pass
    return ""


def restore_if_minimized(wid):
    """Se estiver minimizada, restaura SEM ativar (a captura não vê janela minimizada)."""
    try:
        if IS_WIN and user32.IsIconic(int(wid)):
            user32.ShowWindow(int(wid), 4)  # SW_SHOWNOACTIVATE
    except Exception:
        pass


def window_exists(wid):
    try:
        if IS_WIN:
            return bool(user32.IsWindow(int(wid)))
        return any(str(w["id"]) == str(wid) for w in list_windows())
    except Exception:
        return True


def focus_window(wid):
    """Traz a janela para frente (a captura é da região da tela onde ela está)."""
    try:
        if IS_WIN:
            h = int(wid)
            if user32.IsIconic(h):
                user32.ShowWindow(h, 9)
            user32.SetForegroundWindow(h)
        elif shutil.which("wmctrl"):
            subprocess.run(["wmctrl", "-ia", str(wid)], timeout=3)
    except Exception:
        pass


# ---------------------------------------------------------------- captura direta de janela
# Medições (aparecem no painel e no console): onde o tempo está indo.
STATS = {"cap": 0, "cb_ms": 0.0, "enc": 0, "enc_ms": 0.0, "sent": 0, "age": 0.0, "kb": 0.0}


class WindowGrabber:
    """Captura UMA janela pelo Windows Graphics Capture (como o OBS).
    Independe do que está na frente (Alt+Tab, menu Iniciar, outras janelas).
    Só não funciona com a janela minimizada: aí fica o último quadro."""

    def __init__(self, hwnd, title):
        self.hwnd = int(hwnd)
        self.title = title
        self.lock = threading.Lock()
        self.enc_lock = threading.Lock()
        self.raw = None
        self.raw_seq = 0
        self.enc_seq = 0
        self.jpeg = b""
        self.t_enc = 0.0
        self.enc_ta = time.time()
        self.control = None
        self.how = ""

    def start(self):
        from windows_capture import WindowsCapture
        extras = [dict(draw_border=False, cursor_capture=False), dict(cursor_capture=False), {}]
        ident = [("hwnd", dict(window_hwnd=self.hwnd)), ("título", dict(window_name=self.title))]
        last = None
        for how, idk in ident:
            if how == "título" and not self.title:
                continue
            for ex in extras:
                try:
                    cap = WindowsCapture(**idk, **ex)
                    self._bind(cap)
                    self.control = cap.start_free_threaded()
                    self.how = how
                    return
                except Exception as e:  # versão sem esse parâmetro, janela não achada, etc.
                    last = e
        raise RuntimeError(last or "não foi possível iniciar a captura")

    def _bind(self, cap):
        @cap.event
        def on_frame_arrived(frame, control):
            t_cb = time.perf_counter()
            try:
                buf = frame.frame_buffer
                h, w = int(buf.shape[0]), int(buf.shape[1])
                if w < 2 or h < 2:
                    return
                data = buf.tobytes()
                with self.lock:
                    self.raw = (w, h, data, time.time())
                    self.raw_seq += 1
                STATS["cap"] += 1
                STATS["cb_ms"] += (time.perf_counter() - t_cb) * 1000
            except Exception as e:
                print("Erro no quadro da janela:", e)

        @cap.event
        def on_closed():
            pass

    def stop(self):
        try:
            if self.control is not None:
                self.control.stop()
        except Exception:
            pass
        self.control = None

    def snapshot(self):
        """(jpeg, número do quadro). Codifica no ritmo do preset; devolve o último se nada mudou."""
        width, quality, fps = PRESETS[state["quality"]]
        fps = max(fps, 25)   # letra/slide: troca rara, então codificar até 25/s custa pouco e reduz o atraso
        with self.lock:
            raw, rseq = self.raw, self.raw_seq
        if raw is None or rseq == self.enc_seq or time.time() - self.t_enc < 1.0 / fps:
            return self.jpeg, self.enc_seq
        if not self.enc_lock.acquire(blocking=False):
            return self.jpeg, self.enc_seq
        try:
            t_e = time.perf_counter()
            w, h, data, ta = raw
            img = Image.frombytes("RGB", (w, h), data, "raw", "BGRX")
            if img.width > width:
                img = img.resize((width, max(2, int(img.height * width / img.width))),
                                 RESAMPLE, reducing_gap=2.0)
            out = io.BytesIO()
            img.save(out, "JPEG", quality=quality)
            self.jpeg = out.getvalue()
            self.enc_seq = rseq
            self.t_enc = time.time()
            self.enc_ta = ta
            STATS["enc"] += 1
            STATS["enc_ms"] += (time.perf_counter() - t_e) * 1000
            STATS["kb"] = 0.7 * STATS["kb"] + 0.3 * len(self.jpeg) / 1024
        finally:
            self.enc_lock.release()
        return self.jpeg, self.enc_seq


GRAB = {"g": None}


def stop_grabber():
    g = GRAB["g"]
    GRAB["g"] = None
    if g:
        g.stop()


def start_grabber(wid, title):
    """Devolve o WindowGrabber ou None (e o motivo) se não deu para iniciar."""
    try:
        g = WindowGrabber(wid, window_title(wid) or title)
        g.start()
        GRAB["g"] = g
        return g, ""
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------- captura
_tl = {}
_cache = {"key": None, "t": 0.0, "data": b""}

try:
    from PIL import Image
    RESAMPLE = Image.Resampling.BILINEAR if hasattr(Image, "Resampling") else Image.BILINEAR
except Exception:  # pragma: no cover
    Image = None


def _sct():
    import mss
    if "sct" not in _tl:
        _tl["sct"] = mss.mss()
    return _tl["sct"]


def list_monitors():
    sct = _sct()
    mons = []
    for i, m in enumerate(sct.monitors[1:], start=1):
        mons.append({"id": i, "label": f"Tela {i} ({m['width']}x{m['height']})"})
    if len(sct.monitors) > 2:
        mons.append({"id": 0, "label": "Todas as telas juntas"})
    return mons


def get_frame():
    """Roda sempre na thread de captura. Devolve um JPEG."""
    width, quality, fps = PRESETS[state["quality"]]
    key = (json.dumps(state["source"], sort_keys=True), state["quality"])
    now = time.time()
    if _cache["key"] == key and now - _cache["t"] < (0.8 / fps):
        return _cache["data"]

    sct = _sct()
    src = state["source"] or {"kind": "screen", "monitor": 1}
    if src.get("kind") == "window":
        rect = window_rect(src.get("id"))
        vs = sct.monitors[0]
        if rect:
            x, y, w, h = rect
            x0, y0 = max(x, vs["left"]), max(y, vs["top"])
            x1 = min(x + w, vs["left"] + vs["width"])
            y1 = min(y + h, vs["top"] + vs["height"])
            if x1 - x0 >= 2 and y1 - y0 >= 2:
                mon = {"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0}
            else:
                mon = sct.monitors[1]
        else:
            mon = sct.monitors[1]
    else:
        idx = int(src.get("monitor", 1))
        if idx < 0 or idx >= len(sct.monitors):
            idx = 1
        mon = sct.monitors[idx]

    shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > width:
        img = img.resize((width, max(2, int(img.height * width / img.width))), RESAMPLE)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    data = buf.getvalue()
    _cache.update(key=key, t=time.time(), data=data)
    return data


# ---------------------------------------------------------------- util
SETTINGS_FILE = BASE / "settings.json"
RECENT_URLS = []


def load_settings():
    try:
        d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    state["css"] = str(d.get("css", ""))
    state["badge"] = d.get("badge") if d.get("badge") in ("auto", "always", "never") else "auto"
    state["wmethod"] = d.get("wmethod") if d.get("wmethod") in ("direct", "region") else "direct"
    RECENT_URLS[:] = [u for u in d.get("urls", []) if isinstance(u, str)][:10]


def save_settings():
    try:
        SETTINGS_FILE.write_text(
            json.dumps({"css": state["css"], "urls": RECENT_URLS, "badge": state["badge"],
                        "wmethod": state["wmethod"]}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception as e:
        print("Não consegui salvar settings.json:", e)


def remember_url(u):
    if u in RECENT_URLS:
        RECENT_URLS.remove(u)
    RECENT_URLS.insert(0, u)
    del RECENT_URLS[10:]
    save_settings()


DISCOVERY_PORT = int(os.environ.get("TVCAST_DISCOVERY_PORT", "8766"))


def discovery_loop():
    """Responde ao 'TVCAST?' do app da TV: ele acha o PC sem digitar o IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", DISCOVERY_PORT))
    except OSError as e:
        print("Busca automática desativada (porta UDP %d): %s" % (DISCOVERY_PORT, e))
        return
    reply = json.dumps({"app": "tvcast", "port": PORT, "name": socket.gethostname()}).encode()
    while True:
        try:
            data, addr = s.recvfrom(256)
            if data.strip() == b"TVCAST?":
                s.sendto(reply, addr)
        except OSError:          # no Windows, UDP pode dar "connection reset": ignora
            time.sleep(0.2)


def start_discovery():
    threading.Thread(target=discovery_loop, daemon=True).start()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def is_loopback(remote):
    try:
        ip = ipaddress.ip_address(remote)
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return ip.is_loopback
    except Exception:
        return False


def tv_state():
    m = state["mode"]
    s = {"mode": m, "name": state["name"], "rev": state["rev"]}
    if m in ("screen", "window"):
        s["url"] = f"/stream?r={state['rev']}"
    elif m in ("video", "image") and state["file_id"] in FILES:
        s["url"] = f"/file/{state['file_id']}/{quote(FILES[state['file_id']].name)}"
    s["bg"] = bg_payload()
    s["lyrics"] = state["lyrics"]
    s["css"] = state["css"]
    s["badge"] = state["badge"]
    s["page"] = state["page"] if m == "url" else ""
    return s


def bg_payload():
    b = state["bg"]
    if not b or b["file_id"] not in FILES:
        return None
    return {"url": f"/file/{b['file_id']}/{quote(FILES[b['file_id']].name)}", "muted": b["muted"]}


async def broadcast(role, payload):
    for ws in list(clients[role]):
        try:
            await ws.send_json(payload)
        except Exception:
            clients[role].discard(ws)


async def push_status():
    await broadcast("panel", {
        "type": "status",
        "tv": len(clients["tv"]),
        "mode": state["mode"],
        "name": state["name"],
        "quality": state["quality"],
        "progress": progress,
        "bg_name": state["bg"]["name"] if state["bg"] else "",
        "bg_muted": state["bg"]["muted"] if state["bg"] else True,
        "bg_url": (bg_payload() or {}).get("url", ""),
    })


async def push_state():
    await broadcast("tv", {"type": "state", "state": tv_state()})
    await push_status()


# ---------------------------------------------------------------- middleware
@web.middleware
async def guard(request, handler):
    p = request.path
    public = (
        p.startswith(("/tv", "/file/", "/stream", "/ping", "/assets"))
        or (p == "/ws" and request.query.get("role") == "tv")
    )
    if not public and not ALLOW_REMOTE_PANEL and not is_loopback(request.remote or ""):
        raise web.HTTPForbidden(text="O painel só abre no próprio PC (http://localhost:%d)." % PORT)
    return await handler(request)


# ---------------------------------------------------------------- páginas
async def page_panel(request):
    return web.FileResponse(WEB / "panel.html", headers={"Cache-Control": "no-cache"})


async def page_tv(request):
    return web.FileResponse(WEB / "tv.html", headers={"Cache-Control": "no-cache"})


async def ping(request):
    return web.Response(text="tvcast")


# ---------------------------------------------------------------- stream MJPEG
async def stream(request):
    if state["mode"] not in ("screen", "window"):
        raise web.HTTPNotFound()
    loop = asyncio.get_running_loop()
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
    })
    await resp.prepare(request)
    # Buffer de envio pequeno: evita acumular quadros velhos (principal causa de atraso).
    try:
        sock = request.transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
    except Exception:
        pass
    last_seq, last_write = -1, 0.0
    try:
        await resp.write(b"--frame\r\n")
        while state["mode"] in ("screen", "window"):
            t0 = time.time()
            g = GRAB["g"] if state["mode"] == "window" else None
            if g is not None:
                frame, seq = await loop.run_in_executor(None, g.snapshot)
                if not frame or (seq == last_seq and t0 - last_write < 2.0):
                    await asyncio.sleep(0.01)   # janela parada: não reenvia o mesmo quadro
                    continue
                fresh = seq != last_seq
                last_seq = seq
            else:
                fresh = False
                tg = time.time()
                frame = await loop.run_in_executor(CAPTURE_POOL, get_frame)
                STATS["enc"] += 1
                STATS["enc_ms"] += (time.time() - tg) * 1000
            last_write = time.time()
            head = b"Content-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame)
            # O delimitador do PRÓXIMO quadro já vai junto: o navegador exibe este quadro na hora,
            # sem esperar o quadro seguinte (que numa janela parada pode demorar segundos).
            await resp.write(head + frame + b"\r\n--frame\r\n")
            STATS["sent"] += 1
            if g is not None:
                if fresh:   # atraso interno: do quadro chegar até sair pela rede
                    STATS["age"] = 0.7 * STATS["age"] + 0.3 * (time.time() - g.enc_ta) * 1000
                continue    # o ritmo já é limitado na codificação: não dorme depois de enviar
            fps = PRESETS[state["quality"]][2]
            wait = 1.0 / fps - (time.time() - t0)
            if wait > 0:
                await asyncio.sleep(wait)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception as e:  # captura falhou
        print("Erro na captura:", e)
    return resp


async def serve_file(request):
    path = FILES.get(request.match_info["fid"])
    if not path or not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


# ---------------------------------------------------------------- websocket
async def ws_handler(request):
    role = request.query.get("role", "tv")
    if role not in clients:
        raise web.HTTPBadRequest()
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    clients[role].add(ws)
    if role == "tv":
        await ws.send_json({"type": "state", "state": tv_state()})
    await push_status()
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT or role != "tv":
                continue
            try:
                d = json.loads(msg.data)
            except ValueError:
                continue
            if d.get("type") == "progress":
                progress.clear()
                progress.update({k: d.get(k) for k in ("t", "d", "paused", "volume")})
                await push_status()
            elif d.get("type") == "error":
                await broadcast("panel", {"type": "toast", "text": "TV: " + str(d.get("msg", "erro"))})
    finally:
        clients[role].discard(ws)
        await push_status()
    return ws


# ---------------------------------------------------------------- API (painel)
async def api_info(request):
    return web.json_response({"ip": lan_ip(), "port": PORT, "tv": len(clients["tv"]),
                              "mode": state["mode"], "name": state["name"], "quality": state["quality"]})


async def api_sources(request):
    loop = asyncio.get_running_loop()
    mons = await loop.run_in_executor(CAPTURE_POOL, list_monitors)
    wins = await loop.run_in_executor(None, list_windows)
    return web.json_response({"monitors": mons, "windows": wins})


async def api_media(request):
    items = []
    for p in sorted(MEDIA.iterdir(), key=lambda x: x.name.lower()):
        ext = p.suffix.lower()
        if ext in VIDEO_EXT or ext in IMAGE_EXT:
            items.append({"name": p.name, "path": str(p), "kind": "video" if ext in VIDEO_EXT else "image"})
    return web.json_response({"folder": str(MEDIA), "items": items})


def _pick_dialog(kind):
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if kind == "video":
            types = [("Vídeos", " ".join("*" + e for e in sorted(VIDEO_EXT))), ("Todos", "*.*")]
        else:
            types = [("Imagens", " ".join("*" + e for e in sorted(IMAGE_EXT))), ("Todos", "*.*")]
        path = filedialog.askopenfilename(title="Escolha o arquivo", filetypes=types)
        root.destroy()
        return path or None
    except Exception as e:
        print("Não consegui abrir o seletor de arquivos:", e)
        return None


async def api_pick(request):
    d = await request.json()
    loop = asyncio.get_running_loop()
    path = await loop.run_in_executor(PICK_POOL, _pick_dialog, d.get("kind", "video"))
    return web.json_response({"path": path})


async def api_show(request):
    d = await request.json()
    mode = d.get("mode")
    loop = asyncio.get_running_loop()
    progress.clear()
    await loop.run_in_executor(None, stop_grabber)

    if mode == "screen":
        state.update(mode="screen", source={"kind": "screen", "monitor": int(d.get("monitor", 1))},
                     name=d.get("title") or "Tela inteira", file_id=None)
    elif mode == "window":
        wid = d.get("id")
        title = d.get("title") or ""
        grab, why = None, ""
        if WGC_OK and state["wmethod"] == "direct":
            await loop.run_in_executor(None, restore_if_minimized, wid)
            grab, why = await loop.run_in_executor(None, start_grabber, wid, title)
            if grab:
                print("Janela escolhida:", title, "-> captura direta (%s)" % grab.how)
                for _ in range(30):                      # espera o 1º quadro (até 3 s)
                    if grab.raw_seq > 0:
                        break
                    await asyncio.sleep(0.1)
                else:
                    await broadcast("panel", {"type": "toast", "text":
                        "A janela ainda não enviou imagem. Ela está minimizada? Restaure (não precisa ficar na frente)."})
        if not grab:
            # Reserva: copia a região da tela onde a janela está (precisa ficar visível).
            await loop.run_in_executor(None, focus_window, wid)
            await asyncio.sleep(0.3)
            rect = await loop.run_in_executor(None, window_rect, wid)
            print("Janela escolhida:", title, "-> captura por região:", rect, "|", why or "windows-capture não instalado")
            msg = ("Captura direta indisponível (%s). Usando a região da tela: a janela precisa ficar visível. "
                   "Rode o run.bat de novo para instalar o windows-capture." % (why or "pacote não instalado"))
            if state["wmethod"] == "direct" or not WGC_OK:
                await broadcast("panel", {"type": "toast", "text": msg})
        state.update(mode="window", source={"kind": "window", "id": wid},
                     name=d.get("title") or "Janela", file_id=None)
    elif mode in ("file", "video", "image"):
        path = Path(str(d.get("path", "")))
        if not path.is_file():
            return web.json_response({"ok": False, "error": "Arquivo não encontrado."}, status=400)
        ext = path.suffix.lower()
        if ext in VIDEO_EXT:
            kind = "video"
        elif ext in IMAGE_EXT:
            kind = "image"
        else:
            return web.json_response({"ok": False, "error": "Tipo de arquivo não suportado."}, status=400)
        fid = uuid.uuid4().hex[:10]
        FILES[fid] = path
        state.update(mode=kind, source={}, name=path.name, file_id=fid)
    elif mode == "url":
        u = str(d.get("url", "")).strip()
        if u and "://" not in u and "." in u and " " not in u:
            u = "https://" + u
        pu = urlparse(u)
        if pu.scheme not in ("http", "https") or not pu.netloc:
            return web.json_response({"ok": False, "error": "Endereço inválido. Use http:// ou https://"}, status=400)
        state.update(mode="url", source={}, name=u, file_id=None, page=u)
        remember_url(u)
    elif mode == "blank":
        state.update(mode="blank", source={}, name="", file_id=None)
    else:
        return web.json_response({"ok": False, "error": "Modo inválido."}, status=400)

    state["rev"] += 1
    await push_state()
    return web.json_response({"ok": True})


async def api_control(request):
    d = await request.json()
    await broadcast("tv", {"type": "control", "action": d.get("action"), "value": d.get("value")})
    return web.json_response({"ok": True})


async def api_bg(request):
    """Vídeo de fundo em loop: fica por baixo; pausa quando algo é projetado."""
    d = await request.json()
    act = d.get("action")
    if act == "set":
        path = Path(str(d.get("path", "")))
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXT:
            return web.json_response({"ok": False, "error": "Escolha um arquivo de vídeo."}, status=400)
        fid = uuid.uuid4().hex[:10]
        FILES[fid] = path
        state["bg"] = {"file_id": fid, "name": path.name, "muted": bool(d.get("muted", True))}
    elif act == "clear":
        state["bg"] = None
    elif act == "muted" and state["bg"]:
        state["bg"]["muted"] = bool(d.get("value"))
    await broadcast("tv", {"type": "bg", "bg": bg_payload()})
    await push_status()
    return web.json_response({"ok": True})


async def window_watch():
    """Se a janela projetada fechar, volta para o fundo (ou espera)."""
    loop = asyncio.get_running_loop()
    misses = 0
    while True:
        await asyncio.sleep(1)
        try:
            if state["mode"] != "window":
                misses = 0
                continue
            ok = await loop.run_in_executor(None, window_exists, state["source"].get("id"))
            misses = 0 if ok else misses + 1
            if misses >= 2:
                misses = 0
                await loop.run_in_executor(None, stop_grabber)
                state.update(mode="blank", source={}, name="", file_id=None)
                state["rev"] += 1
                progress.clear()
                await push_state()
                await broadcast("panel", {"type": "toast", "text": "A janela foi fechada."})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("Erro ao vigiar a janela:", e)


async def perf_loop():
    """A cada 2 s manda as medições ao painel; a cada 6 s também escreve no console."""
    last = dict(STATS)
    t_last = time.time()
    n = 0
    while True:
        await asyncio.sleep(2)
        try:
            now = time.time()
            dt = max(0.1, now - t_last)
            t_last = now
            d = {k: STATS[k] - last[k] for k in ("cap", "enc", "enc_ms", "sent", "cb_ms")}
            last = dict(STATS)
            if state["mode"] not in ("screen", "window"):
                continue
            direct = state["mode"] == "window" and GRAB["g"] is not None
            perf = {"how": "janela direta" if direct else ("janela por região" if state["mode"] == "window" else "tela inteira"),
                    "cap": round(d["cap"] / dt), "enc": round(d["enc"] / dt), "send": round(d["sent"] / dt),
                    "cb_ms": round(d["cb_ms"] / max(1, d["cap"]), 1), "enc_ms": round(d["enc_ms"] / max(1, d["enc"])),
                    "age": round(STATS["age"]), "kb": round(STATS["kb"])}
            await broadcast("panel", {"type": "perf", **perf})
            n += 1
            if n % 3 == 0:
                print("[medição] %(how)s | chegam %(cap)s q/s (leitura %(cb_ms)s ms) | codifica %(enc)s q/s (%(enc_ms)s ms) "
                      "| envia %(send)s q/s | atraso interno %(age)s ms | %(kb)s KB/quadro" % perf)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("Erro nas medições:", e)


async def _start_watch(app):
    app["watch"] = asyncio.create_task(window_watch())
    app["perf"] = asyncio.create_task(perf_loop())


async def _stop_watch(app):
    app["watch"].cancel()
    app["perf"].cancel()


async def api_lyrics(request):
    """Texto (letra) enviado direto para a TV: sem vídeo, sem atraso."""
    d = await request.json()
    text = str(d.get("text", ""))[:3000]
    try:
        size = min(2.0, max(0.5, float(d.get("size", 1.0))))
    except (TypeError, ValueError):
        size = 1.0
    state["lyrics"] = {"text": text, "size": size}
    await broadcast("tv", {"type": "lyrics", **state["lyrics"]})
    return web.json_response({"ok": True})


async def api_css(request):
    d = await request.json()
    state["css"] = str(d.get("css", ""))[:20000]
    save_settings()
    await broadcast("tv", {"type": "css", "css": state["css"]})
    return web.json_response({"ok": True})


async def api_badge(request):
    d = await request.json()
    m = d.get("mode")
    if m in ("auto", "always", "never"):
        state["badge"] = m
        save_settings()
        await broadcast("tv", {"type": "badge", "mode": m})
    return web.json_response({"ok": True})


async def api_wmethod(request):
    d = await request.json()
    if d.get("mode") in ("direct", "region"):
        state["wmethod"] = d["mode"]
        save_settings()
    return web.json_response({"ok": True})


async def api_settings(request):
    return web.json_response({"css": state["css"], "urls": RECENT_URLS, "badge": state["badge"],
                             "wmethod": state["wmethod"]})


async def api_quality(request):
    d = await request.json()
    if d.get("preset") in PRESETS:
        state["quality"] = d["preset"]
        await push_status()
    return web.json_response({"ok": True})


def make_app():
    load_settings()
    app = web.Application(middlewares=[guard], client_max_size=1024 ** 2)
    app.router.add_static("/assets", WEB / "assets", follow_symlinks=False)
    app.on_startup.append(_start_watch)
    app.on_cleanup.append(_stop_watch)
    app.add_routes([
        web.get("/", page_panel),
        web.get("/tv", page_tv),
        web.get("/ping", ping),
        web.get("/stream", stream),
        web.get("/file/{fid}/{name}", serve_file),
        web.get("/ws", ws_handler),
        web.get("/api/info", api_info),
        web.get("/api/sources", api_sources),
        web.get("/api/media", api_media),
        web.post("/api/pick", api_pick),
        web.post("/api/show", api_show),
        web.post("/api/control", api_control),
        web.post("/api/quality", api_quality),
        web.post("/api/bg", api_bg),
        web.post("/api/lyrics", api_lyrics),
        web.post("/api/css", api_css),
        web.post("/api/badge", api_badge),
        web.post("/api/wmethod", api_wmethod),
        web.get("/api/settings", api_settings),
    ])
    return app


if __name__ == "__main__":
    if Image is None:
        sys.exit("Falta instalar: pip install -r requirements.txt")
    ip = lan_ip()
    print("=" * 56)
    print(" TV Cast rodando")
    print(f" Painel (abra no PC):   http://localhost:{PORT}")
    print(f" Na TV, digite o IP:    {ip}:{PORT}")
    print(" Captura de janela:     " + ("direta (estilo OBS)" if WGC_OK else
          "por região (instale: pip install windows-capture)" if IS_WIN else "por região"))
    print(" Para parar: Ctrl+C")
    print("=" * 56)
    start_discovery()
    web.run_app(make_app(), host="0.0.0.0", port=PORT, print=None)
