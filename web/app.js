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
  if (res.ok) {
    try { tourOnApi(path); } catch (e) { /* ツアー未初期化時は無視 */ }
  }
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
  renderDemo();
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

// --- ドラッグ範囲選択（ラバーバンド） -------------------------------------
// パネルの余白からドラッグで枠を描き、枠に触れたメンバーカードを一括選択する。
// Ctrl/Shift 押下で既存の選択に追加。余白をクリック（ドラッグなし）で全解除。
// メンバーカード上からのドラッグは従来どおり D&D 移動なので開始点を余白に限る。
const rubberEl = document.createElement("div");
rubberEl.id = "rubber-band";
document.body.appendChild(rubberEl);

let rubber = null;  // {x, y, base:Set, moved} ドラッグ中だけ存在

function rubberZone(target) {
  if (target.closest(".member, button, select, input, a, .column-head, .modal")) return false;
  return !!target.closest("#channels-panel, #teams-panel");
}

function rubberRect(st, e) {
  return {
    left: Math.min(st.x, e.clientX),
    top: Math.min(st.y, e.clientY),
    right: Math.max(st.x, e.clientX),
    bottom: Math.max(st.y, e.clientY),
  };
}

function rubberIds(st, rect) {
  const ids = new Set(st.base);
  document.querySelectorAll(".member[data-user-id]").forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.left < rect.right && r.right > rect.left &&
        r.top < rect.bottom && r.bottom > rect.top) {
      ids.add(el.dataset.userId);
    }
  });
  return ids;
}

document.addEventListener("mousedown", (e) => {
  if (e.button !== 0 || !rubberZone(e.target)) return;
  e.preventDefault();  // テキスト選択を開始させない
  rubber = {
    x: e.clientX, y: e.clientY,
    base: (e.ctrlKey || e.shiftKey) ? new Set(selectedIds) : new Set(),
    moved: false,
  };
});

document.addEventListener("mousemove", (e) => {
  if (!rubber) return;
  if (!rubber.moved) {
    if (Math.abs(e.clientX - rubber.x) < 5 && Math.abs(e.clientY - rubber.y) < 5) return;
    rubber.moved = true;  // 5px 動いたらドラッグ扱い（クリックと区別）
    rubberEl.style.display = "block";
  }
  const rect = rubberRect(rubber, e);
  rubberEl.style.left = `${rect.left}px`;
  rubberEl.style.top = `${rect.top}px`;
  rubberEl.style.width = `${rect.right - rect.left}px`;
  rubberEl.style.height = `${rect.bottom - rect.top}px`;
  const ids = rubberIds(rubber, rect);
  document.querySelectorAll(".member[data-user-id]").forEach((el) => {
    el.classList.toggle("selected", ids.has(el.dataset.userId));
  });
});

document.addEventListener("mouseup", (e) => {
  if (!rubber) return;
  const st = rubber;
  rubber = null;
  rubberEl.style.display = "none";
  if (!st.moved) {
    if (selectedIds.size) { selectedIds.clear(); render(); }  // 余白クリック = 全解除
    return;
  }
  const ids = rubberIds(st, rubberRect(st, e));
  selectedIds.clear();
  ids.forEach((id) => selectedIds.add(id));
  render();
});

// --- チュートリアル（デモモード + ガイドツアー） ----------------------------
// デモデータへ切り替えた上で、吹き出しガイドに沿って実際に操作して覚える。
// waitState: スナップショットが条件を満たすと自動で次へ / waitApi: そのAPIが成功すると次へ
const TOUR_CHAPTERS = {
  basic: {
    title: "基本フロー",
    steps: [
      {
        title: "ようこそ！",
        text: "これから大会運営の基本フローを練習します。いま表示されているのは<b>デモデータ</b>なので、" +
          "何をしても実際のサーバーには影響しません。安心して触ってください。",
        target: null,
      },
      {
        title: "画面の見方",
        text: "左が<b>ボイスチャンネル一覧</b>です。VCごとに接続中のメンバーが表示され、リアルタイムに更新されます。" +
          "右はチーム編成のパネルです。",
        target: "#channels-panel",
      },
      {
        title: "メインVCを設定する",
        text: "プルダウンから<b>「集合ロビー」</b>を選んでください。メインVCは「集合」の集合先で、" +
          "読み上げbotの入室先にもなります。",
        target: "#controlbar",
        waitState: (s) => !!s.main_channel_id,
      },
      {
        title: "チームを2つ作る",
        text: "<b>「＋チーム追加」</b>を押してチームを2つ作ってください（名前は自由。空欄でもOK）。",
        target: "#teams-panel",
        waitState: (s) => (s.teams || []).length >= 2,
      },
      {
        title: "メンバーをチームに入れる",
        text: "左のメンバーカードを右のチームへ<b>ドラッグ＆ドロップ</b>してください。" +
          "余白からドラッグすると<b>範囲選択</b>で複数人まとめて動かせます。合計4人以上入れてみましょう。",
        target: ["#channels-panel", "#teams-panel"],  // 移動元と移動先の両方を照らす
        waitState: (s) => (s.teams || []).reduce((n, t) => n + t.members.length, 0) >= 4,
      },
      {
        title: "シャッフル",
        text: "<b>🔀 シャッフル</b>を押すと、チームのメンバーをランダムかつ均等に振り分け直せます。" +
          "動かしたくない人は📌でピン留めできます（詳しくは応用編で）。",
        target: "#shuffle-teams",
        waitApi: "/api/teams/shuffle",
      },
      {
        title: "チームをVCへ移動する",
        text: "チームの<b>見出し</b>を「対戦VC 1」「対戦VC 2」へそれぞれドラッグしてください。" +
          "チーム全員が一括移動し、そのVCが<b>散開先（⛺）</b>として記録されます。",
        // 移動元（チーム）と移動先（対戦VCの列）の両方を照らす
        target: () => [
          document.getElementById("teams-panel"),
          document.querySelector('.list[data-id="demo-vc1"]')?.closest(".column"),
          document.querySelector('.list[data-id="demo-vc2"]')?.closest(".column"),
        ].filter(Boolean),
        waitState: (s) => (s.teams || []).length > 0 && (s.teams || []).every((t) => t.home_channel_id),
      },
      {
        title: "集合",
        text: "<b>⬇ 集合</b>を押すと、チームに所属している全員がメインVCへ集まります。" +
          "（チーム未所属の人は動きません）",
        target: "#gather-toggle",
        waitApi: "/api/gather",
      },
      {
        title: "散開",
        text: "<b>⬆ 散開</b>を押すと、各チームが⛺の散開先VCへ一斉に移動します。" +
          "「集合して作戦会議 → 散開して試合」がこの2つのボタンで回せます。",
        target: "#gather-toggle",
        waitApi: "/api/scatter",
      },
      {
        title: "基本フロー完了！ 🎉",
        text: "これで大会運営の基本操作はマスターです。実際のサーバーで使うには、" +
          "⚙ 設定から Bot トークンを設定してください。応用編・読み上げ編は近日追加予定です。",
        target: null,
        last: true,
      },
    ],
  },
};

const tourHighlightEl = document.createElement("div");
tourHighlightEl.id = "tour-highlight";
const tourBubbleEl = document.createElement("div");
tourBubbleEl.id = "tour-bubble";
document.body.appendChild(tourHighlightEl);
document.body.appendChild(tourBubbleEl);

let tour = null;       // {chapterId, index} 実行中のみ
let tourTimer = null;  // 対象要素の再描画に追従するための位置更新タイマー

function tourStep() {
  return tour ? TOUR_CHAPTERS[tour.chapterId].steps[tour.index] : null;
}

async function startTour(chapterId) {
  document.getElementById("tour-menu").classList.add("hidden");
  document.getElementById("tour-suggest").classList.add("hidden");
  localStorage.setItem("vcm-tutorial-prompted", "1");
  await api("POST", "/api/demo/start");  // 既にデモ中でも状態がリセットされる
  tour = { chapterId, index: 0 };
  if (!tourTimer) tourTimer = setInterval(positionTour, 250);
  showTourStep();
}

async function endTour() {
  if (tourTimer) { clearInterval(tourTimer); tourTimer = null; }
  tour = null;
  tourHighlightEl.style.display = "none";
  tourBubbleEl.style.display = "none";
  await api("POST", "/api/demo/stop");
}

function advanceTour() {
  if (!tour) return;
  if (tour.index >= TOUR_CHAPTERS[tour.chapterId].steps.length - 1) { endTour(); return; }
  tour.index += 1;
  showTourStep();
}

function showTourStep() {
  const step = tourStep();
  if (!step) return;
  const total = TOUR_CHAPTERS[tour.chapterId].steps.length;
  const waiting = !!(step.waitState || step.waitApi);
  tourBubbleEl.innerHTML =
    `<div class="tour-head"><span class="tour-progress">${tour.index + 1} / ${total}</span>` +
    `<button class="tour-close" title="チュートリアルを終了">✕ 終了</button></div>` +
    `<h3>${step.title}</h3><p>${step.text}</p>` +
    `<div class="tour-actions">` +
    (waiting
      ? `<span class="tour-wait">👉 実際に操作すると次へ進みます</span>` +
        `<button class="btn ghost sm tour-skip">スキップ</button>`
      : `<button class="btn tour-next">${step.last ? "チュートリアルを終える" : "次へ"}</button>`) +
    `</div>`;
  tourBubbleEl.querySelector(".tour-close").onclick = endTour;
  const next = tourBubbleEl.querySelector(".tour-next");
  if (next) next.onclick = step.last ? endTour : advanceTour;
  const skip = tourBubbleEl.querySelector(".tour-skip");
  if (skip) skip.onclick = advanceTour;
  tourBubbleEl.style.display = "block";
  positionTour();
}

function tourTargets(step) {
  // target は セレクタ / セレクタ配列 / 要素配列を返す関数 のいずれか
  if (!step.target) return [];
  if (typeof step.target === "function") return step.target().filter(Boolean);
  const sels = Array.isArray(step.target) ? step.target : [step.target];
  return sels.map((s) => document.querySelector(s)).filter(Boolean);
}

function positionTour() {
  const step = tourStep();
  if (!step) return;
  const targets = tourTargets(step);
  if (targets.length) {
    // 複数ターゲットは全体を囲む結合矩形にスポットライトを当てる
    const rects = targets.map((el) => el.getBoundingClientRect());
    const r = {
      left: Math.min(...rects.map((x) => x.left)),
      top: Math.min(...rects.map((x) => x.top)),
      right: Math.max(...rects.map((x) => x.right)),
      bottom: Math.max(...rects.map((x) => x.bottom)),
    };
    r.width = r.right - r.left;
    r.height = r.bottom - r.top;
    tourHighlightEl.style.display = "block";
    tourHighlightEl.style.left = `${r.left - 6}px`;
    tourHighlightEl.style.top = `${r.top - 6}px`;
    tourHighlightEl.style.width = `${r.width + 12}px`;
    tourHighlightEl.style.height = `${r.height + 12}px`;
    const bh = tourBubbleEl.offsetHeight || 180;
    const below = r.bottom + 14 + bh < innerHeight;
    tourBubbleEl.style.top = `${below ? r.bottom + 14 : Math.max(10, r.top - bh - 14)}px`;
    tourBubbleEl.style.left = `${Math.min(Math.max(10, r.left), innerWidth - 390)}px`;
    tourBubbleEl.style.transform = "";
  } else {
    tourHighlightEl.style.display = "none";
    tourBubbleEl.style.top = "45%";
    tourBubbleEl.style.left = "50%";
    tourBubbleEl.style.transform = "translate(-50%, -50%)";
  }
}

function tourOnState() {
  const step = tourStep();
  if (step && step.waitState && step.waitState(lastSnapshot)) advanceTour();
}

function tourOnApi(path) {
  const step = tourStep();
  if (step && step.waitApi && path === step.waitApi) advanceTour();
}

function renderDemo() {
  document.getElementById("demo-badge").classList.toggle("hidden", !lastSnapshot.demo);
  tourOnState();
  // 初回接続後にチュートリアルを一度だけ提案
  if (!lastSnapshot.demo && !tour && lastSnapshot.bot_state === "ready" &&
      !localStorage.getItem("vcm-tutorial-prompted")) {
    document.getElementById("tour-suggest").classList.remove("hidden");
  }
}

document.getElementById("tutorial-btn").onclick = () =>
  document.getElementById("tour-menu").classList.remove("hidden");
document.getElementById("tour-menu-close").onclick = () =>
  document.getElementById("tour-menu").classList.add("hidden");
document.querySelectorAll(".tour-chapter[data-chapter]").forEach((b) => {
  b.onclick = () => startTour(b.dataset.chapter);
});
document.getElementById("demo-exit").onclick = endTour;
document.getElementById("tour-suggest-start").onclick = () => startTour("basic");
document.getElementById("tour-suggest-later").onclick = () => {
  localStorage.setItem("vcm-tutorial-prompted", "1");
  document.getElementById("tour-suggest").classList.add("hidden");
};

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
