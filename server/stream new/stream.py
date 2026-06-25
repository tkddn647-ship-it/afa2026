import asyncio
import cv2
import websockets
import time

async def send_ping(websocket):
    while True:
        await asyncio.sleep(3)
        timestamp = int(time.time() * 1000)
        await websocket.send(f"ping:{timestamp}".encode())

async def receive_pong(websocket):
    async for msg in websocket:
        if isinstance(msg, bytes):
            try:
                decoded = msg.decode()
                if decoded.startswith("pong:"):
                    print("⏱️ Pong received:", decoded)
            except:
                pass

async def send_frames():
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 30)

    async with websockets.connect("wss://afa2025.ddns.net:7000") as websocket:
        asyncio.create_task(receive_pong(websocket))
        asyncio.create_task(send_ping(websocket))

        while True:
            ret, frame = cap.read()
            if not ret:
                continue
            _, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 30])
            await websocket.send(jpeg.tobytes())
            await asyncio.sleep(1 / 120)

asyncio.run(send_frames())
