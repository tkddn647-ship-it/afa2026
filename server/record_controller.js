// record_controller.js
import { spawn } from 'child_process';
import fs from 'fs';
const logsDir = './logs';
if (!fs.existsSync(logsDir)) fs.mkdirSync(logsDir);

let ffmpegProcess = null;
let logStream = null;

export function startRecording(filename) {
  const logPath = `${logsDir}/${filename}.log`;
  const videoPath = `${logsDir}/${filename}.mp4`;

  // 예: MJPEG 스트림 → ffmpeg로 mp4 저장
  ffmpegProcess = spawn('ffmpeg', [
    '-y',
    '-i', 'http://localhost:8081/stream', // MJPEG 스트림 주소
    '-c:v', 'libx264',
    '-preset', 'ultrafast',
    '-tune', 'zerolatency',
    '-pix_fmt', 'yuv420p',
    '-r', '30',
    videoPath
  ]);

  ffmpegProcess.stderr.on('data', data => {
    console.log(`ffmpeg: ${data}`);
  });

  logStream = fs.createWriteStream(logPath, { flags: 'a' });
  console.log(`✅ Logging started → ${logPath}`);
}

export function stopRecording() {
  if (ffmpegProcess) {
    ffmpegProcess.kill('SIGINT');
    ffmpegProcess = null;
  }
  if (logStream) {
    logStream.end();
    logStream = null;
  }
  console.log('🛑 Logging & Recording stopped.');
}

export function logData(data) {
  if (logStream) {
    logStream.write(JSON.stringify(data) + '\n');
  }
}
