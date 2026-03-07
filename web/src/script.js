let cameraLastFrameAt = 0;
let cameraFramesInWindow = 0;
let cameraFpsWindowStartedAt = Date.now();

function ensureCameraPanel() {
  if (document.getElementById('camera-panel')) return;

  const container = document.getElementById('container');
  if (!container) return;

  const article = document.createElement('article');
  article.id = 'camera-panel';
  article.innerHTML = `
    <h1><i class="fa-solid fa-fw fa-camera"></i>&ensp;camera</h1>
    <div class="nav-divider"></div>
    <div class="content" style="padding-top: 0.75rem;">
      <div style="position: relative; border-radius: 14px; overflow: hidden; background: #020617; border: 1px solid rgba(255,255,255,0.12); min-height: 220px;">
        <video id="camera-fallback-video" autoplay muted loop playsinline preload="auto" style="display: block; width: 100%; height: 100%; object-fit: cover; min-height: 220px;">
          <source src="assets/race_sim.mp4" type="video/mp4" />
        </video>
        <img id="camera-feed" alt="camera" style="display: none; width: 100%; height: 100%; object-fit: cover; min-height: 220px;" />
        <canvas id="camera-random-fx" style="display:none; position:absolute; inset:0; width:100%; height:100%; pointer-events:none;"></canvas>
        <div id="camera-overlay" style="position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #cbd5e1; background: rgba(2,6,23,0.72); font-weight: 700; letter-spacing: 0.06em;">NO SIGNAL</div>
      </div>
      <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 0.7rem; color: #cbd5e1; font-size: 0.9rem;">
        <span id="camera-status">offline</span>
        <span id="camera-fps">0 fps</span>
      </div>
    </div>
  `;

  container.prepend(article);
  setCameraFallbackMode();
}


let cameraRandomFxTimer = null;
let cameraRandomFxTick = 0;

function drawRandomCameraFxFrame() {
  const panel = document.querySelector('#camera-panel .content > div:first-child');
  const cv = document.getElementById('camera-random-fx');
  if (!panel || !cv) return;

  const w = Math.max(2, Math.floor(panel.clientWidth));
  const h = Math.max(2, Math.floor(panel.clientHeight));
  if (cv.width !== w || cv.height !== h) {
    cv.width = w;
    cv.height = h;
  }

  const ctx = cv.getContext('2d');
  if (!ctx) return;

  cameraRandomFxTick += 1;

  ctx.clearRect(0, 0, w, h);

  // translucent sweep
  const gx = (cameraRandomFxTick * 13) % (w + 240) - 240;
  const grad = ctx.createLinearGradient(gx, 0, gx + 240, 0);
  grad.addColorStop(0, 'rgba(0,255,255,0)');
  grad.addColorStop(0.5, 'rgba(0,255,255,0.14)');
  grad.addColorStop(1, 'rgba(0,255,255,0)');
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, w, h);

  // random dots + boxes
  for (let i = 0; i < 12; i += 1) {
    const x = Math.random() * w;
    const y = Math.random() * h;
    const r = 2 + Math.random() * 5;
    ctx.fillStyle = `rgba(${80 + Math.floor(Math.random() * 170)}, ${80 + Math.floor(Math.random() * 170)}, 255, 0.55)`;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
  }

  for (let i = 0; i < 4; i += 1) {
    const x = Math.random() * (w - 80);
    const y = Math.random() * (h - 30);
    const rw = 30 + Math.random() * 90;
    const rh = 12 + Math.random() * 26;
    ctx.strokeStyle = 'rgba(255, 80, 80, 0.5)';
    ctx.lineWidth = 1.5;
    ctx.strokeRect(x, y, rw, rh);
  }

  // test tag
  ctx.fillStyle = 'rgba(255,255,255,0.85)';
  ctx.font = '700 20px Arial';
  ctx.textAlign = 'left';
  ctx.fillText('RANDOM TEST FEED', 16, 30);
}

function startRandomCameraFx() {
  if (cameraRandomFxTimer) return;
  drawRandomCameraFxFrame();
  cameraRandomFxTimer = setInterval(drawRandomCameraFxFrame, 120);
}

function stopRandomCameraFx() {
  if (!cameraRandomFxTimer) return;
  clearInterval(cameraRandomFxTimer);
  cameraRandomFxTimer = null;
}
function setCameraOffline() {
  const status = document.getElementById('camera-status');
  setCameraFallbackMode();
  if (status) status.textContent = 'offline';
}

function setCameraFallbackMode() {
  const fallback = document.getElementById('camera-fallback-video');
  const img = document.getElementById('camera-feed');
  const overlay = document.getElementById('camera-overlay');
  if (fallback) {
    fallback.style.display = 'block';
    const p = fallback.play();
    if (p && typeof p.catch === 'function') p.catch(() => {});
  }
  if (img) img.style.display = 'none';
  if (overlay) overlay.style.display = 'none';
}

function setCameraLiveMode() {
  const fallback = document.getElementById('camera-fallback-video');
  const img = document.getElementById('camera-feed');
  const overlay = document.getElementById('camera-overlay');
  if (fallback) fallback.style.display = 'none';
  if (img) img.style.display = 'block';
  if (overlay) overlay.style.display = 'none';
}

function renderCameraFrame(payload) {
  ensureCameraPanel();
  dockVehicleHudIntoCamera();

  const img = document.getElementById('camera-feed');
  const status = document.getElementById('camera-status');
  const fps = document.getElementById('camera-fps');
  if (!img) return;

  const frame = payload?.frame ?? payload?.data ?? payload;
  if (typeof frame !== 'string' || !frame.length) return;

  const mime = (typeof payload?.mime === 'string' && payload.mime.length) ? payload.mime : 'image/jpeg';
  const src = frame.startsWith('data:') ? frame : `data:${mime};base64,${frame}`;

  img.src = src;
  cameraLastFrameAt = Date.now();
  cameraFramesInWindow += 1;

  setCameraLiveMode();
  if (status) {
    status.textContent = payload?.width && payload?.height
      ? `online ${payload.width}x${payload.height}`
      : 'online';
  }

  const now = Date.now();
  if (now - cameraFpsWindowStartedAt >= 1000) {
    if (fps) fps.textContent = `${cameraFramesInWindow} fps`;
    cameraFramesInWindow = 0;
    cameraFpsWindowStartedAt = now;
  }
}

function startCameraWatchdog() {
  setInterval(() => {
    if (!cameraLastFrameAt) {
      setCameraOffline();
      return;
    }

    if (Date.now() - cameraLastFrameAt > 3000) {
      setCameraOffline();
    }
  }, 1000);
}
function dockVehicleHudIntoCamera() {
  const cameraPanel = document.getElementById('camera-panel');
  const car = document.getElementById('car');
  if (!cameraPanel || !car) return;

  const cameraContent = cameraPanel.querySelector('.content');
  if (!cameraContent) return;

  if (car.dataset.hudBuilt === '1') return;

  const speedContainer = car.querySelector('.speedometer-container');
  const accelContainer = car.querySelector('.accel-container');
  if (!speedContainer || !accelContainer) return;

  const hudRoot = document.createElement('div');
  hudRoot.id = 'camera-hud-root';

  const vehicleColumn = document.createElement('div');
  vehicleColumn.className = 'hud-vehicle-column';
  const vehicleTitle = document.createElement('div');
  vehicleTitle.className = 'hud-title';
  vehicleTitle.textContent = 'vehicle';
  vehicleColumn.appendChild(vehicleTitle);

  for (const block of Array.from(car.querySelectorAll('.fault-indicator'))) {
    vehicleColumn.appendChild(block);
  }

  const accelPanel = document.createElement('div');
  accelPanel.className = 'hud-accel-panel';
  accelPanel.innerHTML = '<div class="hud-title">accel</div>';
  accelPanel.appendChild(accelContainer);
  const accelValues = document.createElement('div');
  accelValues.className = 'hud-accel-values';
  accelValues.innerHTML = '<div>x <span id="hud-accel-x">0.0</span> g</div><div>y <span id="hud-accel-y">0.0</span> g</div><div>z <span id="hud-accel-z">0.0</span> g</div>';
  accelPanel.appendChild(accelValues);

  const speedPanel = document.createElement('div');
  speedPanel.className = 'hud-speed-panel';
  speedPanel.innerHTML = '<div class="hud-title">speed</div>';
  speedPanel.appendChild(speedContainer);

  const metaPanel = document.createElement('div');
  metaPanel.className = 'hud-meta-panel';
  metaPanel.innerHTML = '<span id="hud-uptime">-</span><br>ECU temp: <span id="hud-core-temp">0.0</span> C';

  hudRoot.appendChild(vehicleColumn);
  hudRoot.appendChild(accelPanel);
  hudRoot.appendChild(speedPanel);
  hudRoot.appendChild(metaPanel);

  cameraContent.appendChild(hudRoot);

  car.style.display = 'none';
  car.dataset.hudBuilt = '1';
}
function updateCameraHudValues(status) {
  const hx = document.getElementById('hud-accel-x');
  const hy = document.getElementById('hud-accel-y');
  const hz = document.getElementById('hud-accel-z');
  const htemp = document.getElementById('hud-core-temp');
  if (hx) hx.textContent = parseFloat(status.car.accel2.accel2_x).toFixed(1);
  if (hy) hy.textContent = parseFloat(status.car.accel2.accel2_y).toFixed(1);
  if (hz) hz.textContent = parseFloat(status.car.accel2.accel2_z).toFixed(1);
  if (htemp) htemp.textContent = parseFloat(status.temperature).toFixed(1);
}

let hudRecorder = null;
let hudChunks = [];
let hudRenderTimer = null;
let hudRecordCanvas = null;
let hudRecordCtx = null;

function ensureHudRecordingControls() {
  if (document.getElementById('start-hud-recording')) return;

  const sidebarFooter = document.querySelector('.sidebar-footer');
  if (!sidebarFooter) return;

  const wrap = document.createElement('div');
  wrap.id = 'hud-record-control';
  wrap.style.marginTop = '10px';
  wrap.innerHTML = `
    <button id="start-hud-recording" style="display:block; width:100%; margin-bottom:6px;">HUD REC START</button>
    <button id="stop-hud-recording" style="display:block; width:100%; margin-bottom:6px;" disabled>HUD REC STOP</button>
    <div id="hud-record-download" style="font-size:12px; word-break:break-all;"></div>
  `;
  sidebarFooter.insertBefore(wrap, sidebarFooter.firstChild);

  document.getElementById('start-hud-recording').addEventListener('click', startHudRecording);
  document.getElementById('stop-hud-recording').addEventListener('click', stopHudRecording);
}

function getRelRect(target, base) {
  if (!target || !base) return null;
  const tr = target.getBoundingClientRect();
  const br = base.getBoundingClientRect();
  return {
    x: tr.left - br.left,
    y: tr.top - br.top,
    w: tr.width,
    h: tr.height,
  };
}

function drawHudRecordingFrame() {
  const panel = document.querySelector('#camera-panel .content > div:first-child');
  const feed = document.getElementById('camera-feed');
  const fallbackVideo = document.getElementById('camera-fallback-video');
  const overlay = document.getElementById('camera-overlay');
  if (!panel || !hudRecordCtx || !hudRecordCanvas) return;

  const width = Math.max(2, Math.floor(panel.clientWidth));
  const height = Math.max(2, Math.floor(panel.clientHeight));
  if (hudRecordCanvas.width !== width || hudRecordCanvas.height !== height) {
    hudRecordCanvas.width = width;
    hudRecordCanvas.height = height;
  }

  const ctx = hudRecordCtx;
  ctx.fillStyle = '#020617';
  ctx.fillRect(0, 0, width, height);

  let drewCameraSource = false;
  if (feed && feed.complete && feed.naturalWidth > 0) {
    ctx.drawImage(feed, 0, 0, width, height);
    drewCameraSource = true;
  } else if (
    fallbackVideo &&
    fallbackVideo.readyState >= 2 &&
    getComputedStyle(fallbackVideo).display !== 'none'
  ) {
    ctx.drawImage(fallbackVideo, 0, 0, width, height);
    drewCameraSource = true;
  }

  const fxCanvas = document.getElementById('camera-random-fx');
  if (fxCanvas && fxCanvas.width > 0 && fxCanvas.height > 0) {
    ctx.drawImage(fxCanvas, 0, 0, width, height);
  }

  if (!drewCameraSource && overlay && overlay.style.display !== 'none') {
    ctx.fillStyle = 'rgba(2,6,23,0.65)';
    ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = '#cbd5e1';
    ctx.font = '700 36px Arial';
    ctx.textAlign = 'center';
    ctx.fillText('NO SIGNAL', width / 2, height / 2);
  }

  const accelCanvas = document.getElementById('accelCanvas');
  const speedCanvas = document.getElementById('speedometer');
  const accelPanel = document.querySelector('#camera-hud-root .hud-accel-panel');
  const speedPanel = document.querySelector('#camera-hud-root .hud-speed-panel');
  const vehicleCol = document.querySelector('#camera-hud-root .hud-vehicle-column');
  const metaPanel = document.querySelector('#camera-hud-root .hud-meta-panel');

  if (accelCanvas && accelPanel) {
    const ar = getRelRect(accelPanel, panel);
    if (ar) {
      const drawW = 220;
      const drawH = 220;
      ctx.drawImage(accelCanvas, ar.x, ar.y + 28, drawW, drawH);
      ctx.fillStyle = '#ffffff';
      ctx.font = '700 34px Arial';
      ctx.textAlign = 'left';
      const hx = document.getElementById('hud-accel-x')?.textContent || '0.0';
      const hy = document.getElementById('hud-accel-y')?.textContent || '0.0';
      const hz = document.getElementById('hud-accel-z')?.textContent || '0.0';
      ctx.fillText(`x ${hx} g`, ar.x, ar.y + 286);
      ctx.fillText(`y ${hy} g`, ar.x, ar.y + 330);
      ctx.fillText(`z ${hz} g`, ar.x, ar.y + 374);
    }
  }

  if (speedCanvas && speedPanel) {
    const sr = getRelRect(speedPanel, panel);
    if (sr) {
      const sw = 330;
      const sh = 165;
      ctx.drawImage(speedCanvas, sr.x, sr.y + 22, sw, sh);
    }
  }

  if (vehicleCol) {
    const vr = getRelRect(vehicleCol, panel);
    if (vr) {
      ctx.fillStyle = '#ffffff';
      ctx.font = '700 30px Arial';
      ctx.textAlign = 'left';
      let y = vr.y + 28;
      const items = vehicleCol.querySelectorAll('.status .text');
      const icons = vehicleCol.querySelectorAll('.status i');
      for (let i = 0; i < items.length; i += 1) {
        const label = items[i].textContent || '';
        const iconColor = icons[i] ? getComputedStyle(icons[i]).color : 'rgb(255,0,0)';
        ctx.fillStyle = iconColor;
        ctx.beginPath();
        ctx.arc(vr.x + 10, y - 8, 6, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#ffffff';
        ctx.fillText(label, vr.x + 26, y);
        y += 38;
      }
    }
  }

  if (metaPanel) {
    const mr = getRelRect(metaPanel, panel);
    if (mr) {
      ctx.fillStyle = '#ffffff';
      ctx.font = '700 28px Arial';
      ctx.textAlign = 'left';
      const t = document.getElementById('hud-uptime')?.textContent || '-';
      const temp = document.getElementById('hud-core-temp')?.textContent || '0.0';
      ctx.fillText(`${t}`, mr.x, mr.y + 24);
      ctx.fillText(`ECU temp: ${temp} C`, mr.x, mr.y + 60);
    }
  }
}

function startHudRecording() {
  if (hudRecorder) return;

  ensureCameraPanel();
  dockVehicleHudIntoCamera();

  hudRecordCanvas = document.createElement('canvas');
  hudRecordCtx = hudRecordCanvas.getContext('2d');
  drawHudRecordingFrame();

  const stream = hudRecordCanvas.captureStream(30);
  let mimeType = '';
  if (MediaRecorder.isTypeSupported('video/webm;codecs=vp9')) mimeType = 'video/webm;codecs=vp9';
  else if (MediaRecorder.isTypeSupported('video/webm;codecs=vp8')) mimeType = 'video/webm;codecs=vp8';
  else mimeType = 'video/webm';

  hudChunks = [];
  hudRecorder = new MediaRecorder(stream, { mimeType });
  hudRecorder.ondataavailable = e => {
    if (e.data && e.data.size > 0) hudChunks.push(e.data);
  };
  hudRecorder.onstop = () => {
    const blob = new Blob(hudChunks, { type: mimeType || 'video/webm' });
    const url = URL.createObjectURL(blob);
    const name = `hud-${new Date().toISOString().replace(/[:.]/g, '-')}.webm`;
    const dl = document.getElementById('hud-record-download');
    if (dl) dl.innerHTML = `<a href="${url}" download="${name}">HUD DOWNLOAD</a>`;
  };

  hudRecorder.start(1000);
  hudRenderTimer = setInterval(drawHudRecordingFrame, 33);

  const startBtn = document.getElementById('start-hud-recording');
  const stopBtn = document.getElementById('stop-hud-recording');
  if (startBtn) startBtn.disabled = true;
  if (stopBtn) stopBtn.disabled = false;
}

function stopHudRecording() {
  if (!hudRecorder) return;
  hudRecorder.stop();
  hudRecorder = null;

  if (hudRenderTimer) {
    clearInterval(hudRenderTimer);
    hudRenderTimer = null;
  }

  const startBtn = document.getElementById('start-hud-recording');
  const stopBtn = document.getElementById('stop-hud-recording');
  if (startBtn) startBtn.disabled = false;
  if (stopBtn) stopBtn.disabled = true;
}
if (typeof io === 'undefined') {
  Swal.fire({
    icon: 'error',
    title: '서버 응답 없음',
    html: `텔레 서버가 응답하지 않습니다.`
  });
} else {
  const socket = io.connect("/", { query: { client: true, channel: "afa", key: 1234} });
  ensureCameraPanel();
  dockVehicleHudIntoCamera();
  startCameraWatchdog();
  ensureHudRecordingControls();
  setTimeout(() => {
    if (!lastRealReportAt) startRandomTelemetryTest();
  }, 2500);
  // on socket lost
  socket.on('connect_error', () => {
    $("#server i").css("color", "red");
  });

  // on client connected
  socket.on('client_connected', data => {
    $("#server i").css("color", "green");
     process_status(data.status);
     telemetry = data.status;  // set telemetry
  });

  socket.on('device_connected', data => {
  
    Swal.fire({
      title: '서버 연결 성공',
      text: data.message,
      icon: 'success',
      confirmButtonText: 'OK'
    });
    $("#telemetry i").css("color", "green");
  });
  socket.on('socket-lost', data => {
    $("#telemetry i").css("color", "red");
    data.telemetry = 0;
    setCameraOffline();
    Swal.fire({
      title: 'SERVER CONNECTION ERROR',
      text: data.message,
      icon: 'error',
      confirmButtonText: 'OK'
    });
  });
    
  $(document).ready(function() {
    if (localStorage.getItem("logRecording") === "true") {
      $("#start-log-recording").text("LOG STOP").addClass("stop");
      const filename = localStorage.getItem("logFilename");
      if (filename) {
        socket.emit("start_log_recording", { filename: filename });
      }
    }
  });
  
  $("#start-log-recording").on("click", function() {
  const btn = $(this);
  if (btn.text().trim() === "LOG START") {
    Swal.fire({
      title: 'Enter log filename',
      input: 'text',
      inputLabel: 'Type the log filename.',
      inputPlaceholder: 'e.g. test'
    }).then(result => {
      if (result.isConfirmed && result.value.trim() !== "") {
        const filename = result.value.trim();
        localStorage.setItem("logRecording", "true");
        localStorage.setItem("logFilename", filename);
        socket.emit("start_log_recording", { filename: filename });
        btn.text("LOG STOP").addClass("stop");
        Swal.fire({
          icon: "success",
          title: "Log recording started",
          text: `Recording to "${filename}.log"`
        });
      }
    });
  } else {
    socket.emit("stop_log_recording");
    localStorage.removeItem("logRecording");
    localStorage.removeItem("logFilename");
    btn.text("LOG START").removeClass("stop");
    Swal.fire({
      icon: "info",
      title: "Log recording stopped",
      text: "Standalone log recording stopped."
    });
  }
});

// 1) 기록 시작 버튼
$('#start-excel-recording').on('click', () => {
  socket.emit('start_recording', { intervalSec: 0.005 }); // 200Hz (5ms)
  $('#start-excel-recording').prop('disabled', true);
  $('#stop-excel-recording').prop('disabled', false);
  $('#excel-download').empty();
});

// 2) 기록 중지 버튼
$('#stop-excel-recording').on('click', () => {
  socket.emit('stop_recording');
  $('#stop-excel-recording').prop('disabled', true);
});

// 3) 서버에서 기록 시작 확인
socket.on('recording_started', ({ targetHz } = {}) => {
  Swal.fire({ icon: 'success', title: 'DATA RECORDING STARTED', timer: 1000 });
});

// 4) 서버에서 기록 중지 및 파일 생성 알림
socket.on('recording_stopped', ({ file, stats }) => {
  // file ? e.g. "/recorded/recording-<id>-<timestamp>.xlsx"
  const link = $(`<a href="${file}" download>DOWNLOAD XLSX</a>`);
  $('#excel-download').empty().append(link);
  $('#start-excel-recording').prop('disabled', false);
  const hzInfo = stats?.actualHz ? ` (${stats.actualHz}Hz)` : '';
  Swal.fire({ icon: 'success', title: `XLSX READY${hzInfo}` });
});

// 5) 오류 처리
socket.on('error', msg => {
  Swal.fire({ icon: 'error', title: 'ERROR', text: msg });
});

// 여기까지



  socket.on('camera_frame', frame => {
    renderCameraFrame(frame);
  });

  socket.on('sensor_update', packet => {
    if (packet?.status) {
      process_status(packet.status);
      telemetry = packet.status;
    }
  });
  // on data report update
  socket.on('report', data => {
    lastRealReportAt = Date.now();
    stopRandomTelemetryTest();
    data.data.datetime = new Date();
    const formattedDateTime = formatKoreanDateTime(data.data.datetime);
    $("#uptime").text(data.data.timestamp);
    $("#hud-uptime").text(formattedDateTime);
    process_status(data.status);
    process_data(data.data);
    updateSpeed(data.status.car.speed);

    if (isLiveCANTrafficOn && data.data.source == "CAN") {
      let item = liveCANTrafficData.find(x => x.id === data.data.key);
      if (item) {
        item.byte0 = data.data.raw[0];
        item.byte1 = data.data.raw[1];
        item.byte2 = data.data.raw[2];
        item.byte3 = data.data.raw[3];
        item.byte4 = data.data.raw[4];
        item.byte5 = data.data.raw[5];
        item.byte6 = data.data.raw[6];
        item.byte7 = data.data.raw[7];
        item.cnt++;
        item.index = item.index;
        item.interval = new Date(data.data.datetime) - new Date(item.timestamp);
        item.timestamp = data.data.datetime;
        $('#can_table').DataTable().row(item.index).data(item);
      } else if (data.data.key) {
        const item = {
          id: data.data.key,
          byte0: data.data.raw[0],
          byte1: data.data.raw[1],
          byte2: data.data.raw[2],
          byte3: data.data.raw[3],
          byte4: data.data.raw[4],
          byte5: data.data.raw[5],
          byte6: data.data.raw[6],
          byte7: data.data.raw[7],
          cnt: 0,
          index: CAN_index++,
          timestamp: data.data.datetime,
          interval: 0
        };
        liveCANTrafficData.push(item);
        $('#can_table').DataTable().row.add(item).draw();
      }
    }
    telemetry = data.status;
  });

  // button handlers
  $("#reset").on("click", e => {
    socket.emit('reset-request');
  });

  socket.on('reset-reply', data => {
    Swal.fire(data).then(result => {
      if (result.isConfirmed) socket.emit('reset-confirm');
    });
  });
}

let telemetry = {};
let currentSpeed = 0;
let displayedSpeed = 0;
let accel = 0;
let brake = 0;
let randomTelemetryTimer = null;
let randomTelemetryTick = 0;
let lastRealReportAt = 0;

function startRandomTelemetryTest() {
  if (randomTelemetryTimer) return;

  randomTelemetryTimer = setInterval(() => {
    randomTelemetryTick += 1;
    const t = randomTelemetryTick / 12;

    // speed: smooth wave + noise
    const speed = Math.max(0, Math.min(120, 42 + 34 * Math.sin(t / 2.2) + (Math.random() - 0.5) * 10));

    // accel: bounded random-ish motion around 0
    const ax = Math.max(-2.5, Math.min(2.5, 1.2 * Math.sin(t * 0.9) + (Math.random() - 0.5) * 0.5));
    const ay = Math.max(-2.5, Math.min(2.5, 1.1 * Math.cos(t * 0.8) + (Math.random() - 0.5) * 0.5));
    const az = Math.max(-2.5, Math.min(2.5, 0.2 + 0.4 * Math.sin(t * 0.7) + (Math.random() - 0.5) * 0.25));

    const now = Date.now();

    const speedEl = document.getElementById('speed');
    if (speedEl) speedEl.textContent = speed.toFixed(0);

    const xEl = document.getElementById('accel2-x');
    const yEl = document.getElementById('accel2-y');
    const zEl = document.getElementById('accel2-z');
    if (xEl) xEl.textContent = ax.toFixed(1);
    if (yEl) yEl.textContent = ay.toFixed(1);
    if (zEl) zEl.textContent = az.toFixed(1);

    const hx = document.getElementById('hud-accel-x');
    const hy = document.getElementById('hud-accel-y');
    const hz = document.getElementById('hud-accel-z');
    if (hx) hx.textContent = ax.toFixed(1);
    if (hy) hy.textContent = ay.toFixed(1);
    if (hz) hz.textContent = az.toFixed(1);

    const up = document.getElementById('uptime');
    const hup = document.getElementById('hud-uptime');
    const formattedNow = formatKoreanDateTime(now);
    if (up) up.textContent = String(now);
    if (hup) hup.textContent = formattedNow;

    const wsBase = speed * 7.5;
    const wsLeft = Math.max(0, wsBase + (Math.random() - 0.5) * 20);
    const wsRight = Math.max(0, wsBase + (Math.random() - 0.5) * 20);
    const wsLeftEl = document.getElementById('wheel-speed-left');
    const wsRightEl = document.getElementById('wheel-speed-right');
    if (wsLeftEl) wsLeftEl.textContent = wsLeft.toFixed(0);
    if (wsRightEl) wsRightEl.textContent = wsRight.toFixed(0);

    updateSpeed(speed);
  }, 120);
}

function stopRandomTelemetryTest() {
  if (!randomTelemetryTimer) return;
  clearInterval(randomTelemetryTimer);
  randomTelemetryTimer = null;
}

function formatKoreanDateTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  const ss = String(date.getSeconds()).padStart(2, '0');
  return `${y}년 ${m}월 ${d}일 ${hh}:${mm}:${ss}`;
}

function updateSpeed(speed) {
  currentSpeed = speed;
  requestAnimationFrame(animateSpeed);
}

function animateSpeed() {
  const diff = currentSpeed - displayedSpeed;
  if (Math.abs(diff) > 0.1) {
    displayedSpeed += diff * 0.1; // 애니메이션 속도 조절
    drawSpeedometer(displayedSpeed);
    requestAnimationFrame(animateSpeed);
  } else {
    displayedSpeed = currentSpeed;
    drawSpeedometer(displayedSpeed);
  }
}

window.onload = function() {
  const canvas = document.getElementById('speedometer');
  drawSpeedometer(0);
};

function drawAcceleration() {
  const canvas = document.getElementById('accelCanvas');
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  const width = canvas.width;
  const height = canvas.height;

  ctx.clearRect(0, 0, width, height);

  // 理쒕? g 踰붿쐞 ?ㅼ젙 (짹5g)
  const maxG = 2.5;

  // 罹붾쾭???덈컲 ?ш린 湲곗??쇰줈 ?ㅼ????먮룞 怨꾩궛
  const scaleX = (width / 2) / maxG;
  const scaleY = (height / 2) / maxG;

  // 寃⑹옄??洹몃━湲?
  ctx.strokeStyle = '#444';  // 寃⑹옄????
  ctx.lineWidth = 0.5;

  // ?섏쭅 寃⑹옄??(x異?湲곗?)
  for (let i = -maxG; i <= maxG; i++) {
    const x = width / 2 + i * scaleX;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  }

  // ?섑룊 寃⑹옄??(y異?湲곗?)
  for (let i = -maxG; i <= maxG; i++) {
    const y = height / 2 - i * scaleY;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }

  //  異?洹몃━湲?(援듦쾶)
  ctx.strokeStyle = 'gray';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, height / 2);
  ctx.lineTo(width, height / 2);
  ctx.moveTo(width / 2, 0);
  ctx.lineTo(width / 2, height);
  ctx.stroke();
  ctx.closePath();

  // 가속도 값 가져오기
  const ax = parseFloat(document.getElementById('accel2-x').innerText) || 0;
  const ay = parseFloat(document.getElementById('accel2-y').innerText) || 0;

  // ???꾩튂 怨꾩궛
  const dotX = width / 2 + ax * scaleX;
  const dotY = height / 2 - ay * scaleY;

  // ??洹몃━湲?
  ctx.fillStyle = 'red';
  ctx.beginPath();
  ctx.arc(dotX, dotY, 5, 0, 2 * Math.PI);
  ctx.fill();
  ctx.closePath();

  // ?띿뒪???쒖떆
  ctx.font = '16px Arial';
  ctx.fillStyle = 'white';
  ctx.textAlign = 'center';
  ctx.fillText(`(${ax.toFixed(2)}, ${ay.toFixed(2)})`, dotX, dotY - 15);

  requestAnimationFrame(drawAcceleration);
}


// 珥덇린 ?ㅽ뻾
drawAcceleration();


function drawSpeedometer(speed) {
  const canvas = document.getElementById('speedometer');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  const centerX = canvas.width / 2;
  const radius = canvas.width / 2 - 10;
  const centerY = canvas.height; 

  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // 諛섏썝 ?뚮몢由?
  ctx.beginPath();
  ctx.arc(centerX, centerY, radius, Math.PI, 2 * Math.PI); // 諛섏썝
  ctx.lineWidth = 20;
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.1)';
  ctx.stroke();
  ctx.closePath();

  // ?덇툑怨??レ옄 (0~120)
  ctx.font = '14px Arial';
  ctx.fillStyle = 'white';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';

  for (let i = 0; i <= 120; i += 10) {
    const angle = (i / 120) * Math.PI + Math.PI; // 180??~ 360??
    const x1 = centerX + (radius - 10) * Math.cos(angle);
    const y1 = centerY + (radius - 10) * Math.sin(angle);
    const x2 = centerX + radius * Math.cos(angle);
    const y2 = centerY + radius * Math.sin(angle);

    // ?덇툑 ??
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.lineWidth = 2;
    ctx.strokeStyle = 'white';
    ctx.stroke();
    ctx.closePath();

    // ?レ옄
    const textX = centerX + (radius - 30) * Math.cos(angle);
    const textY = centerY + (radius - 30) * Math.sin(angle);
    ctx.fillText(i, textX, textY);
  }

  // 속도 제한 (최대 120)
  const clampedSpeed = Math.min(speed, 120);
  const speedAngle = (clampedSpeed / 120) * Math.PI + Math.PI;

  // ?
  const pinX = centerX + (radius - 40) * Math.cos(speedAngle);
  const pinY = centerY + (radius - 40) * Math.sin(speedAngle);

  ctx.beginPath();
  ctx.moveTo(centerX, centerY);
  ctx.lineTo(pinX, pinY);
  ctx.lineWidth = 6;
  ctx.strokeStyle = '#FF0000';
  ctx.stroke();
  ctx.closePath();

  // 寃뚯씠吏 諛?
  const startAngle = Math.PI;
  const endAngle = speedAngle;
  
  ctx.beginPath();
  ctx.arc(centerX, centerY, radius , startAngle, endAngle, false);
  ctx.lineWidth = 12;
  ctx.strokeStyle = 'rgba(169, 223, 216, 0.4)';
  ctx.stroke();
  ctx.closePath();

  //ctx.beginPath();
  //ctx.arc(centerX, centerY, radius - 30, startAngle, endAngle, false);
  //ctx.lineWidth = 10;
  //ctx.strokeStyle = '#A9DFD8';
  //ctx.stroke();
  //ctx.closePath();

  // 속도 숫자 표시
  ctx.font = 'bold 48px Arial';
  ctx.fillStyle = 'white';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(clampedSpeed.toFixed(0), centerX, centerY - 20);
  
  ctx.font = '20px Arial';
  ctx.textAlign = 'left';  
  ctx.fillText('km/h', centerX + 30, centerY - 20);
}
function process_status(status) {
  console.log("Received status:", status);
  $("#telemetry i").css("color", status.telemetry ? "green" : "red");
  $("#lv i").css("color", status.car.system.ESP ? "green" : "red");
  $("#hv i").css("color", status.car.system.HV ? "green" : "red");
  $("#rtd i").css("color", status.car.system.RTD ? "green" : "red");

  $("#sd i").css("color", status.car.system.SD ? "green" : "red");
  $("#can i").css("color", status.car.system.CAN ? "green" : "red");
  $("#acc i").css("color", status.car.system.ACC ? "green" : "red");
  $("#lcd i").css("color", status.car.system.LCD ? "green" : "red");
  $("#gps i").css("color", status.car.system.GPS ? "green" : "red");

  $("#imd i").css("color", status.car.system.IMD ? "green" : "red");
  $("#bms i").css("color", status.car.system.BMS ? "green" : "red");
  $("#bspd i").css("color", status.car.system.BSPD ? "green" : "red");

  $("#speed").text(status.car.speed.toFixed(0));
  $("#core-temperature").text((parseFloat(status.temperature)).toFixed(1));

  $("#voltage-failsafe i").css("color", status.bms.failsafe.voltage ? "red" : "green");
  $("#current-failsafe i").css("color", status.bms.failsafe.current ? "red" : "green");
  $("#relay-failsafe i").css("color", status.bms.failsafe.relay ? "red" : "green");
  $("#balancing-active i").css("color", status.bms.failsafe.balancing ? "green" : "red");
  $("#interlock-failsafe i").css("color", status.bms.failsafe.interlock ? "red" : "green");
  $("#thermistor-invalid i").css("color", status.bms.failsafe.thermistor ? "red" : "green");
  $("#input-power-failsafe i").css("color", status.bms.failsafe.power ? "red" : "green");

  $("#battery-percent").text(parseFloat(status.bms.charge).toFixed(1));
  $("#battery-voltage").text(parseFloat(status.bms.voltage).toFixed(1));
  $("#battery-current").text(parseFloat(status.bms.current).toFixed(1));
  $("#battery-temperature-max").text(parseFloat(status.bms.temperature.max.value).toFixed(0));
  $("#battery-temperature-max-id").text(status.bms.temperature.max.id);
  $("#battery-capacity").text(parseFloat(status.bms.capacity).toFixed(1));
  $("#dcl").text(parseFloat(status.bms.dcl));

  $("#vsm-status-indicator").css('color', status.inverter.fault.post.length + status.inverter.fault.run.length ? "red" : "green");
  $("#vsm-status").text(status.inverter.state.vsm_state);
  $("#inverter-status").text(status.inverter.state.inverter_state);
  $("#inv_mode i").removeClass("fa-square-x").removeClass("fa-square-t").removeClass("fa-square-s")
    .addClass(`fa-square-${status.inverter.state.mode === "토크 모드" ? 't' : 's' }`).css("color", "green");
  $("#rpm").text(status.inverter.motor.speed);
  $("#motor-torque").text(status.inverter.torque.feedback.toFixed(1));
  $("#motor-temperature").text(status.inverter.temperature.motor.toFixed(0));
  $("#motor-coolant").text(status.inverter.temperature.rtd.rtd1.toFixed(0));
  $("#motor-igbt-temperature").text(status.inverter.temperature.igbt.max.temperature.toFixed(0));
  $("#motor-igbt-temperature-id").text(status.inverter.temperature.igbt.max.id);
  $("#precharge i").css("color", status.inverter.state.relay.precharge ? "green" : "red");
  $("#air i").css("color", status.inverter.state.relay.main ? "green" : "red");

  $("#accel-value").text(parseFloat(status.car.accel).toFixed(1));
  $("#brake-value").text(parseFloat(status.car.brake).toFixed(1));

  $("#steering-speed").text(parseFloat(status.car.steering.speed).toFixed(1));
  $("#steering-angle").text(parseFloat(status.car.steering.angle).toFixed(1));
  
  $("#linear-front-right").text(parseFloat(status.car.linear.front_right).toFixed(1));
  $("#linear-front-left").text(parseFloat(status.car.linear.front_left).toFixed(1));
  $("#linear-rear-right").text(parseFloat(status.car.linear.rear_right).toFixed(1));
  $("#linear-rear-left").text(parseFloat(status.car.linear.rear_left).toFixed(1));

  const wheelSpeed = status.car.wheel_speed || {};
  const leftWheelRpm = Number.isFinite(Number(wheelSpeed.LEFT))
    ? Number(wheelSpeed.LEFT)
    : ((Number(wheelSpeed.FL) || 0) + (Number(wheelSpeed.RL) || 0)) / 2;
  const rightWheelRpm = Number.isFinite(Number(wheelSpeed.RIGHT))
    ? Number(wheelSpeed.RIGHT)
    : ((Number(wheelSpeed.FR) || 0) + (Number(wheelSpeed.RR) || 0)) / 2;
  $("#wheel-speed-left").text(leftWheelRpm.toFixed(0));
  $("#wheel-speed-right").text(rightWheelRpm.toFixed(0));
  
   // 가속도
  $("#accel2-x").text(parseFloat(status.car.accel2.accel2_x).toFixed(1));
  $("#accel2-y").text(parseFloat(status.car.accel2.accel2_y).toFixed(1));
  $("#accel2-z").text(parseFloat(status.car.accel2.accel2_z).toFixed(1));
// 타이어온도
  $("#fronttie-temp").text(parseFloat(status.car.temp.front_tie).toFixed(1));
  $("#reartie-temp").text(parseFloat(status.car.temp.rear_tie).toFixed(1));

  updateCameraHudValues(status);

}

// GPS
let map = new kakao.maps.Map(document.getElementById('map'), {
  center: new kakao.maps.LatLng(37.2837709, 127.0434392)
});

let gps_path = [];
let gps_marker;
let gps_polyline = new kakao.maps.Polyline({
  path: gps_path,
  strokeWeight: 5,
  strokeColor: '#00C40D',
  strokeOpacity: 0.8,
  strokeStyle: 'solid'
});
gps_polyline.setMap(map);

// graph configs
// let graphs = {};
// let graph_data = {};
// for (const canvas of document.getElementsByTagName('canvas')) graph_data[canvas.id] = [];
// graph_data["graph-motor-torque-commanded"] = [];
//
// const graph_config = {
//   'graph-speed': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-accel': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//  'graph-braking': { delay: 0, grace: 5, color: 'rgb(255, 99, 132)' },
//   'graph-core-temperature': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-battery-percent': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-battery-voltage': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-battery-current': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-battery-temperature-max': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-battery-temperature-min': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-dcl': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-rpm': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-motor-torque': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-motor-temperature': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-motor-coolant': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-motor-igbt-temperature': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-inverter-temperature': { delay: 0, grace: 5, color: 'rgb(54, 162, 235)' },
//   'graph-steering-wheel-angle': { delay: 0, grace: 5, color: 'rgb(75, 192, 192)' }, // 異붽?
//   'graph-steering-wheel-speed': { delay: 0, grace: 5, color: 'rgb(153, 102, 255)' }, // 異붽?
//   'graph-linear-front-left': { delay: 0, grace: 5, color: 'rgb(255, 159, 64)' },
//   'graph-linear-front-right': { delay: 0, grace: 5, color: 'rgb(255, 205, 86)' },
//   'graph-linear-rear-left': { delay: 0, grace: 5, color: 'rgb(75, 192, 192)' },
//   'graph-linear-rear-right': { delay: 0, grace: 5, color: 'rgb(153, 102, 255)' },
//   'graph-tire-temp-rear': { delay: 0, grace: 5, color: 'rgb(255, 159, 64)' },
// 'graph-tire-temp-front': { delay: 0, grace: 5, color: 'rgb(255, 205, 86)' },
// 'graph-accel-x': { delay: 0, grace: 5, color: 'rgb(75, 192, 192)' },
// 'graph-accel-y': { delay: 0, grace: 5, color: 'rgb(153, 102, 255)' },
// 'graph-accel-z': { delay: 0, grace: 5, color: 'rgb(255, 159, 64)' },
//
// };
//
// // realtime graph updater
// function process_data(data) {
//   switch (data.source) {
//     case "CAN": {
//       switch (data.key) {
//         case "CAN_INV_TEMP_1":
//           graph_data['graph-motor-igbt-temperature'].push({
//             x: data.datetime,
//             y: data.parsed.igbt.max.temperature
//           });
//           break;
//
//         case "CAN_INV_TEMP_2":
//           break;
//
//         case "CAN_INV_TEMP_3":
//           graph_data['graph-motor-temperature'].push({
//             x: data.datetime,
//             y: data.parsed.motor
//           });
//           break;
//
//           case "CAN_INV_ANALOG_IN":
//             // graph_data['graph-accel'].push({
//             //   x: data.datetime,
//             //   y:data.parsed.AIN1
//             // });
//             // graph_data['graph-braking'].push({
//             //   x: data.datetime,
//             //   y:data.parsed.AIN3
//             // });
//             updateBarGraph('graph-accel', data.parsed.AIN1);
//             updateBarGraph('graph-braking', data.parsed.AIN3);
//             break;
//
//
//         case "CAN_INV_MOTOR_POS":
//           graph_data['graph-rpm'].push({
//             x: data.datetime,
//             y: data.parsed.motor_speed
//           });
//           graph_data['graph-speed'].push({
//             x: data.datetime,
//             y: 2 * Math.PI * 0.24765 * 60 * data.parsed.motor_speed / (1000 * 5.188235)
//           });
//           break;
//
//         case "CAN_INV_TORQUE":
//           graph_data['graph-motor-torque'].push({
//             x: data.datetime,
//             y: data.parsed.torque_feedback
//           });
//           graph_data["graph-motor-torque-commanded"].push({
//             x: data.datetime,
//             y: data.parsed.commanded_torque
//           });
//           break;
//
//         case "CAN_BMS_CORE":
//           graph_data['graph-battery-percent'].push({
//             x: data.datetime,
//             y: data.parsed.soc
//           });
//           graph_data['graph-battery-voltage'].push({
//             x: data.datetime,
//             y: data.parsed.voltage
//           });
//           graph_data['graph-battery-current'].push({
//             x: data.datetime,
//             y: data.parsed.current
//           });
//           break;
//
//         case "CAN_BMS_TEMP":
//           graph_data['graph-battery-temperature-max'].push({
//             x: data.datetime,
//             y: data.parsed.temperature.max.value
//           });
//           graph_data['graph-dcl'].push({
//             x: data.datetime,
//             y: data.parsed.dcl
//           });
//           break;
//
//         case "CAN_STEERING_WHEEL_ANGLE":
//           graph_data['graph-steering-wheel-angle'].push({
//             x: data.datetime,
//             y: data.parsed.angle
//           });
//           graph_data['graph-steering-wheel-speed'].push({
//             x: data.datetime,
//             y: data.parsed.speed
//           });
//           break;
//           case "CAN_FRONT_LINER_L":
//   graph_data['graph-linear-front-left'].push({
//     x: data.datetime,
//     y: data.parsed.lengthMM
//   });
//   break;
//
// case "CAN_FRONT_LINER_R":
//   graph_data['graph-linear-front-right'].push({
//     x: data.datetime,
//     y: data.parsed.lengthMM
//   });
//   break;
//
// case "CAN_REAR_LINER_L":
//   graph_data['graph-linear-rear-left'].push({
//     x: data.datetime,
//     y: data.parsed.lengthMM
//   });
//   break;
//
// case "CAN_REAR_LINER_R":
//   graph_data['graph-linear-rear-right'].push({
//     x: data.datetime,
//     y: data.parsed.lengthMM
//   });
//   break;
//
//    case "CAN_FRONTTIE_TEMP":
//   graph_data['graph-tire-temp-front'].push({
//     x: data.datetime,
//     y: data.parsed.lengthMM
//   });
//   break;
//
// case "CAN_REARTIE_TEMP":
//   graph_data['graph-tire-temp-rear'].push({
//     x: data.datetime,
//     y: data.parsed.lengthMM
//   });
//
//   break;
//
// case "CAN_ACCEL":
//   graph_data['graph-accel-x'].push({
//     x: data.datetime,
//     y: data.parsed.accel2_x
//   });
//   graph_data['graph-accel-y'].push({
//     x: data.datetime,
//     y: data.parsed.accel2_y
//   });
//   graph_data['graph-accel-z'].push({
//     x: data.datetime,
//     y: data.parsed.accel2_z
//   });
//   break;
//
//       }
//       break;
//     }
//     case "ADC": {
//       switch (data.key) {
//         case "ADC_CPU": {
//           graph_data['graph-core-temperature'].push({
//             x: data.datetime,
//             y: data.parsed / 10
//           });
//           break;
//         }
//
//       }
//       break;
//     }
//     case "GPS": {
//       switch (data.key) {
//         case "GPS_POS": {
//           let pos = new kakao.maps.LatLng(data.parsed.lat, data.parsed.lon);
//           if (gps_marker) {
//             gps_marker.setMap(null);
//           }
//           gps_marker = new kakao.maps.Marker({ position: pos });
//           gps_marker.setMap(map);
//           gps_path.push(pos);
//           gps_polyline.setPath(gps_path);
//           map.panTo(pos);
//           break;
//         }
//       }
//       break;
//     }
//   }
// }
// function updateBarGraph(canvasId, value) {
//   const ctx = document.getElementById(canvasId).getContext('2d');
//   if (!graphs[canvasId]) {
//     graphs[canvasId] = new Chart(ctx, {
//       type: 'bar',
//       data: {
//         labels: [''],
//         datasets: [{
//           label: canvasId,
//           data: [value],
//           backgroundColor: graph_config[canvasId].color
//         }]
//       },
//       options: {
//         responsive: true,
//         scales: {
//           x: {
//             display: false
//           },
//           y: {
//             min: 0,
//             max: 1,
//             ticks: {
//               stepSize: 0.1,
//             }
//           }
//         }
//       }
//     });
//   } else {
//     graphs[canvasId].data.datasets[0].data[0] = value;
//     graphs[canvasId].update();
//   }
// }
//
// // on graph toggle
// $('input.toggle-graph').on('change', e => {
//   const canvas = document.getElementById(e.target.id.replace('toggle-', ''));
//   if ($(e.target).prop('checked')) {
//     // init chart.js
//     graphs[canvas.id] = new Chart(canvas, {
//       type: 'line',
//       data: {
//         datasets: [{
//           data: graph_data[canvas.id],
//           cubicInterpolationMode: 'monotone',
//           tension: 0.2,
//           borderColor: graph_config[canvas.id].color
//         }, ((canvas.id == "graph-motor-torque") ? {
//           data: graph_data["graph-motor-torque-commanded"],
//           cubicInterpolationMode: 'monotone',
//           tension: 0.2,
//           borderColor: 'rgb(255, 159, 64)'
//         } : {})
//         ]
//       },
//       options: {
//         responsive: true,
//         interaction: {
//           intersect: false,
//         },
//         scales: {
//           x: {
//             type: 'realtime',
//             distribution: 'linear',
//             time: {
//               unit: 'second',
//               unitStepSize: 15,
//               stepSize: 15,
//               displayFormats: {
//                 hour: 'h:mm:ss',
//                 minute: 'h:mm:ss',
//                 second: 'h:mm:ss'
//               }
//             },
//             realtime: {
//               duration: 60000,
//               refresh: 500,
//               delay: graph_config[canvas.id].delay
//             }
//           },
//           y: {
//             grace: graph_config[canvas.id].grace
//           }
//         },
//         plugins: {
//           legend: {
//             display: false
//           }
//         },
//         elements: {
//           point: {
//             borderWidth: 0,
//             radius: 10,
//             backgroundColor: 'rgba(0, 0, 0, 0)'
//           }
//         }
//       }
//     });
//   } else { // discard graph
//     graphs[canvas.id].destroy();
//     delete graphs[canvas.id];
//   }
// });
//
// // on tooltip toggle
// $('input.tooltips').on('change', e => {
//   if ($(e.target).prop('checked')) {
//     const target = e.target.id.replace('tooltip-', '');
//     if (target == "vsm-status") {
//       return Swal.fire({
//         title: 'Vehicle State Machine',
//         html: `
//         <div class="failsafe-desc" style="line-height: 2rem; font-weight: bold; font-size: 1.2rem;">
//           <span style="font-size: 1.1rem; font-weight: initial">POST FAULT</span><br>
//             ${fault_toHTML(telemetry.inverter.fault.post)}
//           <span style="font-size: 1.1rem; font-weight: initial">RUN FAULT</span><br>
//             ${fault_toHTML(telemetry.inverter.fault.run)}
//         </div>`,
//         willClose: () => e.target.click(),
//       });
//     }
//     Swal.fire({
//       icon: 'info',
//       title: tooltips[target].title,
//       html: tooltips[target].desc,
//       willClose: () => e.target.click(),
//     });
//   }
// });
//
// Legacy commented tooltip block removed.
//
