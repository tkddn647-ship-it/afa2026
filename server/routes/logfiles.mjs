import express from 'express';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const router = express.Router();
const logsDir = path.join(__dirname, '../separated_logs'); 

router.get('/test', (req, res) => {
    res.send("Test route works");
  });
  
router.get('/logfiles', (req, res) => {
  fs.readdir(logsDir, (err, files) => {
    if (err) {
      console.error(err);
      return res.status(500).json({ error: '로그 파일 목록을 읽을 수 없습니다.' });
    }
    res.json(files);
  });
});

router.get('/logfile', (req, res) => {
  let name = req.query.name;
  if (!name) return res.status(400).json({ error: '파일 이름이 필요합니다.' });
  
  // 만약 파일 이름에 .log 확장자가 없다면 자동 추가
  if (!name.endsWith('.log')) {
    name += '.log';
  }
  
  const filePath = path.join(logsDir, name);
  fs.readFile(filePath, 'utf8', (err, data) => {
    if (err) {
      console.error(err);
      return res.status(500).json({ error: '파일을 읽을 수 없습니다.' });
    }
    res.send(data);
  });
});

export default router;
