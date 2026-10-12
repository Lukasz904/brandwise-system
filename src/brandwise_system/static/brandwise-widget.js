// Brandwise – widget dla Scriptable (iOS). Pokazuje podsumowanie z https://system.brandwise.cloud
// Instalacja: Scriptable → „+” → wklej ten kod → uruchom raz (▶) i wklej token widgetu (System → Klucze).
// Potem: ekran początkowy → przytrzymaj → „+” → Scriptable → wybierz rozmiar → Edytuj widget → Script: ten skrypt.
// Token trzymany jest w pęku kluczy iOS (Keychain), nie w kodzie.

const BASE = "https://system.brandwise.cloud";
const KEY = "brandwise_widget_token";
const C = { ink: new Color("#3c3c3b"), head: new Color("#56256a"), accent: new Color("#e21752"),
            mute: new Color("#7b7486"), ok: new Color("#1f8a5b"), bg: new Color("#ffffff"),
            bgDark: new Color("#1c1a1f"), inkDark: new Color("#f2eff5") };

function zl(gr) {
  if (gr === null || gr === undefined) return "—";
  const v = Math.round(gr / 100);
  return v.toLocaleString("pl-PL").replace(/,/g, " ") + " zł";
}

async function token() {
  if (Keychain.contains(KEY) && !args.queryParameters.reset) return Keychain.get(KEY);
  if (config.runsInWidget) return null;
  const a = new Alert();
  a.title = "Token widgetu Brandwise";
  a.message = "Wklej token z System → Klucze → „Utwórz token widgetu”.";
  a.addSecureTextField("bw_…", "");
  a.addAction("Zapisz");
  a.addCancelAction("Anuluj");
  if ((await a.present()) === -1) return null;
  const t = a.textFieldValue(0).trim();
  if (t) Keychain.set(KEY, t);
  return t || null;
}

async function load(t) {
  const r = new Request(BASE + "/api/widget");
  r.headers = { Authorization: "Bearer " + t };
  r.timeoutInterval = 20;
  const data = await r.loadJSON();
  if (r.response.statusCode === 401) throw new Error("Token nieważny – uruchom skrypt i wklej nowy.");
  if (r.response.statusCode !== 200) throw new Error("Serwer: " + r.response.statusCode);
  return data;
}

function line(w, label, value, color, size) {
  const s = w.addStack();
  s.centerAlignContent();
  const l = s.addText(label);
  l.font = Font.systemFont(size - 2);
  l.textColor = C.mute;
  l.lineLimit = 1;
  s.addSpacer();
  const v = s.addText(value);
  v.font = Font.semiboldSystemFont(size);
  v.textColor = color;
  v.lineLimit = 1;
  v.minimumScaleFactor = 0.6;
}

function build(d, err) {
  const w = new ListWidget();
  w.backgroundColor = Color.dynamic(C.bg, C.bgDark);
  w.url = BASE + "/";
  w.refreshAfterDate = new Date(Date.now() + 30 * 60 * 1000);
  const ink = Color.dynamic(C.ink, C.inkDark);
  const family = config.widgetFamily || "medium";
  const size = family === "small" ? 13 : 14;

  const title = w.addText("brandwise" + (d ? " · " + d.miesiac : ""));
  title.font = Font.boldSystemFont(size - 1);
  title.textColor = Color.dynamic(C.head, C.inkDark);
  w.addSpacer(6);

  if (err) {
    const e = w.addText(err);
    e.font = Font.systemFont(12);
    e.textColor = C.accent;
    return w;
  }
  const f = d.faktury;
  if (f) line(w, "Po terminie", zl(f.po_terminie) + (f.po_terminie_n ? ` (${f.po_terminie_n})` : ""),
              f.po_terminie_n ? C.accent : C.ok, size);
  line(w, "Do zafakturowania", zl(d.do_zafakturowania), d.do_zafakturowania_n ? C.accent : ink, size);
  if (family !== "small") {
    if (f) line(w, "Nieopłacone", zl(f.nieoplacone), ink, size);
    line(w, "Przychód (mies.)", zl(d.przychod), ink, size);
  }
  line(w, "Prospekty", zl(d.prospekty) + (family !== "small" ? ` (${d.prospekty_n})` : ""), ink, size);
  if (d.marek && family !== "small") {
    line(w, "Marek czeka na Ciebie", d.marek.stop ? "STOP" : String(d.marek.czeka),
         d.marek.stop || d.marek.czeka ? C.accent : ink, size);
  }
  w.addSpacer();
  const t = w.addDate(new Date());
  t.applyTimeStyle();
  t.font = Font.systemFont(10);
  t.textColor = C.mute;
  return w;
}

let widget;
try {
  const t = await token();
  widget = t ? build(await load(t)) : build(null, "Brak tokenu – uruchom skrypt w aplikacji Scriptable.");
} catch (e) {
  widget = build(null, String(e.message || e));
}
if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  await widget.presentMedium();
}
Script.complete();
