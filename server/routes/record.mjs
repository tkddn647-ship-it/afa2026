import express from 'express';
import multer from 'multer';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { exec } from 'child_process';
import fetch from 'node-fetch'; // 🔄 추가

const router = express.Router();
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ✅ 저장 디렉토리 설정
const uploadDir = path.join(__dirname, '../recorded');
if (!fs.existsSync(uploadDir)) fs.mkdirSync(uploadDir, { recursive: true });

// ✅ Multer 설정: video/*만 허용
const storage = multer.diskStorage({
  destination: (req, file, cb) => cb(null, uploadDir),
  filename: (req, file, cb) => cb(null, file.originalname),
});
const upload = multer({
  storage,
  limits: { fileSize: 300 * 1024 * 1024 },
  fileFilter: (req, file, cb) => {
    if (file.mimetype.startsWith('video/')) cb(null, true);
    else cb(new Error('비디오 파일만 허용됩니다.'));
  }
});

// ✅ GET /api/record/list - mp4 영상만 반환
router.get('/list', (req, res) => {
  fs.readdir(uploadDir, (err, files) => {
    if (err) return res.status(500).json({ error: '디렉토리 읽기 실패' });
    const mp4Files = files.filter(f => f.toLowerCase().endsWith('.mp4'));
    res.json(mp4Files);
  });
});

// ✅ POST /api/record/upload_video
router.post('/upload_video', upload.single('video'), (req, res) => {
  if (!req.file) return res.status(400).json({ message: '업로드된 파일이 없습니다.' });

  const uploadedPath = path.join(uploadDir, req.file.filename);
  const ext = path.extname(req.file.originalname).toLowerCase();

  if (ext === '.avi') {
    const basename = path.basename(req.file.filename, '.avi');
    const mp4Filename = `${basename}.mp4`;
    const mp4Path = path.join(uploadDir, mp4Filename);

    const ffmpegCmd = `ffmpeg -y -i "${uploadedPath}" -preset ultrafast -vcodec libx264 -acodec aac "${mp4Path}"`;
    console.log("🎬 변환 명령 실행:", ffmpegCmd);

    exec(ffmpegCmd, async (error, stdout, stderr) => {
      if (error) {
        console.error("❌ ffmpeg 변환 오류:", error);
        return res.status(500).json({ message: 'ffmpeg 변환 실패', error: error.message });
      }

      fs.unlink(uploadedPath, () => {
        console.log("🧹 원본 avi 삭제:", uploadedPath);
      });

      // ✅ 릴레이 서버에 알림 전송
      try {
        const notifyBody = {
          type: "upload_done",
          filename: mp4Filename
        };
        const notifyRes = await fetch("http://localhost:8888/publisher_notify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(notifyBody)
        });
        if (!notifyRes.ok) {
          console.warn("⚠️ 퍼블리셔 알림 실패:", notifyRes.status);
        } else {
          console.log("📨 퍼블리셔 알림 성공:", notifyBody);
        }
      } catch (e) {
        console.error("❌ 퍼블리셔 알림 중 오류:", e);
      }

      console.log("✅ 변환 완료, 저장됨:", mp4Filename);
      return res.status(200).json({ message: '변환 및 업로드 성공', filename: mp4Filename });
    });
  } else {
    console.log("📥 mp4 업로드 완료:", req.file.filename);
    res.status(200).json({ message: 'mp4 업로드 성공', filename: req.file.filename });
  }
});

// ✅ DELETE /api/record/:filename
router.delete('/:filename', (req, res) => {
  const filename = path.basename(decodeURIComponent(req.params.filename));
  const filepath = path.join(uploadDir, filename);

  console.log("🧾 삭제 요청 받은 파일:", filename);

  fs.unlink(filepath, (err) => {
    if (err) {
      console.error("❌ 파일 삭제 실패:", err);
      return res.status(500).json({ message: '파일 삭제 실패', error: err.message });
    }
    console.log("🗑️ 파일 삭제 완료:", filename);
    res.status(200).json({ message: '파일 삭제 완료' });
  });
});

export default router;
