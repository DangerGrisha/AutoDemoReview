/* The 3D replay's settings: one panel with tabs, kept in this browser.
 *
 * Every value is described once in SCHEMA (its range, step and default); what comes back from
 * storage goes through it, so a broken or old entry gives the default rather than a broken page.
 * Where CS2 has the same setting its console name is in `cvar`, with CS2's range.
 * The page reads the values and applies them; this module only knows how to show and keep them.
 */

export const STORE_KEY = 'replay3d.settings.v1';

const bool = (key, label, def, extra = {}) => ({ key, label, type: 'bool', def, ...extra });
const range = (key, label, min, max, step, def, extra = {}) => ({ key, label, type: 'range', min, max, step, def, ...extra });
const choice = (key, label, options, def, extra = {}) => ({ key, label, type: 'choice', options, def, ...extra });

/** Tab -> its settings, in the order they show. */
export const SCHEMA = {
  radar: {
    title: 'Радар',
    items: [
      bool('centered', 'Центрировать на игроке', true, { cvar: 'cl_radar_always_centered' }),
      bool('rotate', 'Вращать вместе со взглядом', true, { cvar: 'cl_radar_rotate' }),
      range('scale', 'Масштаб карты', 0.25, 1, 0.05, 0.7, { cvar: 'cl_radar_scale', hint: 'меньше — видно больше карты' }),
      range('hudScale', 'Размер радара', 0.8, 1.3, 0.05, 1, { cvar: 'cl_hud_radar_scale' }),
      range('iconScale', 'Размер значков', 0.4, 1.25, 0.05, 1, { cvar: 'cl_radar_icon_scale_min' }),
      bool('squareOnTab', 'Вся карта при зажатом Tab', true, { cvar: 'cl_radar_square_with_scoreboard' }),
      bool('utility', 'Гранаты и бомба', true),
      bool('numbers', 'Номера игроков', true),
      bool('sight', 'Линии взгляда', true),
      range('dim', 'Затемнение карты', 0, 0.6, 0.02, 0.32, { pct: true }),
    ],
  },
  crosshair: {
    title: 'Прицел',
    items: [
      choice('source', 'Чей прицел', [['player', 'игрока, за которым смотрю'], ['mine', 'всегда мой']], 'player',
        { cvar: 'cl_show_observer_crosshair', hint: 'у старых матчей кодов нет, там всегда мой' }),
      choice('shape', 'Вид', [['cross', 'крест'], ['circle', 'круг'], ['square', 'квадрат'], ['dot', 'только точка']], 'cross',
        { cvar: 'cl_crosshairstyle' }),
      choice('color', 'Цвет', [[0, 'красный'], [1, 'зелёный'], [2, 'жёлтый'], [3, 'синий'], [4, 'голубой'], [5, 'свой']], 5,
        { cvar: 'cl_crosshaircolor' }),
      range('r', 'Красный', 0, 255, 1, 57, { cvar: 'cl_crosshaircolor_r', when: (v) => v.color === 5 }),
      range('g', 'Зелёный', 0, 255, 1, 255, { cvar: 'cl_crosshaircolor_g', when: (v) => v.color === 5 }),
      range('b', 'Синий', 0, 255, 1, 20, { cvar: 'cl_crosshaircolor_b', when: (v) => v.color === 5 }),
      range('alpha', 'Непрозрачность', 0, 255, 1, 255, { cvar: 'cl_crosshaircolor_a' }),
      range('length', 'Длина', 0, 40, 0.5, 7, { cvar: 'cl_crosshair_length', hint: 'пиксели на экране 1080p' }),
      range('thickness', 'Толщина', 1, 16, 0.5, 2, { cvar: 'cl_crosshair_thickness' }),
      range('gap', 'Зазор', -10, 40, 0.5, 4, { cvar: 'cl_crosshair_gap' }),
      bool('dot', 'Точка в центре', false, { cvar: 'cl_crosshairdot' }),
      bool('t', 'Без верхней линии (T)', false, { cvar: 'cl_crosshair_t' }),
      range('outline', 'Обводка', 0, 3, 1, 1, { cvar: 'cl_crosshair_drawoutline' }),
      range('outlineAlpha', 'Непрозрачность обводки', 0, 255, 1, 160, { when: (v) => v.outline > 0 }),
    ],
  },
  viewmodel: {
    title: 'Руки',
    items: [
      range('fov', 'Поле зрения рук', 54, 68, 1, 60, { cvar: 'viewmodel_fov' }),
      range('x', 'Сдвиг вправо', -2, 2.5, 0.1, 1, { cvar: 'viewmodel_offset_x' }),
      range('y', 'Сдвиг вперёд', -2, 2, 0.1, 1, { cvar: 'viewmodel_offset_y' }),
      range('z', 'Сдвиг вверх', -2, 2, 0.1, -1, { cvar: 'viewmodel_offset_z' }),
      bool('visible', 'Показывать руки и оружие', true),
    ],
  },
  camera: {
    title: 'Камера',
    items: [
      range('flySpeed', 'Скорость полёта', 0.25, 4, 0.25, 1, { times: true }),
      range('sensitivity', 'Чувствительность мыши', 0.25, 3, 0.05, 1, { times: true }),
      bool('invertY', 'Инвертировать мышь по вертикали', false),
      bool('autoNext', 'К союзнику после смерти («как в CS2»)', true),
      range('autoNextDelay', 'Через сколько секунд', 0, 5, 0.5, 2.5, { when: (v) => v.autoNext }),
    ],
  },
  graphics: {
    title: 'Графика',
    items: [
      bool('shadows', 'Тени игроков', true),
      range('renderScale', 'Разрешение рендера', 0.5, 1, 0.05, 1, { pct: true, hint: 'ниже — быстрее на слабом компьютере' }),
      bool('showFps', 'Показывать FPS', true),
    ],
  },
  sound: { title: 'Звук', items: [] },
};

/** CS2's own viewmodel presets that are known for sure (`viewmodel_presetpos`). */
export const VIEWMODEL_PRESETS = [
  ['Desktop', { fov: 60, x: 1, y: 1, z: -1 }],
  ['Classic', { fov: 68, x: 2.5, y: 0, z: -1.5 }],
];

function fit(item, v) {
  if (item.type === 'bool') return typeof v === 'boolean' ? v : item.def;
  if (item.type === 'choice') return item.options.some(([o]) => o === v) ? v : item.def;
  const n = typeof v === 'number' ? v : NaN;
  return Number.isFinite(n) ? Math.min(item.max, Math.max(item.min, n)) : item.def;
}

export function defaults(tab) {
  return Object.fromEntries(SCHEMA[tab].items.map(i => [i.key, i.def]));
}

/** What is kept, with anything missing, old or broken replaced by its default. */
export function load() {
  let kept = {};
  try { kept = JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {}; } catch (e) { kept = {}; }
  if (typeof kept !== 'object' || Array.isArray(kept)) kept = {};
  const out = {};
  for (const [tab, { items }] of Object.entries(SCHEMA)) {
    const got = kept[tab] && typeof kept[tab] === 'object' ? kept[tab] : {};
    out[tab] = Object.fromEntries(items.map(i => [i.key, fit(i, got[i.key])]));
  }
  return out;
}

export function save(values) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(values)); } catch (e) { /* private window: this visit only */ }
}

/** Put `patch` into a tab within the schema; what the schema does not know is dropped. */
export function assign(values, tab, patch) {
  for (const item of SCHEMA[tab].items) {
    if (item.key in patch) values[tab][item.key] = fit(item, patch[item.key]);
  }
}

const CSS = `
#setwrap { position:relative; display:inline-flex; }
#setbtn { padding:6px 9px; }
#setpanel { position:absolute; bottom:38px; right:0; z-index:13; width:330px; max-height:min(560px, 70vh);
  display:none; flex-direction:column; background:var(--panel,#0d111a); border:1px solid var(--line,#232a36);
  border-radius:12px; box-shadow:0 10px 34px rgba(0,0,0,.5); backdrop-filter:blur(10px); text-align:left; }
#setpanel.open { display:flex; }
#setpanel .tabs { display:flex; flex-wrap:wrap; gap:4px; padding:10px 10px 8px; border-bottom:1px solid var(--line,#232a36); }
#setpanel .tabs button { padding:4px 8px; font-size:11px; }
#setpanel .tabs button.on { background:var(--accent,#2b6cff); color:#fff; border-color:transparent; }
#setpanel .body { overflow:auto; padding:6px 13px 12px; }
#setpanel .r { display:grid; grid-template-columns:128px 1fr 44px; align-items:center; gap:8px; margin-top:8px; }
#setpanel .r label, #setpanel .cb, #setpanel .k { font-size:11px; color:var(--muted,#8b93a5); }
#setpanel .r output { font-size:10px; color:var(--dim,#5f6875); text-align:right; font-variant-numeric:tabular-nums; }
#setpanel input[type=range] { width:100%; height:14px; margin:0; accent-color:var(--accent,#2b6cff); }
#setpanel select, #setpanel input[type=text] { width:100%; font-size:11px; background:#121826; color:var(--text,#e6e9ef);
  border:1px solid var(--line,#232a36); border-radius:6px; padding:3px 5px; box-sizing:border-box; }
#setpanel .cb { display:flex; align-items:center; gap:7px; margin-top:9px; cursor:pointer; }
#setpanel .hint { font-size:10px; color:var(--dim,#5f6875); margin:2px 0 0 136px; line-height:1.3; }
#setpanel .row { display:flex; gap:6px; align-items:center; margin-top:10px; flex-wrap:wrap; }
#setpanel .row button { font-size:11px; padding:4px 8px; }
#setpanel .err { font-size:10px; color:#ff8080; margin-top:4px; min-height:12px; }
#setpanel .note { font-size:10px; color:var(--dim,#5f6875); margin-top:10px; line-height:1.4; }
#setpanel .foot { display:flex; justify-content:flex-end; margin-top:12px; }
#setpanel .foot button { font-size:11px; padding:4px 9px; }
#setpanel .off { opacity:.45; pointer-events:none; }`;

const shown = (v, step) => {
  const d = step < 0.1 ? 2 : step < 1 ? 1 : 0;
  return (+v).toFixed(d).replace(/\.0+$/, '');
};

/**
 * The ⚙ button and its panel, in `host`. `onChange(tab, values)` after every change.
 * `extras[tab](body, rerender)` adds a tab's own controls under its settings (the sound
 * mixer, the crosshair code, the viewmodel presets); `disabled[tab]` greys a tab out with a note.
 */
export function mountPanel(host, values, onChange, extras = {}, disabled = {}) {
  if (!host || document.getElementById('setwrap')) return null;
  document.head.appendChild(Object.assign(document.createElement('style'), { textContent: CSS }));
  const wrap = document.createElement('span'); wrap.id = 'setwrap';
  const btn = Object.assign(document.createElement('button'), { id: 'setbtn', textContent: '⚙', title: 'Настройки' });
  const panel = Object.assign(document.createElement('div'), { id: 'setpanel' });
  const tabs = Object.assign(document.createElement('div'), { className: 'tabs' });
  const body = Object.assign(document.createElement('div'), { className: 'body' });
  panel.append(tabs, body); wrap.append(btn, panel); host.appendChild(wrap);
  let current = 'radar';
  // a control keeps the focus after a click, and the page's keys skip a focused control
  const done = (el) => { if (el && el.blur && el.type !== 'range' && el.type !== 'text') el.blur(); };

  const render = () => {
    tabs.replaceChildren(...Object.entries(SCHEMA).map(([tab, { title }]) => {
      const b = Object.assign(document.createElement('button'), { textContent: title });
      b.classList.toggle('on', tab === current);
      b.onclick = () => { current = tab; render(); done(b); };
      return b;
    }));
    body.replaceChildren();
    const v = values[current], off = disabled[current];
    const box = document.createElement('div');
    if (off) box.className = 'off';
    for (const item of SCHEMA[current].items) {
      if (item.when && !item.when(v)) continue;
      const title = item.cvar ? `${item.label} (${item.cvar})` : item.label;
      if (item.type === 'bool') {
        const l = Object.assign(document.createElement('label'), { className: 'cb', title });
        const i = Object.assign(document.createElement('input'), { type: 'checkbox', checked: v[item.key] });
        i.onchange = () => { v[item.key] = i.checked; save(values); onChange(current, values); done(i); render(); };
        l.append(i, document.createTextNode(item.label)); box.appendChild(l);
      } else {
        const r = Object.assign(document.createElement('div'), { className: 'r' });
        const l = Object.assign(document.createElement('label'), { textContent: item.label, title });
        let input, out = document.createElement('output');
        if (item.type === 'choice') {
          input = document.createElement('select');
          for (const [o, text] of item.options) input.appendChild(Object.assign(document.createElement('option'), { value: String(o), textContent: text }));
          input.value = String(v[item.key]);
          input.onchange = () => {
            const o = item.options.find(([x]) => String(x) === input.value);
            v[item.key] = o ? o[0] : item.def; save(values); onChange(current, values); done(input); render();
          };
          r.style.gridTemplateColumns = '128px 1fr';
          r.append(l, input);
        } else {
          input = Object.assign(document.createElement('input'), { type: 'range', min: item.min, max: item.max, step: item.step, value: v[item.key] });
          const fmt = (x) => item.pct ? Math.round(x * 100) + '%' : item.times ? '×' + shown(x, item.step) : shown(x, item.step);
          out.textContent = fmt(v[item.key]);
          input.oninput = () => { v[item.key] = fit(item, +input.value); out.textContent = fmt(v[item.key]); save(values); onChange(current, values); };
          // a slider that shows or hides others redraws once it is let go
          input.onchange = () => { if (SCHEMA[current].items.some(o => o.when)) render(); };
          r.append(l, input, out);
        }
        box.appendChild(r);
      }
      if (item.hint) box.appendChild(Object.assign(document.createElement('div'), { className: 'hint', textContent: item.hint }));
    }
    body.appendChild(box);
    // a tab with nothing to change: its settings shown greyed and out of reach, no presets, no reset
    if (off) {
      for (const i of box.querySelectorAll('input,select')) i.disabled = true;
      body.appendChild(Object.assign(document.createElement('div'), { className: 'note', textContent: off }));
      return;
    }
    if (extras[current]) extras[current](body, render);
    if (SCHEMA[current].items.length) {
      const foot = Object.assign(document.createElement('div'), { className: 'foot' });
      const reset = Object.assign(document.createElement('button'), { textContent: 'Сбросить вкладку' });
      reset.onclick = () => { values[current] = defaults(current); save(values); onChange(current, values); render(); };
      foot.appendChild(reset); body.appendChild(foot);
    }
  };

  btn.onclick = () => { panel.classList.toggle('open'); if (panel.classList.contains('open')) render(); done(btn); };
  document.addEventListener('pointerdown', (e) => {
    if (panel.classList.contains('open') && !wrap.contains(e.target)) panel.classList.remove('open');
  }, { passive: true });
  addEventListener('keydown', (e) => {
    if (e.code === 'Escape' && panel.classList.contains('open')) { panel.classList.remove('open'); e.stopImmediatePropagation(); }
  }, { capture: true });
  return { panel, render, open: (tab) => { if (tab) current = tab; panel.classList.add('open'); render(); } };
}
