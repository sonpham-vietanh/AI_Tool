/**
 * AI Book Translator Hub PRO v2.5 — Frontend Controller (app.js)
 * Connects 1-Click Auto Search & Translate, Multi-Source Book Picker,
 * File Drag-and-Drop, Live Translation Studio, Auto-Glossary & Settings Drawer.
 */

(function () {
  "use strict";

  const PROVIDER_DEFAULTS = {
    gtx: {
      model: "gtx-free",
      baseUrl: "",
      label: "Google GTX (Siêu tốc)",
      help: "💡 <strong>Google Translate GTX:</strong> Không cần nhập API Key, không lo giới hạn, tốc độ cực nhanh cho sách dài. Nếu muốn văn phong mượt như dịch giả chuyên nghiệp, hãy chọn <strong>Gemini 2.5 Flash</strong> hoặc <strong>DeepSeek</strong>.",
    },
    gemini: {
      model: "gemini-2.5-pro",
      baseUrl: "https://generativelanguage.googleapis.com/v1beta",
      label: "Gemini Ultra / 2.5 Pro",
      help: "💡 <strong>Google Gemini Ultra / AI Studio:</strong> Hỗ trợ <code>gemini-2.5-pro</code>, <code>gemini-ultra</code>, <code>gemini-2.5-flash</code>... Tự động bật <code>BLOCK_NONE</code> chống ngắt quãng. Có thể dán nhiều key AI Studio hoặc key Gemini Gateway vào Kho 2.",
    },
    hybrid: {
      model: "cc/claude-sonnet-5-5",
      baseUrl: "https://ryg2i56.abc-tunnel.us/v1",
      label: "Hybrid (Claude + Gemini)",
      help: "🔥 <strong>Chế độ Hybrid Song Song:</strong> Tự động chia tải (Load-Balance) đồng thời qua cả <strong>Kho 1 (Claude Gateway)</strong> và <strong>Kho 2 (Gemini Ultra)</strong> — Tăng gấp đôi tốc độ dịch và tự động đổi sang Engine còn lại nếu 1 bên chạm giới hạn Rate Limit!",
    },
    deepseek: {
      model: "deepseek-chat",
      baseUrl: "https://api.deepseek.com/v1",
      label: "DeepSeek V3",
      help: "💡 <strong>DeepSeek API:</strong> Văn phong dịch sách chuyên ngành và tiểu thuyết cực kỳ mượt mà, chi phí rẻ.",
    },
    openai: {
      model: "cc/claude-sonnet-5-5",
      baseUrl: "https://ryg2i56.abc-tunnel.us/v1",
      label: "Claude Gateway",
      help: "💡 <strong>Custom AI Gateway (abc-tunnel.us):</strong> Đã cấu hình sẵn Endpoint <code>https://ryg2i56.abc-tunnel.us/v1</code> với đầy đủ các model: <code>cc/claude-sonnet-5-5</code>, <code>cc/claude-opus-5-5</code>, <code>cc/claude-fable-5-1</code>, <code>cc/claude-haiku-4-5-20251001</code>.",
    },
    claude: {
      model: "cc/claude-sonnet-5-5",
      baseUrl: "https://ryg2i56.abc-tunnel.us/v1",
      label: "Claude Sonnet 5.5",
      help: "💡 <strong>Anthropic Claude (qua Gateway):</strong> Văn phong dịch văn học, kinh tế và tâm lý học tự nhiên, mượt mà nhất.",
    },
    openrouter: {
      model: "google/gemini-2.5-pro",
      baseUrl: "https://openrouter.ai/api/v1",
      label: "OpenRouter AI",
      help: "💡 <strong>OpenRouter:</strong> Dùng 1 API Key duy nhất để gọi mọi model (Gemini, Claude, DeepSeek, Qwen...).",
    },
    ollama: {
      model: "qwen2.5:14b",
      baseUrl: "http://localhost:11434/v1",
      label: "Ollama Local",
      help: "💡 <strong>Ollama Local:</strong> Chạy trực tiếp bằng GPU trên máy bàn mạnh của bạn, miễn phí 100% và bảo mật tuyệt đối.",
    },
  };

  const LANG_NAMES = {
    vi: "Tiếng Việt (vi)",
    en: "English (en)",
    zh: "中文 Giản thể (zh)",
    ja: "日本語 (ja)",
    ko: "한국어 (ko)",
    fr: "Français (fr)",
    de: "Deutsch (de)",
    es: "Español (es)",
  };

  const STATUS_LABELS = {
    queued: { text: "Đang xếp hàng...", cls: "status-running" },
    searching: { text: "Đang tìm sách...", cls: "status-running" },
    downloading: { text: "Đang tải sách...", cls: "status-running" },
    extracting_glossary: { text: "Đang quét Glossary...", cls: "status-running" },
    awaiting_glossary_approval: { text: "⏸️ Chờ duyệt Từ điển...", cls: "status-running" },
    translating: { text: "Đang dịch đa luồng...", cls: "status-running" },
    editing: { text: "Đang biên tập...", cls: "status-running" },
    packaging: { text: "Đang đóng gói file...", cls: "status-running" },
    completed: { text: "Hoàn tất 100%", cls: "status-completed" },
    failed: { text: "Thất bại", cls: "status-failed" },
    cancelled: { text: "Đã dừng", cls: "status-cancelled" },
  };

  // State (Loaded from /api/settings on startup)
  let appSettings = {
    provider: "openai",
    model: "cc/claude-sonnet-5-5",
    base_url: "https://ryg2i56.abc-tunnel.us/v1",
    api_keys: [],
    gemini_api_keys: [],
    gemini_model: "gemini-2.5-pro",
    gemini_base_url: "",
    concurrency: 12,
    custom_prompt: "",
  };
  let searchResultsCache = [];
  let selectedUploadFile = null;
  let jobsList = [];
  let activeJobId = null;
  let pollTimer = null;
  let renderedPreviewCount = 0;

  // DOM Elements
  const el = (id) => document.getElementById(id);

  function showToast(message, type = "info") {
    const container = el("toastContainer");
    if (!container) return;
    const item = document.createElement("div");
    item.className = `toast-item toast-${type}`;
    const icon =
      type === "success"
        ? "✅"
        : type === "error"
        ? "❌"
        : type === "warning"
        ? "⚠️"
        : "✨";
    item.innerHTML = `<span class="toast-icon">${icon}</span><div class="toast-content">${escapeHtml(
      message
    )}</div>`;
    container.appendChild(item);
    setTimeout(() => {
      item.style.opacity = "0";
      item.style.transform = "translateY(8px)";
      setTimeout(() => item.remove(), 260);
    }, 4200);
  }

  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatBytes(bytes) {
    if (!bytes || isNaN(bytes)) return "";
    const mb = bytes / (1024 * 1024);
    if (mb >= 1) return `${mb.toFixed(2)} MB`;
    return `${(bytes / 1024).toFixed(0)} KB`;
  }

  function formatEta(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) {
      return "Đang tính...";
    }
    if (seconds <= 0) return "Hoàn tất";
    if (seconds < 60) return `~${Math.max(1, Math.round(seconds))} giây`;
    const mins = Math.floor(seconds / 60);
    const secs = Math.round(seconds % 60);
    return `~${mins}p ${secs}s`;
  }

  function getFormatBadgeClass(fmt) {
    const f = String(fmt || "epub").toLowerCase();
    if (f === "epub") return "fmt-epub";
    if (f === "docx") return "fmt-docx";
    if (f === "pdf") return "fmt-pdf";
    return "fmt-txt";
  }

  function getCurrentQuickConfig() {
    const langCode = el("targetLangSelect")?.value || "vi";
    const bilingualMode = el("bilingualModeSelect")?.value || "mono";
    const autoGlossary = !!el("autoGlossaryCheckbox")?.checked;
    const enableEditor = el("enableEditorAgentCheckbox")
      ? !!el("enableEditorAgentCheckbox").checked
      : true;
    const agentGlossaryModel = el("agentGlossaryModelSelect")?.value || null;
    const agentEditorModel =
      el("agentEditorModelSelect")?.value || appSettings.model || "cc/claude-sonnet-5-5";
    const genrePreset = el("genrePresetSelect")?.value || "auto";
    const chapterLimit = Number(el("chapterLimitSelect")?.value || 0);
    const pageRange = (el("pageRangeInput")?.value || "").trim();
    const inlinePrompt = (el("quickCustomPromptInput")?.value || "").trim();
    const pauseForGlossary = !!el("pauseForGlossaryCheckbox")?.checked;
    return {
      provider: appSettings.provider === "gtx" ? "google_free" : appSettings.provider,
      model: appSettings.model,
      base_url: appSettings.base_url || null,
      api_keys: appSettings.api_keys,
      gemini_api_keys: appSettings.gemini_api_keys || [],
      gemini_model: appSettings.gemini_model || "gemini-2.5-pro",
      gemini_base_url: appSettings.gemini_base_url || null,
      concurrency: Number(appSettings.concurrency) || 12,
      genre_preset: genrePreset,
      custom_prompt: inlinePrompt || appSettings.custom_prompt || "",
      chapter_limit: chapterLimit,
      page_range: pageRange,
      pause_for_glossary: pauseForGlossary,
      target_lang_code: langCode,
      target_lang: LANG_NAMES[langCode] || "Tiếng Việt",
      bilingual: bilingualMode === "bilingual",
      auto_glossary: autoGlossary,
      enable_editor_agent: enableEditor,
      agent_glossary_model: agentGlossaryModel,
      agent_translator_model: appSettings.model,
      agent_editor_model: agentEditorModel,
    };
  }

  // =========================================================================
  // 1. SETTINGS DRAWER, QUICK COMMAND BAR & HEADER STATUS
  // =========================================================================
  function updateHeaderPills() {
    const pKey = appSettings.provider === "google_free" ? "gtx" : appSettings.provider;
    const def = PROVIDER_DEFAULTS[pKey] || PROVIDER_DEFAULTS.openai;
    let modelText = "";
    if (pKey === "gtx") {
      modelText = "Google GTX Free";
    } else if (pKey === "hybrid") {
      modelText = "Hybrid (Claude + Gemini)";
    } else if (pKey === "gemini") {
      const gm = (appSettings.model || "").startsWith("gemini")
        ? appSettings.model
        : appSettings.gemini_model || "gemini-2.5-pro";
      modelText = gm;
    } else {
      modelText = (appSettings.model || def.model).replace(/^cc\//, "");
    }

    if (el("headerModelText")) el("headerModelText").textContent = modelText;
    if (el("headerConcurrencyText")) {
      el("headerConcurrencyText").textContent = `${appSettings.concurrency || 12} luồng`;
    }

    const gwCount = (appSettings.api_keys || []).length;
    const gmCount = (appSettings.gemini_api_keys || []).length;
    const totalKeys = gwCount + gmCount;
    if (el("headerKeysCountText")) {
      el("headerKeysCountText").textContent = `${totalKeys} Key`;
    }
    if (el("gatewayKeyCountBadge")) {
      el("gatewayKeyCountBadge").textContent = `${gwCount} Key`;
    }
    if (el("geminiKeyCountBadge")) {
      el("geminiKeyCountBadge").textContent = `${gmCount} Key`;
    }

    // Highlight active Engine Mode Pill on Quick Command Bar
    document.querySelectorAll("#quickEngineModePills .engine-pill").forEach((btn) => {
      const btnProv = btn.getAttribute("data-provider");
      if (btnProv === pKey || (pKey === "claude" && btnProv === "openai")) {
        btn.classList.add("active");
      } else {
        btn.classList.remove("active");
      }
    });

    // Sync Quick Model Select dropdown on the main workspace
    const qSelect = el("quickModelSelect");
    if (qSelect) {
      let desiredVal = appSettings.model || "cc/claude-sonnet-5-5";
      if (pKey === "gtx") {
        desiredVal = "gtx-free";
      } else if (pKey === "hybrid") {
        desiredVal = "hybrid:claude+gemini";
      } else if (pKey === "gemini" && !desiredVal.startsWith("gemini")) {
        desiredVal = appSettings.gemini_model || "gemini-2.5-pro";
      }
      ensureSelectHasOption(qSelect, desiredVal);
      qSelect.value = desiredVal;
    }

    // Highlight active vault card inside modal
    el("vaultCardGateway")?.classList.toggle(
      "active-vault",
      pKey === "openai" || pKey === "claude" || pKey === "hybrid"
    );
    el("vaultCardGemini")?.classList.toggle(
      "active-vault",
      pKey === "gemini" || pKey === "hybrid"
    );
  }

  function ensureSelectHasOption(selectEl, val, groupLabel = null) {
    if (!selectEl || !val) return;
    const exists = Array.from(selectEl.options).some((opt) => opt.value === val);
    if (!exists) {
      const opt = document.createElement("option");
      opt.value = val;
      opt.textContent = `${val} (Custom / API)`;
      if (groupLabel) {
        const grp = selectEl.querySelector(`optgroup[id="${groupLabel}"]`);
        if (grp) {
          grp.appendChild(opt);
          return;
        }
      }
      selectEl.appendChild(opt);
    }
  }

  async function loadSettings() {
    try {
      const res = await fetch("/api/settings");
      if (res.ok) {
        const data = await res.json();
        if (data.settings) {
          const s = data.settings;
          appSettings.provider =
            s.ui_provider || (s.provider === "google_free" ? "gtx" : s.provider) || "openai";
          appSettings.model = s.model || "cc/claude-sonnet-5-5";
          appSettings.base_url = s.base_url || "https://ryg2i56.abc-tunnel.us/v1";
          appSettings.api_keys = Array.isArray(s.api_keys) ? s.api_keys : [];
          appSettings.gemini_api_keys = Array.isArray(s.gemini_api_keys)
            ? s.gemini_api_keys
            : [];
          appSettings.gemini_model = s.gemini_model || "gemini-2.5-pro";
          appSettings.gemini_base_url = s.gemini_base_url || "";
          appSettings.concurrency = s.concurrency || 12;
          appSettings.custom_prompt = s.custom_prompt || "";
          if (el("quickCustomPromptInput") && !el("quickCustomPromptInput").value) {
            el("quickCustomPromptInput").value = appSettings.custom_prompt || "";
          }
          if (s.cache_stats) {
            if (el("headerCacheText")) {
              el("headerCacheText").textContent = `Bật (${s.cache_stats.total_entries || 0} đoạn)`;
            }
            if (el("headerCacheCountText")) {
              el("headerCacheCountText").textContent = String(s.cache_stats.total_entries || 0);
            }
          }
        }
      }
    } catch (err) {
      console.warn("Could not fetch /api/settings:", err);
    }

    syncSettingsToModal();
    updateHeaderPills();
    discoverModelsFromServer(false);
  }

  async function persistSettingsToBackend(silent = false) {
    try {
      localStorage.setItem("bth_settings_v30", JSON.stringify(appSettings));
    } catch (_) {}

    try {
      const payload = {
        ...appSettings,
        provider: appSettings.provider === "gtx" ? "google_free" : appSettings.provider,
      };
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok && !silent) {
        showToast("Đã lưu Kho API Key & Cấu hình AI thành công!", "success");
      }
    } catch (err) {
      if (!silent) showToast("Đã lưu cấu hình trên trình duyệt.", "info");
    }
    updateHeaderPills();
  }

  async function discoverModelsFromServer(showNotice = true) {
    try {
      const res = await fetch("/api/settings/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...appSettings,
          provider: appSettings.provider === "gtx" ? "google_free" : appSettings.provider,
        }),
      });
      if (!res.ok) return;
      const data = await res.json();
      const qSelect = el("quickModelSelect");
      const gwSelect = el("settingModelSelectGateway");
      const gmSelect = el("settingGeminiModelSelect");

      let totalCount = 0;
      (data.gateway_models || []).forEach((m) => {
        totalCount++;
        ensureSelectHasOption(qSelect, m, "optgroupGateway");
        ensureSelectHasOption(gwSelect, m);
      });
      (data.gemini_models || []).forEach((m) => {
        totalCount++;
        ensureSelectHasOption(qSelect, m, "optgroupGemini");
        ensureSelectHasOption(gmSelect, m);
      });
      (data.other_models || []).forEach((m) => {
        totalCount++;
        ensureSelectHasOption(qSelect, m, "optgroupOther");
      });

      updateHeaderPills();
      if (showNotice) {
        showToast(
          `Đã quét thành công ${totalCount} Models từ Claude Gateway & Google Gemini Ultra!`,
          "success"
        );
      }
    } catch (err) {
      if (showNotice) {
        showToast("Không thể quét thêm model từ máy chủ lúc này.", "warning");
      }
    }
  }

  function syncSettingsToModal() {
    const pKey = appSettings.provider === "google_free" ? "gtx" : appSettings.provider;
    if (el("settingProviderSelect")) el("settingProviderSelect").value = pKey;

    const gwModel =
      appSettings.model && !appSettings.model.startsWith("gemini")
        ? appSettings.model
        : "cc/claude-sonnet-5-5";
    if (el("settingModelInput")) el("settingModelInput").value = gwModel;
    if (el("settingModelSelectGateway")) {
      ensureSelectHasOption(el("settingModelSelectGateway"), gwModel);
      el("settingModelSelectGateway").value = gwModel;
    }
    if (el("settingBaseUrlInput")) {
      el("settingBaseUrlInput").value =
        appSettings.base_url || "https://ryg2i56.abc-tunnel.us/v1";
    }
    if (el("settingApiKeysTextarea")) {
      el("settingApiKeysTextarea").value = (appSettings.api_keys || []).join("\n");
    }

    const gmModel = appSettings.gemini_model || "gemini-2.5-pro";
    if (el("settingGeminiModelInput")) el("settingGeminiModelInput").value = gmModel;
    if (el("settingGeminiModelSelect")) {
      ensureSelectHasOption(el("settingGeminiModelSelect"), gmModel);
      el("settingGeminiModelSelect").value = gmModel;
    }
    if (el("settingGeminiBaseUrlInput")) {
      el("settingGeminiBaseUrlInput").value = appSettings.gemini_base_url || "";
    }
    if (el("settingGeminiApiKeysTextarea")) {
      el("settingGeminiApiKeysTextarea").value = (appSettings.gemini_api_keys || []).join(
        "\n"
      );
    }

    if (el("settingConcurrencyRange"))
      el("settingConcurrencyRange").value = appSettings.concurrency || 12;
    if (el("concurrencyValueText"))
      el("concurrencyValueText").textContent = appSettings.concurrency || 12;
    if (el("settingCustomPromptTextarea"))
      el("settingCustomPromptTextarea").value = appSettings.custom_prompt || "";

    const def = PROVIDER_DEFAULTS[pKey] || PROVIDER_DEFAULTS.openai;
    if (el("providerHelpText")) el("providerHelpText").innerHTML = def.help;
  }

  function readModalSettings() {
    const pKey = el("settingProviderSelect")?.value || "openai";
    const rawGwKeys = el("settingApiKeysTextarea")?.value || "";
    const gwKeys = rawGwKeys
      .split(/[\r\n,;]+/)
      .map((k) => k.trim())
      .filter(Boolean);

    const rawGmKeys = el("settingGeminiApiKeysTextarea")?.value || "";
    const gmKeys = rawGmKeys
      .split(/[\r\n,;]+/)
      .map((k) => k.trim())
      .filter(Boolean);

    const gwModel =
      (el("settingModelInput")?.value || "").trim() || "cc/claude-sonnet-5-5";
    const gmModel =
      (el("settingGeminiModelInput")?.value || "").trim() || "gemini-2.5-pro";

    const activeModel =
      pKey === "gemini"
        ? gmModel
        : pKey === "gtx"
        ? "gtx-free"
        : gwModel;

    return {
      provider: pKey,
      model: activeModel,
      base_url:
        (el("settingBaseUrlInput")?.value || "").trim() ||
        "https://ryg2i56.abc-tunnel.us/v1",
      api_keys: gwKeys,
      gemini_api_keys: gmKeys,
      gemini_model: gmModel,
      gemini_base_url: (el("settingGeminiBaseUrlInput")?.value || "").trim(),
      concurrency: Number(el("settingConcurrencyRange")?.value) || 12,
      custom_prompt: (el("settingCustomPromptTextarea")?.value || "").trim(),
    };
  }

  async function saveSettingsFromModal() {
    const next = readModalSettings();
    appSettings = { ...appSettings, ...next };
    await persistSettingsToBackend(false);
    closeSettingsModal();
  }

  async function testApiConnection() {
    const alertBox = el("testConnectionResult");
    const btn = el("btnTestConnection");
    if (!alertBox) return;

    const candidate = readModalSettings();
    const payload = {
      ...candidate,
      provider: candidate.provider === "gtx" ? "google_free" : candidate.provider,
    };

    alertBox.className = "connection-alert";
    alertBox.innerHTML = "⏳ Đang kiểm tra kết nối tới AI Provider & Kho Key...";
    if (btn) btn.disabled = true;

    try {
      const res = await fetch("/api/settings/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (data.ok) {
        alertBox.className = "connection-alert alert-success";
        alertBox.innerHTML = `
          <div><strong>✅ ${escapeHtml(data.message)}</strong> (Kho Gateway: ${data.gateway_keys_count || 0} key · Kho Gemini: ${data.gemini_keys_count || 0} key)</div>
          <div style="margin-top:4px;font-size:0.8rem;opacity:0.9;">
            <em>Câu mẫu dịch thử:</em> "${escapeHtml(data.sample_translation || "")}"
          </div>
        `;
      } else {
        alertBox.className = "connection-alert alert-error";
        alertBox.innerHTML = `<strong>❌ Kết nối thất bại:</strong> ${escapeHtml(
          data.message || data.error || "Không rõ nguyên nhân"
        )}`;
      }
    } catch (err) {
      alertBox.className = "connection-alert alert-error";
      alertBox.innerHTML = `<strong>❌ Lỗi kết nối máy chủ:</strong> ${escapeHtml(err.message)}`;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function openSettingsModal(focusGemini = false) {
    syncSettingsToModal();
    updateHeaderPills();
    if (el("testConnectionResult")) {
      el("testConnectionResult").className = "connection-alert hidden";
    }
    el("settingsModalBackdrop")?.classList.remove("hidden");
    if (focusGemini) {
      setTimeout(() => {
        el("settingGeminiApiKeysTextarea")?.focus();
      }, 120);
    }
  }

  function closeSettingsModal() {
    el("settingsModalBackdrop")?.classList.add("hidden");
  }

  // =========================================================================
  // 2. SEARCH BOOKS & 1-CLICK AUTO TRANSLATE
  // =========================================================================
  async function startOneClickAutoJob() {
    const queryInput = el("bookQueryInput");
    const query = (queryInput?.value || "").trim();
    if (!query) {
      showToast("Vui lòng nhập tên cuốn sách bạn cần tìm và dịch!", "warning");
      queryInput?.focus();
      return;
    }

    const preferredFormat = el("preferredFormatSelect")?.value || "auto";
    const config = getCurrentQuickConfig();
    const btn = el("btnOneClickAuto");
    if (btn) btn.disabled = true;

    try {
      showToast(`Đang tự động tìm và dịch sách: "${query}"...`, "info");
      const res = await fetch("/api/jobs/auto", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          preferred_format: preferredFormat,
          config,
        }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.detail || data.message || "Không thể tạo tiến trình dịch tự động.");
      }

      activeJobId = data.job_id;
      renderedPreviewCount = 0;
      await fetchJobsList();
      selectJob(activeJobId);
      el("studioActiveView")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) {
      showToast(err.message || "Lỗi khi bắt đầu dịch tự động", "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  let currentEditionFilter = "all";

  async function searchAndShowVersions() {
    const queryInput = el("bookQueryInput");
    const query = (queryInput?.value || "").trim();
    if (!query) {
      showToast("Vui lòng nhập tên sách để Agent 1 tìm mọi phiên bản & sách leak!", "warning");
      queryInput?.focus();
      return;
    }

    const section = el("searchResultsSection");
    const loading = el("searchLoadingState");
    const tableWrap = el("searchResultsTableWrap");
    const tbody = el("searchResultsTbody");
    const countBadge = el("searchResultsCount");
    const btnSearch = el("btnSearchAndPick");

    section?.classList.remove("hidden");
    loading?.classList.remove("hidden");
    tableWrap?.classList.add("hidden");
    if (tbody) tbody.innerHTML = "";
    if (btnSearch) btnSearch.disabled = true;

    try {
      const res = await fetch(`/api/search?q=${encodeURIComponent(query)}&limit=35`);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Lỗi khi tìm kiếm sách.");
      }

      searchResultsCache = data.results || [];
      currentEditionFilter = "all";
      document.querySelectorAll("#editionFilterPills [data-fmt-filter]").forEach((b) => {
        b.classList.toggle("active-filter", b.getAttribute("data-fmt-filter") === "all");
      });

      if (countBadge) countBadge.textContent = `${searchResultsCache.length} phiên bản`;

      if (searchResultsCache.length === 0) {
        if (tbody) {
          tbody.innerHTML = `
            <tr>
              <td colspan="6" class="text-center text-muted" style="padding: 2rem;">
                Không tìm thấy phiên bản nào cho từ khóa <strong>"${escapeHtml(query)}"</strong>. Hãy thử nhập tên sách ngắn gọn hơn hoặc kèm tên tác giả.
              </td>
            </tr>
          `;
        }
      } else {
        renderSearchResultsTable(searchResultsCache);
        showToast(
          `Agent 1 đã tìm thấy ${searchResultsCache.length} phiên bản / bản leak cho "${query}". Hãy chọn bản bạn muốn dịch!`,
          "success"
        );
      }
    } catch (err) {
      showToast(err.message || "Không thể tìm kiếm sách", "error");
    } finally {
      loading?.classList.add("hidden");
      tableWrap?.classList.remove("hidden");
      if (btnSearch) btnSearch.disabled = false;
    }
  }

  function applyEditionFilter(filterType) {
    currentEditionFilter = filterType || "all";
    document.querySelectorAll("#editionFilterPills [data-fmt-filter]").forEach((b) => {
      b.classList.toggle("active-filter", b.getAttribute("data-fmt-filter") === currentEditionFilter);
    });
    if (!searchResultsCache.length) return;
    let filtered = searchResultsCache;
    if (currentEditionFilter === "epub") {
      filtered = searchResultsCache.filter((r) => String(r.format || "").toLowerCase() === "epub");
    } else if (currentEditionFilter === "pdf") {
      filtered = searchResultsCache.filter((r) => String(r.format || "").toLowerCase() === "pdf");
    } else if (currentEditionFilter === "leak") {
      filtered = searchResultsCache.filter((r) => {
        const src = String(r.source || "").toLowerCase();
        return src.includes("leak") || src.includes("github") || src.includes("drive") || src.includes("direct");
      });
    }
    const countBadge = el("searchResultsCount");
    if (countBadge) {
      countBadge.textContent = `${filtered.length}/${searchResultsCache.length} phiên bản`;
    }
    renderSearchResultsTable(filtered);
  }

  function renderSearchResultsTable(items) {
    const tbody = el("searchResultsTbody");
    if (!tbody) return;

    if (!items || items.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="6" class="text-center text-muted" style="padding: 1.5rem;">
            Không có phiên bản nào thuộc bộ lọc này. Hãy bấm <strong>"Tất cả"</strong> để xem toàn bộ ${searchResultsCache.length} phiên bản.
          </td>
        </tr>
      `;
      return;
    }

    tbody.innerHTML = items
      .map((item) => {
        const realIdx = searchResultsCache.indexOf(item);
        const fmt = String(item.format || "epub").toLowerCase();
        const badgeCls = getFormatBadgeClass(fmt);
        const fmtLabel =
          fmt === "epub"
            ? "EPUB ★ Chuẩn dịch"
            : fmt === "docx"
            ? "DOCX Word"
            : fmt.toUpperCase();

        const coverHtml = item.cover_url
          ? `<img src="${escapeHtml(item.cover_url)}" alt="" class="book-cover-thumb" onerror="this.style.display='none'" />`
          : `<div class="book-cover-fallback">${fmt === "epub" ? "📗" : "📕"}</div>`;

        const yearBadge = item.year
          ? `<span style="display:inline-block;padding:1px 6px;border-radius:4px;background:rgba(99,102,241,0.12);color:#818cf8;font-weight:600;font-size:0.72rem;margin-right:4px;">${escapeHtml(
              item.year
            )}</span>`
          : "";
        const editionText = item.edition || (item.size ? `Dung lượng: ${item.size}` : "Ấn bản tiêu chuẩn");
        const sizeNote = item.size ? ` · ${escapeHtml(item.size)}` : "";

        return `
          <tr>
            <td>
              <div class="book-cell">
                ${coverHtml}
                <div>
                  <div class="book-title-cell">${escapeHtml(item.title)}</div>
                  <div class="book-sub-cell">
                    ${yearBadge}<span>${escapeHtml(editionText)}${sizeNote}</span> · Điểm khớp: <strong>${
          item.priority_score || 80
        }</strong>
                  </div>
                </div>
              </div>
            </td>
            <td>${escapeHtml(item.author || "Unknown")}</td>
            <td><span class="fmt-badge ${badgeCls}">${escapeHtml(fmtLabel)}</span></td>
            <td>${escapeHtml(item.year || "—")}${
          item.size ? `<div class="text-muted text-xs">${escapeHtml(item.size)}</div>` : ""
        }</td>
            <td>
              <span class="source-badge">${escapeHtml(item.source || "Archive")}</span>
              ${
                item.download_url
                  ? `<div style="margin-top:3px;"><a href="${escapeHtml(
                      item.download_url
                    )}" target="_blank" rel="noopener noreferrer" style="font-size:0.72rem;color:#60a5fa;text-decoration:none;">🔗 Xem file gốc ↗</a></div>`
                  : ""
              }
            </td>
            <td class="text-right">
              <button type="button" class="btn btn-primary btn-sm btn-pick-book" data-idx="${realIdx}">
                ⚡ Chọn &amp; Dịch bản này
              </button>
            </td>
          </tr>
        `;
      })
      .join("");

    tbody.querySelectorAll(".btn-pick-book").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const idx = Number(btn.getAttribute("data-idx"));
        const chosen = searchResultsCache[idx];
        if (chosen) {
          await startJobFromSearchResult(chosen, btn);
        }
      });
    });
  }

  async function startJobFromSearchResult(bookItem, triggerBtn) {
    if (triggerBtn) triggerBtn.disabled = true;
    const config = getCurrentQuickConfig();
    try {
      showToast(
        `Đang tải & dịch [${(bookItem.format || "").toUpperCase()}] "${bookItem.title}"...`,
        "info"
      );
      const res = await fetch("/api/jobs/from-search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ book: bookItem, config }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.detail || "Không thể khởi tạo tiến trình dịch.");
      }

      activeJobId = data.job_id;
      renderedPreviewCount = 0;
      await fetchJobsList();
      selectJob(activeJobId);
      el("studioActiveView")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) {
      showToast(err.message || "Lỗi khi bắt đầu dịch phiên bản đã chọn", "error");
    } finally {
      if (triggerBtn) triggerBtn.disabled = false;
    }
  }

  // =========================================================================
  // 3. DRAG & DROP FILE UPLOAD
  // =========================================================================
  function handleFileChosen(file) {
    if (!file) return;
    selectedUploadFile = file;
    if (el("selectedFileName")) el("selectedFileName").textContent = file.name;
    if (el("selectedFileSize"))
      el("selectedFileSize").textContent = `(${formatBytes(file.size)})`;
    el("fileDropzone")?.classList.add("hidden");
    el("selectedFileBar")?.classList.remove("hidden");
  }

  function clearSelectedFile() {
    selectedUploadFile = null;
    if (el("fileUploadInput")) el("fileUploadInput").value = "";
    el("selectedFileBar")?.classList.add("hidden");
    el("fileDropzone")?.classList.remove("hidden");
  }

  async function startUploadTranslation() {
    if (!selectedUploadFile) {
      showToast("Vui lòng chọn một file sách (.epub, .docx, .pdf, .txt) trước!", "warning");
      return;
    }

    const btn = el("btnStartUploadTranslate");
    if (btn) btn.disabled = true;

    try {
      const config = getCurrentQuickConfig();
      const formData = new FormData();
      formData.append("file", selectedUploadFile);
      formData.append("config", JSON.stringify(config));

      showToast(`Đang tải lên & bắt đầu dịch file "${selectedUploadFile.name}"...`, "info");
      const res = await fetch("/api/jobs/upload", {
        method: "POST",
        body: formData,
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.detail || "Không thể tải file lên để dịch.");
      }

      clearSelectedFile();
      activeJobId = data.job_id;
      renderedPreviewCount = 0;
      await fetchJobsList();
      selectJob(activeJobId);
    } catch (err) {
      showToast(err.message || "Lỗi khi dịch file tải lên", "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // =========================================================================
  // 4. JOBS SIDEBAR & LIVE TRANSLATION STUDIO
  // =========================================================================
  async function fetchJobsList() {
    try {
      const res = await fetch("/api/jobs");
      if (!res.ok) return;
      const data = await res.json();
      jobsList = data.jobs || [];

      if (!activeJobId && jobsList.length > 0) {
        activeJobId = jobsList[0].id;
      }

      renderJobsSidebar();

      if (activeJobId) {
        const active = jobsList.find((j) => j.id === activeJobId);
        if (active) {
          renderActiveJobStudio(active);
        }
      }
    } catch (err) {
      console.debug("Polling jobs error:", err);
    }
  }

  function selectJob(jobId) {
    if (activeJobId !== jobId) {
      activeJobId = jobId;
      renderedPreviewCount = 0;
      if (el("livePreviewList")) el("livePreviewList").innerHTML = "";
    }
    renderJobsSidebar();
    const job = jobsList.find((j) => j.id === jobId);
    if (job) {
      renderActiveJobStudio(job);
    }
  }

  function renderJobsSidebar() {
    const container = el("jobsListContainer");
    const badge = el("jobsTotalBadge");
    if (badge) badge.textContent = String(jobsList.length);
    if (!container) return;

    if (jobsList.length === 0) {
      container.innerHTML = `
        <div class="empty-jobs-state" id="emptyJobsState">
          <div class="empty-icon">🚀</div>
          <p>Chưa có tiến trình dịch nào.</p>
          <span>Hãy nhập tên sách ở trên và bấm <strong>1-Click Auto</strong> hoặc kéo thả file vào để bắt đầu!</span>
        </div>
      `;
      return;
    }

    container.innerHTML = jobsList
      .map((job) => {
        const isActive = job.id === activeJobId;
        const fmt = String(job.format || "epub").toUpperCase();
        const fmtClass = getFormatBadgeClass(job.format);
        const stMeta = STATUS_LABELS[job.status] || STATUS_LABELS.queued;
        const pct = Math.min(100, Math.max(0, Math.round(job.progress || 0)));

        return `
          <div class="job-card-item ${isActive ? "active" : ""}" data-job-id="${escapeHtml(job.id)}">
            <div class="job-card-top">
              <span class="fmt-badge ${fmtClass}">${escapeHtml(fmt)}</span>
              <span class="job-status-badge ${stMeta.cls}">${escapeHtml(stMeta.text)}</span>
            </div>
            <div class="job-card-title" title="${escapeHtml(job.title)}">${escapeHtml(job.title)}</div>
            <div class="job-card-meta">
              <span>${escapeHtml(job.target_lang || "Tiếng Việt")}</span>
              <strong>${pct}%</strong>
            </div>
            <div class="job-mini-bar">
              <div class="job-mini-fill" style="width: ${pct}%"></div>
            </div>
          </div>
        `;
      })
      .join("");

    container.querySelectorAll(".job-card-item").forEach((card) => {
      card.addEventListener("click", () => {
        const jid = card.getAttribute("data-job-id");
        if (jid) selectJob(jid);
      });
    });
  }

  function renderActiveJobStudio(job) {
    const emptyView = el("studioEmptyView");
    const activeView = el("studioActiveView");
    if (!job) {
      emptyView?.classList.remove("hidden");
      activeView?.classList.add("hidden");
      return;
    }

    emptyView?.classList.add("hidden");
    activeView?.classList.remove("hidden");

    // Header badges & title
    const fmt = String(job.format || "epub").toUpperCase();
    const fmtBadge = el("activeJobFormatBadge");
    if (fmtBadge) {
      fmtBadge.className = `fmt-badge ${getFormatBadgeClass(job.format)}`;
      fmtBadge.textContent = fmt;
    }

    const stMeta = STATUS_LABELS[job.status] || STATUS_LABELS.queued;
    const stBadge = el("activeJobStatusBadge");
    if (stBadge) {
      stBadge.className = `job-status-badge ${stMeta.cls}`;
      stBadge.textContent = stMeta.text;
    }

    if (el("activeJobLangBadge")) {
      const modeStr = job.bilingual ? "Song ngữ" : "Đơn ngữ";
      el("activeJobLangBadge").textContent = `➔ ${job.target_lang || "Tiếng Việt"} (${modeStr})`;
    }

    if (el("activeJobEditedBadge")) {
      const edCount = job.edited_chunks || 0;
      el("activeJobEditedBadge").textContent =
        job.enable_editor_agent === false
          ? "⚡ Agent 4: Tắt"
          : `✨ Agent 4 đã chuốt: ${edCount} đoạn`;
    }

    if (el("activeJobTitle")) el("activeJobTitle").textContent = job.title || "Sách đang dịch";
    if (el("activeJobMessage")) el("activeJobMessage").textContent = job.message || "";

    // Cancel button visibility
    const isRunning = [
      "queued",
      "searching",
      "downloading",
      "extracting_glossary",
      "awaiting_glossary_approval",
      "translating",
      "editing",
      "packaging",
    ].includes(job.status);
    const cancelBtn = el("btnCancelActiveJob");
    if (cancelBtn) {
      cancelBtn.style.display = isRunning ? "inline-flex" : "none";
    }

    // Glossary Approval Banner & Tab Button
    const isAwaitingGlossary = job.status === "awaiting_glossary_approval";
    el("glossaryApprovalBanner")?.classList.toggle("hidden", !isAwaitingGlossary);
    el("btnApproveGlossaryTab")?.classList.toggle("hidden", !isAwaitingGlossary);

    // Highlight active Agent Node Card in top 5-Agent Pipeline
    const activeAgentNum = Number(job.active_agent || job.stage || 1);
    document.querySelectorAll("#agentPipelineCards .agent-node-card").forEach((node) => {
      const n = Number(node.getAttribute("data-agent-node"));
      node.classList.toggle("agent-running", isRunning && n === activeAgentNum);
    });

    // Live indicator
    const liveBadge = el("liveIndicatorBadge");
    if (liveBadge) {
      liveBadge.textContent = isRunning ? "LIVE" : job.status === "completed" ? "DONE" : "STOPPED";
    }

    // Stepper 5 steps
    const currentStage = Number(job.stage) || 1;
    const stepper = el("jobStepper");
    if (stepper) {
      stepper.querySelectorAll(".step-item").forEach((stepEl) => {
        const sNum = Number(stepEl.getAttribute("data-step"));
        stepEl.classList.remove("active", "completed");
        if (job.status === "completed" || sNum < currentStage) {
          stepEl.classList.add("completed");
        } else if (sNum === currentStage) {
          stepEl.classList.add("active");
        }
      });
    }

    // Progress bar & metrics
    const pct = Math.min(100, Math.max(0, Number(job.progress || 0)));
    if (el("activeJobPercent")) el("activeJobPercent").textContent = `${pct.toFixed(1)}%`;
    if (el("activeJobProgressFill")) el("activeJobProgressFill").style.width = `${pct}%`;

    const doneChunks = job.done_chunks || 0;
    const totalChunks = job.total_chunks || 0;
    if (el("activeJobChunks")) {
      el("activeJobChunks").textContent =
        totalChunks > 0
          ? `(${doneChunks} / ${totalChunks} đoạn văn)`
          : job.status === "completed"
          ? "(Hoàn tất)"
          : "(Đang chuẩn bị...)";
    }

    if (el("activeJobSpeed")) {
      const spd = job.speed_chunks_per_min || 0;
      el("activeJobSpeed").textContent =
        spd > 0 ? `⚡ Tốc độ: ${spd} đoạn/phút` : "⚡ Tốc độ: Đa luồng Async";
    }
    if (el("activeJobEta")) {
      el("activeJobEta").textContent = `⏱️ ETA: ${formatEta(job.eta_seconds)}`;
    }

    // Download Outputs Bar
    const dlBar = el("downloadOutputsBar");
    const outputs = job.outputs || {};
    if (job.status === "completed" && (outputs.primary || outputs.docx || outputs.bilingual)) {
      dlBar?.classList.remove("hidden");
      const btnPrimary = el("btnDownloadPrimary");
      const btnBi = el("btnDownloadBilingual");
      const btnDocx = el("btnDownloadDocx");

      if (el("primaryExtText")) el("primaryExtText").textContent = `.${fmt}`;

      if (btnPrimary) {
        if (outputs.primary) {
          btnPrimary.href = outputs.primary;
          btnPrimary.classList.remove("hidden");
        } else {
          btnPrimary.classList.add("hidden");
        }
      }
      if (btnBi) {
        if (outputs.bilingual && outputs.bilingual !== outputs.primary) {
          btnBi.href = outputs.bilingual;
          btnBi.classList.remove("hidden");
        } else {
          btnBi.classList.add("hidden");
        }
      }
      if (btnDocx) {
        if (outputs.docx) {
          btnDocx.href = outputs.docx;
          btnDocx.classList.remove("hidden");
        } else {
          btnDocx.classList.add("hidden");
        }
      }
    } else {
      dlBar?.classList.add("hidden");
    }

    // Render Live Preview Stream (with Agent 3 Draft vs Agent 4 Polished + Inline Edit)
    renderLivePreviews(job.previews || []);

    // Render Agent Handoff Logs
    renderAgentLogs(job.agent_logs || []);

    // Render Glossary Table
    renderGlossary(job.glossary || []);
  }

  function renderLivePreviews(previews) {
    const stream = el("livePreviewList");
    if (!stream) return;

    // Do not overwrite DOM if user is currently editing a segment inline
    if (stream.querySelector(".inline-edit-area")) {
      return;
    }

    if (!previews || previews.length === 0) {
      stream.innerHTML = `
        <div class="preview-empty-hint">
          Đang chờ những đoạn văn đầu tiên từ Agent 3 (Dịch thuật) &amp; Agent 4 (Biên tập)...
        </div>
      `;
      renderedPreviewCount = 0;
      return;
    }

    if (previews.length !== renderedPreviewCount) {
      renderedPreviewCount = previews.length;
      stream.innerHTML = previews
        .map((p, i) => {
          const segIndex = p.index || i + 1;
          const editedHtml =
            p.was_edited && p.draft
              ? `
                <div class="preview-editor-badge">✨ Agent 4 đã chuốt văn phong (${escapeHtml(
                  p.editor_model || "Editor"
                )})</div>
                <div class="preview-dst-text">${escapeHtml(p.dst)}</div>
                <div class="preview-draft-box">
                  <strong>Bản dịch thô (Agent 3):</strong> ${escapeHtml(p.draft)}
                </div>
              `
              : `<div class="preview-dst-text">${escapeHtml(p.dst)}</div>`;

          return `
          <div class="preview-pair-card" data-preview-idx="${i}" data-seg-index="${segIndex}">
            <div class="preview-col-source">
              <div class="preview-top-bar">
                <span class="preview-chunk-badge">#Đoạn ${segIndex} · Gốc</span>
              </div>
              <div class="preview-src-text">${escapeHtml(p.src)}</div>
            </div>
            <div class="preview-col-target">
              <div class="preview-top-bar">
                <span class="preview-chunk-badge">#Đoạn ${segIndex} · Xuất bản</span>
                <div class="preview-card-actions">
                  <button type="button" class="btn-xs btn-inline-edit" data-preview-idx="${i}" title="Sửa trực tiếp đoạn dịch này">✏️ Sửa tay</button>
                  <button type="button" class="btn-xs btn-repolish-seg" data-preview-idx="${i}" title="Yêu cầu Agent 4 (Tổng Biên Tập) chuốt lại đoạn văn này">✨ Agent 4 chuốt lại</button>
                </div>
              </div>
              <div class="preview-target-body">
                ${editedHtml}
              </div>
            </div>
          </div>
        `;
        })
        .join("");

      bindPreviewCardActions(stream, previews);

      if (el("autoScrollPreviewCheckbox")?.checked) {
        stream.scrollTop = stream.scrollHeight;
      }
    }
  }

  function bindPreviewCardActions(stream, previews) {
    stream.querySelectorAll(".btn-inline-edit").forEach((btn) => {
      btn.addEventListener("click", () => {
        const idx = Number(btn.getAttribute("data-preview-idx"));
        const p = previews[idx];
        const card = btn.closest(".preview-pair-card");
        const body = card?.querySelector(".preview-target-body");
        if (!p || !body) return;

        body.innerHTML = `
          <textarea class="inline-edit-area">${escapeHtml(p.dst || "")}</textarea>
          <div class="inline-edit-btns">
            <button type="button" class="btn btn-ghost-sm btn-cancel-inline">Hủy</button>
            <button type="button" class="btn btn-primary btn-sm btn-save-inline">💾 Lưu vào bản xuất bản</button>
          </div>
        `;
        const area = body.querySelector(".inline-edit-area");
        area?.focus();

        body.querySelector(".btn-cancel-inline")?.addEventListener("click", () => {
          renderedPreviewCount = -1;
          renderLivePreviews(previews);
        });

        body.querySelector(".btn-save-inline")?.addEventListener("click", async () => {
          const newDst = (area?.value || "").trim();
          if (!newDst || !activeJobId) return;
          try {
            const res = await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/previews/update`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                index: p.index || idx + 1,
                src: p.src,
                dst: newDst,
              }),
            });
            const data = await res.json();
            if (!res.ok || !data.ok) throw new Error(data.detail || "Không thể lưu bản sửa.");
            p.dst = newDst;
            p.was_edited = true;
            p.editor_model = "Sửa thủ công (Inline Edit)";
            renderedPreviewCount = -1;
            renderLivePreviews(previews);
            showToast(
              "Đã lưu đoạn sửa vào Bộ nhớ đệm! Bạn có thể bấm 'Đóng gói lại file (Agent 5)' khi hoàn tất.",
              "success"
            );
          } catch (err) {
            showToast(err.message || "Lỗi khi lưu đoạn sửa", "error");
          }
        });
      });
    });

    stream.querySelectorAll(".btn-repolish-seg").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const idx = Number(btn.getAttribute("data-preview-idx"));
        const p = previews[idx];
        if (!p || !activeJobId) return;
        const origText = btn.textContent;
        btn.disabled = true;
        btn.textContent = "⏳ Agent 4 đang chuốt...";
        try {
          const res = await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/previews/repolish`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              index: p.index || idx + 1,
              src: p.src,
              draft: p.dst || p.draft || "",
            }),
          });
          const data = await res.json();
          if (!res.ok || !data.ok) throw new Error(data.detail || "Agent 4 không thể chuốt lại đoạn này.");
          p.draft = p.draft || p.dst;
          p.dst = data.polished;
          p.was_edited = true;
          p.editor_model = data.preview?.editor_model || "Agent 4 · Critic";
          renderedPreviewCount = -1;
          renderLivePreviews(previews);
          showToast(`Agent 4 đã chuốt lại đoạn #${p.index || idx + 1}!`, "success");
        } catch (err) {
          showToast(err.message || "Lỗi khi gọi Agent 4", "error");
          btn.disabled = false;
          btn.textContent = origText;
        }
      });
    });
  }

  function renderAgentLogs(logs) {
    const badge = el("agentLogsCountBadge");
    const listEl = el("agentLogsList");
    if (badge) badge.textContent = String(logs.length || 0);
    if (!listEl) return;

    if (!logs || logs.length === 0) {
      listEl.innerHTML = `
        <div class="preview-empty-hint">
          Chưa có sự kiện bàn giao nào giữa các Agent.
        </div>
      `;
      return;
    }

    listEl.innerHTML = logs
      .slice()
      .reverse()
      .map((item) => {
        const dt = item.ts
          ? new Date(item.ts * 1000).toLocaleTimeString("vi-VN")
          : "";
        return `
          <div class="agent-log-item">
            <div class="agent-log-left">
              <span class="agent-log-title">${escapeHtml(item.agent_name || `Agent ${item.agent_id}`)}</span>
              <span class="agent-log-detail">${escapeHtml(item.detail || "")}</span>
            </div>
            <div class="agent-log-meta">
              <span>${escapeHtml(item.model || "")}</span>
              <span>${escapeHtml(dt)}</span>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function renderGlossary(glossary) {
    const badge = el("glossaryCountBadge");
    const tbody = el("glossaryTbody");
    if (badge) badge.textContent = String(glossary.length || 0);
    if (!tbody) return;

    // Avoid clobbering user's focus while typing inside glossary inputs
    if (document.activeElement && tbody.contains(document.activeElement)) {
      return;
    }

    if (!glossary || glossary.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="5" class="text-center text-muted" style="padding: 1.5rem;">
            Chưa có thuật ngữ nào. Bạn có thể tự thêm thuật ngữ ở thanh công cụ phía trên hoặc để Agent 2 tự động trích xuất.
          </td>
        </tr>
      `;
      return;
    }

    tbody.innerHTML = glossary
      .map(
        (g, idx) => `
        <tr data-glossary-row="${idx}">
          <td>${idx + 1}</td>
          <td>
            <input type="text" class="glossary-inline-input gl-src" value="${escapeHtml(g.src)}" />
          </td>
          <td>
            <input type="text" class="glossary-inline-input gl-dst" value="${escapeHtml(g.dst)}" />
          </td>
          <td>
            <input type="text" class="glossary-inline-input gl-note" value="${escapeHtml(
              g.note || g.category || ""
            )}" placeholder="Thuật ngữ / Xưng hô" />
          </td>
          <td class="text-right">
            <button type="button" class="btn-xs btn-del-glossary" data-idx="${idx}" title="Xóa thuật ngữ này">🗑️</button>
          </td>
        </tr>
      `
      )
      .join("");

    tbody.querySelectorAll(".btn-del-glossary").forEach((btn) => {
      btn.addEventListener("click", () => {
        btn.closest("tr")?.remove();
      });
    });
  }

  function collectGlossaryFromTable() {
    const tbody = el("glossaryTbody");
    if (!tbody) return [];
    const items = [];
    tbody.querySelectorAll("tr[data-glossary-row]").forEach((tr) => {
      const src = (tr.querySelector(".gl-src")?.value || "").trim();
      const dst = (tr.querySelector(".gl-dst")?.value || "").trim();
      const note = (tr.querySelector(".gl-note")?.value || "").trim();
      if (src && dst) {
        items.push({ src, dst, note });
      }
    });
    return items;
  }

  async function saveActiveJobGlossary(silent = false) {
    if (!activeJobId) {
      showToast("Hãy chọn một chiến dịch sách trước khi lưu từ điển.", "warning");
      return null;
    }
    const items = collectGlossaryFromTable();
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/glossary`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ glossary: items }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.detail || "Không thể lưu từ điển.");
      const job = jobsList.find((j) => j.id === activeJobId);
      if (job) job.glossary = data.glossary;
      renderGlossary(data.glossary);
      if (!silent) {
        showToast(`Đã lưu ${data.glossary.length} thuật ngữ cho chiến dịch hiện tại!`, "success");
      }
      return data.glossary;
    } catch (err) {
      if (!silent) showToast(err.message || "Lỗi khi lưu từ điển", "error");
      return null;
    }
  }

  async function approveGlossaryAndResume() {
    if (!activeJobId) return;
    const items = collectGlossaryFromTable();
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/glossary/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ glossary: items }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.detail || "Không thể duyệt từ điển.");
      showToast(
        "Đã duyệt Từ điển Agent 2! Hệ thống đã bàn giao cho Agent 3 & Agent 4 bắt đầu dịch.",
        "success"
      );
      await fetchJobsList();
    } catch (err) {
      showToast(err.message || "Lỗi khi duyệt từ điển", "error");
    }
  }

  async function repackageActiveJob() {
    if (!activeJobId) return;
    const btn = el("btnRepackageOutputs");
    if (btn) btn.disabled = true;
    try {
      showToast(
        "Agent 5 đang đóng gói lại các file xuất bản (.EPUB/.PDF/.DOCX) với bản hiệu đính mới nhất...",
        "info"
      );
      const res = await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/repackage`, {
        method: "POST",
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.detail || "Không thể đóng gói lại file.");
      renderedPreviewCount = 0;
      await fetchJobsList();
    } catch (err) {
      showToast(err.message || "Lỗi khi đóng gói lại file", "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function runQuickSandboxTranslate() {
    const srcArea = el("quickSourceTextarea");
    const resultBox = el("quickResultContainer");
    const metaBox = el("quickTranslateMetaText");
    const btn = el("btnRunQuickTranslate");
    const text = (srcArea?.value || "").trim();
    if (!text) {
      showToast("Vui lòng dán đoạn văn hoặc chương cần dịch thử vào ô bên trái!", "warning");
      srcArea?.focus();
      return;
    }

    if (btn) btn.disabled = true;
    if (metaBox) metaBox.textContent = "⏳ Agent 3 đang dịch thô & Agent 4 đang chuốt văn phong...";
    if (resultBox) {
      resultBox.innerHTML = `<div class="text-muted text-xs">Đang xử lý qua dây chuyền Agent 3 ➔ Agent 4...</div>`;
    }

    try {
      const config = getCurrentQuickConfig();
      const res = await fetch("/api/translate/quick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, config }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.detail || "Lỗi khi dịch nhanh văn bản.");

      if (metaBox) {
        metaBox.textContent = `✅ Hoàn tất ${data.paragraphs_count || 1} đoạn trong ${data.elapsed_seconds}s (${data.translator_model}${data.editor_model ? " ➔ " + data.editor_model : ""})`;
      }
      if (resultBox) {
        const diffHtml =
          data.editor_model && data.draft && data.draft.trim() !== data.polished.trim()
            ? `<div class="preview-draft-box" style="margin-top:0.65rem;"><strong>Bản dịch thô ban đầu (Agent 3):</strong>\n${escapeHtml(
                data.draft
              )}</div>`
            : "";
        resultBox.innerHTML = `<div id="quickPolishedTextOutput">${escapeHtml(
          data.polished
        )}</div>${diffHtml}`;
      }
    } catch (err) {
      if (metaBox) metaBox.textContent = "";
      if (resultBox) {
        resultBox.innerHTML = `<div style="color:#fda4af;">❌ Lỗi: ${escapeHtml(err.message)}</div>`;
      }
      showToast(err.message || "Lỗi khi dịch nhanh", "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function clearTranslationCache() {
    try {
      const res = await fetch("/api/cache/clear", { method: "POST" });
      const data = await res.json();
      if (res.ok && data.ok) {
        if (el("headerCacheCountText")) el("headerCacheCountText").textContent = "0";
        showToast(data.message || "Đã xóa sạch Bộ nhớ đệm dịch thuật!", "success");
      }
    } catch (err) {
      showToast("Không thể xóa Cache: " + err.message, "error");
    }
  }

  async function cancelActiveJob() {
    if (!activeJobId) return;
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/cancel`, {
        method: "POST",
      });
      if (res.ok) {
        showToast("Đã gửi lệnh dừng tiến trình dịch.", "warning");
        await fetchJobsList();
      }
    } catch (err) {
      showToast("Không thể hủy tiến trình: " + err.message, "error");
    }
  }

  // =========================================================================
  // 5. EVENT LISTENERS & INITIALIZATION
  // =========================================================================
  function bindEvents() {
    // Mode Switcher (Search vs Upload vs Quick Text Sandbox)
    const tabSearch = el("tabSearchMode");
    const tabUpload = el("tabUploadMode");
    const tabQuickText = el("tabQuickTextMode");
    const paneSearch = el("searchModePane");
    const paneUpload = el("uploadModePane");
    const paneQuickText = el("quickTextModePane");

    function switchDispatcherTab(mode) {
      el("composerSection")?.classList.remove("hidden");
      tabSearch?.classList.toggle("active", mode === "search");
      tabUpload?.classList.toggle("active", mode === "upload");
      tabQuickText?.classList.toggle("active", mode === "quicktext");

      document.querySelectorAll(".sidebar-nav-item[data-nav-mode]").forEach((navBtn) => {
        navBtn.classList.toggle("active", navBtn.getAttribute("data-nav-mode") === mode);
      });

      paneSearch?.classList.toggle("hidden", mode !== "search");
      paneUpload?.classList.toggle("hidden", mode !== "upload");
      paneQuickText?.classList.toggle("hidden", mode !== "quicktext");

      if (mode === "search") {
        setTimeout(() => el("bookQueryInput")?.focus(), 40);
      } else if (mode === "quicktext") {
        setTimeout(() => el("quickSourceTextarea")?.focus(), 40);
      }
    }

    tabSearch?.addEventListener("click", () => switchDispatcherTab("search"));
    tabUpload?.addEventListener("click", () => switchDispatcherTab("upload"));
    tabQuickText?.addEventListener("click", () => switchDispatcherTab("quicktext"));

    document.querySelectorAll(".sidebar-nav-item[data-nav-mode]").forEach((navBtn) => {
      navBtn.addEventListener("click", () => {
        const m = navBtn.getAttribute("data-nav-mode") || "search";
        switchDispatcherTab(m);
        el("composerSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });

    el("btnNewCampaignSidebar")?.addEventListener("click", () => {
      switchDispatcherTab("search");
      el("composerSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
      if (el("bookQueryInput")) {
        el("bookQueryInput").select();
      }
    });

    // Collapsible 5-Agent Composer Workbench Toggle
    el("btnToggleComposer")?.addEventListener("click", () => {
      const comp = el("composerSection");
      if (!comp) return;
      const isHidden = comp.classList.toggle("hidden");
      const lbl = el("toggleComposerLabel");
      if (lbl) {
        lbl.textContent = isHidden ? "Hiện Trạm Điều Phối 5 Agent" : "Trạm Điều Phối 5 Agent";
      }
    });

    // Reader Typography Toggle (Literary Serif vs Modern Sans)
    el("btnToggleSerifFont")?.addEventListener("click", (e) => {
      const btn = e.currentTarget;
      const isSerif = document.body.classList.toggle("serif-preview");
      btn.classList.toggle("active-tool", isSerif);
      showToast(
        isSerif
          ? "Đã bật phông chữ Văn học (Lora Serif) cho Bàn Hiệu Đính Song Ngữ."
          : "Đã chuyển sang phông chữ Hiện đại (Inter Sans).",
        "info"
      );
    });

    // Theme Toggle (Obsidian Dark vs Warm Paper Light)
    el("btnToggleTheme")?.addEventListener("click", () => {
      const root = document.documentElement;
      const current = root.getAttribute("data-theme") || "dark";
      const next = current === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      root.classList.toggle("dark", next === "dark");
      try {
        localStorage.setItem("bth_theme", next);
      } catch (_) {}
      showToast(
        next === "light"
          ? "Đã chuyển sang giao diện Sáng (Màu giấy đọc sách)."
          : "Đã chuyển sang giao diện Tối (Studio Dark).",
        "info"
      );
    });

    // Restore saved theme on load
    try {
      const savedTheme = localStorage.getItem("bth_theme");
      if (savedTheme === "light" || savedTheme === "dark") {
        document.documentElement.setAttribute("data-theme", savedTheme);
        document.documentElement.classList.toggle("dark", savedTheme === "dark");
      }
    } catch (_) {}

    // Quick Sandbox & Cache buttons
    el("btnRunQuickTranslate")?.addEventListener("click", runQuickSandboxTranslate);
    el("btnClearQuickText")?.addEventListener("click", () => {
      if (el("quickSourceTextarea")) el("quickSourceTextarea").value = "";
      if (el("quickResultContainer")) {
        el("quickResultContainer").innerHTML = `<div class="text-muted text-xs">Bản dịch chuẩn xuất bản của Agent 4 và bản dịch thô của Agent 3 sẽ hiển thị đối chiếu tại đây...</div>`;
      }
      if (el("quickTranslateMetaText")) el("quickTranslateMetaText").textContent = "";
    });
    el("btnCopyQuickResult")?.addEventListener("click", () => {
      const out = el("quickPolishedTextOutput")?.textContent || "";
      if (!out.trim()) {
        showToast("Chưa có nội dung bản dịch để copy.", "warning");
        return;
      }
      navigator.clipboard.writeText(out).then(() => {
        showToast("Đã copy bản dịch đã chuốt vào Clipboard!", "success");
      });
    });
    el("btnClearCacheQuick")?.addEventListener("click", clearTranslationCache);

    // Glossary Editor & Approval actions
    el("btnAddGlossaryTerm")?.addEventListener("click", () => {
      const srcIn = el("newGlossarySrcInput");
      const dstIn = el("newGlossaryDstInput");
      const noteIn = el("newGlossaryNoteInput");
      const src = (srcIn?.value || "").trim();
      const dst = (dstIn?.value || "").trim();
      const note = (noteIn?.value || "").trim();
      if (!src || !dst) {
        showToast("Vui lòng nhập cả Thuật ngữ gốc và Bản dịch chuẩn!", "warning");
        return;
      }
      const current = collectGlossaryFromTable();
      current.unshift({ src, dst, note });
      renderGlossary(current);
      if (srcIn) srcIn.value = "";
      if (dstIn) dstIn.value = "";
      if (noteIn) noteIn.value = "";
      if (activeJobId) saveActiveJobGlossary(false);
    });

    el("btnSaveGlossaryChanges")?.addEventListener("click", () => saveActiveJobGlossary(false));
    el("btnApproveGlossaryBanner")?.addEventListener("click", approveGlossaryAndResume);
    el("btnApproveGlossaryTab")?.addEventListener("click", approveGlossaryAndResume);
    el("btnRepackageOutputs")?.addEventListener("click", repackageActiveJob);

    el("btnExportGlossary")?.addEventListener("click", () => {
      const items = collectGlossaryFromTable();
      if (!items.length) {
        showToast("Bảng từ điển đang trống, chưa có dữ liệu để xuất.", "warning");
        return;
      }
      const blob = new Blob([JSON.stringify(items, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `glossary_${activeJobId || "book"}.json`;
      a.click();
      URL.revokeObjectURL(url);
    });

    el("btnImportGlossary")?.addEventListener("click", () => {
      el("importGlossaryFileInput")?.click();
    });
    el("importGlossaryFileInput")?.addEventListener("change", async (e) => {
      const f = e.target.files?.[0];
      if (!f) return;
      try {
        const text = await f.text();
        let imported = [];
        if (f.name.toLowerCase().endsWith(".json")) {
          const parsed = JSON.parse(text);
          if (Array.isArray(parsed)) imported = parsed;
        } else {
          // CSV parser (src,dst,note)
          text.split(/\r?\n/).forEach((line) => {
            const parts = line.split(",");
            if (parts.length >= 2) {
              const s = parts[0].trim().replace(/^"|"$/g, "");
              const d = parts[1].trim().replace(/^"|"$/g, "");
              const n = (parts.slice(2).join(",") || "").trim().replace(/^"|"$/g, "");
              if (s && d && s.toLowerCase() !== "src") {
                imported.push({ src: s, dst: d, note: n });
              }
            }
          });
        }
        if (imported.length > 0) {
          const merged = [...imported, ...collectGlossaryFromTable()];
          renderGlossary(merged);
          if (activeJobId) await saveActiveJobGlossary(false);
          else showToast(`Đã nhập ${imported.length} thuật ngữ vào bảng!`, "success");
        }
      } catch (err) {
        showToast("File từ điển không đúng định dạng JSON/CSV.", "error");
      }
      e.target.value = "";
    });

    // Agent 2 & Agent 4 On/Off visual toggle
    const ag2Check = el("autoGlossaryCheckbox");
    const ag4Check = el("enableEditorAgentCheckbox");
    ag2Check?.addEventListener("change", () => {
      document
        .querySelector('.agent-node-card[data-agent-node="2"]')
        ?.classList.toggle("agent-disabled", !ag2Check.checked);
    });
    ag4Check?.addEventListener("change", () => {
      document
        .querySelector('.agent-node-card[data-agent-node="4"]')
        ?.classList.toggle("agent-disabled", !ag4Check.checked);
    });

    // Search Input & Quick Chips
    const qInput = el("bookQueryInput");
    const clearBtn = el("clearQueryBtn");
    qInput?.addEventListener("input", () => {
      if (qInput.value.trim()) {
        clearBtn?.classList.remove("hidden");
      } else {
        clearBtn?.classList.add("hidden");
      }
    });
    qInput?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        searchAndShowVersions();
      }
    });
    clearBtn?.addEventListener("click", () => {
      if (qInput) {
        qInput.value = "";
        qInput.focus();
      }
      clearBtn.classList.add("hidden");
    });

    document.querySelectorAll(".quick-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const sample = chip.getAttribute("data-sample") || chip.textContent;
        if (qInput) {
          qInput.value = sample;
          clearBtn?.classList.remove("hidden");
          searchAndShowVersions();
        }
      });
    });

    // Edition Filter Pills inside Search Results Drawer
    document.querySelectorAll("#editionFilterPills [data-fmt-filter]").forEach((btn) => {
      btn.addEventListener("click", () => {
        applyEditionFilter(btn.getAttribute("data-fmt-filter") || "all");
      });
    });

    // Hero Main Buttons
    el("btnOneClickAuto")?.addEventListener("click", startOneClickAutoJob);
    el("btnSearchAndPick")?.addEventListener("click", searchAndShowVersions);
    el("closeSearchResultsBtn")?.addEventListener("click", () => {
      el("searchResultsSection")?.classList.add("hidden");
    });

    // Upload Dropzone
    const dropzone = el("fileDropzone");
    const fileInput = el("fileUploadInput");
    dropzone?.addEventListener("click", () => fileInput?.click());
    fileInput?.addEventListener("change", (e) => {
      const f = e.target.files?.[0];
      if (f) handleFileChosen(f);
    });
    dropzone?.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropzone.classList.add("drag-over");
    });
    dropzone?.addEventListener("dragleave", () => {
      dropzone.classList.remove("drag-over");
    });
    dropzone?.addEventListener("drop", (e) => {
      e.preventDefault();
      dropzone.classList.remove("drag-over");
      const f = e.dataTransfer?.files?.[0];
      if (f) handleFileChosen(f);
    });
    el("btnCancelFile")?.addEventListener("click", clearSelectedFile);
    el("btnStartUploadTranslate")?.addEventListener("click", startUploadTranslation);

    // Studio Subtabs (Live Preview vs Agent Logs vs Glossary)
    const subPreviewBtn = el("subtabPreviewBtn");
    const subAgentLogsBtn = el("subtabAgentLogsBtn");
    const subGlossaryBtn = el("subtabGlossaryBtn");
    const subPreviewPane = el("subtabPreviewPane");
    const subAgentLogsPane = el("subtabAgentLogsPane");
    const subGlossaryPane = el("subtabGlossaryPane");

    function activateSubtab(which) {
      subPreviewBtn?.classList.toggle("active", which === "preview");
      subAgentLogsBtn?.classList.toggle("active", which === "agentlogs");
      subGlossaryBtn?.classList.toggle("active", which === "glossary");

      subPreviewPane?.classList.toggle("hidden", which !== "preview");
      subAgentLogsPane?.classList.toggle("hidden", which !== "agentlogs");
      subGlossaryPane?.classList.toggle("hidden", which !== "glossary");
    }

    subPreviewBtn?.addEventListener("click", () => activateSubtab("preview"));
    subAgentLogsBtn?.addEventListener("click", () => activateSubtab("agentlogs"));
    subGlossaryBtn?.addEventListener("click", () => activateSubtab("glossary"));

    // Jobs Controls
    el("refreshJobsBtn")?.addEventListener("click", fetchJobsList);
    el("btnCancelActiveJob")?.addEventListener("click", cancelActiveJob);

    // Settings Modal & Header Pills
    el("openSettingsBtn")?.addEventListener("click", () => openSettingsModal(false));
    el("headerProviderPill")?.addEventListener("click", () => openSettingsModal(false));
    el("headerKeysPill")?.addEventListener("click", () => openSettingsModal(false));
    el("btnQuickAddGeminiKey")?.addEventListener("click", () => openSettingsModal(true));
    el("closeSettingsBtn")?.addEventListener("click", closeSettingsModal);
    el("btnCancelSettings")?.addEventListener("click", closeSettingsModal);
    el("btnSaveSettings")?.addEventListener("click", saveSettingsFromModal);
    el("btnTestConnection")?.addEventListener("click", testApiConnection);

    // Quick Model Discovery buttons
    el("btnRefreshModelsQuick")?.addEventListener("click", () => discoverModelsFromServer(true));
    el("btnDiscoverModelsModal")?.addEventListener("click", () => discoverModelsFromServer(true));

    // Quick Engine Mode Pills on main Hero Command Bar
    document.querySelectorAll("#quickEngineModePills .engine-pill").forEach((pill) => {
      pill.addEventListener("click", async () => {
        const prov = pill.getAttribute("data-provider") || "openai";
        appSettings.provider = prov;
        if (prov === "gemini") {
          appSettings.model = appSettings.gemini_model || "gemini-2.5-pro";
          if (!appSettings.gemini_api_keys || appSettings.gemini_api_keys.length === 0) {
            showToast(
              "Hãy dán API Key Gemini Ultra / AI Studio vào Kho 2 để kích hoạt Gemini!",
              "info"
            );
            openSettingsModal(true);
            return;
          }
        } else if (prov === "openai") {
          if (!appSettings.model || appSettings.model.startsWith("gemini") || appSettings.model === "gtx-free") {
            appSettings.model = "cc/claude-sonnet-5-5";
          }
        } else if (prov === "gtx") {
          appSettings.model = "gtx-free";
        }
        syncSettingsToModal();
        await persistSettingsToBackend(true);
        showToast(
          `Đã chuyển sang chế độ: ${PROVIDER_DEFAULTS[prov]?.label || prov.toUpperCase()}`,
          "success"
        );
      });
    });

    // Quick Model Selector on main Hero Command Bar
    el("quickModelSelect")?.addEventListener("change", async (e) => {
      const val = e.target.value;
      if (val === "hybrid:claude+gemini") {
        appSettings.provider = "hybrid";
      } else if (val === "gtx-free") {
        appSettings.provider = "gtx";
        appSettings.model = "gtx-free";
      } else if (val.startsWith("gemini")) {
        appSettings.provider = "gemini";
        appSettings.gemini_model = val;
        appSettings.model = val;
        if (!appSettings.gemini_api_keys || appSettings.gemini_api_keys.length === 0) {
          showToast(
            `Đã chọn ${val} — Hãy dán API Key Gemini Ultra vào Kho 2!`,
            "info"
          );
          openSettingsModal(true);
          return;
        }
      } else if (val.startsWith("deepseek")) {
        appSettings.provider = "openai";
        appSettings.model = val;
      } else if (val.includes("/") && !val.startsWith("cc/")) {
        appSettings.provider = "openrouter";
        appSettings.model = val;
      } else if (val.includes(":")) {
        appSettings.provider = "ollama";
        appSettings.model = val;
      } else {
        appSettings.provider = "openai";
        appSettings.model = val;
      }
      syncSettingsToModal();
      await persistSettingsToBackend(true);
      showToast(`Đã chọn Model dịch sách: ${val}`, "success");
    });

    // Modal Provider Select
    el("settingProviderSelect")?.addEventListener("change", (e) => {
      const val = e.target.value;
      const def = PROVIDER_DEFAULTS[val] || PROVIDER_DEFAULTS.openai;
      if (el("providerHelpText")) el("providerHelpText").innerHTML = def.help;
      el("vaultCardGateway")?.classList.toggle(
        "active-vault",
        val === "openai" || val === "claude" || val === "hybrid"
      );
      el("vaultCardGemini")?.classList.toggle(
        "active-vault",
        val === "gemini" || val === "hybrid"
      );
    });

    // Modal Vault 1 & Vault 2 Model Pickers
    el("settingModelSelectGateway")?.addEventListener("change", (e) => {
      if (el("settingModelInput")) el("settingModelInput").value = e.target.value;
    });
    el("settingGeminiModelSelect")?.addEventListener("change", (e) => {
      if (el("settingGeminiModelInput")) el("settingGeminiModelInput").value = e.target.value;
    });

    // Live Key Count Badges inside Modal
    el("settingApiKeysTextarea")?.addEventListener("input", (e) => {
      const count = e.target.value
        .split(/[\r\n,;]+/)
        .map((k) => k.trim())
        .filter(Boolean).length;
      if (el("gatewayKeyCountBadge")) el("gatewayKeyCountBadge").textContent = `${count} Key`;
    });
    el("settingGeminiApiKeysTextarea")?.addEventListener("input", (e) => {
      const count = e.target.value
        .split(/[\r\n,;]+/)
        .map((k) => k.trim())
        .filter(Boolean).length;
      if (el("geminiKeyCountBadge")) el("geminiKeyCountBadge").textContent = `${count} Key`;
    });

    el("settingConcurrencyRange")?.addEventListener("input", (e) => {
      if (el("concurrencyValueText")) {
        el("concurrencyValueText").textContent = e.target.value;
      }
    });
  }

  async function init() {
    bindEvents();
    await loadSettings();
    await fetchJobsList();
    pollTimer = setInterval(fetchJobsList, 1500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
