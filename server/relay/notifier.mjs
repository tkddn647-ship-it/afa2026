// notifier.mjs
import fetch from 'node-fetch';

export function publisherNotify(payload) {
  fetch('http://localhost:8888/publisher_notify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(res => {
    console.log("📡 퍼블리셔 알림 전송 완료");
  }).catch(err => {
    console.error("❌ 퍼블리셔 알림 실패:", err);
  });
}
