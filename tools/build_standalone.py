"""Build the standalone (self-hosted) Walk-in Count page from countback.html.

The artifact version gets `window.claude` (AI + storage) from its host. The
standalone version supplies the same interface itself: the AI calls go to the
Anthropic API with the owner's key, and storage lives in this browser.
"""
import pathlib, sys

here = pathlib.Path(__file__).parent
src = (here / "source.html").read_text()
out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else here.parent / "index.html"

# Official SDK 0.134.0, bundled with esbuild into one browser script so the page has no CDN dependency.
SDK_JS = (here / "anthropic-sdk.min.js").read_text()

shim = r"""
<script>
// Standalone runtime: the same `claude.use()` surface the page expects.
window.claude = (() => {
  const KEY = "wc.apiKey", DBK = "wc.db", MODEL = "claude-opus-5-5";
  const ls = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  };

  // ---- AI ----
  let SDK = null;
  async function client() {
    const apiKey = (ls.get(KEY) || "").trim();
    if (!apiKey) throw { code: "no_key", message: "No API key set" };
    SDK = SDK || window.AnthropicSDK.default;
    return new SDK({ apiKey, dangerouslyAllowBrowser: true });
  }
  const toB64 = (blob) => new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1]);
    r.onerror = rej;
    r.readAsDataURL(blob);
  });
  function parseJson(text) {
    try { return JSON.parse(text); } catch {}
    const fence = text.match(/```(?:json)?\s*([\s\S]*?)```/);
    if (fence) { try { return JSON.parse(fence[1]); } catch {} }
    const a = text.search(/[\[{]/), b = Math.max(text.lastIndexOf("}"), text.lastIndexOf("]"));
    if (a >= 0 && b > a) { try { return JSON.parse(text.slice(a, b + 1)); } catch {} }
    throw { code: "invalid_json", message: "Reply was not JSON", text };
  }
  async function json(prompt, opts = {}) {
    const c = await client();
    const content = [];
    const imgs = opts.images ? [].concat(Array.from(opts.images.length !== undefined && !(opts.images instanceof Blob) ? opts.images : [opts.images])) : [];
    for (const b of imgs) content.push({ type: "image", source: { type: "base64", media_type: b.type || "image/jpeg", data: await toB64(b) } });
    content.push({ type: "text", text: prompt + "\n\nReply with only the JSON value, no other text." });
    let res;
    try {
      res = await c.beta.messages.create({
        model: MODEL,
        max_tokens: 16000,
        betas: ["server-side-fallback-2026-07-01"],
        fallbacks: "default",
        output_config: { effort: opts.modelTier === "quick" ? "low" : "medium" },
        messages: [{ role: "user", content }],
      }, { signal: opts.signal });
    } catch (e) {
      if (opts.signal?.aborted) throw { code: "cancelled", message: "Stopped" };
      const s = e?.status;
      throw { code: s === 401 || s === 403 ? "bad_key" : s === 429 || s === 529 ? "rate_limited" : "upstream_error", message: String(e?.message || e) };
    }
    if (res.stop_reason === "refusal") throw { code: "refused", message: "Declined" };
    const text = res.content.filter(b => b.type === "text").map(b => b.text).join("");
    if (!text.trim()) throw { code: "empty_completion", message: "Empty reply" };
    return parseJson(text);
  }
  const sample = Object.assign(async (input, opts) => ({ text: JSON.stringify(await json(input, opts)), truncated: false }), {
    json,
    limits: async () => ({ maxPromptBytes: 262144, images: { maxCount: 12, maxInputBytes: 20e6, mediaTypes: ["image/jpeg", "image/png", "image/webp", "image/gif"] } }),
  });

  // ---- storage (this browser only) ----
  let store = {};
  try { store = JSON.parse(ls.get(DBK) || "{}") || {}; } catch { store = {}; }
  const subs = new Set();
  const save = () => ls.set(DBK, JSON.stringify(store));
  const notify = () => subs.forEach(f => queueMicrotask(f));
  const clone = (d) => JSON.parse(JSON.stringify(d));
  const merge = (a, b) => {
    for (const k of Object.keys(b)) {
      const v = b[k];
      if (v && typeof v === "object" && !Array.isArray(v) && a[k] && typeof a[k] === "object" && !Array.isArray(a[k])) merge(a[k], v);
      else a[k] = v;
    }
    return a;
  };
  const doc = (path) => ({
    id: path.split("/").pop(), path,
    async get() { return { id: path.split("/").pop(), exists: path in store, data: () => store[path] }; },
    async set(d) { store[path] = clone(d); save(); notify(); },
    async update(d) { if (!(path in store)) throw { code: "invalid_argument", message: "Missing document" }; merge(store[path], clone(d)); save(); notify(); },
    async delete() { delete store[path]; save(); notify(); },
  });
  const collection = (cpath, order = null, lim = 0) => {
    const depth = cpath.split("/").length + 1;
    const snap = () => {
      let keys = Object.keys(store).filter(k => k.startsWith(cpath + "/") && k.split("/").length === depth);
      if (order) keys.sort((x, y) => { const a = store[x][order.f], b = store[y][order.f]; return (a < b ? -1 : a > b ? 1 : 0) * (order.dir === "desc" ? -1 : 1); });
      else keys.sort();
      if (lim) keys = keys.slice(0, lim);
      const docs = keys.map(k => ({ id: k.split("/").pop(), exists: true, data: () => store[k] }));
      return { docs, size: docs.length, empty: !docs.length };
    };
    return {
      path: cpath,
      orderBy: (f, dir = "asc") => collection(cpath, { f, dir }, lim),
      limit: (n) => collection(cpath, order, n),
      doc: (id) => doc(cpath + "/" + (id || Date.now().toString(36) + Math.random().toString(36).slice(2, 8))),
      async add(d) { const r = doc(cpath + "/" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8)); await r.set(d); return r; },
      async get() { return snap(); },
      onSnapshot(next) { const run = () => next(snap()); subs.add(run); queueMicrotask(run); return () => subs.delete(run); },
    };
  };
  const db = { doc, collection };

  const caps = { sample, db };
  return { use: async (name) => caps[name] || null, hasKey: () => !!(ls.get(KEY) || "").trim(), setKey: (k) => ls.set(KEY, k.trim()) };
})();
</script>
"""

settings_btn = '<button class="gear" id="gearBtn" aria-label="Settings" aria-expanded="false"><svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M19.4 13a7.5 7.5 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.6 7.6 0 0 0-1.7-1L15 3h-4l-.4 2.9a7.6 7.6 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.5 7.5 0 0 0 0 2l-2.1 1.6 2 3.5 2.5-1c.5.4 1.1.7 1.7 1L11 21h4l.4-2.9c.6-.3 1.2-.6 1.7-1l2.5 1 2-3.5L19.4 13ZM13 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7Z"/></svg></button>'

settings_panel = """
  <section class="card" id="settings" hidden style="margin-bottom:16px">
    <h2>Settings</h2>
    <label class="label" for="apiKey">AI key</label>
    <input id="apiKey" type="password" autocomplete="off" placeholder="Paste your key" style="font:15px var(--mono);padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--ink);width:100%">
    <p class="tip">The key and your inventory are stored on this device only. Each count uses a few cents of API credit.</p>
    <div class="row"><button class="btn" id="saveKey">Save</button><button class="btn secondary" id="closeSettings">Close</button></div>
    <div id="keyMsg" hidden></div>
  </section>
"""

settings_js = """
<script>
(() => {
  const $ = (s) => document.getElementById(s);
  const panel = $("settings"), btn = $("gearBtn");
  const open = (on) => { panel.hidden = !on; btn.setAttribute("aria-expanded", on); if (on) $("apiKey").focus(); };
  btn.onclick = () => open(panel.hidden);
  $("closeSettings").onclick = () => open(false);
  const status = () => { if (!window.claude.hasKey()) $("status").textContent = "Add your API key in Settings"; else $("status").textContent = "Ready · saved on this device"; };
  $("saveKey").onclick = () => {
    const v = $("apiKey").value.trim();
    if (!v) return;
    window.claude.setKey(v);
    $("apiKey").value = "";
    const m = $("keyMsg"); m.hidden = false; m.className = "notice ok"; m.textContent = "Saved. You're ready to count.";
    status();
    setTimeout(() => open(false), 900);
  };
  setTimeout(() => { status(); if (!window.claude.hasKey()) open(true); }, 300);
})();
</script>
"""

gear_css = """
.gear { border: 1px solid var(--line); background: var(--surface); color: var(--ink); border-radius: 50%; width: 38px; height: 38px; display: inline-flex; align-items: center; justify-content: center; cursor: pointer; }
.gear:focus-visible { outline: 3px solid var(--tape); outline-offset: 2px; }
.hdr-right { display: flex; align-items: center; gap: 10px; }
"""

s = src
s = s.replace("</style>", gear_css + "</style>", 1)
s = s.replace('<div class="status" id="status">Connecting…</div>',
              '<div class="hdr-right"><div class="status" id="status">Loading…</div>' + settings_btn + "</div>", 1)
s = s.replace('<main class="wrap">', '<main class="wrap">' + settings_panel, 1)
main_script = s.index("<script>")
s = s[:main_script] + "<script>" + SDK_JS + "</script>\n" + shim + s[main_script:]
s = s.replace('<meta name="claude:viewport-fit-sides" content="cover">\n', "")
s = s + settings_js

head_end = s.index("</style>") + len("</style>")
doc = (
    '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
    '<meta name="theme-color" content="#0e6a5b">\n'
    + s[:head_end]
    + "\n<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)} body{margin:0;-webkit-text-size-adjust:100%} img{max-width:100%}</style>\n"
    + "</head>\n<body>\n"
    + s[head_end:]
    + "\n</body>\n</html>\n"
)
# Visible page text must not mention the AI vendor (the bundled SDK is code, not shown).
assert "Claude" not in doc.replace(SDK_JS, "").replace("window.claude", "").replace("claude-opus", ""), "branding leak"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(doc)
print("wrote", out, len(doc))
