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
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from aiohttp import WSMsgType, web

PORT = int(os.environ.get("TVCAST_PORT", "8765"))
ALLOW_REMOTE_PANEL = os.environ.get("TVCAST_ALLOW_REMOTE") == "1"
BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
MEDIA = BASE / "media"
MEDIA.mkdir(exist_ok=True)

IS_WIN = sys.platform == "win32"

VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ogv", ".ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# preset: (largura máx, qualidade JPEG, fps)
PRESETS = {"leve": (960, 55, 12), "normal": (1280, 65, 15), "alta": (1920, 78, 20)}

state = {
    "mode": "blank",       # blank | screen | window | video | image
    "source": {},          # {"kind": "screen", "monitor": 1} | {"kind": "window", "id": ...}
    "name": "",
    "file_id": None,
    "rev": 0,
    "quality": "normal",
    "bg": None,            # {"file_id", "name", "muted"}: vídeo de fundo em loop
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
    })


async def push_state():
    await broadcast("tv", {"type": "state", "state": tv_state()})
    await push_status()


# ---------------------------------------------------------------- middleware
@web.middleware
async def guard(request, handler):
    p = request.path
    public = (
        p.startswith(("/tv", "/file/", "/stream", "/ping"))
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
    try:
        while state["mode"] in ("screen", "window"):
            t0 = time.time()
            frame = await loop.run_in_executor(CAPTURE_POOL, get_frame)
            head = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame)
            await resp.write(head + frame + b"\r\n")
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

    if mode == "screen":
        state.update(mode="screen", source={"kind": "screen", "monitor": int(d.get("monitor", 1))},
                     name=d.get("title") or "Tela inteira", file_id=None)
    elif mode == "window":
        wid = d.get("id")
        await loop.run_in_executor(None, focus_window, wid)
        await asyncio.sleep(0.3)
        rect = await loop.run_in_executor(None, window_rect, wid)
        print("Janela escolhida:", d.get("title"), "-> posição/tamanho:", rect)
        if not rect:
            await broadcast("panel", {"type": "toast", "text":
                "Não consegui ler a posição dessa janela; mostrando a tela inteira."})
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
                state.update(mode="blank", source={}, name="", file_id=None)
                state["rev"] += 1
                progress.clear()
                await push_state()
                await broadcast("panel", {"type": "toast", "text": "A janela foi fechada."})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("Erro ao vigiar a janela:", e)


async def _start_watch(app):
    app["watch"] = asyncio.create_task(window_watch())


async def _stop_watch(app):
    app["watch"].cancel()


async def api_quality(request):
    d = await request.json()
    if d.get("preset") in PRESETS:
        state["quality"] = d["preset"]
        await push_status()
    return web.json_response({"ok": True})


def make_app():
    app = web.Application(middlewares=[guard], client_max_size=1024 ** 2)
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
    print(" Para parar: Ctrl+C")
    print("=" * 56)
    web.run_app(make_app(), host="0.0.0.0", port=PORT, print=None)
