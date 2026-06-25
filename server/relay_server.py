import asyncio
import websockets
import ssl
import json
from aiohttp import web  # 🔄 HTTP 서버 추가

ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ssl_context.load_cert_chain(
    certfile="/etc/letsencrypt/archive/afa2025.ddns.net/fullchain2.pem",
    keyfile="/etc/letsencrypt/archive/afa2025.ddns.net/privkey2.pem"
)

clients = {}
authorized_viewer = None
publisher_socket = None
AUTH_PASSWORD = "afa2025"
PUBLISHER_SECRET = "sendvideo2025"

publisher_queue = None  # 전역 큐

async def sender(ws, queue):
    global publisher_queue
    if ws == publisher_socket:
        publisher_queue = queue  # 퍼블리셔 큐 저장
    try:
        while True:
            message = await queue.get()
            await ws.send(message)
    except Exception as e:
        print(f"⚠️ 전송 예외: {e}", flush=True)
    finally:
        print(f"📤 sender 종료: {ws.remote_address}", flush=True)

async def broadcast_to_all(message_dict):
    message = json.dumps(message_dict)
    for conn, queue in clients.items():
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            print(f"❗ 큐 가득참: {conn.remote_address}")

def notify_publisher(message_dict):
    asyncio.run_coroutine_threadsafe(
        broadcast_to_all(message_dict),
        asyncio.get_event_loop()
    )
    print("📢 퍼블리셔와 뷰어 모두에게 알림 전송:", message_dict)

async def handler(websocket):
    global authorized_viewer, publisher_socket
    queue = asyncio.Queue()
    clients[websocket] = queue
    send_task = asyncio.create_task(sender(websocket, queue))
    print(f"✅ 연결됨: {websocket.remote_address}", flush=True)

    authenticated = False
    is_publisher = False
    is_viewer = False

    try:
        async for message in websocket:
            if not authenticated:
                if isinstance(message, str):
                    if message.startswith("auth:publisher:"):
                        secret = message.split("auth:publisher:")[1]
                        if secret == PUBLISHER_SECRET:
                            print("🎥 퍼블리셔 인증 성공", flush=True)
                            authenticated = True
                            is_publisher = True
                            publisher_socket = websocket
                            break
                    elif message.startswith("auth:"):
                        password = message.split("auth:")[1]
                        if password == AUTH_PASSWORD and authorized_viewer is None:
                            authorized_viewer = websocket
                            await websocket.send("auth:success")
                            authenticated = True
                            is_viewer = True
                            print("🔓 뷰어 인증 성공", flush=True)
                            break
                        elif password != AUTH_PASSWORD:
                            await websocket.send("auth:wrong")
                            return
                        else:
                            await websocket.send("auth:fail")
                            return
                else:
                    await websocket.send("auth:required")
                    return

        if not authenticated:
            await websocket.send("auth:required")
            return

        async for message in websocket:
            if isinstance(message, str) and message.startswith("ping:"):
                await websocket.send("pong:" + message[5:])
                continue

            for conn, q in clients.items():
                if conn == websocket:
                    continue

                if is_viewer and conn == publisher_socket:
                    try:
                        q.put_nowait(message)
                    except asyncio.QueueFull:
                        print(f"🚫 퍼블리셔 큐 가득참", flush=True)

                elif is_publisher and conn == authorized_viewer:
                    try:
                        q.put_nowait(message)
                    except asyncio.QueueFull:
                        print(f"🚫 뷰어 큐 가득참", flush=True)

    except Exception as e:
        print(f"❌ 예외 발생: {e}", flush=True)

    finally:
        if websocket == authorized_viewer:
            print("👋 뷰어 연결 종료됨", flush=True)
            authorized_viewer = None
        elif websocket == publisher_socket:
            print("👋 퍼블리셔 연결 종료됨", flush=True)
            publisher_socket = None

        del clients[websocket]
        send_task.cancel()
        print(f"🧹 연결 제거 완료: {websocket.remote_address}", flush=True)

# ✅ HTTP API 라우트 추가
def init_http_server():
    async def publisher_notify(request):
        try:
            data = await request.json()
            print("🌐 HTTP로 퍼블리셔 알림 수신:", data, flush=True)
            notify_publisher(data)
            return web.json_response({"status": "notified"})
        except Exception as e:
            print("❌ 알림 처리 중 예외:", e, flush=True)
            return web.json_response({"status": "error", "error": str(e)}, status=500)

    app = web.Application()
    app.router.add_post("/publisher_notify", publisher_notify)
    return app

async def main():
    print("🚀 WebSocket + HTTP Relay 서버 시작 중...", flush=True)
    ws_server = websockets.serve(handler, "0.0.0.0", 7000, ssl=ssl_context, max_size=None)
    http_app = init_http_server()
    runner = web.AppRunner(http_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8888)

    await asyncio.gather(ws_server, site.start(), asyncio.Future())

asyncio.run(main())
