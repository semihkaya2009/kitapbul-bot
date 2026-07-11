const chatShell = document.querySelector("[data-animate]");
const chatFeed = document.getElementById("chatFeed");
const searchForm = document.getElementById("searchForm");
const searchInput = document.getElementById("searchInput");
const searchButton = document.getElementById("searchButton");
const clearButton = document.getElementById("clearButton");

let currentResults = [];

const observer = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (entry.isIntersecting) {
      chatShell.classList.add("is-visible");
      searchInput.focus({ preventScroll: true });
    }
  });
}, { threshold: 0.28 });

observer.observe(chatShell);

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function scrollChat() {
  chatFeed.scrollTop = chatFeed.scrollHeight;
}

function addMessage(type, content) {
  const message = document.createElement("article");
  message.className = `message message--${type}`;

  if (type === "assistant") {
    message.innerHTML = `
      <div class="avatar" aria-hidden="true">AI</div>
      <div class="bubble">${content}</div>
    `;
  } else {
    message.innerHTML = `<div class="bubble">${content}</div>`;
  }

  chatFeed.appendChild(message);
  scrollChat();
  return message;
}

function addTyping() {
  return addMessage("assistant", `
    <strong>Kitap asistanı</strong>
    <p class="typing" aria-label="Aranıyor">
      <span></span><span></span><span></span>
    </p>
  `);
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || "İstek tamamlanamadı");
  }
  return data;
}

function formatBytes(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function renderResults(data) {
  currentResults = data.results;
  const resultItems = data.results.map((item, index) => {
    const copyInfo = item.copy_count > 1
      ? `<span class="pill">${item.copy_count} kopya</span>`
      : "";

    return `
      <label class="result-item">
        <input type="checkbox" value="${escapeHtml(item.id)}" data-title="${escapeHtml(item.file)}">
        <div>
          <p class="result-title">${escapeHtml(item.file)}</p>
          <div class="result-meta">
            <span>📁 ${escapeHtml(item.group)}</span>
            ${copyInfo}
          </div>
        </div>
        <span class="result-number">#${index + 1}</span>
      </label>
    `;
  }).join("");

  const mergedText = data.merged_count > 0
    ? `<span class="pill">${data.merged_count} tekrar birleştirildi</span>`
    : "";

  const content = `
    <strong>${data.result_count} tekil sonuç bulundu</strong>
    <div class="result-summary">
      <span class="pill">${escapeHtml(data.query)}</span>
      ${mergedText}
      <span class="pill">${data.duration_ms} ms</span>
    </div>
    <div class="result-list">${resultItems}</div>
    <div class="select-actions">
      <button type="button" class="fetch-selected-button">Seçilileri getir</button>
      <button type="button" class="secondary select-all-button">Tümünü seç</button>
      <button type="button" class="secondary clear-selected-button">Seçimi kaldır</button>
    </div>
  `;

  const message = addMessage("assistant", content);
  bindResultActions(message);
}

function selectedIds(root) {
  return [...root.querySelectorAll(".result-item input:checked")].map((input) => input.value);
}

function bindResultActions(root) {
  const fetchButton = root.querySelector(".fetch-selected-button");
  const selectAllButton = root.querySelector(".select-all-button");
  const clearSelectedButton = root.querySelector(".clear-selected-button");

  if (!fetchButton || !selectAllButton || !clearSelectedButton) return;

  fetchButton.addEventListener("click", () => fetchSelected(root));
  selectAllButton.addEventListener("click", () => {
    root.querySelectorAll(".result-item input").forEach((input) => {
      input.checked = true;
    });
  });
  clearSelectedButton.addEventListener("click", () => {
    root.querySelectorAll(".result-item input").forEach((input) => {
      input.checked = false;
    });
  });
}

function renderDownloads(data) {
  const downloadCards = data.downloaded.map((item) => `
    <div class="download-card">
      <strong>${escapeHtml(item.file)}</strong>
      <small>
        ${escapeHtml(item.group)}
        ${item.source_count > 1 ? ` · kaynak ${item.used_source}/${item.source_count}` : ""}
        ${item.size ? ` · ${formatBytes(item.size)}` : ""}
      </small>
      <a class="download-link" href="${escapeHtml(item.url)}">İndir</a>
    </div>
  `).join("");

  const failedCards = data.failed.map((item) => `
    <div class="download-card">
      <strong class="error-text">${escapeHtml(item.file)}</strong>
      <small>${escapeHtml(item.error || "İndirilemedi")}</small>
    </div>
  `).join("");

  const zipLink = data.zip
    ? `<a class="download-link" href="${escapeHtml(data.zip.url)}">Tümünü ZIP indir (${formatBytes(data.zip.size)})</a>`
    : "";

  addMessage("assistant", `
    <strong>Dosyalar hazır</strong>
    <div class="download-list">
      ${zipLink}
      ${downloadCards}
      ${failedCards}
    </div>
  `);
}

async function fetchSelected(root) {
  const ids = selectedIds(root);
  if (ids.length === 0) {
    addMessage("assistant", `<strong>Seçim yok</strong><p>Getirmek için en az bir sonuç seç.</p>`);
    return;
  }

  addMessage("user", `<strong>${ids.length} kitap getir</strong>`);
  const typing = addTyping();

  try {
    const data = await postJson("/api/fetch", { ids });
    typing.remove();
    renderDownloads(data);
  } catch (error) {
    typing.remove();
    addMessage("assistant", `<strong class="error-text">Dosyalar getirilemedi</strong><p>${escapeHtml(error.message)}</p>`);
  }
}

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = searchInput.value.trim();
  if (query.length < 2) return;

  addMessage("user", `<strong>${escapeHtml(query)}</strong>`);
  searchInput.value = "";
  searchButton.disabled = true;

  const typing = addTyping();
  try {
    const data = await postJson("/api/search", { query });
    typing.remove();
    if (data.results.length === 0) {
      addMessage("assistant", `<strong>Sonuç bulunamadı</strong><p>${escapeHtml(query)} için kayıt yok.</p>`);
    } else {
      renderResults(data);
    }
  } catch (error) {
    typing.remove();
    addMessage("assistant", `<strong class="error-text">Arama tamamlanamadı</strong><p>${escapeHtml(error.message)}</p>`);
  } finally {
    searchButton.disabled = false;
    searchInput.focus();
  }
});

clearButton.addEventListener("click", () => {
  currentResults = [];
  chatFeed.innerHTML = `
    <article class="message message--assistant">
      <div class="avatar" aria-hidden="true">AI</div>
      <div class="bubble">
        <strong>Kitap asistanı</strong>
        <p>Aramak istediğin kitabı yaz; sonuçları seçilebilir liste olarak getireceğim.</p>
      </div>
    </article>
  `;
  searchInput.focus();
});
