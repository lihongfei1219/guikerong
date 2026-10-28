"use strict";
const $ = id => document.getElementById(id);
const state = {session: null, products: [], knowledge: [], busy: false, target: null, view: null};
let matchingProgress = null;
const composerDrafts = new Map();
let uploadLimits = {max_documents: 100, max_document_bytes: 1048576, max_total_document_bytes: 10485760};
function selectedFiles() {
  return [...$("folder-input").files, ...$("files").files]
    .filter(file => /\.md$/i.test(file.name))
    .sort((a, b) => (a.webkitRelativePath || a.name).localeCompare(b.webkitRelativePath || b.name, "zh-CN"));
}
function showSelection() {
  const files = selectedFiles();
  const total = $("folder-input").files.length + $("files").files.length;
  const ignored = total - files.length;
  $("selection-summary").textContent = total
    ? `已选择 ${files.length} 份 Markdown（包含子目录）${ignored ? `，已忽略 ${ignored} 个其他类型文件` : ""}。`
    : "尚未选择文件夹或文件。";
  $("selected-files").hidden = !files.length;
  $("selected-file-list").replaceChildren(...files.map(file => node("li", file.webkitRelativePath || file.name)));
}
const labels = {PASS: "符合", FAIL: "不符合", UNKNOWN: "待核验", DRAFT: "草稿", PUBLISHED: "已发布", RETIRED: "已停用"};
const decisionLabels = {FULLY_SATISFIED: "完全满足", PENDING_VERIFICATION: "待核验", NOT_ELIGIBLE: "不符合条件"};
const needLabels = {MATERIAL_CONFIRMATION: "材料待确认", PROFESSIONAL_REVIEW: "专业要求待解释"};
// getRandomValues also works on LAN HTTP, where randomUUID may be unavailable.
function identifier() { return Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join(""); }
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function notice(message, error = false) { $("notice").textContent = message; $("notice").className = error ? "error" : ""; $("notice").hidden = !error; }
async function api(path, body, method) {
  const headers = {"Content-Type": "application/json"};
  const response = await fetch(`/api/v1${path}`, {method: method || (body === undefined ? "GET" : "POST"), headers,
    ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || `请求失败 (${response.status})`);
  return data;
}
async function work(action, message = "正在处理，请稍候…") {
  if (state.busy) return;
  state.busy = true;
  renderComposer();
  document.body.setAttribute("aria-busy", "true");
  document.querySelectorAll("button").forEach(button => { button.dataset.wasDisabled = String(button.disabled); button.disabled = true; });
  notice(message);
  try { const message = await action(); notice(message || "操作已完成，记录已保存。"); }
  catch (error) {
    notice(error.message, true);
    if (state.session) {
      try {
        const previousView = state.view;
        state.session = await api(`/sessions/${state.session.id}`); renderSession();
        if (previousView) { state.view = previousView; renderResults(); }
      } catch { /* Preserve original error. */ }
    }
  } finally {
    state.busy = false;
    document.body.setAttribute("aria-busy", "false");
    document.querySelectorAll("button").forEach(button => {
      if (button.dataset.wasDisabled !== undefined) button.disabled = button.dataset.wasDisabled === "true";
      delete button.dataset.wasDisabled;
    });
    $("match").disabled = !state.session;
    updateSupplementFields();
    renderComposer();
    if (state.focusComposer) { $("chat-input").focus(); state.focusComposer = false; }
  }
}
function chooseTab(knowledge) {
  if (knowledge && !$("knowledge-panel").open) $("knowledge-panel").showModal();
  else if (!knowledge && $("knowledge-panel").open) $("knowledge-panel").close();
}
async function loadSessions() {
  const data = await api("/sessions");
  $("sessions").replaceChildren();
  if (!data.sessions.length) $("sessions").append(node("p", "还没有会话，点击上方新建。", "session-empty"));
  for (const session of data.sessions) {
    const active = session.id === state.session?.id;
    const row = node("div", undefined, `session-row${active ? " is-active" : ""}`);
    const button = node("button", undefined, `session-open${active ? " active" : ""}`);
    button.title = session.company.name;
    if (active) button.setAttribute("aria-current", "true");
    const icon = node("span", "", "session-icon");
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 21V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v16M17 9h3v12M2 21h20M8 7h1m3 0h1M8 11h1m3 0h1M8 15h1m3 0h1M9 21v-3h3v3"/></svg>';
    const text = node("span", undefined, "session-copy");
    const metadata = node("span", undefined, "session-meta");
    metadata.append(node("span", session.origin ? "历史继续" : "企业会话", session.origin ? "session-branch" : ""));
    const date = new Date(session.created_at);
    if (!Number.isNaN(date.getTime())) {
      const time = node("time", new Intl.DateTimeFormat("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(date));
      time.dateTime = date.toISOString(); metadata.append(time);
    }
    text.append(node("span", session.company.name, "session-name"), metadata);
    button.append(icon, text);
    button.onclick = () => work(async () => {
      state.session = await api(`/sessions/${session.id}`); renderSession(); await loadSessions();
    }, "正在恢复会话…");
    const remove = node("button", "删除", "session-delete danger");
    remove.type = "button";
    remove.setAttribute("aria-label", `删除会话：${session.company.name}`);
    const menu = node("div", undefined, "session-menu");
    menu.id = `session-menu-${session.id}`; menu.setAttribute("popover", "auto");
    remove.textContent = "删除会话"; menu.append(remove);
    const more = node("button", "···", "session-more");
    more.type = "button"; more.title = "会话操作";
    more.setAttribute("aria-label", `会话操作：${session.company.name}`);
    more.setAttribute("aria-controls", menu.id); more.setAttribute("aria-expanded", "false");
    more.onclick = () => {
      if (state.busy) return;
      if (menu.matches(":popover-open")) { menu.hidePopover(); return; }
      menu.showPopover();
      const rect = more.getBoundingClientRect();
      menu.style.left = `${Math.max(8, Math.min(rect.right - 156, window.innerWidth - 164))}px`;
      menu.style.top = `${Math.min(rect.bottom + 6, window.innerHeight - menu.offsetHeight - 8)}px`;
      remove.focus();
    };
    menu.addEventListener("toggle", () => more.setAttribute("aria-expanded", String(menu.matches(":popover-open"))));
    remove.onclick = () => work(async () => {
      menu.hidePopover();
      const current = await api(`/sessions/${session.id}`);
      if (!await confirmSessionDeletion(session)) return "已取消删除。";
      await api(`/sessions/${session.id}`, {expected_version: current.version}, "DELETE");
      composerDrafts.delete(session.id);
      if (state.session?.id === session.id) {
        state.session = null; state.view = null; state.target = null;
        matchingProgress = null;
        for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
        $("supplement-form").reset(); $("review-form").reset();
        $("review-target").textContent = "";
        state.supplementSessionId = null;
        renderSession();
      } else if (state.session?.origin?.session_id === session.id) {
        state.session.origin.deleted = true; renderNavigation();
      }
      await loadSessions();
      return "会话及其历史数据已永久删除。";
    }, "正在删除会话…");
    row.append(button, more, menu); $("sessions").append(row);
  }
}
function confirmSessionDeletion(session) {
  const dialog = $("delete-session-dialog");
  $("delete-session-name").textContent = session.company.name;
  const cancel = $("cancel-session-delete"), confirm = $("confirm-session-delete");
  cancel.disabled = false; confirm.disabled = false;
  dialog.returnValue = "";
  return new Promise(resolve => {
    dialog.onclose = () => { dialog.onclose = null; resolve(dialog.returnValue === "delete"); };
    cancel.onclick = () => dialog.close("cancel");
    confirm.onclick = () => dialog.close("delete");
    dialog.showModal(); cancel.focus();
  });
}
async function loadKnowledge() {
  state.knowledge = (await api("/knowledge")).items;
  renderKnowledge();
}
async function connect() {
  const health = await (await fetch("/health")).json();
  uploadLimits = {...uploadLimits, ...health.upload_limits};
  $("health").textContent = health.model_configured ? "模型已就绪" : "待配置模型";
  state.products = (await api("/financial-products")).products;
  for (const id of ["knowledge-product", "supplement-product", "composer-product"]) {
    const select = $(id); while (select.options.length > 1) select.remove(1);
    for (const product of state.products) select.add(new Option(product.product_name, product.product_id));
  }
  await loadSessions(); await loadKnowledge();
  if (state.session) { state.session = await api(`/sessions/${state.session.id}`); renderSession(); }
}
function renderSession() {
  const session = state.session;
  if (!session) {
    state.view = null;
    $("session-title").textContent = "企业匹配助手";
    $("session-meta").textContent = "选择或创建企业会话。";
    $("messages").replaceChildren(node("div", "选择或创建企业会话。", "empty"));
    $("matching-progress").hidden = true;
    $("create-panel").open = true;
    $("supplement-list").replaceChildren();
    $("saved-supplements-title").textContent = "已保存的人工补充（0 条）";
    $("edit-document").replaceChildren(new Option("选择已有文件", ""));
    $("document-content").value = "";
    resetResultFilters(); renderResults(); renderComposer();
    return;
  }
  state.view = null;
  if (matchingProgress?.sessionId !== session.id) $("matching-progress").hidden = true;
  $("session-title").textContent = session.request.company.name;
  $("session-meta").textContent = `${session.request.documents.length} 份企业资料`;
  $("create-panel").open = false;
  $("messages").replaceChildren();
  if (!session.messages.length) $("messages").append(node("div", "资料已准备好。点击“开始匹配”，或在下方输入问题。", "bubble assistant"));
  for (const message of session.messages) {
    const bubble = node("div", undefined, `bubble ${message.role}`);
    bubble.append(node("small", `${message.role === "user" ? "你" : "桂科融助手"}${message.context_label ? ` · ${message.context_label}` : ""}`), node("div", message.content));
    if (message.run_id) {
      const button = node("button", "查看本次结果");
      button.onclick = () => openRun(message.run_id);
      bubble.append(button);
    }
    $("messages").append(bubble);
  }
  $("messages").scrollTop = $("messages").scrollHeight;
  $("stale").hidden = !session.stale;
  renderSupplements();
  renderResults();
  renderComposer();
}

function composerDraft() {
  const key = state.session?.id || "new";
  if (!composerDrafts.has(key)) composerDrafts.set(key, {mode: "question", text: "", productId: "", target: null, verified: false});
  return composerDrafts.get(key);
}
function renderComposer() {
  const draft = composerDraft();
  const fact = draft.mode === "fact";
  const unavailable = !state.session || state.busy;
  $("compose-question").setAttribute("aria-pressed", String(!fact));
  $("compose-fact").setAttribute("aria-pressed", String(fact));
  $("chat-input").value = draft.text;
  $("chat-input").maxLength = fact ? 30000 : 12000;
  $("chat-input").disabled = unavailable;
  $("chat-input").placeholder = !state.session ? "先创建或选择企业会话…" : fact
    ? "补充具体事实，例如：本次拟申请贷款期限为2年。" : "输入你的问题，例如：这个产品为什么还需要核验？";
  if (draft.productId && ![...$("composer-product").options].some(option => option.value === draft.productId)) {
    $("composer-product").add(new Option(draft.target?.productName || draft.productId, draft.productId));
  }
  $("composer-product").value = draft.productId;
  $("composer-product").disabled = unavailable;
  $("chat-verified").checked = draft.verified;
  $("chat-verified").disabled = unavailable;
  $("composer-verification").hidden = !fact;
  $("composer-target").hidden = !draft.target;
  $("composer-target-text").textContent = draft.target ? `${draft.target.productName} · ${draft.target.quote || "产品整体"}` : "";
  $("chat-send").textContent = state.busy ? "处理中…" : fact ? "保存并匹配" : "发送";
  $("chat-send").disabled = unavailable || (fact && Boolean(state.view));
  $("manage-materials").disabled = unavailable;
  $("match").textContent = state.session?.run ? "重新匹配" : "开始匹配";
  $("match").disabled = unavailable || Boolean(state.view);
  $("match").title = state.view ? "从所选历史轮次继续后匹配" : draft.productId ? "匹配下方所选产品" : "匹配全部产品";
  $("composer-context").hidden = !state.view && !fact;
  $("composer-context").textContent = state.view
    ? (fact ? "先从右侧历史轮次继续，再保存事实。" : "正在讨论所选历史轮次。")
    : `保存事实并重新匹配${draft.productId ? "所选产品" : "全部产品"}。`;
}
function aimComposer(product, condition = null, mode = "fact") {
  if (state.busy) return;
  const draft = composerDraft();
  draft.mode = mode; draft.productId = product.product_id;
  draft.target = {productName: product.product_name, baseRunId: product.run_id,
    conditionId: condition?.condition_id || "__eligibility__", quote: condition?.source_quote_verified ? condition.source_quote : ""};
  notice(`已关联${product.product_name}${condition ? "的所选条款" : ""}，请在中间输入框${mode === "fact" ? "补充事实" : "提问"}。`);
  renderComposer(); $("chat-input").focus(); $("chat-form").scrollIntoView({block: "nearest"});
}
function conversationButton(product, condition = null, mode = "fact") {
  const button = node("button", "补充信息", "conversation-link");
  button.onclick = () => aimComposer(product, condition, mode);
  return button;
}
function matchingCommand(text) {
  // Only whole, explicit commands dispatch work; questions and new facts stay in their selected mode.
  const command = text.trim().replace(/\s+/g, "");
  const match = /^(?:请|麻烦)?(?:你)?(?:帮我|帮忙)?(?:重新匹配|开始匹配|再匹配|匹配)(?:一下|一次)?(全部(?:产品)?|所有(?:产品)?|所选产品|当前产品|这个产品)?(?:一下|一次)?(?:吧)?[。！!]*$/.exec(command);
  if (!match) return null;
  return {scope: /^(全部|所有)/.test(match[1] || "") ? "all" : match[1] ? "selected" : "composer"};
}
function submitComposer() {
  if (!state.session || state.busy || !$("chat-form").reportValidity()) return;
  return work(async () => {
    const draft = composerDraft();
    const text = draft.text;
    if (!text.trim()) throw new Error("请先输入问题或补充事实。");
    if (text.length > (draft.mode === "fact" ? 30000 : 12000)) throw new Error("输入超过字数限制，请精简后发送。");
    const command = matchingCommand(text);
    if (command) {
      if (state.view) throw new Error("当前正在查看历史轮次，请先点击“从这轮继续”，再重新匹配。");
      if (command.scope === "selected" && !draft.productId) throw new Error("请先在输入框上方选择要匹配的产品。");
      const productIds = command.scope === "all" || !draft.productId ? [] : [draft.productId];
      const result = await performMatch(productIds);
      if (!state.session.run.result.summary.failed_count) {
        draft.text = "";
        draft.target = null; // A successful run can change the selected clause and its base run.
      }
      renderComposer();
      return result;
    }
    if (draft.mode === "question") {
      const historicalView = state.view;
      const question = node("div", undefined, "bubble user"); question.append(node("small", "你"), node("div", text));
      const pending = node("div", "正在阅读资料并回复…", "bubble assistant pending-reply");
      $("messages").append(question, pending); $("messages").scrollTop = $("messages").scrollHeight;
      state.session = await api(`/sessions/${state.session.id}/messages`, {expected_version: state.session.version,
        message: text, product_id: draft.productId || null, condition_quote: draft.target?.quote || "", run_id: historicalView?.data.id || null});
      draft.text = "";
      renderSession();
      if (historicalView) { state.view = historicalView; renderResults(); }
      return "回复已保存，可继续提问或补充事实。";
    }
    if (state.view) throw new Error("请先从右侧历史轮次继续，再保存事实。");
    const current = state.session.effective_matches?.find(p => p.product_id === draft.productId);
    const verification = draft.verified ? "CONFIRMED" : "UNVERIFIED";
    if (draft.target && current?.run_id !== draft.target.baseRunId) throw new Error("评估已更新，请从右侧当前条款重新选择补充位置；输入内容已保留。");
    if (current) {
      state.session = await api(`/sessions/${state.session.id}/products/${encodeURIComponent(current.product_id)}/supplements`, {
        expected_version: state.session.version, base_run_id: current.run_id,
        entries: [{condition_id: draft.target?.conditionId || "__eligibility__", content: text, verification}],
      });
    } else {
      state.session = await api(`/sessions/${state.session.id}/documents`, {expected_version: state.session.version,
        documents: [{document_id: `supplement_${identifier()}`, filename: "对话补充事实.md", content: text}],
        supplement: {product_id: draft.productId || null, verification}});
    }
    const ids = draft.productId ? [draft.productId] : [];
    draft.text = ""; draft.verified = false; draft.target = null;
    renderSession();
    try { return await performMatch(ids); }
    catch (error) { throw new Error(`事实已保存，匹配未完成。可直接重新匹配，无需重复输入。${error.message}`); }
  }, "正在处理你的输入…");
}
function startReview(product, conditionId, text) {
  state.target = {product_id: product.product_id, condition_id: conditionId, run_id: product.run_id};
  $("review-target").textContent = `${product.product_name} · ${text}`;
  $("review-form").reset(); $("review-dialog").showModal();
}
function reviewButton(product, id, text) {
  const button = node("button", "人工复核"); button.disabled = Boolean(state.view) || !product.can_review;
  button.onclick = () => startReview(product, id, text); return button;
}
function supplementDescription(info) {
  const product = state.products.find(p => p.product_id === info.product_id);
  return `${info.product_id ? product?.product_name || info.product_id : "企业通用"}${info.condition_quote ? " · 限定条款" : ""} · ${info.legacy ? "历史记录未注明核实状态" : info.verification === "CONFIRMED" ? "已核实" : "尚未核实"}`;
}
function updateSupplementFields() {
  const editing = $("supplement-mode").value === "EDIT";
  const product = state.products.find(p => p.product_id === $("supplement-product").value);
  $("supplement-condition-fields").hidden = editing || !product;
  $("supplement-product-source").textContent = product?.application_conditions || "";
  for (const id of ["supplement-scope", "supplement-text-field", "supplement-verification-field"]) $(id).hidden = editing;
  $("supplement-text").disabled = editing;
  $("supplement-text").required = !editing;
  $("document-edit-fields").hidden = !editing;
  $("edit-document").required = editing;
  $("edit-document").disabled = !editing;
  $("document-content").required = editing;
  $("document-content").disabled = !editing || !$("edit-document").value;
  $("save-material").textContent = editing ? "保存文件" : "保存材料";
  const selected = state.session?.request.documents.some(d => d.document_id === $("edit-document").value);
  const lastDocument = state.session?.request.documents.length === 1;
  $("delete-document").hidden = !editing;
  $("delete-document").disabled = state.busy || !selected || lastDocument;
  $("last-document-hint").hidden = !editing || !selected || !lastDocument;
}
function loadDocumentContent() {
  const doc = state.session?.request.documents.find(d => d.document_id === $("edit-document").value);
  $("document-content").value = doc?.content || "";
  updateSupplementFields();
}
function renderSupplements() {
  if (state.supplementSessionId !== state.session.id) {
    $("supplement-form").reset();
    state.supplementSessionId = state.session.id;
  }
  const selected = $("edit-document").value;
  $("edit-document").replaceChildren(new Option("选择已有文件", ""));
  for (const doc of state.session.request.documents) $("edit-document").add(new Option(`${doc.filename} · ${doc.document_id.slice(-6)}`, doc.document_id));
  $("edit-document").value = selected;
  if (!$("edit-document").value) $("document-content").value = "";
  const documents = state.session.request.documents.filter(d => d.human_supplement);
  $("saved-supplements-title").textContent = `已保存的人工补充（${documents.length} 条）`;
  $("supplement-list").replaceChildren();
  for (const doc of [...documents].reverse()) {
    const info = doc.human_supplement;
    const item = node("article", undefined, "supplement-record");
    item.append(node("p", supplementDescription(info), "hint"), node("p", doc.content));
    if (info.condition_quote) item.append(node("p", `限定条款：${info.condition_quote}`, "hint"));
    $("supplement-list").append(item);
  }
  updateSupplementFields();
}
function renderSupplementFeedback(product) {
  const feedback = product.model_assessment?.supplement_feedback || [];
  if (!feedback.length) return null;
  const labels = {ADOPTED: "已采用", NOT_USED: "未采用", CONFLICT: "冲突待核对", NEEDS_CONFIRMATION: "尚需核实", NOT_REPORTED: "采纳反馈待核对", OUT_OF_SCOPE: "不适用本产品", CONDITIONS_CHANGED: "绑定条件已变化"};
  const details = node("details", undefined, "supplement-feedback");
  details.append(node("summary", `人工补充处理反馈（已采用 ${feedback.filter(f => f.status === "ADOPTED").length} 条）`),
    node("p", "以下为本轮模型的处理反馈；已采用不代表符合条件，历史反馈不会随新材料自动更新。", "hint"));
  const documents = (state.view?.data.snapshot.request || state.session.request).documents;
  for (const item of feedback) {
    const section = node("div", undefined, "supplement-record");
    section.append(node("strong", labels[item.status]), node("p", item.reason));
    if (item.impact) section.append(node("p", `对条款的影响：${item.impact}`));
    const clauses = product.eligibility.condition_results.filter(c => item.condition_ids.includes(c.condition_id));
    if (clauses.length) section.append(node("p", `相关条款：${clauses.map(c => c.source_quote).join("；")}`, "hint"));
    const doc = documents.find(d => d.document_id === item.document_id);
    if (doc) {
      const source = node("details"); source.append(node("summary", "查看这条人工补充"), node("p", doc.content));
      if (doc.human_supplement) source.append(node("p", supplementDescription(doc.human_supplement), "hint"));
      section.append(source);
    }
    details.append(section);
  }
  return details;
}
function showHuman(review) {
  const needs = (review.verification_needs || []).map(need => needLabels[need]).join("、");
  return node("p", `人工复核：${review.reviewer} · ${labels[review.status]}${needs ? ` · ${needs}` : ""}\n理由：${review.reason}\n依据：${review.evidence}\n${new Date(review.created_at).toLocaleString("zh-CN")}`, "human");
}
const attemptLabels = {SUCCEEDED: "评估成功", FAILED: "评估失败", UNAVAILABLE: "产品不可用", INCOMPLETE: "尚未完成 / 已中断", COMPLETED: "已完成"};
function timeLabel(value) { return value ? new Date(value).toLocaleString("zh-CN") : "时间未记录"; }
function resetResultFilters() { $("status-filter").value = "all"; $("product-search").value = ""; }
function openRun(runId, productId = null, history = null) {
  return work(async () => {
    const data = await api(`/sessions/${state.session.id}/runs/${runId}`);
    state.view = {data, productId, history}; resetResultFilters(); renderResults(); chooseTab(false);
    $("result-navigation").scrollIntoView({block: "nearest"});
  }, "正在读取已保存的匹配记录…");
}
function openProductHistory(product) {
  return work(async () => {
    const history = await api(`/sessions/${state.session.id}/products/${product.product_id}/history`);
    const data = await api(`/sessions/${state.session.id}/runs/${history.runs[0].id}`);
    state.view = {data, productId: product.product_id, history};
    resetResultFilters(); renderResults(); chooseTab(false);
    $("result-navigation").scrollIntoView({block: "nearest"});
  }, "正在读取该产品的全部匹配历史…");
}
function continueHistoryButton(productId = null, conditionId = null, label = "从这轮继续") {
  const button = node("button", label, "primary history-continue");
  button.disabled = !state.view?.data.can_continue;
  const sourceSession = state.session.id;
  const sourceRun = state.view?.data.id;
  button.onclick = () => work(async () => {
    state.session = await api(`/sessions/${sourceSession}/runs/${sourceRun}/continue`, {product_id: productId});
    resetResultFilters(); renderSession(); chooseTab(false); await loadSessions();
    const targetId = productId || state.session.effective_matches[0]?.product_id;
    const card = [...$("results").querySelectorAll(".product")].find(p => p.dataset.productId === targetId);
    if (card) {
      const product = state.session.effective_matches.find(p => p.product_id === targetId);
      const draft = composerDraft();
      draft.mode = "fact"; draft.productId = targetId;
      const clause = product.eligibility.condition_results.find(c => c.condition_id === conditionId);
      draft.target = {productName: product.product_name, baseRunId: product.run_id,
        conditionId: clause?.condition_id || "__eligibility__", quote: clause?.source_quote_verified ? clause.source_quote : ""};
      renderComposer(); $("chat-form").scrollIntoView({block: "nearest"});
      state.focusComposer = true;
    }
    return "已从所选轮次继续。可直接补充信息并匹配，原会话记录已保留。";
  }, "正在载入所选轮次的资料与评估…");
  return button;
}
function renderNavigation() {
  const navigation = $("result-navigation"); navigation.replaceChildren();
  $("history-snapshot").replaceChildren();
  if (!state.view) {
    if (state.session?.origin) {
      const origin = state.session.origin;
      navigation.append(node("p", `继续自 ${timeLabel(origin.evaluated_at)} 的评估`, "hint"));
      const back = node("button", origin.deleted ? "原会话已删除" : "返回原会话");
      back.disabled = Boolean(origin.deleted);
      back.onclick = () => work(async () => {
        state.session = await api(`/sessions/${origin.session_id}`); resetResultFilters(); renderSession(); await loadSessions();
      });
      navigation.append(back);
    }
    if (state.session?.runs?.length) {
      const label = node("details", undefined, "history-picker"); label.append(node("summary", "匹配历史"));
      const select = node("select"); select.add(new Option("选择历史轮次…", ""));
      for (const run of state.session.runs) select.add(new Option(`${timeLabel(run.created_at)} · ${run.attempts.length} 个产品 · ${attemptLabels[run.status]}`, run.id));
      select.onchange = () => { if (select.value) openRun(select.value); };
      label.append(select); navigation.append(label);
    }
    return;
  }
  const {data, productId, history} = state.view;
  const back = node("button", "返回全部产品总览");
  back.onclick = () => { state.view = null; resetResultFilters(); renderResults(); };
  navigation.append(back, node("h3", productId ? "单产品历史" : "本次匹配结果"),
    node("p", `${timeLabel(data.created_at)} · ${attemptLabels[data.status]}`, "hint"));
  navigation.append(continueHistoryButton(productId), node("p", data.can_continue
    ? "使用本轮资料继续，原记录保留。"
    : "该轮缺少完整资料快照或成功评估，请选择其他轮次继续。", "hint"));
  if (history) {
    const label = node("label", "选择产品版本"); const select = node("select");
    for (const run of history.runs) {
      const attempt = run.attempts.find(a => a.product_id === productId);
      select.add(new Option(`${timeLabel(run.created_at)} · ${attemptLabels[attempt.status]} · ${run.id.slice(-6)}`, run.id));
    }
    select.value = data.id; select.onchange = () => openRun(select.value, productId, history);
    label.append(select); navigation.append(label);
  }
  const snapshot = node("details", undefined, "snapshot"); snapshot.append(node("summary", "查看本轮企业资料与专业知识快照"));
  const request = data.snapshot.request;
  if (!request) snapshot.append(node("p", "该历史轮次没有完整输入快照。", "hint"));
  for (const doc of request?.documents || []) {
    const detail = node("details"); detail.append(node("summary", doc.filename), node("pre", doc.content)); snapshot.append(detail);
    if (doc.human_supplement) detail.append(node("p", supplementDescription(doc.human_supplement), "hint"));
  }
  const knowledge = new Map();
  for (const [pid, items] of Object.entries(data.snapshot.knowledge || {})) {
    if (productId && pid !== productId) continue;
    for (const item of items) knowledge.set(item.id, item);
  }
  snapshot.append(node("p", `本轮采用 ${knowledge.size} 条专业知识。`, "hint"));
  for (const item of knowledge.values()) snapshot.append(node("h4", item.title), node("p", item.content), node("p", `版本 ${item.version}`, "hint"));
  const reviews = data.reviews.filter(r => !productId || r.product_id === productId);
  if (reviews.length) {
    const trail = node("details"); trail.append(node("summary", "本轮人工复核记录"));
    for (const review of reviews) {
      trail.append(node("p", `${review.product_id} · ${review.condition_id}`, "hint"), showHuman(review));
    }
    snapshot.append(trail);
  }
  $("history-snapshot").append(snapshot);
}
function renderResults() {
  const session = state.session;
  renderNavigation();
  const products = (state.view?.data.effective_matches || session?.effective_matches || [])
    .filter(p => !state.view?.productId || p.product_id === state.view.productId);
  const filter = $("status-filter").value;
  const needFilter = Object.hasOwn(needLabels, filter) ? filter : null;
  const search = $("product-search").value.trim().toLowerCase();
  const visibleProducts = products.filter(product =>
    `${product.product_name} ${product.institution}`.toLowerCase().includes(search) &&
    (filter === "all" || product.decision === filter ||
      (product.decision === "PENDING_VERIFICATION" && product.verification_needs.includes(filter))));
  $("result-count").textContent = filter === "all" && !search
    ? `${products.length} 个已评估产品` : `显示 ${visibleProducts.length} / ${products.length} 个产品`;
  $("result-filter-summary").hidden = !needFilter;
  $("result-filter-summary").textContent = needFilter ? `当前展示${needLabels[needFilter]}的条款。` : "";
  $("stale").hidden = Boolean(state.view) || !session?.stale;
  $("stale").textContent = `${products.filter(p => p.stale).length} 个产品的资料已变化，需要重新匹配。`;
  $("results").replaceChildren();
  for (const product of visibleProducts) {
    const card = node("article", undefined, "product"); card.dataset.productId = product.product_id;
    const heading = node("div", undefined, "product-head");
    const title = node("div"); title.append(node("h3", product.product_name), node("span", product.institution, "hint"));
    heading.append(title, node("span", decisionLabels[product.decision], `tag ${product.eligibility.status}`));
    card.append(heading, node("p", product.eligibility.explanation, "product-explanation"));
    if (!state.view && product.stale) card.append(node("p", `待更新：${product.stale_reasons.join("；")}`, "warning"));
    if (!state.view && product.latest_attempt && product.latest_attempt.status !== "SUCCEEDED") {
      card.append(node("p", "上次匹配未完成，已保留资料和旧结果。", "warning"));
      const retry = node("button", "重新匹配此产品"); retry.onclick = () => runMatch([product.product_id]); card.append(retry);
    }
    const comparison = renderContinuation(product); if (comparison) card.append(comparison);
    const conditions = product.eligibility.condition_results;
    const preliminary = c => c.status === "PASS" && c.assessment_basis === "PRELIMINARY_POLICY" && !product.human_reviews?.[c.condition_id];
    const preliminaryCount = conditions.filter(preliminary).length;
    if (preliminaryCount) card.append(node("p", `${preliminaryCount}项概括性条件按初筛口径通过`, "hint preliminary-summary"));
    const highlighted = c => c.status === "UNKNOWN" && (!needFilter || c.verification_needs.includes(needFilter));
    const others = node("details", undefined, "other-conditions");
    const otherCount = conditions.filter(c => !highlighted(c)).length;
    others.append(node("summary", `查看其余 ${otherCount} 条条件`));
    if (!conditions.some(highlighted) && product.eligibility.status === "UNKNOWN") {
      card.append(node("p", "待核验事项见上方整体判断。", "hint"));
      card.append(state.view ? continueHistoryButton(product.product_id, null, "从这里继续补充") : conversationButton(product));
    }
    for (const condition of conditions) {
      const item = node("div", undefined, "condition"); item.dataset.conditionId = condition.condition_id;
      item.append(node("span", preliminary(condition) ? "初筛通过" : labels[condition.status], `tag ${condition.status}`), node("blockquote", condition.source_quote));
      if (highlighted(condition)) {
        item.append(node("p", condition.required_material || condition.explanation));
        item.append(state.view ? continueHistoryButton(product.product_id, condition.condition_id, "从这里继续补充") : conversationButton(product, condition));
      }
      const evidence = node("details", undefined, "condition-evidence"); evidence.append(node("summary", "依据与复核"), node("p", condition.explanation));
      if (!condition.source_quote_verified) evidence.append(node("p", "该条款未逐字定位到申请条件原文，请核对。", "warning"));
      for (const id of condition.evidence_ids) {
        const ref = product.evidence?.[id];
        if (ref) evidence.append(node("blockquote", `${ref.quote}\n— ${ref.filename}，第 ${ref.line_start}–${ref.line_end} 行`));
      }
      if (product.human_reviews[condition.condition_id]) evidence.append(showHuman(product.human_reviews[condition.condition_id]));
      if (!state.view) {
        evidence.append(reviewButton(product, condition.condition_id, condition.source_quote));
        if (!highlighted(condition)) evidence.append(conversationButton(product, condition));
      }
      item.append(evidence);
      if (highlighted(condition)) card.append(item); else others.append(item);
    }
    if (otherCount) card.append(others);
    const more = node("details", undefined, "product-details"); more.append(node("summary", "产品详情"));
    more.append(node("p", `评估时间：${timeLabel(product.evaluated_at)}`, "hint"));
    const source = node("details"); source.append(node("summary", "完整申请条件"), node("p", product.application_conditions)); more.append(source);
    if (product.overall_review_needed) more.append(node("p", "条款已有人工修改，请复核整体资格。", "warning"));
    if (product.human_reviews.__eligibility__) more.append(showHuman(product.human_reviews.__eligibility__));
    const feedback = renderSupplementFeedback(product); if (feedback) more.append(feedback);
    if (!state.view) {
      const history = node("button", "查看历史"); history.onclick = () => openProductHistory(product);
      const retry = node("button", "重新匹配此产品"); retry.onclick = () => runMatch([product.product_id]);
      more.append(history, retry, reviewButton(product, "__eligibility__", "整体资格"));
    }
    card.append(more);
    for (const warning of product.validation_warnings) card.append(node("p", warning, "warning"));
    $("results").append(card);
  }
  const attempts = state.view?.data.attempts || session?.product_attempts || [];
  for (const attempt of attempts) {
    if (filter !== "all" || attempt.status === "SUCCEEDED" || (state.view?.productId && state.view.productId !== attempt.product_id)) continue;
    if (!attempt.product_name.toLowerCase().includes(search)) continue;
    $("results").append(node("div", `${attempt.product_name}：${attemptLabels[attempt.status]}。${attempt.message || attempt.error_code || "可重新匹配。"}`, "warning"));
  }
  if (!$("results").children.length) $("results").append(node("div", state.view || products.length || session?.run ? "当前筛选下没有产品。" : "开始匹配后，在这里查看结果。", "empty"));
  renderComposer();
}

function renderContinuation(product) {
  const comparison = product.continuation;
  if (!comparison) return null;
  const section = node("details", undefined, "continuation-result");
  const outdated = !state.view && (product.stale || (product.latest_attempt && product.latest_attempt.status !== "SUCCEEDED"));
  section.append(node("summary", `${outdated ? "上次评估变化" : "本轮变化"}：${labels[comparison.before]} → ${labels[comparison.after]}`),
    node("p", "与上轮模型判断对比，人工复核单独展示。", "hint"));
  const details = node("div");
  for (const change of comparison.changes) {
    if (change.before === change.after && change.after !== "UNKNOWN") continue;
    details.append(node("p", `${change.source_quote}\n${change.before ? labels[change.before] : "本轮重新划分"} → ${labels[change.after]}\n${change.explanation}`));
    if (change.after === "UNKNOWN" && change.required_material) details.append(node("p", `仍需确认：${change.required_material}`, "hint"));
  }
  if (!details.children.length) details.append(node("p", "条款状态与上轮一致，当前没有待核验条款。"));
  if (comparison.unpaired_previous_count) details.append(node("p", "部分条款划分或原文定位发生变化，未直接对应旧条款；可查看两轮完整结果。", "hint"));
  section.append(details);
  const changedIds = new Set([...comparison.added_document_ids, ...comparison.updated_document_ids]);
  const feedbackLabels = {ADOPTED: "已采用", NOT_USED: "信息不足或不适用", CONFLICT: "与资料冲突", NEEDS_CONFIRMATION: "尚需核实", NOT_REPORTED: "采纳情况未能核对"};
  for (const item of product.model_assessment?.supplement_feedback || []) {
    if (!changedIds.has(item.document_id)) continue;
    section.append(node("p", `本轮补充：${feedbackLabels[item.status] || item.status}\n${item.reason}\n${item.impact || ""}`));
  }
  if (comparison.removed_document_ids.length) section.append(node("p", `该轮移除 ${comparison.removed_document_ids.length} 份资料，结果依据该轮保存的资料重新判断。`, "hint"));
  return section;
}
function updateKnowledgeScope() {
  $("knowledge-quote").required = Boolean($("knowledge-product").value);
}
function renderKnowledge() {
  $("knowledge-list").replaceChildren();
  for (const item of [...state.knowledge].reverse()) {
    const card = node("article", undefined, "knowledge-card");
    card.append(node("h3", item.title), node("span", `${labels[item.status]}${item.status === "PUBLISHED" && item.applicable === false ? " · 未匹配到适用产品条件" : ""}`, "tag"));
    const product = state.products.find(p => p.product_id === item.product_id);
    card.append(node("p", product ? `适用产品：${product.product_name}` : "通用专业解释", "hint"), node("p", item.content));
    if (item.condition_quote) card.append(node("p", `条款匹配：${item.condition_quote}`, "hint"));
    if (item.status !== "RETIRED") {
      const button = node("button", item.status === "DRAFT" ? "发布" : "停用");
      button.onclick = () => work(async () => {
        await api(`/knowledge/${item.id}/decision`, {expected_version: item.version,
          status: item.status === "DRAFT" ? "PUBLISHED" : "RETIRED"});
        await loadKnowledge();
        if (state.session) { state.session = await api(`/sessions/${state.session.id}`); renderSession(); }
      });
      card.append(button);
    }
    $("knowledge-list").append(card);
  }
  if (!state.knowledge.length) $("knowledge-list").append(node("p", "还没有专业知识。可从待核验条款进入录入，也可追加通用解释。", "empty"));
}
function drawMatchProgress() {
  const progress = matchingProgress;
  const percent = progress.total ? Math.floor(progress.completed / progress.total * 100) : 0;
  $("matching-progress-percent").textContent = `${percent}%`;
  $("matching-progress-fill").style.width = `${percent}%`;
  $("matching-progress-track").setAttribute("aria-valuenow", String(percent));
  $("matching-progress-label").textContent = progress.label;
  $("matching-progress-count").textContent = progress.total
    ? `已处理 ${progress.completed} / ${progress.total} 个产品${progress.failed ? ` · ${progress.failed} 个评估失败` : ""}`
    : "等待产品清单";
  $("matching-progress-elapsed").textContent = `已用 ${Math.floor((performance.now() - progress.startedAt) / 1000)} 秒`;
}
function receiveMatchProgress(event) {
  const progress = matchingProgress;
  if (event.type === "started") { progress.total = event.total; progress.label = "正在准备企业资料…"; }
  if (event.type === "phase") progress.label = event.phase === "finalizing" ? "正在汇总并保存结果…" : event.message;
  if (event.type === "product") {
    progress.total = event.total;
    progress.completed = Math.min(event.total, Math.max(progress.completed, event.completed));
    if (event.status === "failed") progress.failures.add(event.product_id);
    progress.failed = progress.failures.size;
    progress.label = progress.completed === progress.total ? "产品已处理，正在汇总结果…" : "正在逐项匹配产品…";
  }
  drawMatchProgress();
}
async function streamMatch(session, productIds) {
  const headers = {"Content-Type": "application/json"};
  const response = await fetch(`/api/v1/sessions/${session.id}/match/stream`, {method: "POST", headers,
    body: JSON.stringify({expected_version: session.version, product_ids: productIds})});
  if (!response.ok) {
    const data = await response.json();
    throw new Error(data.error?.message || `请求失败 (${response.status})`);
  }
  if (!response.body) throw new Error("浏览器未能读取匹配进度，请刷新会话查看记录。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, {stream: true});
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
        const lines = frame.split("\n");
        const event = lines.find(line => line.startsWith("event:"))?.slice(6).trim();
        const data = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
        if (!event || !data) continue;
        const payload = JSON.parse(data);
        if (event === "progress") receiveMatchProgress(payload);
        if (event === "result") return payload;
        if (event === "error") throw new Error(payload.error?.message || "匹配失败，请刷新会话查看记录。");
      }
      if (done) throw new Error("匹配连接中断，尚未收到完整结果，请刷新会话查看记录。");
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
function runMatch(productIds) {
  if (!state.session) return;
  return work(() => performMatch(productIds), "正在调用模型匹配，完成后结果自动显示。请保持页面打开；操作记录会保存。");
}
async function performMatch(productIds) {
  matchingProgress = {sessionId: state.session.id, completed: 0, total: 0, failed: 0, failures: new Set(),
    startedAt: performance.now(), label: "正在准备匹配…"};
  $("matching-progress").hidden = false;
  $("matching-progress").dataset.status = "running";
  drawMatchProgress();
  const timer = setInterval(drawMatchProgress, 1000);
  try {
    state.session = await streamMatch(state.session, productIds);
    const summary = state.session.run.result.summary;
    matchingProgress.total = matchingProgress.completed = summary.evaluated_count + summary.failed_count;
    matchingProgress.failed = summary.failed_count;
    matchingProgress.label = summary.failed_count ? "处理完成，部分产品评估失败" : "匹配完成";
    $("matching-progress").dataset.status = "completed";
    renderSession(); chooseTab(false);
    if (productIds.length === 1) {
      resetResultFilters(); renderResults();
      const card = [...$("results").querySelectorAll(".product")].find(p => p.dataset.productId === productIds[0]);
      if (card) { card.classList.add("just-updated"); card.scrollIntoView({block: "nearest"}); }
    }
    return summary.failed_count ? "部分产品评估失败，已保留补充与上次成功结果，可重新匹配重试。" : "匹配已完成，可查看本轮判断变化和剩余问题。";
  } catch (error) {
    matchingProgress.label = "匹配已中断，请查看上方提示";
    $("matching-progress").dataset.status = "error";
    throw error;
  } finally { clearInterval(timer); drawMatchProgress(); }
}
$("create-form").onsubmit = event => {
  event.preventDefault(); work(async () => {
    const documents = [];
    const files = selectedFiles();
    const pasted = $("initial-doc").value;
    const hasPasted = Boolean(pasted.trim());
    if (files.length + Number(hasPasted) > uploadLimits.max_documents) throw new Error(`资料数量超过 ${uploadLimits.max_documents} 份限制，请只选择一家企业的资料文件夹。`);
    const pastedBytes = hasPasted ? new TextEncoder().encode(pasted).length : 0;
    if (pastedBytes > uploadLimits.max_document_bytes || files.some(file => file.size > uploadLimits.max_document_bytes)) throw new Error(`单份资料不能超过 ${uploadLimits.max_document_bytes / 1048576} MiB。`);
    if (files.reduce((sum, file) => sum + file.size, pastedBytes) > uploadLimits.max_total_document_bytes) throw new Error(`资料合计不能超过 ${uploadLimits.max_total_document_bytes / 1048576} MiB。`);
    for (const file of files) {
      const filename = file.webkitRelativePath || file.name;
      let content;
      try { content = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer()); }
      catch { throw new Error(`无法读取 ${filename}，请确认文件为 UTF-8 编码。`); }
      if (!content.trim()) throw new Error(`${filename} 内容为空，请移除空文件后重新选择。`);
      documents.push({document_id: `doc_${identifier()}`, filename, content});
    }
    if (hasPasted) documents.push({document_id: `doc_${identifier()}`, filename: "企业资料.md", content: pasted});
    if (!documents.length) throw new Error("所选文件夹中没有 .md 文件，请选择包含企业 Markdown 资料的文件夹或粘贴资料。");
    state.session = await api("/sessions", {company: {name: $("company").value}, documents});
    $("create-form").reset(); showSelection(); renderSession(); await loadSessions();
  });
};
$("supplement-form").onsubmit = event => {
  event.preventDefault(); if (!state.session) { notice("请先创建或选择企业会话。", true); return; }
  work(async () => {
    if ($("supplement-mode").value === "EDIT") {
      state.session = await api(`/sessions/${state.session.id}/documents/edit`, {
        expected_version: state.session.version, document_id: $("edit-document").value,
        content: $("document-content").value});
      renderSession(); loadDocumentContent(); return;
    }
    state.session = await api(`/sessions/${state.session.id}/documents`, {expected_version: state.session.version,
      documents: [{document_id: `supplement_${identifier()}`, filename: "人工补充资料.md", content: $("supplement-text").value}],
      supplement: {verification: $("supplement-verification").value,
        product_id: $("supplement-product").value || null,
        condition_quote: $("supplement-product").value ? $("supplement-condition").value : ""}});
    $("supplement-form").reset(); renderSession();
  });
};
$("delete-document").onclick = () => {
  if (state.busy) return;
  const doc = state.session?.request.documents.find(d => d.document_id === $("edit-document").value);
  if (!doc) { notice("请先选择要删除的文件。", true); return; }
  if (!window.confirm(`确定删除“${doc.filename}”吗？\n删除后该文件不再用于后续匹配，历史评估仍保留原资料。`)) return;
  work(async () => {
    state.session = await api(`/sessions/${state.session.id}/documents/delete`, {
      expected_version: state.session.version, document_id: doc.document_id});
    renderSession(); loadDocumentContent();
  }, "正在删除文件…");
};
$("knowledge-form").onsubmit = event => {
  event.preventDefault(); work(async () => {
    await api("/knowledge", {title: $("knowledge-title").value, content: $("knowledge-content").value,
      product_id: $("knowledge-product").value || null, condition_quote: $("knowledge-quote").value});
    $("knowledge-form").reset(); updateKnowledgeScope(); await loadKnowledge();
  });
};
$("review-form").onsubmit = event => {
  event.preventDefault(); work(async () => {
    const selection = $("review-status").value;
    const needs = {UNKNOWN_MATERIAL: ["MATERIAL_CONFIRMATION"], UNKNOWN_PROFESSIONAL: ["PROFESSIONAL_REVIEW"],
      UNKNOWN_BOTH: ["MATERIAL_CONFIRMATION", "PROFESSIONAL_REVIEW"]}[selection] || [];
    state.session = await api(`/sessions/${state.session.id}/reviews`, {expected_version: state.session.version,
      ...state.target, status: needs.length ? "UNKNOWN" : selection,
      verification_needs: needs, reviewer: $("reviewer").value,
      reason: $("review-reason").value, evidence: $("review-evidence").value});
    $("review-dialog").close(); renderSession();
  });
};
$("close-review").onclick = () => $("review-dialog").close();
$("folder-input").onchange = () => { if ($("folder-input").files.length) $("files").value = ""; showSelection(); };
$("files").onchange = () => { if ($("files").files.length) $("folder-input").value = ""; showSelection(); };
$("new-session").onclick = () => { $("create-panel").open = true; $("company").focus(); };
$("match").onclick = () => runMatch(composerDraft().productId ? [composerDraft().productId] : []);
$("status-filter").onchange = renderResults;
$("supplement-product").onchange = () => { $("supplement-condition").value = ""; updateSupplementFields(); };
$("supplement-mode").onchange = () => { updateSupplementFields(); if ($("supplement-mode").value === "EDIT") loadDocumentContent(); };
$("edit-document").onchange = loadDocumentContent;
$("product-search").oninput = renderResults;
$("open-knowledge").onclick = () => { if (!state.busy) chooseTab(true); };
$("close-knowledge").onclick = () => chooseTab(false);
$("knowledge-product").onchange = updateKnowledgeScope;
$("manage-materials").onclick = () => { if (!state.busy) $("materials-dialog").showModal(); };
$("close-materials").onclick = () => $("materials-dialog").close();
$("compose-question").onclick = () => { if (!state.busy) { composerDraft().mode = "question"; renderComposer(); $("chat-input").focus(); } };
$("compose-fact").onclick = () => { if (!state.busy) { composerDraft().mode = "fact"; renderComposer(); $("chat-input").focus(); } };
$("composer-product").onchange = () => { const draft = composerDraft(); draft.productId = $("composer-product").value; draft.target = null; renderComposer(); };
$("clear-composer-target").onclick = () => { if (!state.busy) { composerDraft().target = null; renderComposer(); } };
$("chat-input").oninput = () => {
  const draft = composerDraft(); draft.text = $("chat-input").value;
};
$("chat-verified").onchange = () => { composerDraft().verified = $("chat-verified").checked; };
$("chat-form").onsubmit = event => { event.preventDefault(); submitComposer(); };
$("chat-input").onkeydown = event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault(); if (!state.busy) $("chat-form").requestSubmit();
  }
};
work(connect, "正在连接服务…");
