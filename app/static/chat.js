"use strict";
const $ = id => document.getElementById(id);
const state = {session: null, products: [], knowledge: [], busy: false, target: null, view: null};
let matchingProgress = null;
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
function needsBadges(needs) {
  const container = node("div", undefined, "verification-needs");
  for (const need of needs || []) container.append(node("span", needLabels[need], `tag ${need}`));
  return container;
}
// getRandomValues also works on LAN HTTP, where randomUUID may be unavailable.
function identifier() { return Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join(""); }
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function notice(message, error = false) { $("notice").textContent = message; $("notice").className = error ? "error" : ""; }
async function api(path, body) {
  const headers = {"Content-Type": "application/json"};
  const response = await fetch(`/api/v1${path}`, {method: body === undefined ? "GET" : "POST", headers,
    ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || `请求失败 (${response.status})`);
  return data;
}
async function work(action, message = "正在处理，请稍候…") {
  if (state.busy) return;
  state.busy = true;
  document.body.setAttribute("aria-busy", "true");
  document.querySelectorAll("button").forEach(button => { button.dataset.wasDisabled = String(button.disabled); button.disabled = true; });
  notice(message);
  try { await action(); notice("操作已完成，记录已保存。"); }
  catch (error) {
    notice(error.message, true);
    if (state.session) {
      try { state.session = await api(`/sessions/${state.session.id}`); renderSession(); } catch { /* Preserve original error. */ }
    }
  } finally {
    state.busy = false;
    document.body.setAttribute("aria-busy", "false");
    document.querySelectorAll("button").forEach(button => {
      if (button.dataset.wasDisabled !== undefined) button.disabled = button.dataset.wasDisabled === "true";
      delete button.dataset.wasDisabled;
    });
    ["match", "refresh-session", "export"].forEach(id => { $(id).disabled = !state.session; });
    updateSupplementFields();
  }
}
function chooseTab(knowledge) {
  $("knowledge-panel").hidden = !knowledge; $("results-panel").hidden = knowledge;
  $("knowledge-tab").setAttribute("aria-selected", String(knowledge));
  $("results-tab").setAttribute("aria-selected", String(!knowledge));
}
async function loadSessions() {
  const data = await api("/sessions");
  $("sessions").replaceChildren();
  for (const session of data.sessions) {
    const button = node("button", session.company.name, session.id === state.session?.id ? "active" : "");
    button.onclick = () => work(async () => {
      state.session = await api(`/sessions/${session.id}`); renderSession(); await loadSessions();
    }, "正在恢复会话…");
    $("sessions").append(button);
  }
}
async function loadKnowledge() {
  state.knowledge = (await api("/knowledge")).items;
  renderKnowledge();
}
async function connect() {
  const health = await (await fetch("/health")).json();
  uploadLimits = {...uploadLimits, ...health.upload_limits};
  $("health").textContent = `${health.model_provider === "deepseek" ? "DeepSeek" : "火山方舟"} · ${health.model_configured ? "已配置" : "未配置模型"}`;
  state.products = (await api("/financial-products")).products;
  for (const id of ["match-product", "knowledge-product", "supplement-product"]) {
    const select = $(id); while (select.options.length > 1) select.remove(1);
    for (const product of state.products) select.add(new Option(product.product_name, product.product_id));
  }
  await loadSessions(); await loadKnowledge();
  if (state.session) { state.session = await api(`/sessions/${state.session.id}`); renderSession(); }
}
function renderSession() {
  const session = state.session;
  if (!session) return;
  state.view = null;
  if (matchingProgress?.sessionId !== session.id) $("matching-progress").hidden = true;
  $("session-title").textContent = session.request.company.name;
  $("session-meta").textContent = `${session.request.documents.length} 份企业资料 · ${session.messages.length} 条会话记录 · 已保存`;
  $("create-panel").open = false;
  $("messages").replaceChildren();
  if (!session.messages.length) $("messages").append(node("div", "企业资料已保存。点击“开始匹配”获取反馈；之后可从消息中的“查看本次结果”回看。", "bubble"));
  for (const message of session.messages) {
    const bubble = node("div", undefined, `bubble ${message.role}`);
    bubble.append(node("small", message.role === "user" ? "你" : "桂科融助手"), node("div", message.content));
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
}
function startReview(product, conditionId, text) {
  state.target = {product_id: product.product_id, condition_id: conditionId, run_id: product.run_id};
  $("review-target").textContent = `${product.product_name} · ${text}`;
  $("review-form").reset(); $("review-dialog").showModal();
}
function startKnowledge(product, quote) {
  chooseTab(true); $("knowledge-editor").open = true;
  $("knowledge-product").value = product.product_id; $("knowledge-quote").value = quote;
  updateKnowledgeScope();
  $("knowledge-title").focus();
}
function reviewButton(product, id, text) {
  const button = node("button", "人工复核"); button.disabled = Boolean(state.view) || !product.can_review;
  button.onclick = () => startReview(product, id, text); return button;
}
function materialButton(product, text, quote = "") {
  const button = node("button", "补充 / 确认材料");
  button.onclick = () => {
    $("supplement-text").closest("details").open = true;
    $("supplement-product").value = product.product_id;
    $("supplement-mode").value = "ADD";
    $("supplement-condition").value = quote;
    updateSupplementFields();
    $("supplement-text").placeholder = `${product.product_name}：${text}`;
    $("supplement-text").focus();
  };
  return button;
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
  const active = feedback.filter(f => !["OUT_OF_SCOPE", "CONDITIONS_CHANGED"].includes(f.status));
  const details = node("details", undefined, "supplement-feedback"); details.open = active.length > 0;
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
function renderNavigation() {
  const navigation = $("result-navigation"); navigation.replaceChildren();
  $("history-snapshot").replaceChildren();
  if (!state.view) {
    navigation.append(node("h3", "全部产品总览"));
    if (state.session?.runs?.length) {
      const label = node("label", "按匹配轮次回看");
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
  navigation.append(back, node("h3", productId ? "单产品历史（只读）" : "本次匹配结果（只读）"),
    node("p", `${timeLabel(data.created_at)} · ${attemptLabels[data.status]}`, "hint"));
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
  snapshot.append(node("p", `资料版本 ${data.snapshot.input_revision ?? "未记录"} · 模型 ${data.snapshot.model || "未记录"} · ${data.snapshot.provider || ""}`, "hint"));
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
  $("result-count").textContent = `${products.length} 个已评估产品`;
  const current = state.view ? products : products.filter(p => !p.stale);
  $("status-summary").textContent = `${state.view ? "本轮结论" : `资料与口径未变化 ${current.length} 个`}：` + Object.entries(decisionLabels).map(([key, label]) => `${label} ${current.filter(p => p.decision === key).length}`).join(" · ");
  $("stale").hidden = Boolean(state.view) || !session?.stale;
  $("stale").textContent = `${products.filter(p => p.stale).length} 个产品需要更新，旧结论保留供参考，未计入上方当前状态统计。`;
  $("results").replaceChildren();
  let lastGroup;
  for (const product of products) {
    if (!`${product.product_name} ${product.institution}`.toLowerCase().includes($("product-search").value.trim().toLowerCase())) continue;
    const filter = $("status-filter").value;
    if (filter !== "all" && product.decision !== filter && !(product.decision === "PENDING_VERIFICATION" && product.verification_needs.includes(filter))) continue;
    if (lastGroup !== product.decision) {
      $("results").append(node("h3", decisionLabels[product.decision], "result-group"));
      lastGroup = product.decision;
    }
    const card = node("article", undefined, "product");
    card.dataset.productId = product.product_id;
    const heading = node("div", undefined, "product-head");
    const title = node("div"); title.append(node("h3", product.product_name), node("span", product.institution, "hint"));
    title.append(node("div", `条款覆盖 ${product.match_score}%（辅助信息）`, "hint"));
    heading.append(title, node("span", decisionLabels[product.decision], `tag ${product.eligibility.status}`)); card.append(heading);
    card.append(needsBadges(product.verification_needs));
    const supplementFeedback = renderSupplementFeedback(product);
    if (supplementFeedback) card.append(supplementFeedback);
    card.append(node("p", `评估时间：${timeLabel(product.evaluated_at)}`, "hint"));
    if (!state.view) {
      if (product.stale) card.append(node("p", `需要更新：${product.stale_reasons.join("；")}。以下为上次结论。`, "warning"));
      if (product.latest_attempt && product.latest_attempt.status !== "SUCCEEDED") card.append(node("p", `最近尝试：${attemptLabels[product.latest_attempt.status]}，当前保留上次成功评估。请重新匹配后复核。`, "warning"));
      const history = node("button", `查看历史（${product.history_count} 次）`);
      history.onclick = () => openProductHistory(product);
      const retry = node("button", "重新匹配此产品"); retry.onclick = () => runMatch([product.product_id]);
      const actions = node("div", undefined, "product-history-actions"); actions.append(history, retry); card.append(actions);
    }
    const overall = node("div", undefined, "condition");
    overall.append(node("p", product.eligibility.explanation));
    if (product.overall_review_needed) overall.append(node("p", "条款已有人工修改，请另行复核整体资格。", "warning"));
    if (product.human_reviews.__eligibility__) overall.append(showHuman(product.human_reviews.__eligibility__));
    overall.append(reviewButton(product, "__eligibility__", "整体资格（请综合且 / 或 / 豁免关系判断）"));
    if (product.verification_needs.includes("MATERIAL_CONFIRMATION")) overall.append(materialButton(product, product.eligibility.explanation));
    if (product.verification_needs.includes("PROFESSIONAL_REVIEW")) {
      const knowledge = node("button", "解释产品要求");
      knowledge.onclick = () => startKnowledge(product, product.application_conditions);
      overall.append(knowledge);
    }
    card.append(overall);
    const source = node("details"); source.append(node("summary", "完整申请条件"), node("p", product.application_conditions)); card.append(source);
    if (state.view) {
      const original = state.view.data.result?.matches.find(p => p.product_id === product.product_id);
      if (original) {
        const detail = node("details"); detail.append(node("summary", "模型原始判断（未叠加人工复核）"),
          node("p", `${labels[original.eligibility.status]}：${original.eligibility.explanation}`));
        for (const condition of original.eligibility.condition_results) detail.append(node("p", `${condition.source_quote}\n${labels[condition.status]}：${condition.explanation}`));
        card.append(detail);
      }
    }
    for (const condition of product.eligibility.condition_results) {
      const item = node("div", undefined, "condition");
      item.append(node("span", labels[condition.status], `tag ${condition.status}`), node("blockquote", condition.source_quote), node("p", condition.explanation));
      if (condition.status === "UNKNOWN") item.append(needsBadges(condition.verification_needs));
      if (!condition.source_quote_verified) item.append(node("p", "该条件描述未逐字定位到产品原文，请核对完整申请条件。", "hint"));
      for (const id of condition.evidence_ids) {
        const evidence = product.evidence?.[id];
        if (evidence) item.append(node("blockquote", `${evidence.quote}\n— ${evidence.filename}，第 ${evidence.line_start}–${evidence.line_end} 行`));
      }
      if (condition.status === "UNKNOWN" && condition.required_material) item.append(node("p", `待核验：${condition.required_material}`, "hint"));
      if (product.human_reviews[condition.condition_id]) item.append(showHuman(product.human_reviews[condition.condition_id]));
      item.append(reviewButton(product, condition.condition_id, condition.source_quote));
      if (condition.verification_needs.includes("MATERIAL_CONFIRMATION")) {
        item.append(materialButton(product, condition.required_material || condition.explanation, condition.source_quote_verified ? condition.source_quote : ""));
      }
      const knowledge = node("button", "补充专业知识"); knowledge.onclick = () => startKnowledge(product, condition.source_quote); item.append(knowledge);
      const retry = node("button", "重新匹配此产品"); retry.onclick = () => runMatch([product.product_id]); item.append(retry);
      card.append(item);
    }
    for (const warning of product.validation_warnings) card.append(node("p", warning, "warning"));
    if (state.view) card.querySelectorAll("button").forEach(button => { button.hidden = true; });
    $("results").append(card);
  }
  const attempts = state.view?.data.attempts || session?.product_attempts || [];
  for (const attempt of attempts) {
    if (attempt.status === "SUCCEEDED" || (state.view?.productId && state.view.productId !== attempt.product_id)) continue;
    if (!attempt.product_name.toLowerCase().includes($("product-search").value.trim().toLowerCase())) continue;
    $("results").append(node("div", `${attempt.product_name}：${attemptLabels[attempt.status]}。${attempt.message || attempt.error_code || "可重新匹配。"}`, "warning"));
  }
  if (!$("results").children.length) $("results").append(node("div", session?.run ? "当前筛选下没有产品。" : "开始匹配后，将在此展示条款和原文依据。", "empty"));
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
  work(async () => {
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
    } catch (error) {
      matchingProgress.label = "匹配已中断，请查看上方提示";
      $("matching-progress").dataset.status = "error";
      throw error;
    } finally { clearInterval(timer); drawMatchProgress(); }
  }, "正在调用模型匹配，完成后结果自动显示。请保持页面打开；操作记录会保存。");
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
$("match").onclick = () => runMatch($("match-product").value ? [$("match-product").value] : []);
$("refresh-session").onclick = () => work(async () => { state.session = await api(`/sessions/${state.session.id}`); renderSession(); });
$("status-filter").onchange = renderResults;
$("supplement-product").onchange = () => { $("supplement-condition").value = ""; updateSupplementFields(); };
$("supplement-mode").onchange = () => { updateSupplementFields(); if ($("supplement-mode").value === "EDIT") loadDocumentContent(); };
$("edit-document").onchange = loadDocumentContent;
$("product-search").oninput = renderResults;
$("results-tab").onclick = () => chooseTab(false);
$("knowledge-tab").onclick = () => chooseTab(true);
$("knowledge-product").onchange = updateKnowledgeScope;
$("export").onclick = () => work(async () => {
  const data = await api(`/sessions/${state.session.id}/events`);
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"}));
  const link = node("a"); link.href = url; link.download = `${state.session.id}-完整记录.json`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
work(connect, "正在连接服务…");
