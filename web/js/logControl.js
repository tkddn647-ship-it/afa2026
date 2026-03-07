let isRecording = false;

function setupRecordingButton(btnId = "start-log-recording") {
  const btn = document.getElementById(btnId);
  if (!btn) return;

  btn.addEventListener("click", () => {
    if (!isRecording) {
      Swal.fire({
        title: "파일명 입력",
        input: "text",
        inputPlaceholder: "예: test2025"
      }).then(result => {
        if (result.isConfirmed && result.value.trim() !== "") {
          const filename = result.value.trim();
          fetch("/api/start_record", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ filename })
          }).then(() => {
            isRecording = true;
            btn.textContent = "로그 기록 정지";
          });
        }
      });
    } else {
      fetch("/api/stop_record", { method: "POST" }).then(() => {
        isRecording = false;
        btn.textContent = "로그 기록 시작";
        Swal.fire("✅ 기록 완료", "로그와 영상이 저장되었습니다.", "success");
      });
    }
  });
}
