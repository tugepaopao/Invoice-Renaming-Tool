/* =========================================================
   全电发票批量重命名工具 · 前端逻辑
   ========================================================= */
"use strict";

const $  = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let S = null;              // 当前快照
// 初始不选中任何行 —— 右栏先显示「本批汇总」。
// （原来初始选中第 1 行，结果那块汇总几乎永远看不到）
let sel = -1;               // 选中行
let busy = false;          // 是否有任务在跑
let pollTimer = null;
let dupOnly = false;       // 只看重复票号（v2.7 起并入 filterMode，保留兼容）
let filterMode = "all";    // all / todo / dup / multi / bad（v2.7 状态筛选）
let sortBy = "file";       // file / amount / date / buyer（v2.7 排序）
// 用户点了「我知道了」的跨批次提醒（本次运行内不再显示；票号）
const seenMuted = new Set();
// 只看「以前处理过」的那些（汇总区提示条上的「只看这些」）
let filterSeen = false;
let searchText = "";       // 列表搜索关键词（纯前端过滤，见 applySearch）
let lastResult = null;     // 上次执行结果

/* ---------------- 与 Python 通信 ---------------- */
/**
 * 等 pywebview 的 JS API 就绪后再执行回调。
 *
 * 这里必须写得很保守，原因是踩过一个很隐蔽的坑：
 *   pywebview 是先挂上 window.pywebview、再补上 .api、最后派发 pywebviewready 事件。
 *   如果本脚本恰好在「pywebview 已挂上、但 .api 还没补上」的那一瞬间执行，
 *   就会走到监听分支；而事件可能在此之前已经派发过了 —— 于是监听器永远等不到，
 *   整个界面停在初始静态 HTML 上（看起来像「打开了但没反应」）。
 *
 * 所以这里三重保险：立即检查 → 监听事件 → 定时轮询兜底。
 */
function ready(fn) {
  let fired = false;
  const go = () => {
    if (fired) return;
    fired = true;
    try { fn(); } catch (e) { console.error("初始化失败", e); }
  };

  if (window.pywebview && window.pywebview.api) return go();

  window.addEventListener("pywebviewready", go);

  // 兜底轮询：万一事件已经错过，也能自己起来
  let tries = 0;
  const timer = setInterval(() => {
    if (window.pywebview && window.pywebview.api) {
      clearInterval(timer);
      go();
    } else if (++tries > 150) {      // 约 30 秒后放弃
      clearInterval(timer);
      console.error("pywebview API 未就绪");
    }
  }, 200);
}

async function call(name, ...args) {
  try {
    return await window.pywebview.api[name](...args);
  } catch (e) {
    toast("操作失败：" + e, true);
    return null;
  }
}

/* ---------------- Toast ---------------- */
let toastTimer = null;
function toast(msg, isErr) {
  const el = $("#toast");
  // 兜底：页面正在关闭/重载时元素可能已经没了，toast 只是提示，不值得抛异常
  if (!el) { console.warn("toast 元素缺失：", msg); return; }
  el.textContent = msg;
  el.classList.toggle("err", !!isErr);
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("show"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => { el.hidden = true; }, 220);
  }, isErr ? 4200 : 2600);
}

/* ---------------- 任务轮询 ---------------- */
function startPoll() {
  if (pollTimer) return;
  busy = true;
  document.body.classList.add("busy");
  pollTimer = setInterval(tick, 170);
  tick();
}

function stopPoll() {
  busy = false;
  document.body.classList.remove("busy");
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function tick() {
  const t = await call("poll");
  if (!t) return;
  if (t.total > 0) setProgress(t.done, t.total, t.msg);
  else setStatus(t.msg || "处理中…");

  // v2.7 流式扫描：扫描途中就把已经读出来的行画上去，
  // 而不是盯着空列表等整批读完（几百张票时体感差别很大）
  if (t.busy && t.kind === "scan" && t.partial) applyPartial(t.partial);

  if (!t.busy) {
    stopPoll();
    if (t.error) {
      toast("出错：" + t.error, true);
      setStatus("失败");
    } else if (t.payload !== null && t.payload !== undefined) {
      await handlePayload(t.kind, t.payload);
    }
  }
}

/**
 * 流式扫描的中间态（v2.7）。
 *
 * 后端每读完 8 张就把「已经读出来的行」捎过来一次，这里直接替换列表，
 * 让用户看到票一张张冒出来。此时**还没有**算重复票、配对、风控校验，
 * 所以统计只能粗略算（张数 + 金额），最终快照到了会整体覆盖。
 */
function applyPartial(p) {
  if (!S) return;
  const rows = (p && p.rows) || [];
  if (!rows.length) return;
  let ok = 0, amt = 0;
  rows.forEach(r => {
    if (r.ok) {
      ok++;
      if (r.amount != null) amt += r.amount;
    }
  });
  S = Object.assign({}, S, {
    rows,
    stats: Object.assign({}, S.stats, {
      count: rows.length,
      okCount: ok,
      badCount: rows.length - ok,
      total: amt.toLocaleString("zh-CN",
        { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    }),
  });
  if (sel >= rows.length) sel = -1;
  renderHeader();
  renderStats();
  renderRows();
}

/**
 * 拖放之后启动轮询。
 *
 * 真正开始扫描的是 **Python 侧的 drop 回调**（浏览器不给 JS 文件绝对路径，
 * 见 app.py 的 _wire_file_drop）；前端只是被动等结果。
 * 两个回调谁先执行没有保证 —— 直接调 startPoll() 的话，
 * tick() 第一眼可能看到「空闲」就立刻停表了。所以先试探一会儿。
 */
function startDropWatch() {
  let tries = 0;
  const probe = setInterval(async () => {
    const t = await call("poll");
    if (t && t.busy) { clearInterval(probe); startPoll(); return; }
    if (++tries > 30) {                    // 约 6 秒还没动静
      clearInterval(probe);
      setStatus("就绪");
      toast("没识别到可导入的发票文件", true);
    }
  }, 200);
}

/**
 * 拖放导入的结果说明。
 * 「解压到哪」「什么被跳过了」必须说出来 ——
 * 拖进来一堆东西、其中一部分没被处理却毫无提示，用户只会以为程序坏了。
 * 用 seq 去重：不然每次重新扫描都会把同一条提示再弹一遍。
 */
let lastImportSeq = 0;
function showImportInfo(info) {
  if (!info || !info.seq || info.seq === lastImportSeq) return;
  lastImportSeq = info.seq;
  const parts = [];
  if (info.msg) parts.push(info.msg);
  const ex = info.extracted || [];
  // 「新解开」和「复用已有的」要分开说：重复拖同一个文件夹时，
  // 如果说成「已解压 3 个」就是假消息（其实一个都没新解）
  if (ex.length) parts.push(`已解开 ${ex.length} 个压缩包（含嵌套）`);
  if (info.reused) parts.push(`另有 ${info.reused} 个先前已解开、直接复用`);
  if (info.skipped && info.skipped.length) {
    parts.push(`跳过 ${info.skipped.length} 个：${info.skipped[0]}`);
  }
  if (parts.length) toast(parts.join("；"), !!(info.msg || (info.skipped || []).length));
}

/* ---------------- 拖拽导入 ----------------
 * 这里只干两件事（拿路径是 Python 的活）：
 *   1) preventDefault —— 不做的话 WebView2 会直接导航到被拖进来的文件，
 *      整个界面被替换成那个 PDF；而 dragover 也必须 preventDefault，
 *      否则浏览器根本不认为这个区域可以放下。
 *   2) 画高亮反馈。
 * 文件路径、解压缩、扫描都由 Python 侧的 drop 回调负责。 */
let dragDepth = 0;

/**
 * 拖放门禁 —— ⚠️ 这里踩过一个很隐蔽的时序坑，别再改回去：
 *
 *   `dropReady` 由 Python 侧在「页面 loaded 之后」才回填
 *   （app.py 的 _wire_file_drop 挂在 window.events.loaded 上），
 *   而前端缓存的快照 S 是**页面初始化时**取的 —— 那一刻它还全是 false。
 *   于是 `if (!S.dropReady) return` 会把整条拖放链路静默拦死：
 *   高亮不出现、drop 什么都不做，表现就是「拖进去毫无反应」。
 *
 * 所以判断口径改成「**除非明确知道它坏了，否则就试**」：
 * 只有后端报了 _dropErr 才拦；否则一律往下走，
 * 真拿不到路径时由 take_drop_paths 返回明确错误，前端 toast 出来。
 * （宁可试一次再报错，也不要因为一个可能过期的标志位什么都不做。）
 */
function dropBlocked() { return !!(S && S.dropErr); }

function setDropHint(on) {
  const el = $("#dropHint");
  if (el) el.hidden = !on;
}
window.addEventListener("dragenter", (e) => {
  const types = Array.from((e.dataTransfer && e.dataTransfer.types) || []);
  if (!types.includes("Files")) return;
  e.preventDefault();
  if (dropBlocked()) return;
  dragDepth++;
  setDropHint(true);
});
// dragover 不 preventDefault 的话浏览器不允许 drop
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("dragleave", (e) => {
  e.preventDefault();
  // dragenter/dragleave 在子元素之间来回触发，用计数避免高亮闪烁
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) setDropHint(false);
});
/**
 * drop 之后叫后端把路径收一下（补救通道）。
 *
 * 主通道是 Python 侧那个 pywebview drop 回调 —— 它拿得到 WebView2 给的真实路径，
 * 通常在我们之前就已经把文件收下了。这里只是兜底：
 * pywebview 回配路径失败时（中文名对不上、序列化丢了 name 会直接抛异常）
 * 它可能什么都没做，那一次就靠这条路捞回来。
 *
 * 所以「后端说没东西可捞」不等于失败 —— 很可能是主通道已经处理完了。
 * 判据以**列表里有没有出现文件**为准，避免出现
 * 「明明导进去了却弹报错」这种最让人困惑的情况。
 */
async function requestDropImport(tryNo) {
  const r = await call("take_drop_paths");
  if (r && r.ok && r.mode !== "dedup") { startDropWatch(); return; }

  const st = await call("get_state");
  if (st) { S = st; render(); }
  if (st && st.rows && st.rows.length) {     // 主通道已经导好了
    setStatus(`识别完成：共 ${st.stats.count} 张`);
    showImportInfo(st.importInfo);
    return;
  }
  if (tryNo < 4) { setTimeout(() => requestDropImport(tryNo + 1), 260); return; }

  setStatus("就绪");
  toast("没能读到拖入的文件，请改用右上角「选择文件夹」或「添加文件」", true);
}

window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  setDropHint(false);
  if (dropBlocked()) {
    toast("拖放功能没能启用，请用右上角「选择文件夹」或「添加文件」", true);
    return;
  }
  if (busy) { toast("正在处理上一批，请稍候", true); return; }
  setStatus("正在导入拖入的文件…");
  requestDropImport(0);
});

async function handlePayload(kind, p) {
  if (kind === "scan") {
    S = p;
    filterMode = "all";
    filterSeen = false;
    dupOnly = false;
    if (sel >= S.rows.length) sel = 0;
    render();
    setStatus(`识别完成：共 ${S.stats.count} 张`);
    showImportInfo(S.importInfo);
  }
  else if (kind === "execute") {
    lastResult = p;
    setStatus(`完成：成功 ${p.done} 个`);
    showDone(p);
    await call("rescan");       // 立刻按磁盘真实情况刷新
    startPoll();
  }
  else if (kind === "undo") {
    setStatus(`已还原 ${p.restored} 个文件`);
    toast(`已还原 ${p.restored} 个文件`);
    await call("rescan");
    startPoll();
  }
  else if (kind === "split") {
    // 拆分结果：原整份已从列表移出，拆出来的几张已经在 snapshot 里了
    if (p.snapshot) {
      S = p.snapshot;
      if (sel >= S.rows.length) sel = -1;
      render();
    }
    setStatus(`已拆成 ${p.count} 个文件`);
    toast(`已拆成 ${p.count} 个文件，并重新读入列表`);
  }
  else if (kind === "pack") {
    setStatus(`已打包 ${p.count} 个文件`);
    doneFolderTarget = null;
    const rows = [];
    rows.push(`<div class="factGrid">
        <div class="fact"><div class="k">已打包</div><div class="v ok">${p.count} <small>个文件</small></div></div>
      </div>`);
    rows.push(`<div class="warnLine ok">
        <b>报销单文件夹</b>
        <div class="lnk">${esc(p.dir)}</div>
        <div class="lnkMeta">原文件都还在，这里是复制过去的副本${
          p.list ? `　·　已生成「${esc(p.list)}」` : ""}</div>
      </div>`);
    if (p.failed && p.failed.length) {
      rows.push(`<div class="warnLine"><b>以下文件复制失败</b>
        ${p.failed.map(f => `<div class="lnk">${esc(f.file)}<br>${esc(f.error)}</div>`).join("")}
      </div>`);
    }
    $("#doneTitle").textContent = "打包完成 ✅";
    $("#doneBody").innerHTML = rows.join("");
    $("#btnDoneFolder").hidden = false;
    openModal("#mDone");
  }
  else if (kind === "merge") {
    setStatus(`合并完成：${p.pages} 页`);
    showMergeDone(p);
  }
}

/* ---------------- 进度 / 状态 ---------------- */
function setProgress(done, total, msg) {
  const pct = total ? Math.round(done / total * 100) : 0;
  $("#prog").firstElementChild.style.width = pct + "%";
  $("#progTxt").textContent = `${msg || ""} ${done}/${total}`.trim();
}
function setStatus(text) {
  $("#progTxt").textContent = text || "";
}

/* ---------------- 渲染 ---------------- */
function render() {
  if (!S) return;
  syncViewFromCfg();
  renderHeader();
  renderRules();
  renderStats();
  renderToolbarSel();
  renderRows();
  renderDetail();
  renderFooter();
  // 工具栏宽度会随「已选摘要」文字长短变化，每次渲染后量一次
  requestAnimationFrame(fitToolbar);
}

function renderHeader() {
  // 版本号只需写一次（它不随状态变化）
  const ver = $("#appVer");
  if (ver && !ver.textContent && S.version) ver.textContent = "v" + S.version;
  // 作者署名：只在第一次拿到状态时写一次。
  // v2.7.4：做成可点的「发邮件」入口 —— 直接写 <a href="mailto:"> 在 WebView2 里
  // 常被导航策略拦掉，所以点这里走 Python 的 os.startfile（见 bridge.contact_author）。
  const au = $("#appAuthor");
  if (au && !au.textContent && S.author) {
    au.innerHTML = `作者 <button type="button" class="mailLink" id="btnHome"
        title="打开项目主页：https://github.com/tugepaopao/Invoice-Renaming-Tool"
        >${esc(S.author)}<span class="mailIco">↗</span></button>`;
    const mb = $("#btnHome");
    if (mb) {
      mb.onclick = async () => {
        const r = await call("open_homepage");
        if (r && !r.ok) toast(r.msg || "打不开浏览器，请手动访问项目主页", true);
      };
  }
  const sep = $("#metaSep");
  if (sep) sep.hidden = !au.textContent.trim();
  }

  const p = $("#srcPath");
  if (S.folder) {
    p.textContent = S.folder;
    p.title = S.folder;
  } else if (S.manual) {
    p.textContent = "已手动添加文件";
  } else {
    p.textContent = "还没有选择文件夹";
  }
  $("#srcDot").hidden = !S.folder;
  // 原来这里写死「个 PDF」，加了 OFD/XML 之后就不准了。
  // 有几种格式就说几种，用户才知道自己拖进来的东西被认成了什么。
  const fmts = [...new Set(S.rows.map(r => r.fmt).filter(Boolean))].sort();
  $("#srcMeta").textContent = S.rows.length
    ? `${S.rows.length} 个文件${fmts.length ? "（" + fmts.join(" / ") + "）" : ""}`
    : "";
  $("#btnRescan").disabled = !(S.folder || S.manual) || busy;
}

function renderRules() {
  const c = S.cfg;

  // 面板未打开时才从快照回填草稿；打开期间以草稿为准，
  // 否则每次后端返回都会把用户正在拖的顺序冲掉。
  if (!ruleOpen) {
    ruleFields = (c.fields || ["buyer", "amount"]).slice();
    ruleSep = c.separator === undefined ? "—" : c.separator;
  }
  renderRuleLabel();

  $$("#segAmount span").forEach(s => s.classList.toggle("on", s.dataset.v === c.amountMode));
  $$("#segMode span").forEach(s => s.classList.toggle("on", s.dataset.v === c.mode));
  $("#swDistribute").checked = !!c.distribute;
  $("#swBackup").checked = !!c.backup;
  $("#swSub").checked = !!c.recursive;

  const seg = $$("#segMode span");
  if (seg.length === 2) {
    seg[0].textContent = c.distribute ? "移动归档" : "原地重命名";
    seg[0].title = c.distribute
      ? "把发票移动到对应的客户文件夹里"
      : "直接在原文件夹改名（可撤销）";
    seg[1].title = c.distribute
      ? "复制一份到客户文件夹，原文件保留"
      : "原文件保留，改名后的副本放到「重命名输出」文件夹";
  }
  $("#swDistribute").title = "按购买方公司名自动建立子文件夹归档";

  // v2.8.1：折叠功能已去掉，规则卡永远展开
  const swCross = $("#swCross");
  if (swCross) swCross.checked = c.crossCheck !== false;
}

/**
 * 工具栏右侧的「已选 N 张 · ¥X」+ 「删除选中」按钮的状态（v2.7.3）。
 *
 * 为什么加这条：窄窗口下工具栏右侧本来是一块空白（业主截图红框），
 * 用"这笔钱"把它占住 —— 用户既能看到选了多少，也能一眼确认金额对不对。
 */
function renderToolbarSel() {
  const rows = (S && S.rows) || [];
  let n = 0, sum = 0;
  for (const r of rows) {
    if (!r.checked) continue;
    n++;
    if (r.amount != null) sum += r.amount;
  }
  const el = $("#toolbarSel");
  if (el) {
    el.classList.toggle("has", n > 0);
    el.innerHTML = n
      ? `已选 <b>${n}</b> 张 <span class="amt">· <b>${fmtMoney(sum)}</b></span>`
      : "已选 0 张";
  }
  const del = $("#btnDelSel");
  if (del) {
    del.disabled = busy || n === 0;
    del.textContent = n ? `删除选中 ${n}` : "删除选中";
  }
}

/**
 * 工具栏自适应（v2.7.3）。
 *
 * ⚠️ 为什么不用 CSS 媒体查询：工具栏在左侧卡片里，卡片宽 = 窗口 − 右侧详情栏 372px。
 *    窗口 1326 时卡片只有 899px，媒体查询看的是整个窗口，会误判成"够宽"，
 *    结果照样溢出、照样出现横向滚动条（实测 sw 1077 / cw 899）。
 *    所以直接量工具栏自身的 scrollWidth。
 *
 * 带回滞：收起后要留 16px 余量才允许展开，避免在临界宽度来回跳。
 */
function fitToolbar() {
  const tb = document.querySelector(".master .toolbar");
  if (!tb) return;
  // ⚠️ 要先摘掉 searchOpen 再量：搜索态会临时藏起「排序 / 已选摘要」，
  //    量出来的宽度会"假性变小"，把 searchOpen 自己摘掉 —— 结果就是
  //    用户打第一个字，输入框就没了（实测复现）。
  const searching = tb.classList.contains("searchOpen");
  tb.classList.remove("compact", "tight", "ultra", "searchOpen");
  if (tb.scrollWidth > tb.clientWidth + 1) {
    tb.classList.add("compact");                       // ① 5 个筛子收进下拉
    if (tb.scrollWidth > tb.clientWidth + 1) {
      tb.classList.add("tight");                       // ② 已选金额收起来、搜索框再窄
      if (tb.scrollWidth > tb.clientWidth + 1) {
        tb.classList.add("ultra");                     // ③ 摘要整条让位（左下角还有）
      }
    }
  }
  if (searching) tb.classList.add("searchOpen");       // 量完再还回去
}

let _fitTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(_fitTimer);
  _fitTimer = setTimeout(fitToolbar, 120);
});

// 再挂一个 ResizeObserver：窗口最大化/还原、右侧详情栏变化都能跟上。
// 只认"宽度变了"，避免 fitToolbar 改高度时自己触发自己。
(function watchToolbar() {
  const master = document.querySelector(".master");
  if (!master || !window.ResizeObserver) return;
  let lastW = 0;
  new ResizeObserver((entries) => {
    const w = Math.round(entries[0].contentRect.width);
    if (w === lastW) return;
    lastW = w;
    fitToolbar();
  }).observe(master);
})();

function renderStats() {
  const s = S.stats;
  $("#stCount").innerHTML   = `${s.count}<small>张</small>`;
  $("#stOk").innerHTML      = `${s.okCount}<small>张</small>`;
  $("#stChecked").innerHTML = `${s.checkedCount}<small>张</small>`;
  $("#stBad").innerHTML     = `${s.badCount}<small>张</small>`;
  // ⚠️ 这里要报**张数**（dupRows），不是票号个数（dupCount）。
  //    dupCount 是「有几个票号重复」，单位是"组"；以前直接拿来当"张"显示，
  //    3 组 7 张会写成「3 张」，和旁边「扫描到 19 张」的口径对不上。
  $("#stDup").innerHTML     = `${s.dupRows || 0}<small>张</small>`;
  const dupEl = $("#stDup");
  if (dupEl) {
    dupEl.title = s.dupCount
      ? `${s.dupCount} 个票号重复，共 ${s.dupRows} 张`
      : "没有重复票号";
  }
  $("#stTotal").textContent = "¥ " + s.total;

  // v2.7：数字不是 0 的才上色 —— 一串灰 chip 里，橙的那个一眼就能看见
  const hot = (id, on) => {
    const el = $("#" + id);
    const box = el && el.closest(".stat");
    if (box) { box.classList.toggle("hot", on); }
  };
  hot("stBad", s.badCount > 0);
  const badBox = $("#stBad") && $("#stBad").closest(".stat");
  if (badBox) badBox.classList.toggle("bad", s.badCount > 0);
  hot("stDup", (s.dupRows || 0) > 0);
  hot("stChecked", false);
}

/**
 * 当前可见的行 = 全量列表 ∩ 状态筛选 ∩ 搜索关键词，再按 sortBy 排序。
 *
 * 两个筛选是**叠加**的（先筛状态、再搜关键词），这样「在重复票里找某家公司」
 * 这种组合需求也支持。
 */
function needTodo(r) {
  // 「需处理」= 程序没法完全确定、需要人看一眼的：
  //   号码异常 / 待核对 / 未识别 / 加密没密码 / 待配对行程单
  if (!r.ok) return true;
  if (r.checkLevel) return true;
  if (r.needPassword) return true;
  if (r.pairReason) return true;
  return false;
}

function visibleRows() {
  let rows = S.rows;
  if (filterMode === "dup") rows = rows.filter(r => r.dup);
  else if (filterMode === "todo") rows = rows.filter(needTodo);
  else if (filterMode === "multi") rows = rows.filter(r => !!r.multi);
  else if (filterMode === "bad") rows = rows.filter(r => !r.ok);
  if (filterSeen) rows = rows.filter(r => r.seenBefore && !seenMuted.has(r.no));
  const q = searchText.trim().toLowerCase();
  if (q) rows = rows.filter(r => rowHaystack(r).includes(q));
  return sortRows(rows);
}

/**
 * 排序（v2.7）。默认「按文件名」= 保持后端给的顺序（扫描顺序），
 * 这样打开列表看到的还是熟悉的样子；其余三种按需排。
 * 行程单永远跟着它的发票 —— 后端已经是这个顺序，排序时把行程单排除后
 * 再按原索引插回去太麻烦，所以行程单排在最后（它们本来也以附件形式挂在发票下）。
 */
function sortRows(rows) {
  if (sortBy === "file") return rows;
  const arr = rows.slice();
  if (sortBy === "amount") {
    arr.sort((a, b) => (b.amount || 0) - (a.amount || 0));
  } else if (sortBy === "date") {
    arr.sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));
  } else if (sortBy === "buyer") {
    arr.sort((a, b) => String(a.buyer || "").localeCompare(
      String(b.buyer || ""), "zh-Hans-CN"));
  }
  return arr;
}

/**
 * 一行的「可搜内容」。覆盖购买方、销售方、原文件名、新文件名、发票号码、金额 ——
 * 用户找不到某张票时，记得住的可能是其中任何一个。
 * 拼一次字符串比挨个字段 includes 快，也更好维护。
 */
function rowHaystack(r) {
  return [r.file, r.buyer, r.seller, r.newName, r.no,
          r.amountText, r.amountShort, r.type, r.remark, r.drawer]
    .filter(Boolean).join(" ").toLowerCase();
}

function renderRows() {
  const box = $("#rows");
  const rows = visibleRows();

  if (!S.rows.length) {
    box.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty";
    if (S.folder || S.manual) {
      empty.innerHTML = `
        <div class="art">票</div>
        <h2>这个位置没有找到 PDF</h2>
        <p>可能发票已经被移动或改名了。重新选一个文件夹试试，<br>
           或者勾选「包含子文件夹」后重新识别。</p>
        <button class="btn primary" id="btnPick3">重新选择文件夹</button>`;
    } else {
      empty.innerHTML = `
        <div class="art">票</div>
        <h2>先把发票文件夹选进来</h2>
        <p>点右上角「选择文件夹」，程序会自动读出每张发票的<b>购买方</b>和<b>开票金额</b>，<br>
           并生成「购买方—金额」格式的新文件名供你确认。</p>
        <p class="fine">支持 <b>PDF / OFD / XML</b> 三种交付格式。</p>
        ${dropBlocked() ? "" : `<p class="fine">
          也可以直接把发票文件、文件夹或压缩包<b>拖进这个窗口</b>。<br>
          拖一个文件夹进来，会自动把它里面（<b>含子文件夹和压缩包</b>）的发票都找出来。</p>`}
        <button class="btn primary" id="btnPick3">选择文件夹</button>
        <p class="fine">不确定效果？先用 tests 文件夹里的样票试试。</p>`;
    }
    box.appendChild(empty);
    const b = $("#btnPick3");
    if (b) b.onclick = pickFolder;
    renderFilters(0);
    return;
  }

  if (!rows.length) {
    // 分清两种「空」：筛出来是空的，和本来就没有重复票
    if (searchText.trim()) {
      box.innerHTML = `<div class="noMatch">
          没有找到匹配「<b>${esc(searchText.trim())}</b>」的发票
          <div style="margin-top:7px;font-size:12px;color:var(--ink-3)">
            可以搜购买方、销售方、文件名或发票号码</div>
        </div>`;
    } else if (filterMode !== "all") {
      // 筛选后为空：说清是「筛掉了」而不是「票没了」，并给一键回到全部
      const label = { todo: "需处理", dup: "重复", multi: "多张票", bad: "识别失败" }[filterMode] || "";
      box.innerHTML = `<div class="noMatch">
          这个筛选下没有发票（「${esc(label)}」）
          <div style="margin-top:7px">
            <button class="btn tiny ghost" id="btnFilterReset">看全部</button></div>
        </div>`;
      const rb = $("#btnFilterReset");
      if (rb) rb.onclick = () => setFilter("all");
    } else {
      box.innerHTML = `<div class="empty"><p>没有重复票号的发票 ✅</p></div>`;
    }
    renderFilters(0);
    return;
  }

  // 已被配对的行程单不再单独占一行 —— 它以「附件」形式出现在对应发票下面。
  // （没配对的那份仍然自己成行，状态是「待配对」，方便用户处理）
  const linked = new Set();
  rows.forEach(r => (r.attached || []).forEach(a => linked.add(a.i)));

  box.innerHTML = rows
    .filter(r => !(r.kind === "trip" && linked.has(r.i)))
    .map(r => {
      const main = rowHtml(r);
      const subs = (r.kind !== "trip" && (r.attached || []).length)
        ? r.attached.map(attachHtml).join("") : "";
      return main + subs;
    }).join("");
  // 筛选/搜索生效时在统计条里说明「显示了几张 / 一共几张」，
  // 否则用户会以为发票丢了。
  // ⚠️ v2.7.3：这段原来写在工具栏最右边 —— 窗口一窄就被压成竖排三行，
  //    把工具栏撑到 60px，还挤出横向滚动条。现在搬到统计条（#statFilter）。
  renderFilters(rows.length);
}

const FILTER_LABEL = { all: "全部", todo: "需处理", dup: "重复", multi: "多张票", bad: "失败" };

/**
 * 状态筛选按钮上的数量（v2.7）—— 不显示数量的话，"需处理"到底是 3 张还是 0 张看不出来。
 * v2.7.3：宽窗口的 5 个胶囊和窄窗口下拉里的 5 项是**两套 DOM**，这里一起同步。
 */
function renderFilters(visibleCount) {
  const all = (S && S.rows) || [];
  const counts = {
    all: all.length,
    todo: all.filter(needTodo).length,
    dup: all.filter(r => r.dup).length,
    multi: all.filter(r => !!r.multi).length,
    bad: all.filter(r => !r.ok).length,
  };
  $$("#filters [data-f], #filterPop [data-f]").forEach(el => {
    const f = el.dataset.f;
    el.classList.toggle("on", f === filterMode);
    let box = el.querySelector("i");
    if (!box) { box = document.createElement("i"); el.appendChild(box); }
    box.textContent = counts[f] === undefined ? 0 : counts[f];
  });

  const menu = $("#btnFilterMenu");
  if (menu) {
    menu.innerHTML = `筛选：${FILTER_LABEL[filterMode] || "全部"} <span class="cv">▾</span>`;
    menu.classList.toggle("on", filterMode !== "all");
  }

  // 统计条上的「筛选中 6 / 10」：搜索也算一种"筛选中"
  const filtering = filterMode !== "all" || filterSeen || !!searchText.trim();
  const chip = $("#statFilter");
  if (chip) {
    chip.hidden = !filtering;
    const n = (visibleCount === undefined || visibleCount === null)
      ? visibleRows().length : visibleCount;
    const t = $("#stFilter");
    if (t) t.textContent = `${n} / ${all.length}`;
  }
}

async function setFilter(f) {
  filterMode = f === filterMode ? "all" : f;
  dupOnly = filterMode === "dup";       // 兼容旧变量（别处还引用）
  const pop = $("#filterPop");
  if (pop) pop.hidden = true;           // 选完就收起窄窗口的下拉
  renderRows();
}

/** 日期只显示月-日（附属行里空间紧张，「2026-07-21」太长） */
function shortDate(s) {
  const v = (s || "").replace(/-/g, "");
  return v.length >= 8 ? `${v.slice(4, 6)}-${v.slice(6, 8)}` : (s || "");
}

/**
 * 重复组的底色色板（2026-10-17 v2.5.8）。
 *
 * ⚠️ 为什么要换掉「按色相现算」那套：
 *    原来用黄金角递推色相、亮度和饱和度固定，3~6 组时很好，但**组数一多就挤**——
 *    实测色差（CIEDE2000 的前身 CIE76 ΔE）：
 *        10 组 → ΔE 5.0（勉强看出）  20 组 → ΔE 2.5（几乎一样）  30 组 → ΔE 1.3
 *    业主问的正是这个："如果只有三种颜色是不够的，颜色要更多"。
 *
 * 现在这 24 个颜色不是随手挑的：在「色相 × 亮度 × 饱和度」的候选空间里
 * 做**最远点采样**（每一步都挑离已选颜色最远的那个），所以
 * **任意两个颜色的色差都尽量大**，而且顺序有意义 —— 第 1、2 组就是差距最大的两个。
 * 实测：前 6 组 ΔE≥13、前 10 组 ≥8.3、前 24 组 ≥5.0（都能明显看出）。
 *
 * 亮度都压在 88~92%、饱和度 40~56%，保证是"淡底色"，
 * 压在深色文字下面不会影响读数，也不会像 v2.5.4 那样把整屏搞花。
 *
 * ⚠️ 超过 24 组会从头循环。真到那个量级（一屏 24 组重复票），
 *    建议直接看状态里的「票号重复 ×N」和右栏清单，别只靠颜色认。
 */
const DUP_TONES = [
  "#F4EED7", "#F2CFF2", "#CFE4F2", "#F2CFCF", "#CFF2CF", "#D7F4EA",
  "#CFD2F2", "#F3E2F3", "#F3E6E2", "#F2CFDF", "#E6F2CF", "#E2F1F3",
  "#E1F0DB", "#DBDDF0", "#F2DFCF", "#E6D7F4", "#CFF2DD", "#CFEFF2",
  "#F0DBE1", "#E2E8F3", "#F3EEE2", "#EFD8D2", "#F4D7F0", "#DDF4D7",
];

/**
 * 第 gid 组的底色。
 * ⚠️ 列表行和右栏清单**必须调同一个函数** —— 之前一处 Math.round、一处 |0，
 *    同一组算出的颜色差 1°，肉眼"差不多"但断言不相等，e2e 直接挂了。
 */
function dupTone(gid) {
  const i = (Math.max(1, gid) - 1) % DUP_TONES.length;
  return DUP_TONES[i];
}

/** 渲染一个主行（发票，或没配上发票的行程单） */
function rowHtml(r) {
  const isTrip = r.kind === "trip";
  const cls = ["row",
    r.checked ? "checked" : "",
    r.dup ? "dupRow" : "",
    r.i === sel ? "sel" : "",
    isTrip ? "tripRow" : "",
    r.checkLevel ? "chkRow" : "",
  ].filter(Boolean).join(" ");
  // 行程单用蓝色「行程单」标替代格式标 —— 一眼分清这是附件不是发票
  const mark = isTrip
    ? `<span class="kindTag trip">行程单</span>`
    : `<span class="fmt f-${(r.fmt || "").toLowerCase()}">${esc(r.fmt || "")}</span>`;
  // 状态标签：显示不下的部分靠悬停看全文。
  // 「待核对」只写了四个字，用户根本不知道要核对什么 —— 把风控的具体结论
  // （如「校验位对不上，税号可能有误」）挂到 title 上；行程单则显示配对失败的原因。
  const why = [];
  (r.checks || []).forEach(c => { if (c && c.msg) why.push(c.msg); });
  if (r.pairReason) why.push("配不上对的原因：" + r.pairReason);
  const tip = why.length ? why.join("\n") : r.status;

  // 重复票（2026-10-17 第四版，最终版）：**一组一色，且任意两组不撞色**。
  // 业主的原话：「每一组重复的几张票用一个颜色，其他的重复的用另一个颜色，
  // 不同的发票不要用相同的颜色」——上一版按两色交替，第 1 组和第 3 组撞了色，
  // 他一眼就看出来了（截图里两块都是暖色），所以这次必须**每组建一个独立颜色**。
  //
  // 怎么生成：色相按**黄金角 137.508°** 递推 ——
  //   这个角度能让任意相邻的 n 个数在色环上都拉得最开，
  //   比"每隔 30° 取一个"更不容易出现两块看起来像的颜色；
  //   亮度/饱和度固定（68% / 91%），保证每块都是同一个"淡"的程度，
  //   不会有的深有的浅、看着像不同性质的东西。
  //   起始角度 35° 是为了让第 1 组落在暖米色，跟原来的观感接得上。
  // 就绪的行不加任何底色（只有 .dupRow 才有 background），保持默认白色。
  const dg = r.dupGroup;
  const gAttr = dg ? ` style="--dup: ${dupTone(dg.gid)}"` : "";
  const dupTip = dg
    ? `票号 ${r.no} 共 ${dg.total} 份\n`
      + (r.dupOthers || []).map(o => "· " + o.file).join("\n")
    : "";
  // ---- v2.7.3：这两个提示不再单独占一行，而是并进状态标签里 ----
  // 业主 2026-10-18 截图反馈：状态列里「待核对」下面再挂一行「9月已报过」，
  // 行高被撑高、两行小标也不好看。现在合成一枚标签：待核对（9月已报过）。
  let marks = [];
  if (r.seenBefore && !seenMuted.has(r.no)) {
    const mo = String(r.seenBefore.at || "").slice(5, 7);
    marks.push(mo ? `${Number(mo)}月已报过` : "以前报过");
  }
  if (r.redMatch) marks.push("冲蓝票");
  else if (r.redBy) marks.push("被红冲");
  const markHtml = marks.length
    ? `<i class="tagSub">（${esc(marks.join("·"))}）</i>` : "";
  const markTip = [];
  if (r.seenBefore && !seenMuted.has(r.no)) {
    markTip.push(`这张票 ${r.seenBefore.at || ""} 已经处理过一次`);
    markTip.push(`上次在：${r.seenBefore.folder || ""}`);
  }
  if (r.redMatch) {
    markTip.push(`推测冲的是：${r.redMatch.file}（${r.redMatch.date || ""} ${r.redMatch.amount || ""}）`);
  }
  if (r.redBy) markTip.push(`本批红字票「${r.redBy.file}」冲的应该是这一张`);
  // 跨批次标记仍然点一下看详情；票号重复的行优先保留「点开取舍台」的行为
  const seenAttr = (r.seenBefore && !seenMuted.has(r.no) && !dg)
    ? ` data-seen="${r.i}"` : "";
  return `
    <div class="${cls}" data-i="${r.i}"${gAttr}>
      <div><div class="ck ${r.checked ? "on" : ""}" data-toggle="${r.i}"></div></div>
      <div class="fname" title="${esc(r.file)}">${mark}<span class="fnameT">${esc(midEll(r.file))}</span></div>
      <div class="buyer ${r.buyer ? "" : "dim"}" title="${esc(r.buyer || "未识别")}">${esc(r.buyer || "未识别")}</div>
      <div class="amt ${r.amount !== null && r.amount < 0 ? "neg" : ""}">${r.amountText || "—"}</div>
      <div class="newn ${r.ok || r.edited ? "" : "dim"}" title="${esc(r.newName)}" data-edit="${r.i}">
        ${esc(stripExt(r.newName))}<span class="ext">${esc(extOf(r.newName))}</span>
      </div>
      <div class="stCell">
        <span class="tag ${r.level}" ${dg ? `data-dup="${dg.gid}"` : ""}${seenAttr}
              title="${esc(dg ? dupTip + "\n点一下打开取舍台，选留哪一份"
                              : (markTip.length ? markTip.join("\n") + "\n" : "") + tip)}">${esc(r.status)}${markHtml}</span>
        ${r.multi ? `<button type="button" class="rowAct" data-split="${r.i}"
                title="这个 PDF 里有多张发票，点开可以按页拆成独立文件">拆开</button>` : ""}
        ${r.needPassword ? `<button type="button" class="rowAct" data-pwd="${r.i}"
                title="这个 PDF 加密了，输入密码才能读出内容">密码</button>` : ""}
      </div>
      <div class="act">
        <button type="button" class="rowDel" data-del="${r.i}"
                title="从列表里移除这一行（不会删除文件）" aria-label="移除这一行">
          <svg viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M2.8 4.6h10.4M6.4 4.6V3.2h3.2v1.4" stroke="currentColor"
                  stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M4.2 4.6l.55 8.1h6.5l.55-8.1" stroke="currentColor"
                  stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M6.6 7v3.2M9.4 7v3.2" stroke="currentColor"
                  stroke-width="1.3" stroke-linecap="round"/>
          </svg>
        </button>
      </div>
    </div>`;
}

/**
 * 渲染「附件」行 —— 行程单挂在它对应的发票下面。
 * 发票是主凭证、行程单是附件，界面必须体现这个从属关系（所以左缩进）。
 */
function attachHtml(a) {
  const bits = [];
  if (a.trips) bits.push(`${a.trips} 趟`);
  if (a.from && a.to) bits.push(`${shortDate(a.from)} 至 ${shortDate(a.to)}`);
  if (a.totalText) bits.push(`合计 ${a.totalText}`);
  const sure = (a.score || 0) >= 90;      // 分数高才是「已配对」，否则是「疑似」
  return `
    <div class="attachRow ${sure ? "" : "wait"}">
      <span class="aIcon"><svg width="13" height="13" viewBox="0 0 16 16" fill="none">
        <path d="M9.6 3.4 5.1 7.9a2.4 2.4 0 0 0 3.4 3.4l4.5-4.5a3.6 3.6 0 0 0-5.1-5.1L3.4 6.2"
              stroke="currentColor" stroke-width="1.4" stroke-linecap="round"
              stroke-linejoin="round"/></svg></span>
      <span class="aName">行程单 · ${esc(a.platform || "未知平台")}</span>
      <span class="aMeta">${esc(bits.join(" · "))}</span>
      <span class="aTag">${sure ? "已配对" : "疑似配对"}${a.reason ? " · " + esc(a.reason) : ""}</span>
    </div>`;
}

/* ---------------- 列表搜索 ----------------
 * 纯前端过滤（不来回问后端）：列表快照本来就在手里，输入即筛最快。
 * ⚠️ 过滤只影响「显示什么」，data-i 仍是原始行号，
 *    所以勾选、改名、合并这些操作全都作用在正确的行上。
 */
const searchInp = $("#rowSearch");
const searchX = $("#rowSearchX");

function applySearch() {
  searchText = (searchInp && searchInp.value) || "";
  const ico = $("#btnSearchOpen");
  if (ico) ico.classList.toggle("on", !!searchText.trim());
  const on = !!searchText.trim();
  if (searchX) searchX.hidden = !on;
  if (searchInp && searchInp.parentElement) {
    searchInp.parentElement.classList.toggle("on", on);
  }
  // 当前选中的那张如果被筛掉了，就自动落到第一条可见的 ——
  // 否则右侧详情还停在一条「列表里根本看不见」的发票上，很怪
  const vis = visibleRows();
  if (vis.length && !vis.some(r => r.i === sel)) sel = vis[0].i;
  render();
}

if (searchInp) {
  searchInp.addEventListener("input", applySearch);
  searchInp.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && searchInp.value) {
      // 先吃掉 Esc：不然会被全局的「Esc 关弹窗」抢走
      e.stopPropagation();
      e.preventDefault();
      searchInp.value = "";
      applySearch();
    }
  });
}
if (searchX) {
  searchX.onclick = () => {
    searchInp.value = "";
    applySearch();
    searchInp.focus();
  };
}

/* ---------------- 搜索收成放大镜（v2.7.4） ----------------
 * 窄窗口（compact 档）时，搜索框整条收起，只留一个放大镜；
 * 点开时输入框**浮在工具栏上**（绝对定位），不参与布局 ——
 * 否则又要把工具栏挤到溢出，等于白改。
 */
function openSearch() {
  // v2.8.8：搜索框改成**常驻**（业主：不要收成放大镜），这里只管聚焦
  const i = $("#rowSearch");
  if (i) setTimeout(() => { i.focus(); i.select(); }, 20);
}
function closeSearch() {
  const tb = document.querySelector(".master .toolbar");
  if (tb) tb.classList.remove("searchOpen");
}
(function bindSearchToggle() {
  const b = $("#btnSearchOpen");
  if (!b) return;
  b.onclick = (e) => {
    e.stopPropagation();
    const tb = document.querySelector(".master .toolbar");
    if (tb && tb.classList.contains("searchOpen")) {
      closeSearch();
      const i = $("#rowSearch"); if (i) i.blur();
    } else {
      openSearch();
    }
  };
  // 点别处收起；搜索框自己不算"别处"
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".searchWrap") && !e.target.closest("#btnSearchOpen")) {
      closeSearch();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSearch();
  });
})();

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
// 支持三种交付格式，扩展名判断不能再写死 .pdf
const EXT_RE = /\.(pdf|ofd|xml)$/i;

/**
 * 中间省略（v2.7）。
 *
 * 为什么必须"中间"省略：电子票的原文件名几乎都以 `dzfp_2531200000…` 开头，
 * 真正能区分的只有末尾那一截。原来 CSS 从尾部截断（text-overflow: ellipsis），
 * 结果十九行看起来一模一样 —— 用户根本没法靠文件名认票。
 * 现在保留头 12 + 尾 14 个字符，中间用 … 收掉。
 */
function midEll(name, head = 12, tail = 14) {
  const s = String(name || "");
  if (s.length <= head + tail + 1) return s;
  return s.slice(0, head) + "…" + s.slice(-tail);
}

function stripExt(n) { return (n || "").replace(EXT_RE, ""); }
function extOf(n) {
  const m = (n || "").match(EXT_RE);
  return m ? m[0].toLowerCase() : "";
}

/** 金额显示（汇总用；单张的金额后端已经格式化好了） */
function fmtMoney(n) {
  return "¥" + (n || 0).toLocaleString("zh-CN",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * 右栏「本批汇总」—— 没选中任何行时显示。
 *
 * 以前这里是一句「点击左侧任意一行」，白白空掉一大块。
 * 换成按购买方的金额分布，正是代理记账每月要看的数字，不用导出就能扫一眼。
 */
function summaryHtml() {
  const inv = S.rows.filter(r => r.kind !== "trip" && r.ok);
  const trips = S.rows.filter(r => r.kind === "trip");
  const total = inv.reduce((a, r) => a + (r.amount || 0), 0);
  const bad = S.rows.filter(r => r.kind !== "trip" && r.checkLevel).length;
  const wait = trips.filter(r => r.pairReason).length;

  // 重复票号按**组**汇总（2026-10-17）：光有个「只看重复票号」筛子，
  // 筛出来还是一长串，分不清哪几张是同一个票号。这里按组列清单，
  // 点文件名能跳到左边那一行 —— 删之前先看清"这组到底有几份、都叫什么"。
  const dupGroups = {};
  S.rows.forEach(r => {
    if (!r.dupGroup) return;
    const g = dupGroups[r.dupGroup.gid] ||
      (dupGroups[r.dupGroup.gid] = { gid: r.dupGroup.gid, no: r.no, items: [] });
    g.items.push(r);
  });
  const gids = Object.keys(dupGroups).map(Number).sort((a, b) => a - b);
  const dupRowsTotal = gids.reduce((n, g) => n + dupGroups[g].items.length, 0);
  const dupHtml = gids.length ? `<div class="sumSec">
      <div class="t">重复票号（${gids.length} 组 / ${dupRowsTotal} 张）</div>
      <div class="dupList">
        ${gids.slice(0, 6).map(g => {
          const grp = dupGroups[g];
          // v2.5.5：不再给每组配颜色、也不写「组N」——一块白底、写清份数和票号就够，
          // 想删哪份点一下文件名的位置就跳过去。
          // v2.5.7：每块的底色跟左边对应的那几行**完全一致**（同一个 --dup），
          // 一眼就知道"这块清单说的是哪几行"。
          return `<div class="dupGrp" style="--dup: ${dupTone(g)}">
            <div class="dupHead">
              <span class="dupNo">共 ${grp.items.length} 份</span>
              <span class="dupCnt">票号 ${esc(grp.no)}</span>
              <button type="button" class="dupPick" data-dup="${g}"
                      title="并排比较这几份，选一份保留">取舍…</button>
            </div>
            ${grp.items.map(x =>
              `<button type="button" class="dupFile" data-goto="${x.i}"
                       title="${esc(x.file)}（点一下跳到这一行）">${esc(x.file)}</button>`
            ).join("")}
          </div>`;
        }).join("")}
        ${gids.length > 6
          ? `<div class="sumEmpty">还有 ${gids.length - 6} 组，点「只看重复票号」继续看。</div>`
          : ""}
      </div>
    </div>` : "";

  const by = {};
  inv.forEach(r => {
    const k = r.buyer || "（未识别）";
    if (!by[k]) by[k] = { n: 0, a: 0 };
    by[k].n++; by[k].a += (r.amount || 0);
  });
  const list = Object.entries(by).sort((x, y) => y[1].a - x[1].a);
  const shown = list.slice(0, 8).map(([name, v]) => `
    <div class="sumItem">
      <span class="n" title="${esc(name)}">${esc(name)}</span>
      <span class="a">${fmtMoney(v.a)}</span>
      <span class="c">${v.n} 张</span>
    </div>`).join("");

  return `
    <div class="sumBox">
      ${seenBannerHtml()}
      <div class="sumHead">本批共 <b>${inv.length}</b> 张发票${
        trips.length ? `、<b>${trips.length}</b> 份行程单` : ""}。
        在左侧点一行可看单张详情。</div>
      <div class="sumCards">
        <div class="sumCard">
          <div class="k">价税合计</div>
          <div class="v">${fmtMoney(total)}</div>
        </div>
        <div class="sumCard">
          <div class="k">待核对</div>
          <div class="v ${bad ? "warn" : "ok"}">${bad} <small>张</small></div>
        </div>
      </div>
      ${list.length ? `<div class="sumSec">
        <div class="t">按购买方${list.length > 8 ? `（前 8 / 共 ${list.length}）` : ""}</div>
        <div class="sumList">${shown}</div>
      </div>` : ""}
      ${dupHtml}
      ${wait ? `<div class="sumSec">
        <div class="t">待配对的行程单</div>
        <div class="sumEmpty">${wait} 份行程单在本批里找不到金额一致的发票 ——
          可能对应的发票还没开，可以去催一下。</div>
      </div>` : ""}
    </div>`;
}

/* ---------------- v2.7：跨批次 / 红蓝票 两张提示卡 ---------------- */

/** 从完整路径里取最后一级目录名（路径太长，卡片里放不下） */
function folderName(p) {
  const s = String(p || "");
  const bits = s.split(/[\\/]/).filter(Boolean);
  return bits.length ? bits[bits.length - 1] : s;
}

/** 本批有几张「以前处理过」——汇总区顶部的提示条 */
function seenBannerHtml() {
  if (!S) return "";
  const list = (S.rows || []).filter(r => r.seenBefore && !seenMuted.has(r.no));
  if (!list.length) return "";
  const mo = String(list[0].seenBefore.at || "").slice(5, 7);
  const when = mo ? `${Number(mo)} 月` : "以前";
  return `<div class="sumBanner">
      <span>⚠ 本批有 <b>${list.length}</b> 张票 ${esc(when)}处理过，可能重复报销</span>
      <button type="button" class="btn tiny" data-seenonly="1">只看这些</button>
    </div>`;
}

/** 单张详情里的「以前处理过」卡片 */
function seenCardHtml(r) {
  const s = r.seenBefore;
  if (!s || seenMuted.has(r.no)) return "";
  return `<div class="dCard warn">
      ⚠ <b>这张票以前处理过一次</b><br>
      ${esc(s.at || "")} 在「${esc(folderName(s.folder) || "别的文件夹")}」处理过，金额一致。
      <div class="dRow">上次的文件：${esc(s.file || "—")}</div>
      <div class="dActs">
        <button type="button" class="btn tiny" data-seenok="${r.i}">我知道了</button>
        <button type="button" class="btn tiny ghost" data-seenoff="1">以后都不提示</button>
      </div>
    </div>`;
}

/** 红字票 ↔ 蓝票的对照卡片 */
function redCardHtml(r) {
  if (r.redMatch) {
    const m = r.redMatch;
    return `<div class="dCard info">
        这张红字票很可能是冲 <b>${esc(m.file)}</b><br>
        同销售方 · 金额正好相反 · 开票日期相近
        <div class="dRow">对应蓝票：${esc(m.date || "—")} · ${esc(m.amount || "")}</div>
        <div class="dActs"><button type="button" class="btn tiny" data-goto="${m.i}">跳到那张蓝票</button></div>
      </div>`;
  }
  if (r.redBy) {
    return `<div class="dCard info">
        这张蓝票被本批的红字票 <b>${esc(r.redBy.file)}</b> 冲了（${esc(r.redBy.amount || "")}）。
        <div class="dActs"><button type="button" class="btn tiny" data-goto="${r.redBy.i}">看那张红字票</button></div>
      </div>`;
  }
  if (r.red) {
    return `<div class="dCard info">这是红字（负数）发票。本批里没找到金额正好相反、
      销售方相同的蓝票 —— 可能对应的蓝票不在这一批里。</div>`;
  }
  return "";
}

function renderDetail() {
  const r = S.rows[sel];
  $("#dTitle").textContent = r ? "当前选中" : "本批汇总";
  $("#dIdx").textContent = r ? `#${sel + 1} / ${S.rows.length}` : "";
  const body = $("#dBody");

  if (!r) {
    // 没选中行时，这里**不再空着** —— 显示本批汇总。
    // 按购买方的金额分布正是代理记账每月要看的数字，不用导出就能扫一眼。
    body.innerHTML = summaryHtml();
    $("#btnRename").disabled = true;
    $("#btnOpen").disabled = true;
    removeFillBtn();
    return;
  }

  body.innerHTML = `
    ${pvBoxHtml(r)}
    ${seenCardHtml(r)}
    ${redCardHtml(r)}
    <div class="kv">
      <div class="k">购买方（付款方）</div>
      <div class="v ${r.buyer ? "" : "dimText"}">${esc(r.buyer || "未识别")}</div>
    </div>
    ${r.buyerTax ? `<div class="kv">
      <div class="k">购买方税号</div>
      <div class="v mono">${esc(r.buyerTax)}</div>
    </div>` : ""}
    <div class="kv">
      <div class="k">价税合计</div>
      <div class="v big">${r.amount !== null ? "¥ " + r.amountText : "—"}</div>
    </div>
    ${(r.net !== null && r.net !== undefined) || (r.tax !== null && r.tax !== undefined)
      ? `<div class="kv">
          <div class="k">不含税 / 税额</div>
          <div class="v">${r.netText ? "¥ " + r.netText : "—"}
            <span class="sep">/</span> ${r.taxText ? "¥ " + r.taxText : "—"}</div>
        </div>` : ""}
    <div class="kv">
      <div class="k">发票号码</div>
      <div class="v mono">${esc(r.no || "—")}${r.dup ? " ⚠ 重复" : ""}</div>
    </div>
    <div class="kv">
      <div class="k">开票日期</div>
      <div class="v mono">${fmtDate(r.date)}</div>
    </div>
    ${r.type ? `<div class="kv">
      <div class="k">发票类型</div>
      <div class="v">${esc(r.type)}</div>
    </div>` : ""}
    ${r.fmt ? `<div class="kv">
      <div class="k">文件格式</div>
      <div class="v"><span class="fmt f-${r.fmt.toLowerCase()}">${esc(r.fmt)}</span></div>
    </div>` : ""}
    ${r.seller ? `<div class="kv">
      <div class="k">销售方（开票方）</div>
      <div class="v">${esc(r.seller)}</div>
    </div>` : ""}
    ${r.sellerTax ? `<div class="kv">
      <div class="k">销售方税号</div>
      <div class="v mono">${esc(r.sellerTax)}</div>
    </div>` : ""}
    ${r.amountCn ? `<div class="kv">
      <div class="k">价税合计大写</div>
      <div class="v">${esc(r.amountCn)}</div>
    </div>` : ""}
    ${r.remark ? `<div class="kv">
      <div class="k">备注</div>
      <div class="v">${esc(r.remark)}</div>
    </div>` : ""}
    ${r.drawer ? `<div class="kv">
      <div class="k">开票人</div>
      <div class="v">${esc(r.drawer)}</div>
    </div>` : ""}
    <div class="kv">
      <div class="k">文件名变更</div>
      <div class="rename">
        <div class="ln"><span class="lb">原名</span>${esc(r.file)}</div>
        <div class="ln out"><span class="lb">新名</span>${esc(r.newName)}</div>
      </div>
    </div>
    ${r.dup ? `<div class="warnLine">这张发票的号码在本批里出现了多次，可能是重复导入。
      程序会按你的「重名规则」自动区分文件名。</div>` : ""}
    ${!r.ok ? `<div class="warnLine">没读出这张发票的信息（多半是扫描件/图片型 PDF）。
      点「手动补录」填上购买方和金额，就能一起处理。</div>` : ""}
  `;

  $("#btnRename").disabled = false;
  $("#btnOpen").disabled = false;
  // 按钮文案跟着当前文件的格式走：OFD / XML 用户看到「打开 PDF」会以为点错了
  $("#btnOpen").textContent = r.fmt ? `打开 ${r.fmt}` : "打开文件";

  // 未识别时补一个「手动补录」按钮
  if (!r.ok) {
    if (!$("#btnFill")) {
      const b = document.createElement("button");
      b.className = "btn accent";
      b.id = "btnFill";
      b.textContent = "手动补录";
      b.onclick = openFill;
      $("#dActs").insertBefore(b, $("#btnRename"));
    }
  } else {
    removeFillBtn();
  }

  // 票面预览：翻页状态跟着这一行走（换行就从第 1 页开始，别把上一张的页码带过去）
  if ($("#pvCanvas")) loadPreview(r.i, 0);
}

function removeFillBtn() {
  const b = $("#btnFill");
  if (b) b.remove();
}

function fmtDate(d) {
  if (!d || d.length !== 8) return "—";
  return `${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}`;
}

function renderFooter() {
  const s = S.stats;
  $("#footSum").innerHTML = `已选 <b>${s.checkedCount}</b> 张`;
  $("#btnRun").disabled = busy || s.checkedCount === 0;
  $("#btnRun").textContent = s.checkedCount
    ? `开始重命名（${s.checkedCount}）` : "开始重命名";
  $("#btnExport").disabled = busy || !S.rows.length;
  // 合并按钮：只有勾选了 PDF 才可用。全是 OFD / XML 时置灰，
  // 并在悬停提示里说清原因 —— 否则用户点了没反应只会以为坏了。
  const pdfChecked = S.rows.filter(r => r.checked && r.fmt === "PDF").length;
  const bm = $("#btnMerge");
  bm.disabled = busy || pdfChecked === 0;
  bm.title = pdfChecked
    ? `把勾选的 ${pdfChecked} 个 PDF 按顺序合成一个文件（原文件不动）`
    : "先勾选要合并的 PDF（OFD / XML 不能合并）";
  refreshUndoBtn();
}

async function refreshUndoBtn() {
  const n = await call("undo_count");
  const b = $("#btnUndo");
  b.disabled = busy || !n;
  b.textContent = n ? `↩ 撤销上次（${n}）` : "↩ 撤销上次";
}

/* ---------------- 行交互 ---------------- */
$("#rows").addEventListener("click", async (e) => {
  if (busy) return;

  // 行末垃圾桶：直接去掉这一行。
  // ⚠️ 必须**最先**判 —— 它在行内部，放在后面会被「点行 = 选中」先吃掉。
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    await removeOneRow(+del.dataset.del);
    return;
  }

  // v2.6：状态列里的小按钮（拆开多票 PDF / 输入密码），同样要先于「点行选中」
  const sp = e.target.closest("[data-split]");
  if (sp) { e.stopPropagation(); openSplit(+sp.dataset.split); return; }
  const pw = e.target.closest("[data-pwd]");
  if (pw) { e.stopPropagation(); openPwd(+pw.dataset.pwd); return; }
  // 点「票号重复 ×N」这一枚药丸 = 打开取舍台（比先找右栏清单顺手）
  const dp = e.target.closest("[data-dup]");
  if (dp) { e.stopPropagation(); openDup(+dp.dataset.dup); return; }

  // v2.7：点「9月已报过」/「冲蓝票」/「被红冲」这些小标记
  //   · 已报过 → 选中这一行，右栏会显示「上次在哪处理过」
  //   · 冲蓝票 → 直接跳到那张蓝票
  //   · 被红冲 → 跳到冲它的那张红字票
  const seenTagEl = e.target.closest("[data-seen]");
  if (seenTagEl) {
    e.stopPropagation();
    sel = +seenTagEl.dataset.seen; renderRows(); renderDetail();
    return;
  }
  const redEl = e.target.closest("[data-red]");
  if (redEl) { e.stopPropagation(); jumpToRow(+redEl.dataset.red); return; }
  const redByEl = e.target.closest("[data-redby]");
  if (redByEl) { e.stopPropagation(); jumpToRow(+redByEl.dataset.redby); return; }

  const t = e.target.closest("[data-toggle]");
  if (t) {
    const i = +t.dataset.toggle;
    S = await call("toggle_row", i);
    render();
    return;
  }
  const row = e.target.closest(".row");
  if (row) {
    sel = +row.dataset.i;
    renderRows();
    renderDetail();
    updateRulePreview();      // 换一张票，「效果」要跟着换成这张票的真实取值
  }
});

/**
 * 移除单行（只从列表里去掉，**不删磁盘文件**）。
 *
 * 为什么不再弹确认框：这是**一行**，而且重新扫描随时能找回来；
 * 每点一下弹一次窗反而烦。改成「移完给一句提示」，提示里写明文件没删，
 * 用户就不会以为自己把文件删没了。
 * 挂在这张发票下面的行程单由后端一并移除（不会剩孤儿行）。
 */
async function removeOneRow(i) {
  const r = S.rows[i];
  if (!r) return;
  const res = await call("remove_rows", [i]);
  if (!res || !res.ok) { toast((res && res.msg) || "移除失败", true); return; }
  if (res.snapshot) S = res.snapshot;
  if (sel >= 0 && sel >= S.rows.length) sel = -1;
  render();
  toast("已从列表移除 1 行（文件没删）");
}

$("#rows").addEventListener("dblclick", (e) => {
  if (busy) return;
  const c = e.target.closest("[data-edit]");
  if (c) openEdit(+c.dataset.edit);
});

/* 右栏「重复票号」清单里点某个文件名 → 跳到左边那一行。
   删重复票最费劲的就是「这一组的另一份在哪儿」，点一下就到，不用一屏屏翻。 */
/** 选中并滚动到某一行（v2.7：红蓝票互跳也用它） */
function jumpToRow(i) {
  sel = i;
  renderRows();
  renderDetail();
  const el = document.querySelector(`#rows .row[data-i="${i}"]`);
  if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
}

$("#dBody").addEventListener("click", (e) => {
  // v2.7：跨批次提示卡上的两个按钮
  const ok = e.target.closest("[data-seenok]");
  if (ok) {
    const r = S.rows[+ok.dataset.seenok];
    if (r) seenMuted.add(r.no);
    render();
    return;
  }
  const off = e.target.closest("[data-seenoff]");
  if (off) {
    (async () => {
      S = await call("set_rule", "crossCheck", false);
      render();
      toast("已关闭重复报销预警，可在「展开规则 → 附加」里重新打开");
    })();
    return;
  }
  const only = e.target.closest("[data-seenonly]");
  if (only) {
    // 「只看这些」——用搜索框做不到（按票号筛），单独加一个临时视图
    filterSeen = !filterSeen;
    renderRows();
    return;
  }
  const b = e.target.closest("[data-goto]");
  if (!b) return;
  jumpToRow(+b.dataset.goto);
});

/* ---------------- 规则交互：命名规则（勾选 + 排序） ----------------
 *
 * 交互模型：
 *   · 折叠态：按钮上显示按当前顺序拼出的中文规则，如「开票日期—购买方名称—发票金额」
 *   · 展开态：上半是「已选内容」，可拖拽 / 用箭头调整先后顺序
 *             下半是「可添加字段」，点一下加入、再点一下移除
 *             最下面是分隔符与实时效果预览
 *
 * 为什么要「草稿」：
 *   面板里连续点几下会触发多次后端写入。若每次都整表重渲染，面板会被
 *   重建（滚动位置丢失、排序控件点空）。所以面板打开期间只在本地维护草稿，
 *   每次改动即时同步后端，但**不重建面板自身**。
 */
let ruleFields = [];        // 草稿：字段 key 数组，顺序 = 文件名中的先后
let ruleSep = "—";
let ruleOpen = false;

function fieldDef(key) {
  return (S.fields || []).find(f => f.key === key);
}
/**
 * 把字段池的 DOM 顺序排成「**已勾选**（按命名顺序）在前，未勾选（按原字段顺序）在后」。
 *
 * 为什么必须排序：勾上之后，行的先后就是文件名里的先后。如果行还赖在原来的位置，
 * 用户只能靠序号去脑补顺序，拖动也没法直观看出效果。浮到最上面最直观。
 *
 * ⚠️ 只**移动已有节点**（appendChild），绝不重建 innerHTML ——
 *    重建会把正在拖的那个节点干掉，dragend / drop 就派发到脱离文档的旧节点上，
 *    事件收不到（老版本「已选内容」条就是这么坏掉的）。
 */
function reorderPool() {
  const pool = $("#rpPool");
  if (!pool) return;
  const nodes = $$("#rpPool .rpOpt");
  if (!nodes.length) return;
  const byKey = {};
  nodes.forEach(n => { byKey[n.dataset.k] = n; });
  const want = [];
  ruleFields.forEach(k => { if (byKey[k]) want.push(byKey[k]); });
  nodes.forEach(n => { if (!want.includes(n)) want.push(n); });
  const cur = Array.from(pool.children);
  // 顺序没变就什么都不做 —— 免得每点一下都把节点挪一遍
  if (cur.length === want.length && cur.every((n, i) => n === want[i])) return;
  want.forEach(n => pool.appendChild(n));
}
function fieldLabel(key) {
  const d = fieldDef(key);
  if (d) return d.label;
  // 老配置里可能还留着已从列表下架的历史字段
  return (S.legacyLabels || {})[key] || key;
}
function fieldHint(key) {
  const d = fieldDef(key);
  return d ? (d.hint || "") : "";
}

/** 把字段数组 + 分隔符渲染成可读的中文规则串 */
function fieldsToText(fields, sep) {
  const labels = (fields || []).map(fieldLabel).filter(Boolean);
  if (!labels.length) return "未设置";
  return labels.join(sep === "" ? " · " : " " + sep + " ");
}

/** 同步草稿到后端，并刷新界面（面板本身不重建，避免打断拖拽/输入） */
async function commitRule() {
  const r = await call("set_fields", ruleFields.slice(), ruleSep);
  if (!r) return;
  S = r;
  syncRuleView();

  // 后端已按新规则重算了每行的新文件名，这里要把列表同步出来。
  // 只调 renderRows，不调 render() —— 后者会连带重建规则面板。
  renderRows();
}

/** 只更新面板里会变的部分：字段池选中态/顺序、计数、预览 */
function syncRuleView() {
  renderRuleLabel();
  if (!ruleOpen) return;

  // 字段池一行承担三件事：勾选、看顺序（序号徽标）、调顺序（按住拖动）。
  // 顶部那块「已选内容」条在 v2.5.2 就删了，所以这里是唯一入口。
  $$("#rpPool .rpOpt").forEach(el => {
    const i = ruleFields.indexOf(el.dataset.k);
    const on = i >= 0;
    el.classList.toggle("on", on);
    el.setAttribute("aria-pressed", on ? "true" : "false");
    el.draggable = on;                 // 只有勾上的能拖：没勾的没有顺序可言
    const ord = el.querySelector(".rpOrd");
    if (ord) ord.textContent = on ? String(i + 1) : "";
  });
  reorderPool();

  const cnt = $("#rpCount");
  if (cnt) cnt.textContent = `已选 ${ruleFields.length} 项`;

  $$("#rpSep .rpSepBtn").forEach(el => {
    el.classList.toggle("on", el.dataset.s === ruleSep);
  });

  updateRulePreview();
}

/**
 * 实时效果预览。
 *
 * 后端优先用「当前选中的那张真实发票」来算（见 bridge.rule_preview 的注释：
 * 老版本只拿内置示例值算，读不到的字段预览里照样好看，问题全被藏起来）。
 * 所以这里要把选中行号传过去，并把「这张票读不到的字段」显式提示出来 ——
 * 否则用户勾了字段却在文件名里看不到它，只会以为程序坏了。
 */
async function updateRulePreview() {
  const el = $("#rpPreview");
  const src = $("#rpPvSrc");
  const hint = $("#rpPvHint");
  if (!el || !ruleOpen) return;

  const clearHint = () => { if (hint) { hint.hidden = true; hint.textContent = ""; } };

  if (!ruleFields.length) {
    el.textContent = "—"; el.title = "";
    if (src) { src.textContent = "示例"; src.classList.remove("real"); }
    clearHint();
    fitRulePanel();          // 内容变短了，把空间还给字段区
    return;
  }

  // 先给本地示例结果，避免等待后端时闪一下空白
  const ex = {};
  (S.fields || []).forEach(f => { ex[f.key] = f.ex; });
  const local = ruleFields.map(k => ex[k] || "").filter(Boolean)
    .join(ruleSep === "" ? "" : ruleSep);
  el.textContent = local || "—";
  el.title = el.textContent;

  // 第 3 个参数是「当前选中第几张票」。
  // 后端签名是 rule_preview(fields, separator, idx, suffix) —— idx 特意排在
  // suffix 前面，就是为了让这个三参数调用天然是对的。
  // （踩过：idx 曾排在 suffix 后面，于是 sel 被当成文件名后缀拼上去，
  //   面板还永远显示第 1 张票。）
  const r = await call("rule_preview", ruleFields.slice(), ruleSep, sel);
  if (!r || !r.ok) return;

  const real = r.source === "real";
  el.textContent = r.preview || (real ? "（这张票上选中的字段都是空的）" : "—");
  el.title = r.preview || el.textContent;
  if (src) {
    src.textContent = real ? "当前票实测" : "示例";
    src.classList.toggle("real", real);       // 蓝色药丸 = 真实票的值
  }

  const miss = r.missing || [];
  if (real && miss.length && hint) {
    hint.hidden = false;
    hint.textContent = "⚠ 当前选中的发票读不到：" + miss.join("、")
      + "。票面上本来就没有的内容会跳过，文件名里不会留下空档。";
  } else {
    clearHint();
  }

  // 内容长度变了，重新分配预览高度 —— 否则勾字段时预览可能被上一轮的
  // 约束卡住，出现「刚勾上的内容看不到」。
  fitRulePanel();
}

/**
 * 折叠态：按钮统一显示「自定义」（面板里有「效果」预览，按钮上再堆一长串
 * 规则文字既看不全又占地方），规则摘要放到右侧「当前规则」那一栏。
 */
function renderRuleLabel() {
  const chain = $("#rlChain");
  if (chain) {
    const txt = fieldsToText(ruleFields, ruleSep === "" ? "" : ruleSep);
    chain.textContent = txt;
    chain.title = txt;                    // 超长时悬停可看全
  }
}

function openRulePanel() {
  ruleFields = (S.cfg.fields || ["buyer", "amount"]).slice();
  ruleSep = S.cfg.separator === undefined ? "—" : S.cfg.separator;
  ruleOpen = true;
  buildRulePanel();
  $("#rulePanel").hidden = false;
  $("#btnTplPick").classList.add("open");
  fitRulePanel();
}

/**
 * 把面板高度限制在「按钮下方 → 窗口底部」之间，并给「效果」预览分配高度。
 *
 * CSS 里的 max-height: calc(100vh - 150px) 是按「面板贴着窗口顶」估的，
 * 但面板实际挂在「命名规则」按钮下方。窗口一矮（1366×768 的老笔记本，
 * 或者用户把窗口拖小），面板底部就会跑到屏幕外，把底部的「完成」按钮切掉。
 * 这里按面板的真实位置量一次可用高度，写回 max-height。
 *
 * 必须在 hidden = false 之后调用 —— 隐藏元素的 getBoundingClientRect() 全是 0。
 */
function fitRulePanel() {
  const p = $("#rulePanel");
  if (!p || p.hidden) return;

  // ① 面板整体高度
  p.style.maxHeight = "";
  const pv = $("#rpPreviewBox");
  const body = $("#rpBody");
  if (pv) pv.style.maxHeight = "";              // 先复位，否则测出来的是上一轮的约束
  const top = p.getBoundingClientRect().top;
  const room = Math.floor(window.innerHeight - top - 12);    // 底部留 12px 呼吸位
  const cap = Math.floor(window.innerHeight - 150);          // 与 CSS 上限一致
  const panelMax = Math.max(220, Math.min(room, cap));
  p.style.maxHeight = panelMax + "px";

  // ② 效果预览：优先**完整显示**，空间真的不够才退化成内部滚动。
  //    为什么不能写死 max-height：
  //      13 个字段时完整文件名需要约 141px，原来写死 132px，正好切掉 7px
  //      （用户反馈「要能完整显示预览效果」就是这么来的）；
  //      而窗口高度是变的，写死多少都不对 —— 只能按可用空间实测分配。
  if (pv && body) {
    pv.style.maxHeight = "none";
    // scrollHeight 不含边框，而全局是 box-sizing:border-box，故 +2
    const need = pv.scrollHeight + 2;
    // 面板内除「字段区」和「预览」之外的固定开销（头部/底部/内外边距/间隙）
    const fixed = p.getBoundingClientRect().height
                - body.getBoundingClientRect().height
                - pv.getBoundingClientRect().height;
    // 字段区至少留 104px，否则勾字段的地方被挤没了
    const avail = Math.max(60, panelMax - fixed - 104);
    pv.style.maxHeight = Math.min(need, avail) + "px";
  }
}

window.addEventListener("resize", () => { if (ruleOpen) fitRulePanel(); });

function closeRulePanel() {
  ruleOpen = false;
  $("#rulePanel").hidden = true;
  $("#btnTplPick").classList.remove("open");
}

/** 一次性构建面板骨架（字段池与分隔符按钮不会随勾选变化，只建一次） */
function buildRulePanel() {
  // 字段池：单列竖排，一行一个；示例值不显示，只在悬停提示里给说明。
  // 勾上的行会**浮到最上面**并按命名顺序排列，右侧带序号徽标和拖动把手。
  // ⚠️ 用 div 而不是 button：里面要放 aria 状态、还要能拖，button 不合适。
  $("#rpPool").innerHTML = (S.fields || []).map(f => `
    <div class="rpOpt" role="button" tabindex="0" data-k="${esc(f.key)}"
         aria-pressed="false" title="${esc(f.hint || f.label)}">
      <span class="rpTick" aria-hidden="true"></span>
      <span class="rpOptName">${esc(f.label)}</span>
      <span class="rpOrd" aria-hidden="true"></span>
      <span class="rpGrip" aria-hidden="true" title="按住可拖动排序"></span>
    </div>`).join("");

  $("#rpSep").innerHTML = (S.separators || ["—", "-", "_", "·", " ", ""])
    .map(s => {
      const name = s === "" ? "无" : (s === " " ? "空格" : s);
      return `<button type="button" class="rpSepBtn" data-s="${esc(s)}"
                      title="${s === "" ? "字段直接相连" : "用「" + s + "」分隔"}">
                ${esc(name)}</button>`;
    }).join("");

  syncRuleView();
}

/* ---- 面板事件 ---- */

// 打开 / 收起
$("#btnTplPick").onclick = (e) => {
  e.stopPropagation();
  if (ruleOpen) closeRulePanel(); else openRulePanel();
};

// 字段行：点一下 = 添加 / 移除（排序靠拖动，不再有 ‹ › 按钮）
$("#rpPool").onclick = (e) => {
  const el = e.target.closest(".rpOpt");
  if (!el) return;
  e.stopPropagation();
  const k = el.dataset.k;
  const i = ruleFields.indexOf(k);
  if (i >= 0) {
    if (ruleFields.length <= 1) { toast("至少要留一项内容"); return; }
    ruleFields.splice(i, 1);
  } else {
    ruleFields.push(k);
  }
  syncRuleView();
  commitRule();
};

/* ---- 拖动排序（2026-10-17 起取代 ‹ › 按钮） ----
 *
 * 规则：**只有勾上的行能拖**，未勾选的行不能拖（它们没有顺序可言）。
 *
 * ⚠️ 拖拽期间**绝不重建 DOM**。老版本的「已选内容」条就是栽在这：
 *    syncRuleView 用 innerHTML 重建节点后，dragend / drop 会派发到已经
 *    脱离文档的旧节点上，事件冒泡不到监听器 —— 表现是「界面顺序变了，
 *    但没提交，关掉面板再打开又还原」。
 *    所以这里：
 *      1) dragover 只在目标行上打个落点提示（改 class，不动结构）；
 *      2) drop 时先改 ruleFields，再**移动已有节点**（appendChild），不重建。
 */
let dragKey = "";
let dropKey = "";
let dropAfter = false;

function clearDragMarks() {
  dragKey = "";
  dropKey = "";
  dropAfter = false;
  $$("#rpPool .rpOpt").forEach(n => n.classList.remove("dragging", "dropOn", "dropAfter"));
}

$("#rpPool").addEventListener("dragstart", (e) => {
  const el = e.target.closest(".rpOpt");
  if (!el || !el.classList.contains("on")) { e.preventDefault(); return; }
  dragKey = el.dataset.k;
  el.classList.add("dragging");
  try {
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", dragKey);
  } catch (_) {}
});

$("#rpPool").addEventListener("dragover", (e) => {
  if (!dragKey) return;
  const el = e.target.closest(".rpOpt");
  if (!el || !el.classList.contains("on") || el.dataset.k === dragKey) return;
  e.preventDefault();
  // 落点在目标行的上半还是下半 —— 决定插到它前面还是后面
  const box = el.getBoundingClientRect();
  const after = e.clientY > box.top + box.height / 2;
  if (dropKey && dropKey !== el.dataset.k) {
    const prev = $(`#rpPool .rpOpt[data-k="${CSS.escape(dropKey)}"]`);
    if (prev) prev.classList.remove("dropOn", "dropAfter");
  }
  dropKey = el.dataset.k;
  dropAfter = after;
  el.classList.add("dropOn");
  el.classList.toggle("dropAfter", after);
});

$("#rpPool").addEventListener("drop", (e) => {
  if (!dragKey || !dropKey) return;
  e.preventDefault();
  const from = ruleFields.indexOf(dragKey);
  let to = ruleFields.indexOf(dropKey);
  if (from < 0 || to < 0) { clearDragMarks(); return; }
  if (dropAfter) to += 1;
  ruleFields.splice(from, 1);
  if (from < to) to -= 1;               // 自己先被摘掉了，后面的位次整体前移
  ruleFields.splice(to, 0, dragKey);
  clearDragMarks();
  syncRuleView();
  commitRule();
});

document.addEventListener("dragend", clearDragMarks);
// 拖到面板外面松手：不留残影
$("#rpPool").addEventListener("dragleave", (e) => {
  if (!e.relatedTarget) clearDragMarks();
});

// 分隔符
$("#rpSep").onclick = (e) => {
  const el = e.target.closest(".rpSepBtn");
  if (!el) return;
  e.stopPropagation();
  ruleSep = el.dataset.s;
  syncRuleView(); commitRule();
};

/* ---- 拖拽排序：已随「已选内容」条一起移除（2026-10-16）
 *
 * 原来的 chip 条支持拖拽，代码里还留着两条注释值得记住：
 *   · dragover 里不要调 syncRuleView() —— 它用 innerHTML 重建节点，
 *     原节点被销毁后 dragend / drop 会派发在游离节点上，事件冒泡不到监听器，
 *     结果「界面顺序变了但没提交」，关掉面板再打开就还原。
 *   · 收尾事件要挂在 document 上，保证一定收得到。
 * 现在排序走 ‹ › 按钮，不再拖拽，所以整段删除；
 * 但上面这两条对以后任何「重建 DOM 的列表」依然适用。
 */

// 恢复默认
$("#rpReset").onclick = (e) => {
  e.stopPropagation();
  ruleFields = ["buyer", "amount"];
  ruleSep = "—";
  syncRuleView();
  commitRule();
};

// 完成（收起面板）
$("#rpDone").onclick = (e) => {
  e.stopPropagation();
  closeRulePanel();
  render();
};

// 右上角 ✕ 收起：窗口很矮时「完成」按钮可能滚出可视区，
// 所以头部必须常驻一个关闭按钮，保证任何窗口高度下都关得掉。
$("#rpClose").onclick = (e) => {
  e.stopPropagation();
  closeRulePanel();
};

// 面板内部点击不冒泡到 document，避免误收起
$("#rulePanel").addEventListener("click", (e) => e.stopPropagation());

// 点空白处收起。
// ⚠️ 例外：点发票列表的行**不收起**。
//    面板里的「效果」显示的是「当前选中那张票」的真实取值（见 bridge.rule_preview），
//    用户核对字段时想一边换票一边看效果；每点一行就把面板关掉，这功能就没法用了。
//
// ⚠️ 判定「点的是不是列表」不能用 e.target.closest("#rows")：
//    列表的点击处理里会调 renderRows() 整块重建 DOM，等事件冒泡到 document 时
//    e.target 已经是**游离节点**，它的祖先链断了，closest 会返回 null ——
//    结果就是「明明加了例外，面板照样关」。
//    composedPath() 是事件派发那一刻就定下来的路径，不受后续 DOM 改动影响。
document.addEventListener("click", (e) => {
  if (!ruleOpen) return;
  const path = e.composedPath ? e.composedPath() : [];
  if (path.some(n => n && n.id === "rows")) return;
  if (e.target && e.target.closest && e.target.closest("#rows")) return;
  closeRulePanel();
});

// Esc 收起
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && ruleOpen) closeRulePanel();
});

$$("#segAmount span").forEach(el => el.onclick = async () => {
  S = await call("set_rule", "amountMode", el.dataset.v);
  render();
});
$$("#segMode span").forEach(el => el.onclick = async () => {
  S = await call("set_rule", "mode", el.dataset.v);
  render();
});
$("#swDistribute").onchange = async (e) => {
  S = await call("set_rule", "distribute", e.target.checked);
  render();
};
$("#swBackup").onchange = async (e) => {
  S = await call("set_rule", "backup", e.target.checked);
  render();
};
$("#swSub").onchange = async (e) => {
  S = await call("set_rule", "recursive", e.target.checked);
  render();
  // 勾选状态变了，立刻按新规则重扫一次，省得用户再点一次「重新识别」
  if (S.folder) {
    const r = await call("rescan");
    if (r && r.ok) startPoll();
  }
};

/* ---------------- 顶部 / 工具条 ---------------- */
async function pickFolder() {
  if (busy) return;
  setStatus("等待选择文件夹…");
  const r = await call("choose_folder");
  if (r && r.ok) { setStatus("正在读取 PDF…"); startPoll(); }
  else if (r && r.msg !== "已取消") toast(r.msg, true);
  else setStatus("就绪");
}
$("#btnPick").onclick = pickFolder;

$("#btnAddFile").onclick = async () => {
  if (busy) return;
  const r = await call("add_files");
  if (r && r.ok) startPoll();
  else if (r && r.msg !== "已取消") toast(r.msg, true);
};

$("#btnRescan").onclick = async () => {
  if (busy) return;
  const r = await call("rescan");
  if (r && r.ok) startPoll();
  else if (r) toast(r.msg, true);
};

/* 三个「批量勾选」按钮。
 *
 * ⚠️ 筛选/搜索生效时，它们**只作用于看得见的那些行** ——
 * 这是通用约定：用户搜出 3 条、点「全选」，期望的是勾上这 3 条，
 * 而不是把隐藏的 200 条也一起勾走（那会误改一堆文件）。
 * 所以给 set_all 传了可见行号；没筛选时不传，行为与以前完全一致。
 */
function visibleIdx() {
  const filtering = !!searchText.trim() || dupOnly;
  return filtering ? visibleRows().map(r => r.i) : null;
}
$("#btnAll").onclick = async () => {
  S = await call("set_all", true, false, visibleIdx()); render();
};
$("#btnNone").onclick = async () => {
  S = await call("set_all", false, false, visibleIdx()); render();
};
$("#btnOnlyOk").onclick = async () => {
  S = await call("set_all", true, true, visibleIdx()); render();
};
$("#btnDupOnly").onclick = () => setFilter("dup");
// v2.7 状态筛选：再点一次同一个 = 回到全部（跟「只看重复票号」的老手感一致）
$$("#filters .pill").forEach(el => {
  el.onclick = () => setFilter(el.dataset.f);
});

/* ---------------- v2.7.3：批量删除 ---------------- */
/**
 * 把勾选的行一次从列表移除（不删磁盘文件）。
 *
 * 为什么现在才做：以前只有「行末垃圾桶」和「清空列表」两种极端。
 * 实际干活时常见的是「把这几张失败的一起清掉」「把筛出来的重复票删到只剩一份」——
 * 只能一行行点，几十行就是几十次点击。
 */
$("#btnDelSel").onclick = async () => {
  if (busy) { toast("正在处理上一批，请稍候", true); return; }
  const idxs = (S.rows || []).filter(r => r.checked).map(r => r.i);
  if (!idxs.length) { toast("先勾选要删除的行", true); return; }
  const ok = await askConfirm({
    title: `从列表删除 ${idxs.length} 行？`,
    text: `这 <b>${idxs.length}</b> 行会从当前列表移除，不再参与改名。`
      + `<br>挂在发票下面的行程单会一起移除。`,
    note: "磁盘上的发票文件不会被删除，重新扫描随时能找回来。",
    okText: `删除 ${idxs.length} 行`,
    danger: true,
  });
  if (!ok) return;
  const res = await call("remove_rows", idxs);
  if (!res || !res.ok) { toast((res && res.msg) || "删除失败", true); return; }
  if (res.snapshot) S = res.snapshot;
  if (sel >= S.rows.length) sel = -1;
  render();
  toast(`已从列表删除 ${res.removed} 行（文件没删）`);
};

/* 窄窗口的「筛选 ▾」：点开/收起，点外面自动关 */
const filterMenuBtn = $("#btnFilterMenu");
const filterPop = $("#filterPop");
if (filterMenuBtn && filterPop) {
  filterMenuBtn.onclick = (e) => {
    e.stopPropagation();
    filterPop.hidden = !filterPop.hidden;
  };
  $$("#filterPop [data-f]").forEach(el => {
    el.onclick = () => setFilter(el.dataset.f);
  });
  document.addEventListener("click", (e) => {
    if (!filterPop.hidden && !e.target.closest("#filterCompact")) {
      filterPop.hidden = true;
    }
  });
}

/* 统计条上的「筛选中 x / y」→ 点 ✕ 回到全部 */
const statX = $("#stFilterX");
if (statX) {
  statX.onclick = () => {
    searchText = "";
    filterSeen = false;
    if (searchInp) searchInp.value = "";
    setFilter("all");
  };
}

$("#btnClear").onclick = async () => {
  if (busy) return;
  if (!S.rows.length && !S.folder && !S.manual) {
    toast("列表已经是空的");
    return;
  }
  const n = S.rows.length;
  const edited = S.rows.filter(r => r.edited).length;
  const ok = await askConfirm({
    title: n ? "清空列表？" : "重新开始？",
    text: n
      ? `列表里的 <b>${n}</b> 张发票记录会被移除，程序也会忘记上次打开的文件夹。`
        + (edited ? `<br>其中 <b>${edited}</b> 张手动改过的文件名也会一起丢掉。` : "")
      : "程序会忘记上次打开的文件夹，下次打开不会自动扫回来。",
    // 这句话才是用户此刻真正想知道的，单独给一条，别缩在括号里
    note: "磁盘上的发票文件不会被删除，也不会被改动。",
    okText: n ? "清空列表" : "重新开始",
    danger: true,
  });
  if (!ok) return;
  S = await call("clear");
  sel = 0;
  filterMode = "all";
  filterSeen = false;
  dupOnly = false;
  render();
  setProgress(0, 0);
  setStatus("已清空。请重新选择发票文件夹");
  toast("列表已清空");
};

/* ---------------- 详情按钮 ---------------- */
$("#btnOpen").onclick = async () => {
  const r = await call("open_pdf", sel);
  if (r && !r.ok) toast(r.msg, true);
};
$("#btnRename").onclick = () => openEdit(sel);

/* ---------------- 弹窗通用 ---------------- */
function openModal(id) { $(id).hidden = false; }
function closeModal(id) { $(id).hidden = true; }
$$("[data-close]").forEach(b => {
  b.onclick = () => b.closest(".modal").hidden = true;
});
$$(".modal").forEach(m => {
  m.addEventListener("mousedown", (e) => {
    if (e.target === m) m.hidden = true;
  });
});

/* v2.8.8：关闭按钮一律事件委托。
   原来启动时一次性绑 $$("[data-close]")，而「识别体检 / 汇总报表」写在 </body> 前、
   比脚本晚，X 和「关闭」全都没绑上，只能按 Esc（业主反馈）。 */
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-close]");
  if (!b) return;
  const m = b.closest(".modal");
  if (m) m.hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $$(".modal").forEach(m => m.hidden = true);
  if (e.key === "Enter" && !$("#mRun").hidden) $("#btnRunGo").click();
});

/* ---------------- 确认框（替代原生 confirm） ----------------
 *
 * 为什么必须自己画：
 *   原生 confirm 的标题栏显示的是「页面标题」—— pywebview 用的是本地 HTTP 服务，
 *   于是变成「127.0.0.1:60824 显示」：暴露内部细节、跟界面风格毫无关系，
 *   字体/按钮/圆角也全不是我们那一套。
 *
 * 用法：const ok = await askConfirm({ ... })   →  true / false
 *   title   标题，说「要做什么」        （默认「确认操作」）
 *   text    正文，说「会发生什么」      ⚠️ 传 HTML，动态内容必须自己 esc()
 *   note    底部绿色提示条，说「什么不会发生」（可选，但破坏性操作强烈建议给）
 *   okText  确认按钮文案，**直接写动作名**（如「清空列表」）而不是「确定」
 *   danger  破坏性操作：按钮变红，且默认焦点落在「取消」上
 *
 * 三个刻意的设计取舍：
 *   1. 标题 / 正文 / 提示条 是三件事，不要糅成一段话 ——
 *      「会不会删我文件」是用户此刻唯一的焦虑，得让它自己跳出来回答。
 *   2. 确认按钮写动作名，省掉「回去重读一遍问题」这一步。
 *   3. danger 时默认聚焦「取消」：避免顺手一个回车就把事情办了。
 */
let cfResolve = null;

function askConfirm(o) {
  const opt = o || {};
  const mask = $("#mConfirm");
  const ok = $("#cfOk");
  const cancel = $("#cfCancel");

  $("#cfTitle").textContent = opt.title || "确认操作";
  $("#cfText").innerHTML = opt.text || "";
  const note = $("#cfNote");
  if (opt.note) { note.textContent = opt.note; note.hidden = false; }
  else { note.hidden = true; note.textContent = ""; }

  ok.textContent = opt.okText || "确定";
  ok.classList.toggle("danger", !!opt.danger);
  ok.classList.toggle("primary", !opt.danger);

  mask.hidden = false;

  const finish = (v) => {
    document.removeEventListener("keydown", onKey, true);
    ok.onclick = cancel.onclick = $("#cfX").onclick = null;
    mask.hidden = true;
    if (cfResolve) { const r = cfResolve; cfResolve = null; r(v); }
  };
  // 用捕获阶段 + stopPropagation，抢在「Esc 关掉所有弹窗」那个全局处理器
  // 和「点遮罩关弹窗」那个通用处理器前面，保证 Promise 一定会被 resolve
  // （否则弹窗看着关了、await 永远卡住）。
  const onKey = (e) => {
    if (e.key === "Escape") {
      e.preventDefault(); e.stopPropagation(); finish(false);
    } else if (e.key === "Enter") {
      e.preventDefault(); e.stopPropagation();
      finish(document.activeElement !== cancel);
    }
  };

  ok.onclick = () => finish(true);
  cancel.onclick = () => finish(false);
  $("#cfX").onclick = () => finish(false);
  mask.onmousedown = (e) => { if (e.target === mask) finish(false); };
  document.addEventListener("keydown", onKey, true);

  setTimeout(() => (opt.danger ? cancel : ok).focus(), 30);
  return new Promise((res) => { cfResolve = res; });
}

/* ---------------- 改名弹窗 ---------------- */
function openEdit(i) {
  const r = S.rows[i];
  if (!r) return;
  sel = i;
  $("#editName").value = stripExt(r.newName);
  openModal("#mEdit");
  setTimeout(() => { $("#editName").focus(); $("#editName").select(); }, 40);
}
$("#btnEditGo").onclick = async () => {
  S = await call("set_new_name", sel, $("#editName").value);
  closeModal("#mEdit");
  render();
  toast("已更新文件名");
};
$("#editName").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#btnEditGo").click();
});

/* ---------------- 补录弹窗 ---------------- */
function openFill() {
  const r = S.rows[sel];
  if (!r) return;
  $("#fillFile").textContent = r.file;
  $("#fillBuyer").value = r.buyer || "";
  $("#fillAmount").value = r.amount !== null ? r.amount : "";
  openModal("#mFill");
  setTimeout(() => $("#fillBuyer").focus(), 40);
}
$("#btnFillGo").onclick = async () => {
  const r = await call("fill_manual", sel, $("#fillBuyer").value, $("#fillAmount").value);
  if (r && r.ok === false) { toast(r.msg, true); return; }
  S = r;
  closeModal("#mFill");
  render();
  toast("已补录并勾选");
};
$("#fillAmount").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#btnFillGo").click();
});

/* ---------------- 执行 ---------------- */
$("#btnRun").onclick = async () => {
  if (busy) return;
  const p = await call("plan_preview");
  if (!p) return;
  if (!p.real) {
    toast("没有需要处理的文件（可能已改好名了）", true);
    return;
  }

  const warn = [];
  if (p.skipped) warn.push(`${p.skipped} 个因目标文件名已存在被跳过`);
  if (p.warns)   warn.push(`${p.warns} 个文件名/路径偏长，可能失败`);
  if (S.stats.dupCount) warn.push(`${S.stats.dupCount} 组重复票号，请确认不是重复入账`);

  const howTo = p.distribute
    ? (p.copyMode ? "复制到各客户文件夹" : "移动到各客户文件夹")
    : (p.copyMode ? "复制到「重命名输出」" : "原地重命名");

  $("#runBody").innerHTML = `
    <div class="factGrid">
      <div class="fact"><div class="k">将要处理</div><div class="v">${p.real} <small>个文件</small></div></div>
      <div class="fact"><div class="k">操作方式</div><div class="v">${howTo}</div></div>
      ${p.renames ? `<div class="fact"><div class="k">原地改名</div><div class="v">${p.renames} <small>个</small></div></div>` : ""}
      ${p.moves ? `<div class="fact"><div class="k">移动到客户文件夹</div><div class="v">${p.moves} <small>个</small></div></div>` : ""}
      ${p.copies ? `<div class="fact"><div class="k">复制副本</div><div class="v">${p.copies} <small>个</small></div></div>` : ""}
      ${p.same ? `<div class="fact"><div class="k">无需改动</div><div class="v">${p.same} <small>个</small></div></div>` : ""}
      ${p.skipped ? `<div class="fact"><div class="k">跳过</div><div class="v warn">${p.skipped} <small>个</small></div></div>` : ""}
    </div>
    <div class="planList">
      ${p.sample.map(r => `
        <div class="li">
          <div class="from" title="${esc(r.src)}">${esc(r.src)}</div>
          <div class="ar">→</div>
          <div class="to" title="${esc(r.dst)}">${esc(r.dst)}</div>
        </div>`).join("")}
      ${p.real > p.sample.length ? `<div class="li"><div class="from">…</div><div class="ar"></div>
        <div class="to">其余 ${p.real - p.sample.length} 个</div></div>` : ""}
    </div>
    ${warn.length ? `<div class="warnLine">${warn.join("<br>")}</div>` : ""}
  `;
  $("#swBackup2").checked = !!S.cfg.backup;
  openModal("#mRun");
};

$("#swBackup2").onchange = async (e) => {
  S = await call("set_rule", "backup", e.target.checked);
  renderRules();
};

$("#btnRunGo").onclick = async () => {
  closeModal("#mRun");
  const r = await call("execute");
  if (r && r.ok === false) { toast(r.msg, true); return; }
  S = await call("get_state");
  render();
  setStatus("正在处理…");
  startPoll();
};

/* ---------------- 结果弹窗 ---------------- */
function showDone(p) {
  const rows = [];
  rows.push(`<div class="factGrid">
      <div class="fact"><div class="k">成功</div><div class="v ok">${p.done} <small>个</small></div></div>
      <div class="fact"><div class="k">失败</div><div class="v ${p.failed ? "warn" : ""}">${p.failed} <small>个</small></div></div>
    </div>`);

  if (p.backup) {
    rows.push(`<div class="warnLine ok">
      <b>原文件已备份到</b>
      <div class="lnk">${esc(p.backup)}</div>
      <div class="lnkMeta">改错了可以直接从这里取回原件</div>
    </div>`);
  }
  if (p.backupFailed) {
    rows.push(`<div class="warnLine">有 ${p.backupFailed} 个文件备份失败，请留意。</div>`);
  }
  if (p.errors && p.errors.length) {
    rows.push(`<div class="warnLine">
      <b>以下文件处理失败</b>
      ${p.errors.map(e =>
        `<div class="lnk">${esc(e.file)}<br>${esc(e.error)}</div>`).join("")}
    </div>`);
  }
  if (!p.failed && !p.skipped) {
    rows.push(`<div style="text-align:center;font-size:13px;color:var(--ink-2)">
      全部处理完毕，列表已按磁盘实际结果刷新。</div>`);
  }

  $("#doneTitle").textContent = p.failed ? "处理完成（有失败项）" : "处理完成 ✅";
  $("#doneBody").innerHTML = rows.join("");
  doneFolderTarget = null;          // 重命名场景：打开源文件夹
  $("#btnDoneFolder").hidden = !S.folder;
  openModal("#mDone");
}

/** 字节数转人话（合并结果的大小要给用户看到） */
function fmtBytes(n) {
  if (!n) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

/**
 * 合并完成提示。
 * 重点是让用户**马上能找到文件** —— 给完整路径，并让「打开所在文件夹」
 * 直接定位到刚生成的那个（而不是只打开目录）。
 */
function showMergeDone(p) {
  doneFolderTarget = p.out || null;
  const rows = [];
  rows.push(`<div class="factGrid">
      <div class="fact"><div class="k">合并</div><div class="v ok">${p.files} <small>份</small></div></div>
      <div class="fact"><div class="k">总页数</div><div class="v">${p.pages} <small>页</small></div></div>
    </div>`);
  rows.push(`<div class="warnLine ok">
      <b>已生成</b>（原文件都还在）
      <div class="lnk">${esc(p.out)}</div>
      <div class="lnkMeta">大小 ${fmtBytes(p.bytes)}</div>
    </div>`);
  if (p.errors && p.errors.length) {
    rows.push(`<div class="warnLine">
      <b>以下文件没能合并进去</b>
      ${p.errors.map(e =>
        `<div class="lnk">${esc(e.file)}<br>${esc(e.error)}</div>`).join("")}
    </div>`);
  }
  $("#doneTitle").textContent = (p.errors && p.errors.length)
    ? "合并完成（有跳过项）" : "合并完成 ✅";
  $("#doneBody").innerHTML = rows.join("");
  $("#btnDoneFolder").hidden = false;
  openModal("#mDone");
}

/**
 * 「打开所在文件夹」的目标随上下文变：
 *   · 重命名完 → 打开源文件夹（null）
 *   · 合并完   → 定位到**刚生成的那个文件**，否则用户还得在一堆文件里自己找
 */
let doneFolderTarget = null;
$("#btnDoneFolder").onclick = () => {
  if (doneFolderTarget) call("reveal_path", doneFolderTarget);
  else call("open_folder", "src");
};

/* ---------------- 撤销 / 导出 ---------------- */
$("#btnUndo").onclick = async () => {
  if (busy) return;
  const r = await call("undo");
  if (r && r.ok === false) { toast(r.msg, true); return; }
  setStatus("正在还原…");
  startPoll();
};

/* 导出台账：下拉选格式。
   Excel 放第一位 —— 20 位发票号码用 Excel 打开 CSV 会变成科学计数法、
   末几位直接丢失，而 CSV 格式没有「单元格格式」的概念，从 CSV 侧解决不了。
   CSV 留着给需要往别的系统导入的场景。 */
$("#btnExport").onclick = (e) => {
  e.stopPropagation();
  const m = $("#exportMenu");
  m.hidden = !m.hidden;
};
$("#exportMenu").onclick = async (e) => {
  const b = e.target.closest("[data-fmt]");
  if (!b) return;
  $("#exportMenu").hidden = true;
  const fmt = b.dataset.fmt;
  // v2.6：改名对照表（原名→新名），给审计留痕用
  if (fmt === "mapping") {
    const r = await call("export_mapping");
    if (r && r.ok) toast(`对照表已导出（${r.rows} 行）`);
    else if (r && r.msg !== "已取消") toast(r.msg, true);
    return;
  }
  const xlsx = fmt === "xlsx";
  const r = await call(xlsx ? "export_excel" : "export_csv");
  if (r && r.ok) toast(xlsx ? "Excel 台账已导出" : "CSV 台账已导出");
  else if (r && r.msg !== "已取消") toast(r.msg, true);
};
document.addEventListener("click", () => {
  const m = $("#exportMenu");
  if (m && !m.hidden) m.hidden = true;
});

/* ---------------- 批量改文件名 ----------------
 * ⚠️ 只改「待改的名字」，不碰磁盘 —— 真正改名仍由「开始重命名」统一下手，
 *    这样才能整体撤销。批量操作最怕一改就落盘、错了退不回来。
 */
let btOp = "prefix";

function batchTargets() {
  // 行程单的名字跟着发票走，不参与批量改名
  return S.rows.filter(r => r.checked && r.kind !== "trip").map(r => r.i);
}

function syncBatchFields() {
  const isRep = btOp === "replace";
  $("#btField2").hidden = !isRep;
  $("#btLabel1").textContent = isRep ? "要被替换掉的文字" : "要加上去的文字";
  $("#btText1").placeholder = isRep ? "例如：_2026"
    : (btOp === "suffix" ? "例如：_已核对" : "例如：2026年9月_");
}

function batchNewName(base) {
  const t1 = $("#btText1").value || "";
  const t2 = $("#btText2").value || "";
  if (btOp === "prefix") return t1 + base;
  if (btOp === "suffix") return base + t1;
  if (btOp === "replace") return t1 ? base.split(t1).join(t2) : base;
  return base;
}

function renderBatchPreview() {
  const idxs = batchTargets();
  const t1 = ($("#btText1").value || "").trim();
  $("#btGo").disabled = !t1;
  $("#btSum").textContent = idxs.length ? `共 ${idxs.length} 张` : "";
  if (!t1) {
    $("#btPreview").innerHTML =
      `<span class="old">填上文字后，这里会显示改完长什么样</span>`;
    return;
  }
  const head = idxs.slice(0, 4).map(i => {
    const r = S.rows[i];
    const ext = extOf(r.newName);
    const oldB = stripExt(r.newName);
    return `<div><span class="old">${esc(oldB)}${esc(ext)}</span>` +
      `<span class="arrow">→</span>` +
      `<span class="new">${esc(batchNewName(oldB))}${esc(ext)}</span></div>`;
  }).join("");
  const more = idxs.length > 4
    ? `<div class="old">…另有 ${idxs.length - 4} 张同样处理</div>` : "";
  $("#btPreview").innerHTML = head + more;
}

$("#btnBatchRename").onclick = () => {
  if (!batchTargets().length) { toast("先勾选要改名的发票", true); return; }
  btOp = "prefix";
  $("#btText1").value = "";
  $("#btText2").value = "";
  $("#btOp").querySelectorAll("span").forEach(s =>
    s.classList.toggle("on", s.dataset.v === "prefix"));
  syncBatchFields();
  renderBatchPreview();
  $("#btCount").textContent = batchTargets().length;
  openModal("#mBatch");
  setTimeout(() => { const i = $("#btText1"); if (i) i.focus(); }, 40);
};
$("#btOp").onclick = (e) => {
  const sp = e.target.closest("span[data-v]");
  if (!sp) return;
  btOp = sp.dataset.v;
  $("#btOp").querySelectorAll("span").forEach(s =>
    s.classList.toggle("on", s === sp));
  syncBatchFields();
  renderBatchPreview();
};
$("#btText1").addEventListener("input", renderBatchPreview);
$("#btText2").addEventListener("input", renderBatchPreview);
$("#btGo").onclick = async () => {
  const r = await call("rename_batch", batchTargets(), btOp,
                       $("#btText1").value, $("#btText2").value);
  if (!r || !r.ok) { toast((r && r.msg) || "应用失败", true); return; }
  if (r.snapshot) { S = r.snapshot; render(); }
  closeModal("#mBatch");
  toast(`改了 ${r.changed} 张的名字（还没落盘，点「开始重命名」才真正执行）`);
};

/* ---------------- 打包文件 ---------------- */
function packTargets() {
  return S.rows.filter(r => r.checked && r.ok && r.kind !== "trip")
    .map(r => r.i);
}

$("#btnPack").onclick = () => {
  const idxs = packTargets();
  if (!idxs.length) { toast("先勾选要打包的发票", true); return; }
  const want = [];
  idxs.forEach(i => {
    const r = S.rows[i];
    want.push({ name: stripExt(r.newName) + extOf(r.newName), trip: false });
    (r.attached || []).forEach(a => want.push({
      name: `${a.platform || "行程单"} 行程单 · ${a.trips || 0}趟`, trip: true }));
  });
  $("#pkList").innerHTML = want.map(w => `
    <div class="mgRow">
      <span class="mgNo">${w.trip ? "附" : "票"}</span>
      <span class="mgName" title="${esc(w.name)}">${esc(w.name)}</span>
    </div>`).join("");
  $("#pkSum").textContent = `共 ${want.length} 个文件`;
  const d = new Date();
  const ymd = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}` +
              `${String(d.getDate()).padStart(2, "0")}`;
  $("#pkName").value = `报销单_${ymd}`;
  $("#pkDir").value = S.folder || "";
  openModal("#mPack");
};
$("#pkPickDir").onclick = async () => {
  const r = await call("choose_merge_dir", $("#pkDir").value);
  if (r && r.ok) $("#pkDir").value = r.dir;
};
$("#pkGo").onclick = async () => {
  const withList = $("#pkWithList") ? $("#pkWithList").checked : true;
  const r = await call("pack_reimbursement", packTargets(),
                       $("#pkDir").value, $("#pkName").value, withList);
  if (!r || !r.ok) { toast((r && r.msg) || "无法开始打包", true); return; }
  closeModal("#mPack");
  setStatus("正在打包…");
  startPoll();
};

/* ---------------- 移除行 ----------------
 * 工具栏那个「移除勾选」按钮已撤掉（2026-10-17 业主要求）：
 * 为了删几行要「先勾选、再点按钮」太绕，改成每行末尾的垃圾桶（见 removeOneRow）。
 *
 * ⚠️ 原实现的注释留着，因为那条原则依然成立：
 *    只从**列表**里移除，绝不删磁盘文件；不然用户会以为文件被删了而不敢点。
 *    真要删文件得另做一个带二次确认的入口，不能藏在列表操作里。
 * 后端 remove_rows 的语义也还是「批量」，将来要做「多选删除」可以直接复用。
 */

/* ---------------- 键盘快捷键 ----------------
 * 几百张票时鼠标点来点去很累。
 * ⚠️ 只在「没在输入」且「没开弹窗」时生效 —— 否则在搜索框里打空格
 *    会把当前行勾掉，那是很恼人的 bug。
 */
document.addEventListener("keydown", (e) => {
  const t = e.target;
  const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
                       || t.isContentEditable);
  if (typing) return;
  const modalOpen = $$(".modal").some(m => !m.hidden);
  if (modalOpen) return;

  // v2.7 新增两个快捷键：Ctrl+Z 撤销上次、Delete 移除当前行（都不删磁盘文件）
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
    e.preventDefault();
    if (!busy) $("#btnUndo").click();
    return;
  }
  if (e.key === "Delete" && sel >= 0) {
    e.preventDefault();
    removeOneRow(sel);
    return;
  }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const rows = visibleRows();
    if (!rows.length) return;
    e.preventDefault();
    const cur = rows.findIndex(r => r.i === sel);
    const next = cur < 0 ? 0
      : (e.key === "ArrowDown" ? Math.min(rows.length - 1, cur + 1)
                               : Math.max(0, cur - 1));
    sel = rows[next].i;
    render();
    const el = document.querySelector(`#rows .row[data-i="${sel}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
    return;
  }
  if (e.key === " " && sel >= 0) {
    e.preventDefault();
    call("toggle_row", sel).then(st => { S = st; render(); });
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
    e.preventDefault(); $("#btnAll").click(); return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
    e.preventDefault();
    openSearch();          // 窄窗口时先把放大镜展开
    const i = $("#rowSearch");
    if (i) { i.focus(); i.select(); }
    return;
  }
  if (e.key === "Enter" && sel >= 0) {
    e.preventDefault(); $("#btnOpen").click();
    return;
  }
  if (e.key === "Escape" && sel >= 0) {
    // 取消选中 → 右栏回到「本批汇总」
    sel = -1;
    render();
  }
});

/* ---------------- 合并 PDF ----------------
 * 清单顺序 = 合并顺序，所以上下箭头是这块最核心的操作。
 * 只有 PDF 能合并，非 PDF 由后端挑出来（merge_preview 的 skipped），
 * 这里负责**明确告诉用户跳过了什么、为什么** —— 静默少文件最让人慌。
 */
let mgItems = [];        // [{i, file, name, pages}]，顺序即合并顺序
let mgSkipped = [];      // [{file, fmt}]

$("#btnMerge").onclick = async () => {
  if (busy) return;
  const r = await call("merge_preview");
  if (!r || !r.ok) { toast("读取待合并清单失败，请重试", true); return; }
  if (!r.items.length) { toast("没有勾选可合并的 PDF", true); return; }

  mgItems = r.items.slice();
  mgSkipped = r.skipped || [];
  $("#mgName").value = r.defaultName || "合并.pdf";
  $("#mgDir").value = r.defaultDir || "";
  renderMergeList();
  openModal("#mMerge");
  setTimeout(() => { const n = $("#mgName"); if (n) { n.focus(); n.select(); } }, 40);
};

function renderMergeList() {
  const rows = mgItems.map((it, n) => `
    <div class="mgRow">
      <span class="mgNo">${n + 1}</span>
      <span class="mgName" title="${esc(it.file)}">${esc(it.file)}</span>
      <span class="mgPg">${it.pages ? it.pages + " 页" : "—"}</span>
      <span class="mgActs">
        <i data-mv="up" data-n="${n}" class="${n === 0 ? "off" : ""}" title="上移">↑</i>
        <i data-mv="down" data-n="${n}" class="${n === mgItems.length - 1 ? "off" : ""}"
           title="下移">↓</i>
        <i data-mv="del" data-n="${n}" title="不合并这一份">✕</i>
      </span>
    </div>`).join("");

  const skips = mgSkipped.map(s => `
    <div class="mgRow skip">
      <span class="mgNo">—</span>
      <span class="mgName" title="${esc(s.file)}">${esc(s.file)}</span>
      <span class="mgSkipTag">${esc(s.fmt)} 无法合并</span>
    </div>`).join("");

  $("#mgList").innerHTML = rows + skips;

  const note = $("#mgNote");
  if (mgSkipped.length) {
    const fmts = [...new Set(mgSkipped.map(s => s.fmt))].join(" / ");
    note.hidden = false;
    note.innerHTML = `⚠ 有 <b>${mgSkipped.length}</b> 个非 PDF 文件被跳过（${esc(fmts)}）。
      OFD / XML 不能直接合并，需要先转成 PDF。`;
  } else {
    note.hidden = true;
  }

  const total = mgItems.reduce((a, x) => a + (x.pages || 0), 0);
  $("#mgSum").textContent = `共 ${mgItems.length} 个文件 · 预计 ${total} 页`;
  $("#mgGo").textContent = `开始合并（${mgItems.length} 个）`;
  $("#mgGo").disabled = mgItems.length === 0;
}

$("#mgList").onclick = (e) => {
  const el = e.target.closest("[data-mv]");
  if (!el || el.classList.contains("off")) return;      // 置灰的箭头点了不响应
  const n = +el.dataset.n, mv = el.dataset.mv;
  if (mv === "up" && n > 0) {
    [mgItems[n - 1], mgItems[n]] = [mgItems[n], mgItems[n - 1]];
  } else if (mv === "down" && n < mgItems.length - 1) {
    [mgItems[n + 1], mgItems[n]] = [mgItems[n], mgItems[n + 1]];
  } else if (mv === "del") {
    if (mgItems.length <= 1) { toast("至少要留一份 PDF", true); return; }
    mgItems.splice(n, 1);
  }
  renderMergeList();
};

$("#mgPickDir").onclick = async () => {
  const r = await call("choose_merge_dir", $("#mgDir").value);
  if (r && r.ok) $("#mgDir").value = r.dir;
  else if (r && r.msg && r.msg !== "已取消") toast(r.msg, true);
};

$("#mgGo").onclick = async () => {
  if (busy) return;
  const r = await call("merge_run",
    mgItems.map(x => x.i),          // 顺序即合并顺序
    $("#mgDir").value,
    $("#mgName").value,
    $("#mgBookmark").checked);
  if (!r || !r.ok) { toast((r && r.msg) || "无法开始合并", true); return; }
  closeModal("#mMerge");
  setStatus("正在合并…");
  startPoll();
};

/* ---------------- 启动 ---------------- */
ready(async () => {
  let err = "";
  try {
    S = await window.pywebview.api.get_state();
  } catch (e) {
    err = String(e);
  }
  if (!S) {
    // API 刚就绪时偶尔还没准备好，等一下再试一次
    await new Promise((r) => setTimeout(r, 600));
    try {
      S = await window.pywebview.api.get_state();
      err = "";
    } catch (e) {
      err = String(e);
    }
  }
  if (!S) {
    // 把真实错误显示在界面上，方便截图反馈（而不是一闪而过的提示）
    setStatus("初始化失败：" + (err || "后端无响应"));
    const box = $("#rows");
    if (box) {
      box.innerHTML = '<div class="empty"><h2>界面初始化失败</h2>' +
        '<p style="font-family:Consolas,monospace;font-size:12px;' +
        'max-width:640px;word-break:break-all;color:#D93A3F">' +
        (err || "后端无响应").replace(/[<>&]/g, "") + "</p></div>";
    }
    toast("界面初始化失败，请把界面上的错误信息发给我", true);
    return;
  }
  render();
  setStatus(S.rows.length ? "就绪" : "就绪：请先选择发票所在文件夹");

  // 上次用过同一个文件夹 → 自动扫描一次，省一步操作
  if (S.cfg.lastFolder && !S.rows.length) {
    const r = await call("rescan");
    if (r && r.ok) startPoll();
  }
});

/* ================================================================
   v2.6 新增：票面预览 / 重复票取舍台 / 操作历史 / 模板库 / 多票拆分 / 密码
   ================================================================ */

/* ---------------- 票面预览 ----------------
 * 右栏顶部显示这张票的**真实票面**，并把购买方 / 价税合计 / 票号
 * 在票面上的位置框出来。核对字段不用再开外部阅读器。
 * 坐标是 0~1 的相对值，所以窗口拉宽拉窄、图片缩放都不用重算。
 */
let pvRow = -1, pvPage = 0, pvPages = 1, pvSeq = 0;

function pvBoxHtml(r) {
  if (!r || r.kind === "trip" || !r.fmt) return "";
  const fmt = (r.fmt || "").toUpperCase();
  if (fmt !== "PDF") {
    return `<div class="pvBox">
      <div class="pvHead"><span class="pvName">票面预览</span></div>
      <div class="pvEmpty">${esc(fmt)} 格式没有可直接显示的页面，字段是从文件结构里读的。
        要核对票面请点下面的「打开 ${esc(fmt)}」。</div>
    </div>`;
  }
  if (r.needPassword) {
    return `<div class="pvBox">
      <div class="pvHead"><span class="pvName">票面预览</span></div>
      <div class="pvEmpty">这个 PDF 加密了，先输入密码才能显示票面。
        <div style="margin-top:8px">
          <button type="button" class="btn accent" data-pwd="${r.i}">输入密码</button>
        </div>
      </div>
    </div>`;
  }
  return `<div class="pvBox">
    <div class="pvHead">
      <span class="pvName">票面预览</span>
      <span class="pvNav">
        <button type="button" class="pvBtn" id="pvPrev" title="上一页" disabled>‹</button>
        <span id="pvPageTxt">1 / 1</span>
        <button type="button" class="pvBtn" id="pvNext" title="下一页" disabled>›</button>
      </span>
    </div>
    <div class="pvCanvas" id="pvCanvas"><div class="pvSpin">正在渲染票面…</div></div>
    <div class="pvLegend">
      <span><i></i>购买方</span><span><i class="g"></i>价税合计</span><span><i class="a"></i>发票号码</span>
      <span class="pvTip">点图可放大看清楚</span>
    </div>
  </div>`;
}

async function loadPreview(i, page) {
  const box = $("#pvCanvas");
  if (!box) return;
  pvRow = i;
  const mySeq = ++pvSeq;
  box.innerHTML = `<div class="pvSpin">正在渲染票面…</div>`;
  let r = null;
  try { r = await call("preview", i, page || 0); } catch (_) { r = null; }
  // 用户可能已经点到别的行了 —— 旧请求回来的结果必须丢掉，否则会串票
  if (mySeq !== pvSeq) return;
  const box2 = $("#pvCanvas");
  if (!box2) return;
  if (!r || !r.ok) {
    box2.innerHTML = `<div class="pvEmpty">${esc((r && r.msg) || "预览失败")}</div>`;
    return;
  }
  pvPage = r.page || 0;
  pvPages = r.pages || 1;
  const rects = r.rects || {};
  const hit = (k, cls) => {
    const q = rects[k];
    if (!q) return "";
    const l = (q[0] * 100).toFixed(2), t = (q[1] * 100).toFixed(2);
    const w = ((q[2] - q[0]) * 100).toFixed(2), h = ((q[3] - q[1]) * 100).toFixed(2);
    return `<span class="pvHit ${cls}" style="left:${l}%;top:${t}%;width:${w}%;height:${h}%"></span>`;
  };
  box2.innerHTML = `<img src="${r.image}" alt="票面">
    ${hit("buyer", "")}${hit("amount", "h-amount")}${hit("no", "h-no")}`;
  box2.classList.add("zoomable");
  const t = $("#pvPageTxt");
  if (t) t.textContent = `${pvPage + 1} / ${pvPages}`;
  const p = $("#pvPrev"), n = $("#pvNext");
  if (p) p.disabled = pvPage <= 0;
  if (n) n.disabled = pvPage >= pvPages - 1;
}

$("#dBody").addEventListener("click", (e) => {
  const nav = e.target.closest("#pvPrev, #pvNext");
  if (nav) {
    const to = pvPage + (nav.id === "pvNext" ? 1 : -1);
    loadPreview(pvRow, Math.max(0, Math.min(pvPages - 1, to)));
    return;
  }
  // 点小预览 → 打开全屏大图（小图只有 280px 宽，看不清是正常的）
  const cvz = e.target.closest(".pvCanvas");
  if (cvz && cvz.querySelector("img")) { openLightbox(pvRow, pvPage); return; }
  const pw = e.target.closest("[data-pwd]");
  if (pw) openPwd(+pw.dataset.pwd);
  const dp = e.target.closest("[data-dup]");
  if (dp) openDup(+dp.dataset.dup);
});

/* ---------------- 重复票取舍台 ----------------
 * 同票号几份并排摆出来（格式/大小/时间/缩略图），选一份保留，其余从列表移除。
 * 以前只能靠颜色看出来"重复了"，但看不出"哪个是 PDF、哪个更新"。
 */
let dupState = { gid: 0, keep: -1, groups: [] };

async function openDup(gid) {
  const r = await call("dup_overview");
  if (!r || !r.ok) { toast((r && r.msg) || "打不开取舍台", true); return; }
  if (!r.groups.length) { toast("本批没有重复票号", true); return; }
  const g = r.groups.find(x => x.gid === gid) || r.groups[0];
  dupState = { gid: g.gid, keep: g.suggest, groups: r.groups };
  renderDup();
  $("#mDup").hidden = false;
}

function renderDup() {
  const g = dupState.groups.find(x => x.gid === dupState.gid);
  if (!g) return;
  $("#dupTitle").textContent = `这 ${g.total} 份是同一张发票，留哪一份？`;
  $("#dupSub").textContent =
    `票号 ${g.no}　${g.items[0].buyer || ""}　¥ ${g.items[0].amountText}`;
  $("#dupCards").innerHTML = g.items.map(x => `
    <div class="dupCard ${x.i === dupState.keep ? "on" : ""}" data-keep="${x.i}">
      <div class="dcTop">
        <span class="fmt f-${esc((x.fmt || "").toLowerCase())}">${esc(x.fmt)}</span>
        ${x.i === g.suggest ? `<span class="dcSug">推荐</span>` : ""}
      </div>
      <div class="dcThumb" data-thumb="${x.i}"><div class="dcNo">缩略图加载中…</div></div>
      <div class="dcName" title="${esc(x.file)}">${esc(x.file)}</div>
      <div class="dcMeta">${esc(x.sizeText)}　${esc(x.mtimeText)}</div>
      <div class="dcPick">${x.i === dupState.keep ? "保留这份" : "选这份"}</div>
    </div>`).join("");
  $("#dupNote").textContent = `另外 ${g.total - 1} 份会从列表移除，磁盘文件不动`;
  g.items.forEach(x => loadDupThumb(x));
}

async function loadDupThumb(x) {
  const box = document.querySelector(`[data-thumb="${x.i}"]`);
  if (!box) return;
  if ((x.fmt || "").toUpperCase() !== "PDF") {
    box.innerHTML = `<div class="dcNo">${esc(x.fmt)}<br>无页面预览</div>`;
    return;
  }
  const r = await call("preview", x.i, 0);
  const box2 = document.querySelector(`[data-thumb="${x.i}"]`);
  if (!box2) return;
  if (!r || !r.ok) {
    box2.innerHTML = `<div class="dcNo">${esc((r && r.msg) || "无预览")}</div>`;
    return;
  }
  box2.innerHTML = `<img src="${r.image}" alt="">`;
}

$("#dupCards").addEventListener("click", (e) => {
  const c = e.target.closest("[data-keep]");
  if (!c) return;
  dupState.keep = +c.dataset.keep;
  // 只切 class，不重绘整个列表 —— 重绘会让缩略图重新加载、闪一下
  $$("#dupCards .dupCard").forEach(el => {
    const on = +el.dataset.keep === dupState.keep;
    el.classList.toggle("on", on);
    const p = el.querySelector(".dcPick");
    if (p) p.textContent = on ? "保留这份" : "选这份";
  });
});

$("#dupGo").onclick = async () => {
  const g = dupState.groups.find(x => x.gid === dupState.gid);
  if (!g || dupState.keep < 0) { toast("先选一份要保留的", true); return; }
  const r = await call("keep_one", dupState.gid, dupState.keep);
  if (!r || !r.ok) { toast((r && r.msg) || "处理失败", true); return; }
  if (r.snapshot) S = r.snapshot;
  $("#mDup").hidden = true;
  if (sel >= S.rows.length) sel = -1;
  render();
  toast(`已保留 1 份，其余 ${r.removed} 份从列表移除（文件没删）`);
};

/* ---------------- 操作历史 / 按批撤销 ---------------- */
async function openHistory() {
  const r = await call("history");
  const groups = (r && r.groups) || [];
  $("#hisList").innerHTML = groups.length ? groups.map(g => `
    <div class="hisRow ${g.undoable ? "" : "done"}">
      <span class="ht">${esc(g.at || "—")}</span>
      <span class="hl">${esc(g.label)}</span>
      <span class="hn">${g.undoable ? `可撤销 ${g.undoable}` : "已撤销"}</span>
      ${g.undoable ? `<button class="btn" data-undo="${esc(g.batch)}">撤销这批</button>` : ""}
    </div>`).join("") : `<div class="hisRow done"><span class="hl">还没有改名记录</span></div>`;
  // 跨批次台账（v2.7）
  try {
    const st = await call("history_stats");
    const t = $("#hisMemTxt");
    if (t) t.textContent = `跨批次记忆：${(st && st.count) || 0} 张票（用于提醒「以前处理过」）`;
  } catch (_) { /* 没有就算了，不影响历史列表 */ }
  $("#mHistory").hidden = false;
}

// 清空跨批次记忆（v2.8.8：事件委托 + 清完立刻重绘弹窗）
document.addEventListener("click", async (e) => {
  if (!e.target.closest("#hisMemClear")) return;
  const ok = await askConfirm({
    title: "清空跨批次记忆？",
    text: "清空后，程序不再记得「哪些票以前处理过」，也就不会提示重复报销。",
    note: "只清这份记忆，不动发票文件，也不影响上面的操作历史。",
    okText: "清空",
    danger: true,
  });
  if (!ok) return;
  const r = await call("clear_history_index");
  if (!r || !r.ok) { toast((r && r.msg) || "清空失败", true); return; }
  await openHistory();          // 重新拉一次：弹窗不关也能看到计数变成 0
  toast(r.msg || "已清空");
  refreshUndoBtn();
});

$("#hisList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-undo]");
  if (!b) return;
  const batch = b.dataset.undo;
  const ok = await askConfirm({
    title: "撤销这一批？",
    text: "只回滚**这一批**改过的文件名，其它批次不受影响。",
    note: "还原是把文件改回原名。如果原位置已经被别的文件占了，那一条会跳过。",
    okText: "撤销这批",
  });
  if (!ok) return;
  const r = await call("undo_batch", batch);
  if (!r || r.ok === false) { toast((r && r.msg) || "撤销失败", true); return; }
  $("#mHistory").hidden = true;
  setStatus("正在还原…");
  startPoll();
});

$("#btnHistory").onclick = () => { if (!busy) openHistory(); };

/* ---------------- 加密 PDF 密码 ---------------- */
let pwdIdx = -1;

function openPwd(i) {
  const r = S.rows[i];
  if (!r) return;
  pwdIdx = i;
  $("#pwdFile").textContent = r.file;
  $("#pwdInput").value = "";
  $("#mPwd").hidden = false;
  setTimeout(() => { const el = $("#pwdInput"); if (el) el.focus(); }, 60);
}

async function applyPwd() {
  if (pwdIdx < 0) return;
  const v = $("#pwdInput").value;
  if (!v) { toast("请输入密码", true); return; }
  const r = await call("set_pdf_password", pwdIdx, v);
  if (!r || !r.ok) {
    toast((r && r.msg) || "密码不对", true);
    const el = $("#pwdInput");
    if (el) { el.focus(); el.select(); }
    return;
  }
  if (r.snapshot) S = r.snapshot;
  $("#mPwd").hidden = true;
  render();
  toast("密码正确，已经重新读出来了");
}

$("#pwdGo").onclick = applyPwd;
$("#pwdInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); applyPwd(); }
});

/* ---------------- 多票 PDF 拆分 ---------------- */
let splitIdx = -1;

function openSplit(i) {
  const r = S.rows[i];
  if (!r || !r.multi) { toast("这个文件里只有一张票，不用拆", true); return; }
  splitIdx = i;
  const gs = r.multi.groups || [];
  $("#splitSub").textContent = `${r.file}　共 ${r.multi.count} 张发票`;
  $("#splitList").innerHTML = gs.map(g => {
    const pages = (g.pages || []).map(p => p + 1).join("、");
    const amt = (g.amount === null || g.amount === undefined)
      ? "—" : "¥ " + fmtMoney(Math.abs(g.amount));
    return `<div class="splitRow">
      <span class="sp">第 ${pages} 页</span>
      <span class="sn">${esc(g.buyer || "（未识别）")}</span>
      <span class="sa">${amt}</span>
    </div>`;
  }).join("");
  $("#mSplit").hidden = false;
}

$("#splitGo").onclick = async () => {
  if (splitIdx < 0) return;
  $("#mSplit").hidden = true;
  const r = await call("split_multi", splitIdx);
  if (!r || !r.ok) { toast((r && r.msg) || "拆分失败", true); return; }
  setStatus("正在拆分…");
  startPoll();
};

/* ---------------- 命名模板库（v2.6） ----------------
 * 做成**弹窗**而不是塞进规则面板：面板只有 396px 宽，再往里加两行控件，
 * 13 个字段就会被挤成滚动条，最下面几行点不到（实测踩过）。
 */
function tplDesc(t) {
  const names = (t.fields || []).map(k => (fieldDef(k) || {}).label || k);
  const sep = t.separator === "" ? "" : (" " + t.separator + " ");
  return names.join(sep);
}

function renderTplList() {
  const box = $("#tplList");
  if (!box) return;
  const tpls = (S.cfg && S.cfg.templates) || [];
  box.innerHTML = tpls.length ? tpls.map(t => `
    <div class="tplRow">
      <span class="tn">${esc(t.name)}</span>
      <span class="td" title="${esc(tplDesc(t))}">${esc(tplDesc(t))}</span>
      <button type="button" class="btn tiny" data-apply="${esc(t.id)}">套用</button>
      <button type="button" class="btn tiny ghost" data-del="${esc(t.id)}">删除</button>
    </div>`).join("")
    : `<div class="tplRow"><span class="tn" style="color:var(--ink-3)">还没有保存的模板</span></div>`;
}

function openTplLib() {
  renderTplList();
  $("#mTpl").hidden = false;
}

$("#btnTplLib").onclick = (e) => { e.stopPropagation(); openTplLib(); };

$("#tplList").addEventListener("click", async (e) => {
  const ap = e.target.closest("[data-apply]");
  if (ap) {
    const r = await call("apply_template", ap.dataset.apply);
    if (!r || !r.ok) { toast((r && r.msg) || "套用失败", true); return; }
    if (r.snapshot) S = r.snapshot;
    ruleFields = (S.cfg.fields || []).slice();
    ruleSep = S.cfg.separator;
    buildRulePanel();
    updateRulePreview();
    renderTplList();
    toast("已套用模板");
    return;
  }
  const del = e.target.closest("[data-del]");
  if (del) {
    const ok = await askConfirm({
      title: "删除这套模板？",
      text: "只是删掉保存的这套规则，不影响已经改过的文件名。",
      okText: "删除",
    });
    if (!ok) return;
    const r = await call("delete_template", del.dataset.del);
    if (!r || !r.ok) { toast("删除失败", true); return; }
    S.cfg.templates = r.templates;
    renderTplList();
    toast("已删除");
  }
});

$("#tplSave").onclick = async () => {
  const name = ($("#tplName").value || "").trim();
  if (!name) { toast("先给这套规则起个名字", true); $("#tplName").focus(); return; }
  const r = await call("save_template", name);
  if (!r || !r.ok) { toast("保存失败", true); return; }
  S.cfg.templates = r.templates;
  $("#tplName").value = "";
  renderTplList();
  toast(`已保存模板「${name}」`);
};

/* ---------------- 票面大图（v2.6.1） ----------------
 * 列表右栏只有 280px 宽，数电票又是横版，小图根本看不清字。
 * 点预览图打开这里：1500px 高清渲染 + 滚轮缩放 + 拖动 + 翻页，
 * 三个字段框跟着一起放大，核对起来才叫核对。
 */
let lbSeq = 0;
let lbState = { i: -1, page: 0, pages: 1, zoom: 1, x: 0, y: 0,
                W: 0, H: 0, img: "", rects: {}, drag: null };

function openLightbox(i, page) {
  const row = S.rows[i];
  if (!row) return;
  lbState.i = i;
  lbState.page = page || 0;
  $("#lbName").textContent = row.file || "";
  $("#mPv").hidden = false;
  loadLightbox(i, lbState.page);
}

async function loadLightbox(i, page) {
  const stage = $("#lbStage");
  if (!stage) return;
  const my = ++lbSeq;
  lbState.page = page;
  // 加载中/失败时把 stage 撑满窗口再居中提示文字（票面定位阶段 stage 必须贴左上角，见 style.css 注释）
  stage.style.cssText = "width:100%;height:100%;display:flex;align-items:center;justify-content:center";
  stage.innerHTML = `<div class="pvSpin" style="padding:60px">正在渲染大图…</div>`;
  $("#lbPageTxt").textContent = `${page + 1} / ${lbState.pages}`;
  let r = null;
  try { r = await call("preview", i, page, 1500); } catch (_) { r = null; }
  if (my !== lbSeq) return;              // 已经切页了，旧结果丢掉
  const stage2 = $("#lbStage");
  if (!stage2) return;
  if (!r || !r.ok) {
    stage2.innerHTML = `<div class="pvEmpty" style="padding:40px;text-align:center;line-height:1.8">${esc((r && r.msg) || "预览失败")}</div>`;
    return;
  }
  lbState.W = r.width || 1;
  lbState.H = r.height || 1;
  lbState.img = r.image;
  lbState.rects = r.rects || {};
  lbState.pages = r.pages || 1;
  lbState.zoom = 1; lbState.x = 0; lbState.y = 0;
  renderLightStage();
  fitLightbox();
  $("#lbPageTxt").textContent = `${lbState.page + 1} / ${lbState.pages}`;
}

function lbHit(k, cls) {
  const q = lbState.rects[k];
  if (!q) return "";
  const l = (q[0] * 100).toFixed(2), t = (q[1] * 100).toFixed(2);
  const w = ((q[2] - q[0]) * 100).toFixed(2), h = ((q[3] - q[1]) * 100).toFixed(2);
  return `<span class="pvHit ${cls}" style="left:${l}%;top:${t}%;width:${w}%;height:${h}%"></span>`;
}

function renderLightStage() {
  const stage = $("#lbStage");
  if (!stage) return;
  stage.style.cssText = "";      // 清掉加载态的「撑满+flex 居中」
  stage.style.width = lbState.W + "px";
  stage.style.height = lbState.H + "px";
  stage.innerHTML = `<img src="${lbState.img}" alt="票面大图">
    ${lbHit("buyer", "")}${lbHit("amount", "h-amount")}${lbHit("no", "h-no")}`;
  applyLb();
}

function applyLb() {
  const stage = $("#lbStage");
  if (!stage) return;
  stage.style.transform = `translate(${lbState.x}px, ${lbState.y}px) scale(${lbState.zoom})`;
  const z = $("#lbZoomTxt");
  if (z) z.textContent = Math.round(lbState.zoom * 100) + "%";
}

function lbCenter() {
  const vp = $("#lbViewport");
  if (!vp) return;
  lbState.x = (vp.clientWidth - lbState.W * lbState.zoom) / 2;
  lbState.y = (vp.clientHeight - lbState.H * lbState.zoom) / 2;
  applyLb();
}

function fitLightbox() {
  const vp = $("#lbViewport");
  if (!vp) return;
  const vw = vp.clientWidth - 48, vh = vp.clientHeight - 48;
  if (vw <= 0 || vh <= 0) return;
  lbState.zoom = Math.max(0.1, Math.min(vw / lbState.W, vh / lbState.H, 1));
  lbCenter();
}

function lbZoomAt(cx, cy, factor) {
  const vp = $("#lbViewport");
  if (!vp) return;
  const box = vp.getBoundingClientRect();
  const mx = cx - box.left, my = cy - box.top;
  // 让光标下的那一点保持在原地 —— 缩放才不会"飘"
  const nx = (mx - lbState.x) / lbState.zoom;
  const ny = (my - lbState.y) / lbState.zoom;
  lbState.zoom = Math.max(0.15, Math.min(6, lbState.zoom * factor));
  lbState.x = mx - nx * lbState.zoom;
  lbState.y = my - ny * lbState.zoom;
  applyLb();
}

const lbVp = $("#lbViewport");
if (lbVp) {
  lbVp.addEventListener("pointerdown", (e) => {
    lbState.drag = { sx: e.clientX, sy: e.clientY, ox: lbState.x, oy: lbState.y };
    lbVp.classList.add("dragging");
    try { lbVp.setPointerCapture(e.pointerId); } catch (_) {}
  });
  lbVp.addEventListener("pointermove", (e) => {
    if (!lbState.drag) return;
    lbState.x = lbState.drag.ox + (e.clientX - lbState.drag.sx);
    lbState.y = lbState.drag.oy + (e.clientY - lbState.drag.sy);
    applyLb();
  });
  const endDrag = () => { lbState.drag = null; lbVp.classList.remove("dragging"); };
  lbVp.addEventListener("pointerup", endDrag);
  lbVp.addEventListener("pointercancel", endDrag);
  lbVp.addEventListener("wheel", (e) => {
    e.preventDefault();
    lbZoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });
}

$("#lbZoomIn").onclick = () => {
  const vp = $("#lbViewport");
  if (vp) lbZoomAt(vp.getBoundingClientRect().left + vp.clientWidth / 2,
                   vp.getBoundingClientRect().top + vp.clientHeight / 2, 1.25);
};
$("#lbZoomOut").onclick = () => {
  const vp = $("#lbViewport");
  if (vp) lbZoomAt(vp.getBoundingClientRect().left + vp.clientWidth / 2,
                   vp.getBoundingClientRect().top + vp.clientHeight / 2, 1 / 1.25);
};
$("#lbFit").onclick = fitLightbox;
$("#lbOne").onclick = () => { lbState.zoom = 1; lbCenter(); };
$("#lbClose").onclick = () => { $("#mPv").hidden = true; };

$("#lbPrev").onclick = () => {
  if (lbState.page > 0) loadLightbox(lbState.i, lbState.page - 1);
};
$("#lbNext").onclick = () => {
  if (lbState.page < lbState.pages - 1) loadLightbox(lbState.i, lbState.page + 1);
};

/* =========================================================
   v2.7：界面偏好（折叠 / 排序）+ 新版点击
   ========================================================= */

let viewInit = false;

/** 第一次拿到状态时，把上次记住的排序恢复回来（之后以用户当前操作为准） */
function syncViewFromCfg() {
  if (viewInit || !S) return;
  viewInit = true;
  const v = (S.cfg && S.cfg.view) || {};
  if (["file", "amount", "date", "buyer"].includes(v.sort)) sortBy = v.sort;
  const el = $("#sortSel");
  if (el) el.value = sortBy;
}

async function saveView(patch) {
  try {
    const r = await call("save_view", patch);
    if (r && r.ok && S) S.cfg.view = r.view;
  } catch (_) { /* 记不住不影响使用，不打扰用户 */ }
}

// 排序
const sortEl = $("#sortSel");
if (sortEl) {
  sortEl.onchange = () => {
    sortBy = sortEl.value || "file";
    renderRows();
    saveView({ sort: sortBy });
  };
}

// 跨批次预警开关
const swCrossEl = $("#swCross");
if (swCrossEl) {
  swCrossEl.onchange = async () => {
    S = await call("set_rule", "crossCheck", swCrossEl.checked);
    render();
    toast(swCrossEl.checked ? "已打开重复报销预警" : "已关闭重复报销预警");
  };
}

/* =========================================================
   v2.7.1：规则卡右侧的「时间 / 日期」
   ---------------------------------------------------------
   业主 2026-10-18 定稿：去掉原来的「当前规则」两行灰字，换成时间。
   格式固定为「上面 时:分、下面 年月日 + 星期」，**不显示秒**（每秒跳会分心）。
   每 10 秒刷一次就够：分钟最多晚 10 秒更新，肉眼看不出来，也不占 CPU。
   ========================================================= */
const WEEK_CN = ["日", "一", "二", "三", "四", "五", "六"];

function pad2(n) { return String(n).padStart(2, "0"); }

/** ISO 8601 周次（周四所在的周算第 1 周），和日历软件口径一致 */
function isoWeekNo(d) {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;            // 周日 0 → 当 7 处理
  t.setUTCDate(t.getUTCDate() + 4 - day);    // 挪到本周四
  const yearStart = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return Math.ceil(((t - yearStart) / 86400000 + 1) / 7);
}

function renderClock() {
  const d = new Date();
  const t = $("#rcTime");
  if (t) t.textContent = pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  const dd = $("#rcDate");
  if (dd) {
    dd.textContent = d.getFullYear() + "年" + pad2(d.getMonth() + 1) + "月"
      + pad2(d.getDate()) + "日";
  }
  const w = $("#rcWeek");
  if (w) {
    w.textContent = "星期" + WEEK_CN[d.getDay()] + " · 第 " + isoWeekNo(d) + " 周";
  }
}

if ($("#ruleClock")) {
  renderClock();
  setInterval(renderClock, 10000);
}


/* =========================================================
   v2.8.3：顶栏「⋯」菜单
   ---------------------------------------------------------
   「添加文件 / 重新识别」没有删，只是隐藏了原件（#btnAddFile / #btnRescan），
   菜单项点的是它们的 click() —— 已有的禁用逻辑、对话框、提示全都不用重写。
   ========================================================= */
(function initTopMore() {
  const btn = $("#btnMore");
  const pop = $("#morePop");
  if (!btn || !pop) return;

  const syncState = () => {
    const re = $("#morePop [data-more='re']");
    const add = $("#morePop [data-more='add']");
    if (re) re.classList.toggle("off", !!$("#btnRescan").disabled);
    if (add) add.classList.toggle("off", !!$("#btnAddFile").disabled);
  };

  btn.onclick = (e) => {
    e.stopPropagation();
    syncState();
    pop.hidden = !pop.hidden;
  };
  $$("#morePop [data-more]").forEach((el) => {
    el.onclick = () => {
      pop.hidden = true;
      const kind = el.dataset.more;
      // 「检查更新 / 导出诊断包」由 initInsights 绑定，别误当成"重新识别"
      if (kind !== "add" && kind !== "re") return;
      const src = kind === "add" ? $("#btnAddFile") : $("#btnRescan");
      if (!src || src.disabled) {
        toast("现在还用不了：正在处理上一批，请稍候", true);
        return;
      }
      src.click();
    };
  });
  document.addEventListener("click", (e) => {
    if (!pop.hidden && !e.target.closest("#topMore")) pop.hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") pop.hidden = true;
  });
})();

/* =========================================================
   v2.8.0：自定义标题栏（系统标题栏已去掉，按钮由界面自己画）
   ---------------------------------------------------------
   拖窗口不是用 JS 逐帧 move —— 那样会抖。
   按下时把鼠标"交给" Windows（WM_NCLBUTTONDOWN + HTCAPTION），
   之后移动、贴边分屏、拖到顶部最大化全由系统处理，跟原生一模一样。
   ========================================================= */
function setWinMax(on) {
  document.body.classList.toggle("win-max", !!on);
  const b = $("#winMax");
  if (b) {
    b.title = on ? "还原" : "最大化";
    b.setAttribute("aria-label", b.title);
  }
}
window.__setWinMax = setWinMax;

(function initWindowChrome() {
  const bar = $("#topbar");
  const btnMin = $("#winMin");
  const btnMax = $("#winMax");
  const btnClose = $("#winClose");
  if (!bar || !btnMin) return;

  // ⚠️ 这段必须等 pywebview 的 JS API 就绪（ready 回调里）再调，
  //    否则 call() 会拿到 null，被误判成"原生标题栏模式"，窗口按钮就整组藏起来了。
  ready(() => {
    call("win_meta").then((r) => {
      const custom = !!(r && r.ok && r.custom);
      document.body.classList.toggle("native-frame", !custom);
      if (!custom) return;               // 回了系统标题栏：什么都不用绑
      call("win_is_max").then((m) => { if (m && m.ok) setWinMax(m.max); });
    });
  });

  btnMin.onclick = () => call("win_minimize");
  btnMax.onclick = () => call("win_toggle_max")
    .then((r) => { if (r && r.ok) setWinMax(r.max); });
  btnClose.onclick = () => call("win_close");

  const isChrome = (el) => !!(el && el.closest(
    "button, input, select, a, .tg, .winbtns, .searchWrap"));

  // 拖窗口：必须"真的拖动超过 5px"才交给系统。
  // ⚠️ 早先直接在 mousedown 里发 WM_NCLBUTTONDOWN，快速单击时消息可能在
  //    鼠标松开之后才到，系统就进入"等待拖动"状态 —— 下一次点界面，
  //    窗口会跟着鼠标跳一下（业主报的"单击软件会自动往下挪"就是这个）。
  let dragFrom = null;
  bar.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || isChrome(e.target)) return;
    dragFrom = { x: e.clientX, y: e.clientY };
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragFrom) return;
    if (Math.abs(e.clientX - dragFrom.x) + Math.abs(e.clientY - dragFrom.y) < 5) return;
    dragFrom = null;
    call("win_drag");                    // 不 await：SendMessage 会阻塞到松手
  });
  window.addEventListener("mouseup", () => { dragFrom = null; });
  window.addEventListener("blur", () => { dragFrom = null; });

  // 双击 = 最大化 / 还原（系统一般已经处理了，这里兜底）
  bar.addEventListener("dblclick", (e) => {
    if (isChrome(e.target)) return;
    btnMax.click();
  });
})();

/* =========================================================
   v2.8.7：窄窗口「批量 ▾」菜单 + 键盘快捷键
   ========================================================= */
(function initToolbarMore() {
  const btn = $("#btnTbMore");
  const pop = $("#tbMorePop");
  if (!btn || !pop) return;

  const MAP = { none: "#btnNone", onlyok: "#btnOnlyOk",
                rename: "#btnBatchRename", del: "#btnDelSel" };

  const sync = () => {
    ["rename", "del"].forEach((k) => {
      const item = pop.querySelector(`[data-act="${k}"]`);
      const src = $(MAP[k]);
      if (item && src) item.classList.toggle("off", !!src.disabled);
    });
  };

  btn.onclick = (e) => {
    e.stopPropagation();
    sync();
    pop.hidden = !pop.hidden;
  };
  pop.querySelectorAll("[data-act]").forEach((el) => {
    el.onclick = () => {
      pop.hidden = true;
      const src = $(MAP[el.dataset.act]);
      if (src && !src.disabled) src.click();
    };
  });
  document.addEventListener("click", (e) => {
    if (!pop.hidden && !e.target.closest("#tbMore")) pop.hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") pop.hidden = true;
  });
})();

/* ---------------------------------------------------------
   v2.8.7：F2 手动改名
   ---------------------------------------------------------
   ⚠️ 不要在这里再写 Ctrl+Z / Delete / ↑↓ / Ctrl+F ——
      下面（原 v2.7 的 keydown）已经有一整套，重复绑会一次按键触发两次
      （实测：Delete 会同时移除一行并弹出批量删除确认）。
      这里只补一个原本没有的 F2。
   --------------------------------------------------------- */
(function initExtraShortcuts() {
  document.addEventListener("keydown", (e) => {
    if (e.key !== "F2") return;
    const t = e.target;
    const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
                         || t.isContentEditable);
    if (typing) return;
    if ($$(".modal").some((m) => !m.hidden)) return;
    const b = $("#btnRename");
    if (b && !b.disabled) {
      e.preventDefault();
      b.click();
    }
  });
})();

/* =========================================================
   v2.8.7：识别体检 / 汇总报表 / 检查更新 / 诊断包
   ========================================================= */
(function initInsights() {
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  /* ---------------- 识别体检 ---------------- */
  function openHealth() {
    const rows = (S && S.rows) || [];
    if (!rows.length) { toast("先扫描一批发票再看体检"); return; }

    const by = new Map();
    rows.forEach((r) => {
      const t = (r.type || "").trim() || "未识别票种";
      const o = by.get(t) || { t, n: 0, buyer: 0, amount: 0, remark: 0, bad: [] };
      o.n += 1;
      if (r.buyer) o.buyer += 1;
      if (r.amount != null) o.amount += 1;
      if (r.remark) o.remark += 1;
      if (!r.buyer || r.amount == null) {
        const miss = [];
        if (!r.buyer) miss.push("购买方");
        if (r.amount == null) miss.push("金额");
        o.bad.push({ i: r.i, file: r.fileName || r.path, miss });
      }
      by.set(t, o);
    });

    const list = [...by.values()].sort((a, b) => b.n - a.n);
    const cell = (ok, n) => `<span class="${ok === n ? "all" : "miss"}">${ok}/${n}</span>`;

    let html = `<div class="hlWrap"><table class="hlTable">
      <tr><th>票种</th><th>张数</th><th>购买方</th><th>金额</th><th>备注</th><th></th></tr>`;
    list.forEach((o) => {
      html += `<tr>
        <td>${esc(o.t)}</td>
        <td class="num">${o.n}</td>
        <td class="num">${cell(o.buyer, o.n)}</td>
        <td class="num">${cell(o.amount, o.n)}</td>
        <td class="num">${o.remark}/${o.n}</td>
        <td>${o.bad.length
          ? `<button class="hlLink" data-goto="${o.bad[0].i}">看第 ${o.bad[0].i + 1} 行</button>`
          : `<span style="color:#98A1B2;font-size:11px">全部读到</span>`}</td>
      </tr>`;
    });
    html += `</table></div>`;

    const badAll = list.reduce((a, o) => a.concat(o.bad), []);
    if (badAll.length) {
      html += `<div style="margin-top:14px">
        <div style="font-size:11.5px;color:#5A6475;margin-bottom:6px">
          有 ${badAll.length} 张没读到关键字段（点一行可定位）：</div>
        <div style="max-height:180px;overflow:auto;border:1px solid #EEF1F6;border-radius:8px">`;
      badAll.slice(0, 50).forEach((b) => {
        html += `<div class="hlBadRow" data-goto="${b.i}"
          style="padding:6px 10px;border-bottom:1px solid #F5F7FA;font-size:11px;cursor:pointer">
          <b>${esc(b.file).slice(0, 46)}</b>
          <span style="color:#D93A3F">　缺：${b.miss.join("、")}</span></div>`;
      });
      html += `</div></div>`;
    }
    $("#healthBody").innerHTML = html;
    $("#mHealth").hidden = false;

    $$("#healthBody [data-goto]").forEach((el) => {
      el.onclick = () => {
        const i = +el.dataset.goto;
        $("#mHealth").hidden = true;
        const row = document.querySelector(`#rows .row[data-i="${i}"]`);
        if (row) { row.click(); row.scrollIntoView({ block: "center" }); }
      };
    });
  }

  /* ---------------- 汇总报表 ---------------- */
  const money = (n) => "¥" + (Number(n) || 0).toLocaleString("zh-CN",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function bars(items, total, unit) {
    if (!items.length) return `<div style="color:#98A1B2;font-size:11.5px">暂无数据</div>`;
    const max = Math.max(...items.map((x) => x.v));
    return items.map((x) => `<div class="repRow">
      <span class="repName" title="${esc(x.k)}">${esc(x.k)}</span>
      <span class="repBar"><i style="width:${max ? Math.round(x.v / max * 100) : 0}%"></i></span>
      <span class="repVal">${unit === "¥" ? money(x.v) : x.v + " 张"}</span>
    </div>`).join("");
  }

  function openReport() {
    const rows = (S && S.rows) || [];
    if (!rows.length) { toast("先扫描一批发票再看报表"); return; }

    const withAmt = rows.filter((r) => r.amount != null);
    const total = withAmt.reduce((a, r) => a + r.amount, 0);

    const byBuyer = new Map();
    const byMonth = new Map();
    withAmt.forEach((r) => {
      const k = r.buyer || "（未识别购买方）";
      byBuyer.set(k, (byBuyer.get(k) || 0) + r.amount);
      const d = String(r.date || "");
      const m = /^\d{8}$/.test(d) ? d.slice(0, 4) + "年" + d.slice(4, 6) + "月" : "日期未知";
      byMonth.set(m, (byMonth.get(m) || 0) + r.amount);
    });
    const topBuyer = [...byBuyer.entries()].map(([k, v]) => ({ k, v }))
      .sort((a, b) => b.v - a.v).slice(0, 8);
    const months = [...byMonth.entries()].map(([k, v]) => ({ k, v }))
      .sort((a, b) => a.k.localeCompare(b.k));

    $("#reportBody").innerHTML = `
      <div style="display:flex;gap:22px;margin-bottom:16px">
        <div><div style="font-size:10.5px;color:#98A1B2">本批张数</div>
          <div style="font-size:22px;font-family:var(--disp)">${rows.length}</div></div>
        <div><div style="font-size:10.5px;color:#98A1B2">价税合计</div>
          <div style="font-size:22px;font-family:var(--disp)">${money(total)}</div></div>
        <div><div style="font-size:10.5px;color:#98A1B2">平均每张</div>
          <div style="font-size:22px;font-family:var(--disp)">${money(withAmt.length ? total / withAmt.length : 0)}</div></div>
      </div>
      <div class="repGrid">
        <div class="repBlock"><h4>按客户（前 8）</h4>${bars(topBuyer, total, "¥")}</div>
        <div class="repBlock"><h4>按月</h4>${bars(months, total, "¥")}</div>
      </div>`;
    $("#reportHint").textContent = `合计 ${money(total)}，可打印成报销单`;
    $("#mReport").hidden = false;
  }

  /* ---------------- 打印报销单 ---------------- */
  async function printReport() {
    const rows = (S && S.rows) || [];
    const use = rows.filter((r) => r.amount != null);
    if (!use.length) { toast("没有可打印的发票"); return; }
    const total = use.reduce((a, r) => a + r.amount, 0);
    let cn = "";
    try {
      const r = await call("cn_amount", total);
      cn = (r && r.text) || "";
    } catch (_) {}

    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const body = use.map((r, k) => `<tr>
      <td>${k + 1}</td>
      <td>${r.date ? String(r.date).replace(/^(\d{4})(\d{2})(\d{2})$/, "$1-$2-$3") : ""}</td>
      <td>${esc(r.buyer || "")}</td>
      <td>${esc(r.no || "")}</td>
      <td class="n">${(r.amount || 0).toFixed(2)}</td>
    </tr>`).join("");

    $("#printArea").innerHTML = `
      <div class="prTitle">发票报销清单</div>
      <div class="prSub">打印日期 ${stamp}　·　共 ${use.length} 张</div>
      <table class="prTable">
        <tr><th style="width:44px">序号</th><th style="width:96px">开票日期</th>
            <th>购买方</th><th style="width:190px">发票号码</th>
            <th style="width:96px">金额</th></tr>
        ${body}
      </table>
      <div class="prFoot">
        <span>合计（小写）：¥ ${total.toFixed(2)}</span>
        <span>合计（大写）：${cn}</span>
      </div>
      <div class="prSign">
        <div>报销人签字</div><div>部门负责人</div><div>财务审核</div>
      </div>`;
    window.print();
  }

  /* ---------------- 检查更新 / 诊断包 ---------------- */
  async function checkUpdate() {
    toast("正在检查新版本…");
    let r = null;
    try { r = await call("check_update"); } catch (_) {}
    if (!r || !r.ok) {
      const msg = (r && r.msg) || "联网检查失败，可稍后再试";
      if (r && r.url) {
        if (confirm(msg + "\n\n要打开下载页面自己看吗？")) call("open_homepage", r.url);
      } else {
        toast(msg, true);
      }
      return;
    }
    if (!r.newer) { toast(`已是最新版（v${r.current}）`); return; }
    const go = confirm(`发现新版本 v${r.latest}（当前 v${r.current}）。\n\n` +
      `打开下载页面？（浏览器会跳到 GitHub Releases）`);
    if (go) {
      const res = await call("open_homepage");
      if (res && !res.ok) toast(res.msg || "打不开浏览器", true);
    }
  }

  async function exportDiag() {
    toast("正在生成诊断包…");
    let r = null;
    try { r = await call("export_diagnostics"); } catch (_) {}
    if (!r || !r.ok) { toast((r && r.msg) || "生成失败", true); return; }
    if (confirm(`诊断包已生成：\n${r.path}\n\n不含任何发票内容。\n要打开所在文件夹吗？`)) {
      call("reveal_file", r.path);
    }
  }

  const on = (id, fn) => {
    const el = $(id);
    if (el) el.addEventListener("click", fn);
  };
  const bind = () => {
    on("#btnHealth", openHealth);
    on("#btnReport", openReport);
    on("#btnPrintReport", printReport);
    on("[data-more='update']", checkUpdate);
    on("[data-more='diag']", exportDiag);
  };
  // ⚠️ 这几个按钮在弹窗里，弹窗写在 </body> 前、可能晚于本脚本执行 ——
  //    直接绑会绑到 null（实测：#btnPrintReport.onclick 一直是空）。
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
