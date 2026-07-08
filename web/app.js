// VCM フロントエンド
// - WebSocket でスナップショットを受信し、VC とチームを再描画
// - ネイティブHTML5 D&D。メンバーカードを VC へ落とすと実移動、チームへ落とすと割当
//   （ドラッグ中はDOMを動かさず、ドロップ時にAPI→サーバー再描画で確定）

let lastSnapshot = { ready: false, channels: [], teams: [] };
const selectedIds = new Set();  // 複数選択中のユーザーID
let wsConnected = false;

const statusEl = document.getElementById("status");
const guildSelectEl = document.getElementById("guild-select");
const channelsEl = document.getElementById("channels");
const teamsEl = document.getElementById("teams");
const mainSelectEl = document.getElementById("main-select");
const gatherToggleBtn = document.getElementById("gather-toggle");
const recruitToggleBtn = document.getElementById("recruit-toggle");
const presetsEl = document.getElementById("presets");

const ttsEngineEl = document.getElementById("tts-engine");
const ttsControlsEl = document.getElementById("tts-controls");
const ttsGuideEl = document.getElementById("tts-guide");
const ttsNowEl = document.getElementById("tts-now");
const dictModalEl = document.getElementById("dict-modal");
const dictListEl = document.getElementById("dict-list");
const dictWordEl = document.getElementById("dict-word");
const dictReadingEl = document.getElementById("dict-reading");

const tokenModalEl = document.getElementById("token-modal");
const tokenInputEl = document.getElementById("token-input");
const tokenCurrentEl = document.getElementById("token-current");
const tokenMsgEl = document.getElementById("token-msg");
const tokenSaveBtn = document.getElementById("token-save");
const tokenCancelBtn = document.getElementById("token-cancel");
const tokenDeleteBtn = document.getElementById("token-delete");

// --- API ---------------------------------------------------------------
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) console.error(method, path, res.status);
  return res.ok ? res.json().catch(() => ({})) : null;
}

// --- WebSocket ---------------------------------------------------------
let updateRestarting = false;  // アップデートによる再起動を待っている状態

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { wsConnected = true; updateStatus(); };
  ws.onclose = () => {
    wsConnected = false;
    updateStatus();
    if (updateRestarting) { waitForRestart(); return; }
    setTimeout(connect, 1500);
  };
  ws.onmessage = (e) => { lastSnapshot = JSON.parse(e.data); render(); };
}

// アップデートの再起動後、サーバーが戻ったらページごと再読み込みして
// 新しいバージョンのGUI（HTML/JS/CSS）に切り替える
function waitForRestart() {
  let tries = 0;
  const timer = setInterval(async () => {
    if (++tries > 180) {  // 3分待っても戻らなければ諦めて案内を出す
      clearInterval(timer);
      updateMsgEl.textContent =
        "再起動を確認できませんでした。アプリを起動し直してからページを再読み込みしてください。";
      updateMsgEl.className = "token-msg err";
      return;
    }
    try {
      const res = await fetch("/", { cache: "no-store" });
      if (res.ok) {
        clearInterval(timer);
        location.reload();
      }
    } catch (e) { /* まだ再起動中 */ }
  }, 1000);
}

// ヘッダの状態表示: WS 切断中は「未接続」、接続中は bot の状態を表示
const BOT_STATUS = {
  ready: ["接続中", "on"],
  connecting: ["bot 起動中…", "warn"],
  no_token: ["トークン未設定", "off"],
  invalid_token: ["トークン無効", "off"],
  error: ["エラー", "off"],
};

const updateLinkEl = document.getElementById("update-link");
const updateModalEl = document.getElementById("update-modal");
const updateApplyBtn = document.getElementById("update-apply");
const updateMsgEl = document.getElementById("update-msg");

function renderUpdate() {
  const u = lastSnapshot.update;
  updateLinkEl.classList.toggle("hidden", !u);
  if (!u) return;
  updateLinkEl.textContent = `⬆ ${u.version} が公開されています`;
  updateLinkEl.href = u.url;

  document.getElementById("update-desc").textContent =
    `新しいバージョン ${u.version} が公開されています。`;
  document.getElementById("update-release").href = u.url;
  updateApplyBtn.classList.toggle("hidden", !u.can_apply);

  const st = lastSnapshot.update_status || "idle";
  updateApplyBtn.disabled = st === "downloading" || st === "restarting";
  if (st === "downloading") {
    updateMsgEl.textContent = "ダウンロード中…";
    updateMsgEl.className = "token-msg";
  } else if (st === "restarting") {
    updateRestarting = true;  // 切断後はサーバー復帰を待って自動リロード
    updateMsgEl.textContent =
      "まもなく再起動します。そのままお待ちください（この画面が自動で切り替わります）。";
    updateMsgEl.className = "token-msg okmsg";
  } else if (st.startsWith("error:")) {
    updateMsgEl.textContent = st.slice(6);
    updateMsgEl.className = "token-msg err";
  } else {
    updateMsgEl.textContent = "";
  }
}

updateLinkEl.addEventListener("click", (e) => {
  const u = lastSnapshot.update;
  if (u && u.can_apply) {  // 配布版はモーダルで選択。ソース実行時はリンクとして動作
    e.preventDefault();
    updateModalEl.classList.remove("hidden");
  }
});
document.getElementById("update-cancel").onclick = () => updateModalEl.classList.add("hidden");
updateApplyBtn.onclick = () => api("POST", "/api/update/apply");

function updateStatus() {
  let text = "未接続", cls = "off", title = "";
  if (wsConnected) {
    const st = BOT_STATUS[lastSnapshot.bot_state] || BOT_STATUS.connecting;
    text = st[0];
    cls = st[1];
    if (lastSnapshot.bot_state === "ready" && lastSnapshot.bot_user) title = lastSnapshot.bot_user;
    if (lastSnapshot.bot_error) title = lastSnapshot.bot_error;
  }
  statusEl.className = `status ${cls}`;
  statusEl.textContent = text;
  statusEl.title = title;
}

// --- トークン設定モーダル ------------------------------------------------
let tokenModalForced = false;   // トークンなしでは操作できない状態（閉じるボタンを隠す）
let tokenAutoOpened = false;    // 同じ異常状態で何度も自動オープンしないためのフラグ

function openTokenModal(forced) {
  tokenModalForced = !!forced;
  tokenCancelBtn.classList.toggle("hidden", tokenModalForced);
  tokenModalEl.classList.remove("hidden");
  tokenInputEl.value = "";
  showTokenMsg(lastSnapshot.bot_error || "", !!lastSnapshot.bot_error);
  refreshTokenInfo();
  tokenInputEl.focus();
}

function closeTokenModal() {
  tokenModalEl.classList.add("hidden");
  tokenModalForced = false;
}

function showTokenMsg(text, isError) {
  tokenMsgEl.textContent = text;
  tokenMsgEl.classList.toggle("err", !!isError);
  tokenMsgEl.classList.toggle("okmsg", !isError && !!text);
}

async function refreshTokenInfo() {
  const info = await api("GET", "/api/token");
  if (info && info.masked) {
    tokenCurrentEl.textContent = `現在のトークン: ${info.masked}（保存済み）`;
    tokenDeleteBtn.classList.toggle("hidden", !info.saved);
  } else {
    tokenCurrentEl.textContent = "トークンはまだ設定されていません。";
    tokenDeleteBtn.classList.add("hidden");
  }
}

tokenSaveBtn.onclick = async () => {
  const token = tokenInputEl.value.trim();
  if (!token) { showTokenMsg("トークンを入力してください", true); return; }
  tokenSaveBtn.disabled = true;
  showTokenMsg("Discord に接続して確認中…");
  const res = await api("POST", "/api/token", { token });
  tokenSaveBtn.disabled = false;
  if (res && res.ok) {
    showTokenMsg("トークンを保存し、接続しました ✓");
    tokenInputEl.value = "";
    refreshTokenInfo();
    setTimeout(closeTokenModal, 800);
  } else {
    showTokenMsg((res && res.error) || "接続に失敗しました", true);
  }
};

tokenDeleteBtn.onclick = async () => {
  if (!confirm("保存済みのトークンを削除しますか？（bot は切断されます）")) return;
  await api("DELETE", "/api/token");
  showTokenMsg("トークンを削除しました");
  refreshTokenInfo();
};

tokenCancelBtn.onclick = closeTokenModal;
document.getElementById("token-eye").onclick = () => {
  tokenInputEl.type = tokenInputEl.type === "password" ? "text" : "password";
};
tokenInputEl.addEventListener("keydown", (e) => { if (e.key === "Enter") tokenSaveBtn.click(); });

// --- 設定モーダル --------------------------------------------------------
const settingsModalEl = document.getElementById("settings-modal");
const setGuildEl = document.getElementById("set-guild");
const setPortEl = document.getElementById("set-port");
const setVvPathEl = document.getElementById("set-vvpath");
const settingsMsgEl = document.getElementById("settings-msg");
const settingsSaveBtn = document.getElementById("settings-save");

function showSettingsMsg(text, kind) {
  settingsMsgEl.textContent = text;
  settingsMsgEl.className = "token-msg" + (kind ? ` ${kind}` : "");
}

async function openSettings() {
  // 起動時に選ぶサーバーの選択肢を現在の参加サーバーから作る
  const guilds = lastSnapshot.guilds || [];
  setGuildEl.innerHTML = `<option value="">最初のサーバー（自動）</option>` +
    guilds.map((g) => `<option value="${g.id}">${escapeHtml(g.name)}</option>`).join("");
  showSettingsMsg("");
  settingsModalEl.classList.remove("hidden");
  const info = await api("GET", "/api/settings");
  if (info) {
    setGuildEl.value = info.guild_id || "";
    setPortEl.value = info.port;
    setVvPathEl.value = info.voicevox_path || "";
  }
}

settingsSaveBtn.onclick = async () => {
  const port = parseInt(setPortEl.value, 10);
  if (!(port >= 1 && port <= 65535)) {
    showSettingsMsg("ポート番号は 1〜65535 で指定してください", "err");
    return;
  }
  settingsSaveBtn.disabled = true;
  const res = await api("POST", "/api/settings", {
    guild_id: setGuildEl.value,
    port,
    voicevox_path: setVvPathEl.value.trim(),
  });
  settingsSaveBtn.disabled = false;
  if (res && res.ok) {
    showSettingsMsg(
      res.restart_required
        ? "保存しました。ポートの変更は次回起動時に反映されます。"
        : "保存しました ✓",
      "okmsg");
  } else {
    showSettingsMsg("保存に失敗しました", "err");
  }
};

const setVvBrowseBtn = document.getElementById("set-vvbrowse");
setVvBrowseBtn.onclick = async () => {
  const prev = setVvBrowseBtn.textContent;
  setVvBrowseBtn.disabled = true;
  setVvBrowseBtn.textContent = "選択中…";
  const res = await api("POST", "/api/settings/browse-voicevox",
    { initial: setVvPathEl.value.trim() });
  setVvBrowseBtn.disabled = false;
  setVvBrowseBtn.textContent = prev;
  if (res && res.path) setVvPathEl.value = res.path;  // キャンセル時は変更しない
};

document.getElementById("settings-btn").onclick = openSettings;
document.getElementById("settings-cancel").onclick = () => settingsModalEl.classList.add("hidden");
document.getElementById("open-token-btn").onclick = () => {
  settingsModalEl.classList.add("hidden");
  tokenAutoOpened = true;
  openTokenModal(false);
};

// bot が使えない状態なら設定モーダルを自動で開く
function maybeOpenTokenModal() {
  const bs = lastSnapshot.bot_state;
  const broken = bs === "no_token" || bs === "invalid_token" || bs === "error";
  if (broken && !tokenAutoOpened && tokenModalEl.classList.contains("hidden")) {
    tokenAutoOpened = true;
    openTokenModal(true);
  }
  if (!broken) tokenAutoOpened = false;  // 復旧したら次回異常時にまた自動で開く
  if (bs === "ready" && tokenModalForced) closeTokenModal();
}

// --- 描画 --------------------------------------------------------------
function memberCard(m, teamId) {
  const div = document.createElement("div");
  div.className = "member" + (m.voice_channel_id ? "" : " offline") +
    (selectedIds.has(m.id) ? " selected" : "");
  div.dataset.userId = m.id;
  div.innerHTML = `<img src="${escapeHtml(m.avatar || "")}" alt=""><span class="name">${escapeHtml(m.name)}</span>`;
  div.addEventListener("click", () => toggleSelect(m.id, div));
  // ネイティブD&D: ドラッグ中にDOMを動かさず、ドロップ時にだけ確定する
  div.draggable = true;
  div.addEventListener("dragstart", (e) => {
    const ids = (selectedIds.has(m.id) && selectedIds.size > 1)
      ? Array.from(selectedIds) : [m.id];
    e.dataTransfer.setData("application/x-vcm-members", JSON.stringify(ids));
    e.dataTransfer.effectAllowed = "move";
    if (ids.length > 1) {
      // 選択カードすべてを重ねたドラッグ画像を出す（単体時は既定＝そのカードが追従）
      const stack = document.createElement("div");
      stack.className = "drag-stack";
      ids.forEach((sid) => {
        const el = document.querySelector(`.member[data-user-id="${sid}"]`);
        if (!el) return;
        const clone = el.cloneNode(true);  // 表示中のカードを複製（画像はキャッシュ済み）
        clone.classList.remove("selected");
        clone.querySelectorAll(".member-remove").forEach((b) => b.remove());
        stack.appendChild(clone);
      });
      document.body.appendChild(stack);
      e.dataTransfer.setDragImage(stack, 12, 12);
      setTimeout(() => stack.remove(), 0);
    }
  });
  // チーム内のカードには「ピン留め」「外す」ボタンを付ける
  if (teamId != null) {
    const pinned = (lastSnapshot.pinned_ids || []).includes(m.id);
    if (pinned) div.classList.add("pinned");
    const pin = document.createElement("button");
    pin.className = "member-pin" + (pinned ? " on" : "");
    pin.textContent = "📌";
    pin.title = pinned
      ? "ピン留め中（シャッフルしてもこのチームに残る）。クリックで解除"
      : "ピン留めしてシャッフルで動かないようにする";
    pin.addEventListener("click", (e) => {
      e.stopPropagation();  // 選択トグルを発火させない
      api("POST", "/api/pins/toggle", { user_id: m.id });
    });
    div.appendChild(pin);
    const rm = document.createElement("button");
    rm.className = "member-remove";
    rm.textContent = "✕";
    rm.title = "チームから外す";
    rm.addEventListener("click", (e) => {
      e.stopPropagation();  // 選択トグルを発火させない
      api("DELETE", `/api/teams/${teamId}/members/${m.id}`);
    });
    div.appendChild(rm);
  }
  return div;
}

function toggleSelect(id, div) {
  if (selectedIds.has(id)) {
    selectedIds.delete(id);
    div.classList.remove("selected");
  } else {
    selectedIds.add(id);
    div.classList.add("selected");
  }
}

function makeColumn(title, count, kind, id) {
  const col = document.createElement("div");
  col.className = "column";
  const head = document.createElement("div");
  head.className = "column-head";
  head.innerHTML = `<span class="column-title">${escapeHtml(title)}</span><span class="column-count">${count}</span>`;
  col.appendChild(head);
  const list = document.createElement("div");
  list.className = "list";
  list.dataset.kind = kind;
  list.dataset.id = id;
  col.appendChild(list);
  return { col, head, list };
}

function renderGuilds() {
  const guilds = lastSnapshot.guilds || [];
  const cur = lastSnapshot.guild_id || "";
  if (!guilds.length) {
    guildSelectEl.innerHTML = `<option value="">サーバーなし</option>`;
    return;
  }
  guildSelectEl.innerHTML = guilds.map((g) =>
    `<option value="${g.id}" ${g.id === cur ? "selected" : ""}>${escapeHtml(g.name)}</option>`).join("");
}

function renderControls() {
  // メインVC セレクト（選択値を維持）
  const cur = lastSnapshot.main_channel_id || "";
  mainSelectEl.innerHTML = `<option value="">未設定</option>` +
    (lastSnapshot.channels || []).map((c) =>
      `<option value="${c.id}" ${c.id === cur ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("");

  // 集合/散開トグル: 集合済みなら「散開」、未集合なら「集合」
  const gathered = !!lastSnapshot.can_scatter;
  gatherToggleBtn.textContent = gathered ? "⬆ 散開" : "⬇ 集合";
  gatherToggleBtn.classList.toggle("scatter-mode", gathered);
  // メインVC未設定時は集合できない
  gatherToggleBtn.disabled = !gathered && !lastSnapshot.main_channel_id;
  gatherToggleBtn.title = gathered
    ? "各チームを散開先VC（⛺表示のVC）へ移動します"
    : "チーム所属メンバー全員をメインVCへ移動します";

  // 参加希望トグル: 募集中なら「締め切る」。メインVCとチームが揃うまで押せない
  const recruiting = !!lastSnapshot.recruiting;
  recruitToggleBtn.textContent = recruiting ? "✋ 募集を締め切る" : "✋ 参加希望を募る";
  recruitToggleBtn.classList.toggle("recruit-mode", recruiting);
  recruitToggleBtn.disabled = !recruiting &&
    (!lastSnapshot.main_channel_id || !(lastSnapshot.teams || []).length);
  recruitToggleBtn.title = recruiting
    ? "メインVCの募集メッセージを削除します"
    : "メインVCのチャットに「参加希望」ボタンを設置します（メインVCとチームの作成が必要）";

  presetsEl.innerHTML = "";
  (lastSnapshot.preset_names || []).forEach((name) => {
    const chip = document.createElement("span");
    chip.className = "preset-chip";
    chip.innerHTML = `<button class="load">${escapeHtml(name)}</button><button class="del" title="削除">✕</button>`;
    chip.querySelector(".load").onclick = () => api("POST", `/api/presets/${encodeURIComponent(name)}/load`);
    chip.querySelector(".del").onclick = () => {
      if (confirm(`プリセット「${name}」を削除しますか？`)) api("DELETE", `/api/presets/${encodeURIComponent(name)}`);
    };
    presetsEl.appendChild(chip);
  });
}

// --- 読み上げ（VOICEVOX） -------------------------------------------------
const ENGINE_STATUS = {
  ready: ["VOICEVOX 接続済み", "on"],
  checking: ["VOICEVOX 確認中…", "warn"],
  starting: ["VOICEVOX 起動中…", "warn"],
  not_installed: ["VOICEVOX 未検出", "off"],
  error: ["VOICEVOX エラー", "off"],
  off: ["読み上げ無効", "off"],
};

function renderTts() {
  const tts = lastSnapshot.tts || { engine: "off" };
  const st = ENGINE_STATUS[tts.engine] || ENGINE_STATUS.off;
  ttsEngineEl.textContent = st[0];
  ttsEngineEl.className = `status ${st[1]}`;
  ttsEngineEl.title = tts.engine_error || "";

  const usable = tts.engine === "ready" && lastSnapshot.bot_state === "ready";
  ttsControlsEl.classList.toggle("hidden", !usable);
  ttsGuideEl.classList.toggle("hidden", tts.engine !== "not_installed" && tts.engine !== "error");
  if (!usable) return;

  // bot の入退室はメインVCの設定に追従する（設定で入室・解除で退出）
  const joined = tts.channel_id || "";
  ["tts-skip", "tts-clear", "tts-test"].forEach((id) => {
    document.getElementById(id).disabled = !joined;
  });

  if (joined) {
    const n = (tts.queue || []).length;
    const now = tts.reading ? `「${tts.reading.slice(0, 30)}」` : "";
    ttsNowEl.textContent = (tts.reading ? `▶ ${now} ` : "") + (n ? `待ち ${n} 件` : (tts.reading ? "" : "待機中"));
  } else {
    ttsNowEl.textContent = "メインVCを設定すると読み上げVCに入室します";
  }
}

document.getElementById("tts-skip").onclick = () => api("POST", "/api/tts/skip");
document.getElementById("tts-clear").onclick = () => api("POST", "/api/tts/clear");
document.getElementById("tts-test").onclick = () => api("POST", "/api/tts/test", {});
document.getElementById("tts-redetect").onclick = () => api("POST", "/api/tts/redetect");

// --- 読み上げ辞書 ----------------------------------------------------------
function renderDict() {
  if (dictModalEl.classList.contains("hidden")) return;
  const dict = (lastSnapshot.tts && lastSnapshot.tts.dict) || {};
  const words = Object.keys(dict).sort();
  dictListEl.innerHTML = words.length ? "" : `<div class="empty-hint">まだ登録がありません</div>`;
  words.forEach((word) => {
    const row = document.createElement("div");
    row.className = "dict-row";
    row.innerHTML =
      `<span class="dict-w">${escapeHtml(word)}</span><span class="dict-arrow">→</span>` +
      `<span class="dict-r">${escapeHtml(dict[word])}</span>` +
      `<button class="dict-del" title="削除">✕</button>`;
    row.querySelector(".dict-del").onclick = () => api("POST", "/api/tts/dict/delete", { word });
    dictListEl.appendChild(row);
  });
}

document.getElementById("tts-dict").onclick = () => {
  dictModalEl.classList.remove("hidden");
  renderDict();
  dictWordEl.focus();
};
document.getElementById("dict-close").onclick = () => dictModalEl.classList.add("hidden");
document.getElementById("dict-add-btn").onclick = async () => {
  const word = dictWordEl.value.trim();
  const reading = dictReadingEl.value.trim();
  if (!word || !reading) return;
  await api("POST", "/api/tts/dict", { word, reading });
  dictWordEl.value = "";
  dictReadingEl.value = "";
  dictWordEl.focus();
};
dictReadingEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("dict-add-btn").click();
});

function render() {
  updateStatus();
  renderUpdate();
  maybeOpenTokenModal();
  renderGuilds();
  renderControls();
  renderTts();
  renderDict();

  // VC 列（カテゴリー別にグループ表示。カテゴリーが無いサーバーは従来通りフラット表示）
  channelsEl.innerHTML = "";
  const chs = lastSnapshot.channels || [];
  // 散開先バッジ: channel_id -> そのVCを散開先にしているチーム名
  const homeTeams = {};
  (lastSnapshot.teams || []).forEach((t) => {
    if (t.home_channel_id) {
      (homeTeams[t.home_channel_id] = homeTeams[t.home_channel_id] || []).push(t.name);
    }
  });
  const hasCategories = chs.some((c) => c.category_id);
  channelsEl.classList.toggle("board", !hasCategories);
  channelsEl.classList.toggle("cat-groups", hasCategories);
  let curCatId;
  let catBoardEl = null;
  chs.forEach((ch) => {
    const { col, head, list } = makeColumn(ch.name, ch.members.length, "vc", ch.id);
    if (lastSnapshot.tts && lastSnapshot.tts.channel_id === ch.id) {
      col.classList.add("tts-joined");
      head.querySelector(".column-title").textContent = `🔊 ${ch.name}`;
    }
    if (homeTeams[ch.id]) {
      const badge = document.createElement("div");
      badge.className = "vc-home";
      badge.textContent = `⛺ ${homeTeams[ch.id].join("、")}`;
      badge.title = "このVCを散開先にしているチーム（チーム見出しをVCへドラッグすると記録されます）";
      col.insertBefore(badge, list);
    }
    ch.members.forEach((m) => list.appendChild(memberCard(m)));
    enableDrop(col, "vc", ch.id);
    if (!hasCategories) {
      channelsEl.appendChild(col);
      return;
    }
    const catId = ch.category_id || null;
    if (catBoardEl === null || catId !== curCatId) {
      curCatId = catId;
      const group = document.createElement("div");
      group.className = "vc-category";
      const label = document.createElement("div");
      label.className = "vc-category-name";
      label.textContent = ch.category || "カテゴリーなし";
      group.appendChild(label);
      catBoardEl = document.createElement("div");
      catBoardEl.className = "board";
      group.appendChild(catBoardEl);
      channelsEl.appendChild(group);
    }
    catBoardEl.appendChild(col);
  });

  // チーム列
  teamsEl.innerHTML = "";
  (lastSnapshot.teams || []).forEach((team) => {
    const { col, head, list } = makeColumn(team.name, team.members.length, "team", team.id);
    // ヘッダをドラッグで VC へ一括移動できるようにする
    head.draggable = true;
    head.classList.add("draggable-head");
    head.title = "ヘッダをVCへドラッグするとチーム全員を移動";
    head.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("application/x-vcm-team", String(team.id));
      e.dataTransfer.effectAllowed = "move";
    });
    // ヘッダにリネーム/削除
    const tools = document.createElement("span");
    tools.innerHTML =
      `<button class="btn ghost sm" data-act="rename">✎</button>` +
      `<button class="btn ghost sm" data-act="delete">🗑</button>`;
    tools.querySelector('[data-act="rename"]').onclick = () => renameTeam(team);
    tools.querySelector('[data-act="delete"]').onclick = () => api("DELETE", `/api/teams/${team.id}`);
    head.appendChild(tools);

    team.members.forEach((m) => list.appendChild(memberCard(m, team.id)));
    enableDrop(col, "team", team.id);
    teamsEl.appendChild(col);
  });

  // シャッフルはチーム2つ以上＆所属メンバーがいるときだけ有効
  const teamList = lastSnapshot.teams || [];
  document.getElementById("shuffle-teams").disabled =
    teamList.length < 2 || !teamList.some((t) => t.members.length > 0);
}

// --- D&D（ネイティブHTML5）---------------------------------------------
// 列（VC / チーム）をドロップ先にする。ドラッグ中はDOMを動かさず、ドロップで確定。
//   - メンバーカード(application/x-vcm-members): VCへ=移動 / チームへ=割当
//   - チーム見出し(application/x-vcm-team): VCへ=そのチーム全員を移動
function enableDrop(col, kind, id) {
  col.addEventListener("dragover", (e) => {
    const types = e.dataTransfer.types;
    const okMembers = types.includes("application/x-vcm-members");
    const okTeam = kind === "vc" && types.includes("application/x-vcm-team");
    if (okMembers || okTeam) {
      e.preventDefault();
      col.classList.add("drop-target");
    }
  });
  col.addEventListener("dragleave", (e) => {
    if (!col.contains(e.relatedTarget)) col.classList.remove("drop-target");
  });
  col.addEventListener("drop", (e) => {
    col.classList.remove("drop-target");
    const memberData = e.dataTransfer.getData("application/x-vcm-members");
    const teamData = e.dataTransfer.getData("application/x-vcm-team");
    if (memberData) {
      e.preventDefault();
      const ids = JSON.parse(memberData);
      selectedIds.clear();
      render();  // 選択ハイライトを消す（DOMはサーバー再描画が正）
      if (kind === "vc") {
        if (ids.length > 1) api("POST", "/api/move/batch", { user_ids: ids, channel_id: id });
        else api("POST", "/api/move", { user_id: ids[0], channel_id: id });
      } else if (kind === "team") {
        if (ids.length > 1) api("POST", `/api/teams/${id}/members/batch`, { user_ids: ids });
        else api("POST", `/api/teams/${id}/members`, { user_id: ids[0] });
      }
    } else if (teamData && kind === "vc") {
      e.preventDefault();
      api("POST", `/api/teams/${teamData}/move`, { channel_id: id });
    }
  });
}

// --- 操作 --------------------------------------------------------------
document.getElementById("add-team").onclick = async () => {
  const name = prompt("チーム名", "");
  if (name !== null) api("POST", "/api/teams", { name: name.trim() });
};

document.getElementById("shuffle-teams").onclick = () => api("POST", "/api/teams/shuffle");

function renameTeam(team) {
  const name = prompt("チーム名", team.name);
  if (name) api("PATCH", `/api/teams/${team.id}`, { name: name.trim() });
}

guildSelectEl.onchange = () => api("POST", "/api/guild", { guild_id: guildSelectEl.value });
mainSelectEl.onchange = () => api("POST", "/api/main", { channel_id: mainSelectEl.value || null });
gatherToggleBtn.onclick = () =>
  api("POST", lastSnapshot.can_scatter ? "/api/scatter" : "/api/gather");
recruitToggleBtn.onclick = () =>
  api("POST", lastSnapshot.recruiting ? "/api/recruit/stop" : "/api/recruit/start");
document.getElementById("save-preset").onclick = () => {
  const name = prompt("プリセット名", "");
  if (name && name.trim()) api("POST", "/api/presets", { name: name.trim() });
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

connect();
