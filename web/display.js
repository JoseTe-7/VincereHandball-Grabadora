'use strict';

(() => {
  const TOKEN = document.querySelector('meta[name="token"]').content;
  const only = new URLSearchParams(location.search).get('s');   // ?s=s2 → solo esa sesión
  const wall = document.getElementById('wall');
  const waiting = document.getElementById('waiting');

  let state = null;
  let syncedAt = 0;
  const cells = new Map();                                       // id de sesión → { root, feed, ... }

  const formatClock = (seconds) => {
    const s = Math.max(0, Math.floor(seconds));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const mm = String(m).padStart(2, '0'), rr = String(r).padStart(2, '0');
    return h ? `${h}:${mm}:${rr}` : `${mm}:${rr}`;
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function makeCell(id) {
    const feed = el('img', 'cell-feed');
    feed.alt = '';
    feed.hidden = true;
    const empty = el('div', 'cell-empty', 'Cámara sin conexión');
    const label = el('span', 'cell-label');
    const top = el('div', 'cell-top', null);
    top.append(label);
    const player = el('div', 'cell-player');
    const exercise = el('div', 'cell-exercise');
    const who = el('div', 'cell-who');
    who.append(player, exercise);
    const clock = el('div', 'cell-clock', '00:00');
    const bottom = el('div', 'cell-bottom');
    bottom.append(who, clock);
    const root = el('article', 'cell');
    root.append(feed, empty, top, bottom);
    feed.addEventListener('error', () => {                     // la vista previa se reanuda sola
      const key = cell.streamKey;
      if (!key) return;
      setTimeout(() => { if (cell.streamKey === key) feed.src = `${feed.dataset.base}&r=${Date.now()}`; }, 1000);
    });
    const cell = { id, root, feed, empty, label, player, exercise, clock, streamKey: null, elapsed: 0, recording: false };
    return cell;
  }

  function render() {
    const sessions = state.sessions.filter((s) => !only || s.id === only);
    const count = sessions.length;
    wall.style.setProperty('--cols', String(count <= 1 ? 1 : count <= 4 ? 2 : count <= 6 ? 3 : 4));
    wall.dataset.size = count === 1 ? 'solo' : 'multi';

    for (const id of [...cells.keys()]) {
      if (!sessions.some((s) => s.id === id)) { cells.get(id).root.remove(); cells.delete(id); }
    }
    for (const s of sessions) if (!cells.has(s.id)) cells.set(s.id, makeCell(s.id));

    const nodes = sessions.map((s) => cells.get(s.id).root);
    if (nodes.some((node, i) => wall.children[i] !== node) || wall.children.length !== nodes.length) wall.replaceChildren(...nodes);

    for (const s of sessions) {
      const cell = cells.get(s.id);
      const cam = s.camera;
      const key = cam.connected ? `${s.id}:${cam.generation}` : null;
      if (key !== cell.streamKey) {                            // solo se reinicia cuando cambia la cámara
        cell.streamKey = key;
        if (key === null) {
          cell.feed.removeAttribute('src');
          cell.feed.hidden = true;
        } else {
          cell.feed.dataset.base = `/stream?t=${encodeURIComponent(TOKEN)}&s=${encodeURIComponent(s.id)}&g=${cam.generation}`;
          cell.feed.src = cell.feed.dataset.base;
          cell.feed.hidden = false;
        }
      }
      cell.empty.hidden = cam.connected;
      cell.empty.textContent = s.connecting ? 'Conectando la cámara…' : 'Cámara sin conexión';
      cell.label.hidden = count === 1;
      cell.label.textContent = s.label;
      cell.player.textContent = s.player || 'Sin jugador';
      cell.exercise.textContent = s.exercise || 'Sin ejercicio';
      cell.root.classList.toggle('is-rec', s.recording);
      cell.recording = s.recording;
      cell.elapsed = s.elapsed;
    }

    if (count === 0) show('No hay ninguna sesión para mostrar', only ? 'Esa sesión ya no existe en la aplicación.' : '');
    else if (waiting.dataset.kind === 'empty') hide();
    tick();
  }

  function tick() {
    const delta = (performance.now() - syncedAt) / 1000;
    for (const cell of cells.values()) cell.clock.textContent = formatClock(cell.recording ? cell.elapsed + delta : 0);
  }
  setInterval(tick, 200);

  function show(title, detail = '', kind = 'empty') {
    waiting.dataset.kind = kind;
    waiting.replaceChildren(el('div', null, title), ...(detail ? [el('p', null, detail)] : []));
    waiting.hidden = false;
  }
  function hide() { waiting.hidden = true; waiting.dataset.kind = ''; }

  function connect() {
    const source = new EventSource(`/events?t=${encodeURIComponent(TOKEN)}`);
    source.onopen = () => { if (waiting.dataset.kind !== 'empty') hide(); };
    source.onmessage = (event) => {
      const next = JSON.parse(event.data).state;
      if (state && next.revision < state.revision) return;
      state = next;
      syncedAt = performance.now();
      if (waiting.dataset.kind === 'link') hide();
      render();
    };
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) show('La aplicación se cerró', 'Ábrela de nuevo para volver a ver las sesiones.', 'link');
      else show('Reconectando…', '', 'link');
    };
  }

  // El cursor desaparece a los 3 segundos; F pone y quita la pantalla completa (modo navegador).
  let cursorTimer = null;
  document.addEventListener('mousemove', () => {
    document.body.classList.remove('hide-cursor');
    clearTimeout(cursorTimer);
    cursorTimer = setTimeout(() => document.body.classList.add('hide-cursor'), 3000);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key.toLowerCase() !== 'f' || event.ctrlKey || event.metaKey || event.altKey) return;
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(() => {});
  });

  connect();
})();
