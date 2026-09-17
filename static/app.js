const steps = [
  ["uploaded", "アップロード"],
  ["audio_extracting", "音声抽出"],
  ["transcribing", "文字起こし"],
  ["diarizing", "話者分離"],
  ["summarizing", "要約"],
  ["completed", "完了"],
];

let meetings = [];
let selectedId = null;
let eventSource = null;

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDate(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

async function loadMeetings() {
  const response = await fetch("/api/meetings");
  const data = await response.json();
  meetings = data.meetings;
  renderMeetingList();
  if (!selectedId && meetings.length) {
    selectMeeting(meetings[0].id);
  }
}

function renderMeetingList() {
  $("meetingList").innerHTML = meetings
    .map(
      (meeting) => `
        <button class="meeting-item ${meeting.id === selectedId ? "active" : ""}" data-id="${meeting.id}" type="button">
          <strong>${escapeHtml(meeting.title)}</strong>
          <small>${escapeHtml(meeting.statusLabel)} · ${formatDate(meeting.createdAt)}</small>
        </button>
      `,
    )
    .join("");

  document.querySelectorAll(".meeting-item").forEach((button) => {
    button.addEventListener("click", () => selectMeeting(button.dataset.id));
  });
}

async function selectMeeting(id) {
  selectedId = id;
  renderMeetingList();
  const response = await fetch(`/api/meetings/${id}`);
  const meeting = await response.json();
  renderDetail(meeting);
  watchMeeting(id);
}

function watchMeeting(id) {
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/meetings/${id}/events`);
  eventSource.onmessage = (event) => {
    const meeting = JSON.parse(event.data);
    if (meeting.id === selectedId) renderDetail(meeting);
    const index = meetings.findIndex((item) => item.id === meeting.id);
    if (index >= 0) {
      meetings[index] = { ...meetings[index], ...meeting, transcript: undefined, fullText: undefined };
      renderMeetingList();
    }
    if (["completed", "failed"].includes(meeting.status)) eventSource.close();
  };
}

function renderDetail(meeting) {
  $("emptyState").classList.add("hidden");
  $("detail").classList.remove("hidden");
  $("meetingTitle").textContent = meeting.title;
  $("meetingMeta").textContent = `${meeting.filename} · ${meeting.mediaType === "video" ? "動画" : "音声"} · ${formatDate(meeting.createdAt)}`;
  $("statusPill").textContent = meeting.statusLabel;
  $("progressBar").style.width = `${meeting.progress || 0}%`;

  $("steps").innerHTML = steps
    .map((step, index) => {
      const className = index < meeting.stepIndex ? "done" : index === meeting.stepIndex ? "current" : "";
      return `<li class="${className}">${step[1]}</li>`;
    })
    .join("");

  if (meeting.error) {
    $("errorBox").textContent = meeting.error;
    $("errorBox").classList.remove("hidden");
  } else {
    $("errorBox").classList.add("hidden");
  }

  $("summaryText").textContent = meeting.summary || "処理完了後に表示されます。";
  $("summaryText").classList.toggle("muted", !meeting.summary);
  $("decisionsList").innerHTML = (meeting.decisions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  $("todosList").innerHTML = (meeting.todos || [])
    .map(
      (todo) => `
        <article class="todo">
          <strong>${escapeHtml(todo.task)}</strong>
          <span>担当: ${escapeHtml(todo.owner || "未設定")} · 期限: ${escapeHtml(todo.due || "未設定")}</span>
        </article>
      `,
    )
    .join("");
  $("transcriptList").innerHTML = (meeting.transcript || [])
    .map(
      (segment) => `
        <article class="segment">
          <span class="time">${escapeHtml(segment.startLabel)} → ${escapeHtml(segment.endLabel)}</span>
          <span class="speaker">${escapeHtml(segment.speaker)}</span>
          <span>${escapeHtml(segment.text)}</span>
        </article>
      `,
    )
    .join("");
}

async function uploadFile(file) {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch("/api/meetings", { method: "POST", body: formData });
  const data = await response.json();
  if (!response.ok) {
    alert(data.error || "アップロードに失敗しました");
    return;
  }
  meetings.unshift(data);
  selectedId = data.id;
  renderMeetingList();
  renderDetail(data);
  watchMeeting(data.id);
}

function setupUpload() {
  const dropZone = $("dropZone");
  const fileInput = $("fileInput");

  fileInput.addEventListener("change", () => {
    const [file] = fileInput.files;
    if (file) uploadFile(file);
    fileInput.value = "";
  });

  ["dragenter", "dragover"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.remove("dragover");
    });
  });

  dropZone.addEventListener("drop", (event) => {
    const [file] = event.dataTransfer.files;
    if (file) uploadFile(file);
  });
}

$("refreshButton").addEventListener("click", loadMeetings);
$("copyButton").addEventListener("click", async () => {
  const response = await fetch(`/api/meetings/${selectedId}`);
  const meeting = await response.json();
  await navigator.clipboard.writeText(meeting.fullText || "");
  $("copyButton").textContent = "コピー済み";
  setTimeout(() => ($("copyButton").textContent = "コピー"), 1200);
});

setupUpload();
loadMeetings();
