import asyncio
import json
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer
from aiortc.sdp import candidate_from_sdp
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer

SIGNALING_URL = "wss://afa2025.ddns.net/ws"
SID = "test-room"
UID = "raspi"

async def run():
    print("Starting WebRTC publisher...")

    # TURN/STUN 서버 설정
    ice_servers = [
        RTCIceServer(
            urls="turn:afa2025.ddns.net:3478?transport=udp",
            username="pion",
            credential="ion"
        ),
        RTCIceServer(
            urls="stun:stun.l.google.com:19302"
        )
    ]

    # RTCPeerConnection 생성
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))

    print("Initializing MediaPlayer with /dev/video0")
    player = MediaPlayer(
        "/dev/video0",
        format="v4l2",
        options={
            "video_size": "640x480",
            "framerate": "30",
            "input_format": "yuyv422"
        }
    )

    print("Adding video track to peer connection")
    if player.video:
        pc.addTrack(player.video)
    else:
        print("No video track found in MediaPlayer")

    print("Created local SDP offer")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    print(f"Connecting to signaling server at {SIGNALING_URL}")
    async with websockets.connect(SIGNALING_URL) as ws:
        print("Connected to signaling server")

        print("Sent join request with SDP offer")
        await ws.send(json.dumps({
            "id": 1,
            "method": "join",
            "params": {
                "sid": SID,
                "uid": UID,
                "offer": {
                    "type": "offer",
                    "sdp": pc.localDescription.sdp
                }
            }
        }))

        async def send_trickle(candidate, target):
            await ws.send(json.dumps({
                "id": 2,
                "method": "trickle",
                "params": {
                    "candidate": {
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdpMid,
                        "sdpMLineIndex": candidate.sdpMLineIndex
                    },
                    "target": target
                }
            }))

        @pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate:
                await send_trickle(candidate, 0)

        while True:
            message = await ws.recv()
            print("Received signaling message:", message)
            msg = json.loads(message)

            if "result" in msg and "sdp" in msg["result"]:
                answer = msg["result"]
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
                )
                print("Set remote SDP answer")

            elif msg.get("method") == "trickle":
                try:
                    c = msg["params"]["candidate"]
                    if c["candidate"]:
                        candidate = candidate_from_sdp(c["candidate"])
                        candidate.sdpMid = c.get("sdpMid", "")
                        candidate.sdpMLineIndex = c.get("sdpMLineIndex", 0)
                        await pc.addIceCandidate(candidate)
                        print("Added remote ICE candidate")
                except Exception as e:
                    print("Error adding ICE candidate:", e)

asyncio.run(run())
