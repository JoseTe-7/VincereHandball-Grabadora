'use strict';

(() => {
  const meta = (name) => document.querySelector(`meta[name="${name}"]`).content;
  const TOKEN = meta('token');
  const VIEWER_KEY = meta('viewer-key');
  const $ = (id) => document.getElementById(id);

  const RESOLUTIONS = [
    { label: '480p', width: 640, height: 480 },
    { label: '720p', width: 1280, height: 720 },
    { label: '1080p', width: 1920, height: 1080 },
  ];
  const SHEETS = { settings: 'sheetSettings', teams: 'sheetTeams', device: 'sheetDevice' };

  let state = null;
  let syncedAt = 0;
  let activeId = null;
  let streamKey = null;
  let lastNoticeId = null;
  let lastFocus = null;
  let deleteTimer = null;
  let tabletStream = null;
  let tabletPump = false;
  let cachedCameras = null;
  let isScanningCameras = false;

  async function scanCameras() {
    isScanningCameras = true;
    renderSettings(session());
    try {
      const res = await fetch(`/api/cameras?t=${encodeURIComponent(TOKEN)}`, { cache: 'no-store' });
      const data = await res.json();
      if (data.ok && data.cameras) {
        cachedCameras = data.cameras.filter((c) => c.kind !== 'virtual');
        const currentEndpointExists = cachedCameras.some((c) =>
          c.index === ui.pending.camera && c.backend === ui.pending.cameraBackend
        );
        if (!currentEndpointExists && cachedCameras.length > 0) {
          const preferred = cachedCameras.find((c) => c.backend === 'dshow') || cachedCameras[0];
          ui.pending.camera = preferred.index;
          ui.pending.cameraBackend = preferred.backend || 'auto';
        }
      } else {
        cachedCameras = [];
      }
    } catch {
      cachedCameras = [];
    }
    isScanningCameras = false;
    renderSettings(session());
  }

  const ui = {
    editing: { players: false, exercises: false },
    adding: null,
    picked: null,
    sheet: null,
    renaming: false,
    confirmDelete: null,
    pending: { sessionId: null, camera: null, resolution: null, cameraBackend: null, youtubeCrop: 'full', youtubeUrl: '', youtubeDelay: 10 },
  };

  // ------------------------------------------------------------------ utilidades
  function el(tag, props = {}, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === false || value == null) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
    return node;
  }

  const ICONS = {
    plus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
    check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
    x: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  };
  const icon = (name) => {
    const holder = document.createElement('span');
    holder.innerHTML = ICONS[name];
    return holder.firstChild;
  };

  const initials = (name) =>
    name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => Array.from(w)[0].toUpperCase()).join('');
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  const formatClock = (seconds) => {
    const s = Math.max(0, Math.floor(seconds));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const mm = String(m).padStart(2, '0'), rr = String(r).padStart(2, '0');
    return h ? `${h}:${mm}:${rr}` : `${mm}:${rr}`;
  };

  function keepingInput(root, build) {
    const old = root.querySelector('input');
    const keep = old && {
      value: old.value,
      focused: document.activeElement === old,
      start: old.selectionStart,
      end: old.selectionEnd,
    };
    build();
    if (!keep) return;
    const input = root.querySelector('input');
    if (!input) return;
    input.value = keep.value;
    if (keep.focused) {
      input.focus();
      try { input.setSelectionRange(keep.start, keep.end); } catch {}
    }
  }

  // ------------------------------------------------------------------ selectores
  const session = () => state.sessions.find((s) => s.id === activeId) || state.sessions[0];
  const profileOf = (s) => state.profiles.find((p) => p.id === s.profile_id) || state.profiles[0];
  const displayOrder = (profile) => {
    const ids = profile.categories.map((c) => c.id);
    return [
      ...ids.flatMap((id) => profile.players.filter((p) => p.category_id === id)),
      ...profile.players.filter((p) => !ids.includes(p.category_id)),
    ];
  };

  // ------------------------------------------------------------------ comunicación con Python
  async function api(action, body = {}) {
    let response;
    try {
      response = await fetch(`/api/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN },
        body: JSON.stringify({ session: activeId, ...body }),
      });
    } catch {
      throw new Error('Se perdió la conexión con la aplicación.');
    }
    const data = await response.json();
    if (data.state) applyState(data.state);
    if (!data.ok) throw new Error(data.error || 'Algo salió mal.');
    return data;
  }

  async function run(action, body) {
    try {
      return await api(action, body);
    } catch (error) {
      toast({ kind: 'error', title: error.message });
      return null;
    }
  }

  function connectEvents() {
    const source = new EventSource(`/events?t=${encodeURIComponent(TOKEN)}`);
    source.onopen = () => setLink('ok');
    source.onmessage = (event) => applyState(JSON.parse(event.data).state);
    source.onerror = () => setLink(source.readyState === EventSource.CLOSED ? 'closed' : 'retry');
  }

  function setLink(status) {
    const banner = $('banner');
    banner.hidden = status === 'ok';
    banner.className = `banner ${status === 'closed' ? 'banner-error' : ''}`;
    banner.textContent = status === 'closed'
      ? 'La aplicación se cerró o se reinició. Ábrela de nuevo para seguir.'
      : 'Reconectando con la aplicación…';
  }

  function applyState(next) {
    if (state && next.revision < state.revision) return;
    state = next;
    syncedAt = performance.now();
    handleNotices(next.notices || []);
    if (!state.sessions.some((s) => s.id === activeId)) activeId = state.sessions[0] ? state.sessions[0].id : null;
    render();
  }

  function handleNotices(list) {
    const newest = list.reduce((max, n) => Math.max(max, n.id), 0);
    if (lastNoticeId === null) { lastNoticeId = newest; return; }
    for (const n of list) if (n.id > lastNoticeId) toast({ kind: n.kind, title: n.text });
    lastNoticeId = Math.max(lastNoticeId, newest);
  }

  // ------------------------------------------------------------------ pintado
  function render() {
    if (!state || !activeId) return;
    const s = session();
    document.body.classList.toggle('is-recording', s.recording);
    renderStage(s);
    renderTabs();
    renderTeamButton(s);
    renderPlayers(s);
    renderExercises(s);
    renderActions(s);
    renderRecent();
    if (ui.sheet === 'settings') renderSettings(s);
    if (ui.sheet === 'teams') renderTeams(s);
    tickClock();
  }

  function renderStage(s) {
    const cam = s.camera;
    const feed = $('feed');
    const key = cam.connected ? `${s.id}:${cam.generation}` : null;
    if (key !== streamKey) {
      streamKey = key;
      if (key === null) {
        feed.removeAttribute('src');
        feed.hidden = true;
      } else {
        feed.src = `/stream?t=${encodeURIComponent(TOKEN)}&s=${encodeURIComponent(s.id)}&g=${cam.generation}`;
        feed.hidden = false;
      }
    }
    $('stage').classList.toggle('no-feed', !cam.connected);

    const empty = $('stageEmpty');
    empty.hidden = cam.connected;
    if (!cam.connected) {
      if (s.connecting) {
        $('emptyTitle').textContent = cam.source === 'youtube'
          ? `Conectando YouTube (${cam.name || 'Cámara YouTube'})`
          : `Conectando la cámara ${cam.index}`;
        $('emptyText').textContent = 'Puede tardar unos segundos.';
      } else if (cam.error) {
        $('emptyTitle').textContent = 'No se pudo conectar la cámara';
        $('emptyText').textContent = cam.error;
      } else {
        $('emptyTitle').textContent = 'Sin cámara conectada';
        $('emptyText').textContent = 'Conecta la cámara por USB o añade una transmisión desde YouTube.';
      }
      $('spinner').hidden = !s.connecting;
      $('btnConnect').hidden = s.connecting;
      empty.querySelector('.empty-icon').style.display = s.connecting ? 'none' : '';
    }

    $('camPill').hidden = !cam.connected;
    if (cam.connected) {
      const sourceName = cam.source === 'youtube'
        ? (cam.name || 'YouTube')
        : cam.source === 'tablet' ? 'Cámara de tablet' : `Cámara ${cam.index}`;
      $('camName').textContent = state.sessions.length > 1 ? `${s.label}, ${sourceName}` : sourceName;
      $('camRes').textContent = `${cam.width}×${cam.height}`;
      $('camFps').textContent = `${cam.fps} fps`;
    }
    const viewers = state.clients.viewers;
    $('linkPill').hidden = viewers === 0;
    $('linkText').textContent = plural(viewers, 'pantalla conectada', 'pantallas conectadas');
    $('recPill').hidden = !s.recording;

    const profile = profileOf(s);
    const player = profile.players.find((p) => p.id === s.player_id);
    const exercise = profile.exercises.find((e) => e.id === s.exercise_id);
    $('capPlayer').textContent = player ? player.name : 'Sin jugador';
    $('capExercise').textContent = exercise ? exercise.name : 'Sin ejercicio';
    $('capTarget').textContent = s.target ? `Se guarda en ${s.target}` : '';
  }

  function renderTabs() {
    const nav = $('sessionTabs');
    const signature = JSON.stringify([
      state.sessions.map((s) => [s.id, s.label, s.pavilion_id, s.recording, s.camera.connected, s.connecting]),
      state.pavilions || [], activeId, state.max_sessions,
    ]);
    if (nav.dataset.signature === signature) return;
    nav.dataset.signature = signature;
    const atMax = state.sessions.length >= state.max_sessions;
    const makeTab = (s) => el(
      'button',
      { class: 'tab', type: 'button', 'aria-pressed': String(s.id === activeId), onclick: () => switchTo(s.id) },
      el('i', { class: `dot ${s.recording ? 'dot-rec' : s.camera.connected ? 'dot-ok' : 'dot-off'}` }),
      el('span', { text: s.label }),
      s.recording ? el('span', { class: 'sr-only', text: ', grabando' }) : null,
    );
    const pavilions = state.pavilions || [];
    let tabs;
    if (!pavilions.length) {
      tabs = state.sessions.map(makeTab);
    } else {
      tabs = [];
      for (const pavilion of pavilions) {
        const members = state.sessions.filter((s) => s.pavilion_id === String(pavilion.id));
        if (!members.length) continue;
        tabs.push(el('div', { class: 'pavilion-tab-group' },
          el('span', { class: 'pavilion-tab-title', text: pavilion.name }),
          el('div', { class: 'pavilion-tab-items' }, members.map(makeTab)),
        ));
      }
      const unassigned = state.sessions.filter((s) => !s.pavilion_id || !pavilions.some((p) => String(p.id) === String(s.pavilion_id)));
      if (unassigned.length) tabs.push(el('div', { class: 'pavilion-tab-group unassigned' },
        el('span', { class: 'pavilion-tab-title', text: 'Sin pabellón' }),
        el('div', { class: 'pavilion-tab-items' }, unassigned.map(makeTab)),
      ));
    }
    nav.replaceChildren(
      ...tabs,
      el('button', {
        class: 'tab tab-add', type: 'button', disabled: atMax,
        'aria-label': 'Añadir otra sesión con otra cámara', title: atMax ? 'Máximo de sesiones alcanzado' : 'Añadir sesión',
        onclick: addSession,
      }, icon('plus')),
    );
  }

  function switchTo(id) {
    if (id === activeId) return;
    activeId = id;
    ui.editing = { players: false, exercises: false };
    ui.adding = null;
    ui.picked = null;
    ui.renaming = false;
    ui.confirmDelete = null;
    render();
  }

  async function addSession() {
    const result = await run('add_session');
    if (result && result.session) switchTo(result.session);
  }

  function renderTeamButton(s) {
    const profile = profileOf(s);
    $('teamName').textContent = profile.name;
    $('teamCount').textContent = plural(profile.players.length, 'jugador', 'jugadores');
    $('btnTeam').disabled = s.recording;
  }

  // ------------------------------------------------------------------ chips
  function chip({ label, selected, locked, plain, shortcut, onClick }) {
    return el(
      'button',
      { class: `chip${plain ? ' plain' : ''}`, type: 'button', 'aria-pressed': String(!!selected), disabled: locked, onclick: onClick },
      plain ? null : el('span', { class: 'badge', text: initials(label) }),
      el('span', { class: 'name', text: label }),
      shortcut ? el('span', { class: 'num', text: shortcut }) : null,
    );
  }

  function removeButton(label, onRemove) {
    return el('button', { class: 'chip-x', type: 'button', 'aria-label': label, onclick: onRemove }, icon('x'));
  }

  function editChip({ label, picked, pickable, onPick, onRemove, removeLabel, dragType, dragId }) {
    const body = pickable
      ? el('button', { class: 'chip-pick', type: 'button', 'aria-pressed': String(!!picked), onclick: onPick },
        el('span', { class: 'badge', text: initials(label) }), el('span', { class: 'name', text: label }))
      : el('span', { class: 'chip-pick static' },
        el('span', { class: 'badge', text: initials(label) }), el('span', { class: 'name', text: label }));
    return el('span', { class: `chip chip-edit${picked ? ' picked' : ''}`, draggable: !!dragType, ondragstart: dragType ? (event) => { event.dataTransfer.setData('text/plain', JSON.stringify({ type: dragType, id: dragId })); event.dataTransfer.effectAllowed = 'move'; } : null }, body, removeButton(removeLabel, onRemove));
  }

  const ADD = {
    player: { noun: 'jugador', action: 'add_player', label: 'Añadir jugador' },
    exercise: { noun: 'ejercicio', action: 'add_exercise', label: 'Añadir ejercicio' },
    exercise_category: { noun: 'categoría de ejercicios', action: 'add_exercise_category', label: 'Añadir categoría de ejercicios' },
    category: { noun: 'categoría', action: 'add_category', label: 'Añadir categoría' },
    team: { noun: 'equipo', action: 'create_profile', label: 'Nuevo equipo' },
  };

  function addControl(kind, category = null, labelOverride = '') {
    const cfg = ADD[kind];
    const active = ui.adding && ui.adding.kind === kind && ui.adding.category === category;
    if (!active) {
      return el(
        'button',
        {
          class: 'chip chip-add plain', type: 'button', disabled: kind !== 'team' && session().recording && kind === 'category',
          onclick: () => { ui.adding = { kind, category }; render(); focusAddInput(); },
        },
        icon('plus'),
        el('span', { class: 'name', text: labelOverride || cfg.label }),
      );
    }
    const input = el('input', { type: 'text', maxlength: '40', placeholder: 'Nombre', 'aria-label': `Nombre del ${cfg.noun}`, autocomplete: 'off' });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { event.stopPropagation(); ui.adding = null; render(); }
    });
    const form = el('form', { class: 'chip chip-input' }, input,
      el('button', { type: 'submit', 'aria-label': `Añadir ${cfg.noun}` }, icon('check')));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const name = input.value.trim();
      if (!name) { ui.adding = null; render(); return; }
      const profile = profileOf(session());
      const body = kind === 'team' ? { name } : { profile: profile.id, name, category };
      const result = await run(cfg.action, body);
      if (result) { ui.adding = null; render(); } else input.focus();
    });
    return form;
  }

  function focusAddInput() {
    requestAnimationFrame(() => {
      const input = document.querySelector('.chip-input input');
      if (input) input.focus();
    });
  }

  // ------------------------------------------------------------------ jugadores
  function renderPlayers(s) {
    const profile = profileOf(s);
    const locked = s.recording;
    const editing = ui.editing.players && !locked;
    const root = $('players');
    const signature = JSON.stringify([profile.id, profile.categories, profile.players, s.player_id, locked, editing, ui.adding, ui.picked]);
    if (root.dataset.signature === signature) return;
    root.dataset.signature = signature;

    const order = displayOrder(profile);
    const hasCategories = profile.categories.length > 0;
    const groups = profile.categories.map((c) => ({
      id: c.id, title: c.name, players: profile.players.filter((p) => p.category_id === c.id),
    }));
    const loose = profile.players.filter((p) => !profile.categories.some((c) => c.id === p.category_id));
    if (!hasCategories) groups.push({ id: null, title: null, players: loose });
    else if (loose.length || editing) groups.push({ id: null, title: 'Sin categoría', players: loose });

    keepingInput(root, () => {
      const nodes = groups.map((group) => el(
        'div', { class: 'group' },
        group.title ? groupHead(group, editing) : null,
        el('div', { class: 'chips', ondragover: editing ? (event) => { if (event.dataTransfer.types.includes('text/plain')) { event.preventDefault(); event.currentTarget.classList.add('drop-target'); } } : null, ondragleave: (event) => event.currentTarget.classList.remove('drop-target'), ondrop: editing ? (event) => { event.preventDefault(); event.currentTarget.classList.remove('drop-target'); try { const d = JSON.parse(event.dataTransfer.getData('text/plain')); if (d.type === 'player') run('move_player', { player: d.id, category: group.id }); else if (d.type === 'exercise') run('move_exercise', { exercise: d.id, category: group.id }); } catch {} } : null },
          group.players.map((p) => editing
            ? editChip({
              label: p.name, picked: ui.picked === p.id, pickable: hasCategories,
              onPick: () => { ui.picked = ui.picked === p.id ? null : p.id; render(); },
              onRemove: () => { if (ui.picked === p.id) ui.picked = null; run('remove_player', { player: p.id }); },
              dragType: 'player', dragId: p.id,
              removeLabel: `Quitar a ${p.name} del equipo`,
            })
            : chip({
              label: p.name, selected: p.id === s.player_id, locked,
              shortcut: (() => { const i = order.findIndex((o) => o.id === p.id); return i >= 0 && i < 9 ? String(i + 1) : ''; })(),
              onClick: () => choose({ player: p.id }),
            })),
          addControl('player', group.id, order.length === 0 && group.id === null ? 'Añadir el primer jugador' : ''),
        ),
      ));
      if (editing) nodes.push(el('div', { class: 'group' }, el('div', { class: 'chips' }, addControl('category'))));
      root.replaceChildren(...nodes);
    });

    const toggle = $('editPlayers');
    toggle.setAttribute('aria-pressed', String(editing));
    toggle.disabled = locked;
  }

  function groupHead(group, editing) {
    const canMove = editing && ui.picked !== null;
    return el(
      'div', { class: 'group-head' },
      el('h3', { class: 'group-title', text: group.title }),
      canMove ? el('button', { class: 'move-btn', type: 'button', onclick: () => movePicked(group.id) }, 'Mover aquí') : null,
      editing && group.id !== null
        ? removeButton(`Quitar la categoría ${group.title}`, () => run('remove_category', { category: group.id }))
        : null,
    );
  }

  async function movePicked(categoryId) {
    const player = ui.picked;
    ui.picked = null;
    render();
    await run('move_player', { player, category: categoryId });
  }

  // ------------------------------------------------------------------ ejercicios
  function renderExercises(s) {
    const profile = profileOf(s);
    const locked = s.recording;
    const editing = ui.editing.exercises && !locked;
    const root = $('exercises');
    const categories = profile.exercise_categories || [];
    const signature = JSON.stringify([profile.id, categories, profile.exercises, s.exercise_id, locked, editing, ui.adding]);
    if (root.dataset.signature === signature) return;
    root.dataset.signature = signature;

    keepingInput(root, () => {
      const groups = categories.map(c => ({
        id: c.id,
        title: c.name,
        exercises: profile.exercises.filter(e => e.category_id === c.id),
      }));
      const loose = profile.exercises.filter(e => !categories.some(c => c.id === e.category_id));
      if (!categories.length) groups.push({ id: null, title: null, exercises: loose });
      else if (loose.length || editing) groups.push({ id: null, title: 'Sin categoría', exercises: loose });

      const nodes = [];
      if (!editing) {
        nodes.push(el('div', { class: 'group exercise-uncategorized' },
          el('div', { class: 'chips' }, chip({
            label: 'Sin ejercicio', plain: true, selected: s.exercise_id === null,
            locked, onClick: () => choose({ exercise: null }),
          })),
        ));
      }
      for (const group of groups) {
        const chips = group.exercises.map(e => editing
          ? editChip({ label: e.name, pickable: false, dragType: 'exercise', dragId: e.id, onRemove: () => run('remove_exercise', { exercise: e.id }), removeLabel: `Quitar ${e.name} de la lista` })
          : chip({ label: e.name, selected: e.id === s.exercise_id, locked, onClick: () => choose({ exercise: e.id }) }));
        if (editing) chips.push(addControl('exercise', group.id));
        nodes.push(el('div', { class: 'group exercise-group' },
          group.title ? groupHeadExercise(group, editing) : null,
          el('div', { class: 'chips', ondragover: editing ? (event) => { event.preventDefault(); event.currentTarget.classList.add('drop-target'); } : null, ondragleave: (event) => event.currentTarget.classList.remove('drop-target'), ondrop: editing ? (event) => { event.preventDefault(); event.currentTarget.classList.remove('drop-target'); try { const d = JSON.parse(event.dataTransfer.getData('text/plain')); if (d.type === 'exercise') run('move_exercise', { exercise: d.id, category: group.id }); else if (d.type === 'player') run('move_player', { player: d.id, category: group.id }); } catch {} } : null }, ...chips),
        ));
      }
      if (editing) nodes.push(el('div', { class: 'group' }, el('div', { class: 'chips' }, addControl('exercise_category'))));
      root.replaceChildren(...nodes);
    });
    const toggle = $('editExercises');
    toggle.setAttribute('aria-pressed', String(editing));
    toggle.disabled = locked;
  }

  function groupHeadExercise(group, editing) {
    return el('div', { class: 'group-head' },
      el('h3', { class: 'group-title', text: group.title }),
      editing && group.id !== null
        ? removeButton(`Quitar categoría ${group.title}`, () => run('remove_exercise_category', { category: group.id }))
        : null,
    );
  }

  async function choose(selection) {
    const s = session();
    if ('player' in selection) s.player_id = selection.player;
    if ('exercise' in selection) s.exercise_id = selection.exercise;
    render();
    await run('select', selection);
  }

  function renderActions(s) {
    const ready = s.camera.connected && !s.connecting && s.player_id !== null;
    $('btnRec').disabled = s.recording || !ready;
    $('btnStop').disabled = !s.recording;
    $('btnCancel').disabled = !s.recording;
    const isYouTube = s.camera.source === 'youtube';
    const retro = $('retroTimeline');
    if (retro) retro.hidden = isYouTube;
    $('btnRetro').hidden = isYouTube;
    $('btnRetro').disabled = isYouTube || !s.camera.connected || s.recording;
    $('btnRetro').setAttribute('aria-hidden', String(isYouTube));
  }

  function renderRecent() {
    const list = $('recent');
    const many = state.sessions.length > 1;
    const signature = JSON.stringify([state.recent.map((c) => c.id), many]);
    $('recentEmpty').hidden = state.recent.length > 0;
    if (list.dataset.signature === signature) return;
    list.dataset.signature = signature;
    list.replaceChildren(...state.recent.map((clip) => el(
      'li', {},
      el('button', { type: 'button', title: clip.rel, onclick: () => run('open_folder', { path: clip.path }) },
        el('span', { class: 'r-main', text: clip.player }),
        el('span', { class: 'r-sub', text: many ? `${clip.session}: ${clip.exercise || 'Sin ejercicio'}` : (clip.exercise || 'Sin ejercicio') }),
        el('span', { class: 'r-dur', text: formatClock(clip.duration) })),
    )));
  }

  // ------------------------------------------------------------------ reloj
  function tickClock() {
    if (!state || !activeId) return;
    const s = session();
    const seconds = s.recording ? s.elapsed + (performance.now() - syncedAt) / 1000 : 0;
    $('clock').textContent = formatClock(seconds);
  }
  setInterval(tickClock, 200);

  // ------------------------------------------------------------------ paneles laterales
  async function showDeviceConnect() {
    openSheet('device');
    const target = $('deviceConnectContent');
    target.replaceChildren(el('p', { text: 'Preparando enlace…' }));
    try {
      const response = await fetch(`/api/device-connect?t=${encodeURIComponent(TOKEN)}`, { cache: 'no-store' });
      const data = await response.json();
      target.replaceChildren();
      if (!data.ok) {
        target.append(el('p', { class: 'hint', text: data.error || 'No se pudo preparar la conexión.' }));
        return;
      }
      if (!data.enabled) {
        target.append(el('p', { class: 'hint', text: 'La conexión LAN no está activada.' }));
        return;
      }
      target.append(el('p', { class: 'hint', text: 'Se muestran siempre 6 QR. Escanea uno que use la misma red que la tablet.' }));
      const urls = (data.urls || []).slice(0, 6);
      const qrs = data.qrs || [];
      const qrGrid = el('div', { class: 'device-qr-grid' });
      urls.forEach((url, index) => {
        const card = el('div', { class: 'device-qr-card' });
        if (qrs[index]) card.append(el('img', { class: 'device-qr', src: qrs[index], alt: `Código QR ${index + 1}` }));
        card.append(el('div', { class: 'device-qr-label', text: `QR ${index + 1}` }));
        const copy = el('button', { class: 'btn-outline device-qr-copy', type: 'button', text: 'Copiar enlace', onclick: async () => { try { await navigator.clipboard.writeText(url); toast({title:'Enlace copiado'}); } catch { toast({title:url}); } } });
        card.append(copy);
        qrGrid.append(card);
      });
      target.append(qrGrid);
      const list = el('details', { class: 'device-url-details' });
      list.append(el('summary', { text: 'Ver direcciones IP y enlaces' }));
      const urlList = el('div', { class: 'device-url-list' });
      for (const url of urls) {
        const row = el('div', { class: 'device-url-row' }, el('a', { href: url, target: '_blank', rel: 'noopener', text: url }));
        const copy = el('button', { class: 'btn-outline', type: 'button', text: 'Copiar', onclick: async () => { try { await navigator.clipboard.writeText(url); toast({title:'Enlace copiado'}); } catch { toast({title:url}); } } });
        row.append(copy); urlList.append(row);
      }
      list.append(urlList);
      target.append(list);
    } catch {
      target.replaceChildren(el('p', { class: 'hint', text: 'No se pudo obtener el enlace de conexión.' }));
    }
  }

  function openSheet(name) {
    if (ui.sheet) closeSheet();
    lastFocus = document.activeElement;
    ui.sheet = name;
    ui.pending = { sessionId: null, camera: null, resolution: null, cameraBackend: null, youtubeCrop: 'full', youtubeUrl: '', youtubeDelay: 10 };
    ui.renaming = false;
    ui.confirmDelete = null;
    ui.adding = null;
    
    if (name === 'settings') cachedCameras = null;
    
    render();
    const sheet = $(SHEETS[name]);
    sheet.inert = false;
    sheet.classList.add('open');
    $('backdrop').classList.add('open');
    sheet.querySelector('[data-close]').focus();
    if (name === 'settings') scanCameras();
  }

  function closeSheet() {
    if (!ui.sheet) return;
    const sheet = $(SHEETS[ui.sheet]);
    sheet.classList.remove('open');
    sheet.inert = true;
    $('backdrop').classList.remove('open');
    ui.sheet = null;
    ui.adding = null;
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  // ------------------------------------------------------------------ equipos
  function renderTeams(s) {
    const profile = profileOf(s);
    const many = state.sessions.length > 1;
    $('teamsIntro').textContent = many
      ? `Elige el equipo de ${s.label}. Cada sesión puede trabajar con uno distinto.`
      : 'Elige con qué equipo trabajas. Los jugadores y ejercicios de cada uno se guardan solos.';

    keepingInput($('teamNew'), () => {
      $('teamNew').replaceChildren(addControl('team'));
    });

    $('teamList').replaceChildren(...state.profiles.map((p) => el(
      'button',
      { class: 'team-card', type: 'button', 'aria-pressed': String(p.id === s.profile_id), disabled: s.recording, onclick: () => openTeam(p.id) },
      el('span', { class: 'team-card-name', text: p.name }),
      el('span', { class: 'team-card-sub', text: plural(p.players.length, 'jugador', 'jugadores') }),
    )));

    $('manageTitle').textContent = profile.name;
    const row = $('manageRow');
    keepingInput(row, () => {
      if (ui.renaming) {
        const input = el('input', { type: 'text', maxlength: '40', 'aria-label': 'Nuevo nombre del equipo', autocomplete: 'off', value: profile.name });
        input.addEventListener('keydown', (event) => {
          if (event.key === 'Escape') { event.stopPropagation(); ui.renaming = false; renderTeams(session()); }
        });
        const form = el('form', { class: 'name-form' }, input, el('button', { class: 'btn-solid', type: 'submit', text: 'Guardar nombre' }));
        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          const result = await run('rename_profile', { profile: profile.id, name: input.value.trim() });
          if (result) { ui.renaming = false; renderTeams(session()); }
        });
        row.replaceChildren(form);
        requestAnimationFrame(() => input.focus());
        return;
      }
      const confirming = ui.confirmDelete === profile.id;
      row.replaceChildren(
        el('button', { class: 'btn-outline', type: 'button', text: 'Renombrar', onclick: () => { ui.renaming = true; renderTeams(session()); } }),
        el('button', { class: 'btn-outline', type: 'button', text: 'Duplicar', onclick: () => run('duplicate_profile', { profile: profile.id }) }),
        el('button', { class: 'btn-outline', type: 'button', text: 'Guardar copia', onclick: () => exportTeam(profile.id) }),
        el('button', {
          class: 'btn-outline btn-danger', type: 'button', disabled: state.profiles.length <= 1,
          text: confirming ? 'Pulsa otra vez para eliminar' : 'Eliminar', onclick: () => deleteTeam(profile.id),
        }),
      );
    });
  }

  async function openTeam(profileId) {
    const result = await run('open_profile', { profile: profileId });
    if (result) closeSheet();
  }

  async function exportTeam(profileId) {
    const result = await run('export_profile', { profile: profileId });
    if (result) {
      toast({ title: 'Copia guardada', detail: result.path, actionLabel: 'Abrir carpeta', onAction: () => run('open_folder', { path: result.path }) });
    }
  }

  function deleteTeam(profileId) {
    if (ui.confirmDelete !== profileId) {
      ui.confirmDelete = profileId;
      clearTimeout(deleteTimer);
      deleteTimer = setTimeout(() => { ui.confirmDelete = null; if (ui.sheet === 'teams') renderTeams(session()); }, 4000);
      renderTeams(session());
      return;
    }
    ui.confirmDelete = null;
    clearTimeout(deleteTimer);
    run('delete_profile', { profile: profileId });
  }

  async function importTeam() {
    const path = await pickFile();
    if (!path) return;
    const result = await run('import_profile', { path });
    if (result) { toast({ title: 'Equipo importado' }); closeSheet(); }
  }

  async function pickFile() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.choose_file) {
      return window.pywebview.api.choose_file();
    }
    return window.prompt('Ruta del archivo .json de la copia del equipo:');
  }

  // ------------------------------------------------------------------ ajustes
  const matchResolution = (cam) => (RESOLUTIONS.find((r) => r.width === cam.width && r.height === cam.height) || RESOLUTIONS[1]).label;

  function renderSettings(s) {
    const cam = s.camera;
    if (ui.pending.sessionId !== s.id) {
      ui.pending = {
        sessionId: s.id,
        camera: cam.index,
        resolution: matchResolution(cam),
        cameraBackend: cam.backend || 'auto',
        youtubeCrop: 'full',
        youtubeUrl: '',
        youtubeDelay: 10
      };
      $('sessionName').value = s.label;
    }
    const takenBy = (endpoint) => state.sessions.find((o) => {
      if (o.id === s.id || o.camera.source !== 'usb' || !(o.camera.connected || o.connecting)) return false;
      const sameEndpoint = o.camera.index === endpoint.index && o.camera.backend === endpoint.backend;
      const sameLogicalDevice = endpoint.device_group && o.camera.device_group === endpoint.device_group;
      return sameEndpoint || sameLogicalDevice;
    });

    // Inventario unificado: cada endpoint conserva su backend y su índice real.
    // Los endpoints con nombre único en ambos sistemas aparecen en una sola tarjeta,
    // con una opción explícita para abrir por DirectShow o por Media Foundation.
    const cameraChips = $('cameraChips');
    if (cachedCameras === null) {
      if (isScanningCameras) {
        cameraChips.replaceChildren(el('p', { class: 'hint', text: 'Buscando webcams y capturadoras físicas...' }));
      } else {
        cameraChips.replaceChildren(
          el('button', { class: 'btn-outline', type: 'button', text: 'Buscar cámaras', onclick: scanCameras })
        );
      }
    } else if (cachedCameras.length > 0) {
      const groups = new Map();
      for (const c of cachedCameras) {
        const groupId = c.display_group || `${c.backend}:${c.index}`;
        if (!groups.has(groupId)) groups.set(groupId, []);
        groups.get(groupId).push(c);
      }
      const choices = [...groups.values()];
      cameraChips.replaceChildren(...choices.map((endpoints) => {
        endpoints.sort((a, b) => (a.backend === 'dshow' ? -1 : b.backend === 'dshow' ? 1 : 0));
        const primary = endpoints[0];
        const virtual = endpoints.some((c) => c.kind === 'virtual');
        const buttons = endpoints.map((c) => {
          const other = takenBy(c);
          const selected = ui.pending.camera === c.index && ui.pending.cameraBackend === c.backend;
          const backendLabel = c.backend === 'dshow' ? 'DirectShow' : c.backend === 'msmf' ? 'Media Foundation' : 'Sistema del equipo';
          return el('button', {
            class: `chip plain camera-endpoint${selected ? ' is-selected' : ''}`,
            type: 'button', 'aria-pressed': String(selected), disabled: !!other,
            title: other ? `La utiliza ${other.label}` : `Abrir ${primary.name} con ${backendLabel}`,
            onclick: () => {
              ui.pending.camera = c.index;
              ui.pending.cameraBackend = c.backend || 'auto';
              renderSettings(session());
            },
          },
            el('span', { class: 'name', text: backendLabel }),
            other ? el('span', { class: 'num', text: other.label }) : null,
          );
        });
        return el('div', { class: 'camera-device-option' },
          el('div', { class: 'camera-device-heading' },
            el('span', { class: 'camera-device-title', text: primary.name || 'Cámara sin nombre' }),
            el('span', { class: `camera-device-type${virtual ? ' is-virtual' : ''}`, text: 'Webcam / capturadora' }),
          ),
          el('div', { class: 'camera-device-endpoints' }, ...buttons),

        );
      }));
      cameraChips.append(el('button', { class: 'btn-outline camera-refresh', type: 'button', text: '↻ Volver a buscar', onclick: scanCameras }));
    } else {
      cameraChips.replaceChildren(
        el('p', { class: 'hint', text: 'No se han encontrado webcams o capturadoras físicas. Comprueba que Windows las reconozca y pulsa Reintentar búsqueda. Para el móvil, utiliza DroidCam directo por IP.' }),
        el('button', { class: 'btn-outline', type: 'button', text: 'Reintentar búsqueda', onclick: scanCameras })
      );
    }

    $('resChips').replaceChildren(...RESOLUTIONS.map((res) => chip({
      label: res.label, plain: true, selected: ui.pending.resolution === res.label, locked: false,
      onClick: () => { ui.pending.resolution = res.label; renderSettings(session()); },
    })));

    $('cameraBlock').disabled = s.recording;
    const same = cam.connected && ui.pending.camera === cam.index && ui.pending.cameraBackend === cam.backend;
    $('btnApplyCamera').textContent = s.connecting ? 'Conectando…' : same ? 'Reconectar con estos ajustes' : 'Conectar';
    $('btnApplyCamera').disabled = s.recording || s.connecting;

    const ytbList = $('youtubeActiveCameras');
    if (ytbList) {
      const activeCameras = s.cameras || [];
      if (activeCameras.length === 0) {
        ytbList.replaceChildren(el('p', { class: 'hint', text: 'No hay cámaras de YouTube conectadas aún.' }));
      } else {
        ytbList.replaceChildren(...activeCameras.map((c) => chip({
          label: c.name || 'CAMYT',
          plain: true,
          selected: s.camera && s.camera.name === c.name,
          locked: false,
          onClick: async () => {
            await run('switch_active_camera', { camera_name: c.name });
            toast({ title: `Vista cambiada a ${c.name}` });
          }
        })));
      }
    }

    const cropChips = $('youtubeCropChips');
    if (cropChips) {
      const cropOptions = [
        { id: 'top_left', label: 'Pista Izquierda (Superior)' },
        { id: 'top_right', label: 'Pista Derecha (Superior)' },
        { id: 'bottom', label: 'Panorámica (Inferior)' },
        { id: 'full', label: 'Pantalla Completa' }
      ];
      cropChips.replaceChildren(...cropOptions.map(opt => chip({
        label: opt.label,
        plain: true,
        selected: ui.pending.youtubeCrop === opt.id,
        locked: false,
        onClick: () => {
          ui.pending.youtubeCrop = opt.id;
          renderSettings(session());
        }
      })));
    }

    const pavilions = state.pavilions || [];
    const pavilionSelect = $('sessionPavilion');
    const desiredPavilion = s.pavilion_id || 'none';
    pavilionSelect.replaceChildren(
      el('option', { value: 'none', text: 'Sin pabellón' }),
      ...pavilions.map((p) => el('option', { value: String(p.id), text: p.name })),
    );
    pavilionSelect.value = pavilions.some((p) => String(p.id) === String(desiredPavilion)) ? String(desiredPavilion) : 'none';
    pavilionSelect.disabled = s.recording;
    const selectedPavilion = pavilions.find((p) => String(p.id) === String(pavilionSelect.value));
    $('btnRenamePavilion').disabled = !selectedPavilion || s.recording;
    $('btnDeletePavilion').disabled = !selectedPavilion || s.recording;
    $('btnCreatePavilion').disabled = s.recording;

    $('sessionsHint').textContent = `${state.sessions.length} de ${state.max_sessions}. Cada sesión tiene su cámara, su equipo y su grabación.`;
    $('btnAddSession').disabled = state.sessions.length >= state.max_sessions;
    $('btnRemoveSession').disabled = state.sessions.length <= 1 || s.recording;

    const many = state.sessions.length > 1;
    $('btnDisplayOne').hidden = !many;
    $('btnDisplayOne').textContent = `Solo ${s.label}`;
    const { operators, viewers } = state.clients;
    $('presence').textContent = `Conectadas ahora: ${plural(operators, 'ventana de control', 'ventanas de control')} y ${plural(viewers, 'pantalla', 'pantallas')}.`;

    $('basePath').textContent = state.base_dir;
    $('btnChooseDir').disabled = state.sessions.some((o) => o.recording);
  }

  async function applyCamera() {
    const res = RESOLUTIONS.find((r) => r.label === ui.pending.resolution) || RESOLUTIONS[1];
    const index = ui.pending.camera;
    const camera_backend = ui.pending.cameraBackend || 'auto';
    $('btnApplyCamera').disabled = true;
    const result = await run('connect', { index, width: res.width, height: res.height, camera_backend });
    if (result) { closeSheet(); toast({ title: `Cámara lista` }); }
    else renderSettings(session());
  }

  async function chooseDirectory() {
    let path = null;
    if (window.pywebview && window.pywebview.api && window.pywebview.api.choose_dir) {
      path = await window.pywebview.api.choose_dir(state.base_dir);
    } else {
      path = window.prompt('Ruta de la carpeta de los clips:', state.base_dir);
    }
    if (path) {
      const result = await run('set_base_dir', { path });
      if (result) toast({ title: 'Carpeta actualizada', detail: path });
    }
  }

  async function openDisplay(sessionId) {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_display) {
      const result = await window.pywebview.api.open_display(sessionId);
      if (!result.ok) { toast({ kind: 'error', title: result.error }); return; }
      toast({ title: result.second_monitor ? 'Pantalla abierta en el segundo monitor' : 'Pantalla abierta', detail: result.second_monitor ? '' : 'No hay un segundo monitor.' });
      return;
    }
    const url = `/display?k=${encodeURIComponent(VIEWER_KEY)}${sessionId ? `&s=${encodeURIComponent(sessionId)}` : ''}`;
    window.open(url, '_blank');
  }

  // ------------------------------------------------------------------ avisos
  function toast({ kind = 'ok', title, detail = '', actionLabel = '', onAction = null }) {
    const node = el(
      'div',
      { class: `toast ${kind === 'error' ? 'error' : ''}`, role: kind === 'error' ? 'alert' : 'status' },
      el('i', { class: 'dot' }),
      el('div', { class: 'toast-text' },
        el('div', { class: 'toast-title', text: title }),
        detail ? el('div', { class: 'toast-detail', text: detail }) : null),
      actionLabel ? el('button', { class: 'btn-outline', type: 'button', text: actionLabel, onclick: () => { if (onAction) onAction(); node.remove(); } }) : null,
    );
    $('toasts').append(node);
    setTimeout(() => node.remove(), kind === 'error' ? 8000 : 5000);
  }

  // ------------------------------------------------------------------ grabar y parar
  function tabletFramePump() {
    const video = $('tabletFeed'), canvas = $('tabletCanvas');
    const ctx = canvas.getContext('2d', { alpha: false });
    let inFlight = false;
    let lastSentAt = 0;
    const minFrameIntervalMs = 1000 / 30;
    const maxWidth = 1280;

    // Follow actual camera frames instead of polling every 100 ms (which hard-capped
    // this path at 10 FPS). If upload/encoding falls behind, skip that frame rather
    // than building a stale queue. The source camera continues at its own real rate.
    const onFrame = () => {
      if (!tabletPump || !tabletStream || !activeId) return;
      scheduleNext();
      const now = performance.now();
      if (inFlight || !video.videoWidth || !video.videoHeight ||
          video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA ||
          now - lastSentAt < minFrameIntervalMs - 1) return;

      lastSentAt = now;
      inFlight = true;
      const width = Math.min(maxWidth, video.videoWidth);
      const height = Math.max(1, Math.round(video.videoHeight * width / video.videoWidth));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width; canvas.height = height;
      }
      ctx.drawImage(video, 0, 0, width, height);
      canvas.toBlob(async (blob) => {
        try {
          if (blob && tabletPump && tabletStream && activeId) {
            const response = await fetch(`/api/remote/frame?session=${encodeURIComponent(activeId)}`, {
              method: 'POST', headers: { 'Content-Type': 'image/jpeg', 'X-Token': TOKEN }, body: blob,
            });
            if (!response.ok) throw new Error(response.status === 413
              ? 'El fotograma supera el tamaño permitido. Reduce la resolución de la cámara.'
              : 'El servidor rechazó un fotograma de la tablet.');
          }
        } catch (err) {
          toast({ kind: 'error', title: 'Se perdió el streaming de la tablet', detail: err.message });
          tabletPump = false;
        } finally {
          inFlight = false;
        }
      }, 'image/jpeg', 0.82);
    };

    let rafId = 0;
    let videoFrameId = 0;
    function scheduleNext() {
      if (!tabletPump || !tabletStream) return;
      if (typeof video.requestVideoFrameCallback === 'function') {
        if (!videoFrameId) videoFrameId = video.requestVideoFrameCallback((_, metadata) => {
          videoFrameId = 0;
          onFrame(metadata);
        });
      } else if (!rafId) {
        rafId = requestAnimationFrame(() => {
          rafId = 0;
          onFrame();
        });
      }
    }

    // Constraints are preferences: actual cadence is measured on the server.
    try {
      const track = tabletStream.getVideoTracks()[0];
      if (track && track.applyConstraints) {
        track.applyConstraints({ width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30, max: 30 } })
          .catch(() => {});
      }
    } catch {}
    tabletPump = true;
    scheduleNext();
  }

  async function toggleTabletCamera() {
    if (tabletStream) {
      tabletPump = false;
      tabletStream.getTracks().forEach(track => track.stop());
      tabletStream = null;
      $('tabletFeed').srcObject = null;
      $('tabletFeed').hidden = true;
      $('feed').hidden = !session()?.camera?.connected;
      $('btnTabletCamera').textContent = 'Usar cámara de esta tablet';
      toast({ title: 'Cámara de tablet desconectada' });
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      toast({ kind: 'error', title: 'Cámara no disponible', detail: 'Abre Extremos por HTTPS y acepta el certificado.' });
      return;
    }
    try {
      tabletStream = await navigator.mediaDevices.getUserMedia({ video: {
        facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 },
        frameRate: { ideal: 30, max: 30 }
      }, audio: false });
      const video = $('tabletFeed');
      video.srcObject = tabletStream;
      video.hidden = false;
      $('feed').hidden = true;
      await video.play();
      await api('connect_remote');
      $('feed').hidden = true;
      $('btnTabletCamera').textContent = 'Desconectar cámara de tablet';
      tabletFramePump();
      toast({ title: 'Cámara de tablet conectada' });
    } catch (err) {
      if (tabletStream) tabletStream.getTracks().forEach(track => track.stop());
      tabletStream = null;
      $('tabletFeed').srcObject = null;
      $('tabletFeed').hidden = true;
      toast({ kind: 'error', title: 'No se pudo activar la cámara', detail: err.message || 'Comprueba permisos del navegador.' });
    }
  }


  const RETRO_OPTIONS = [10, 15, 20, 25, 30, 60];
  let selectedRetroSeconds = 10;
  try {
    const saved = Number(localStorage.getItem('extremos-retro-seconds'));
    if (RETRO_OPTIONS.includes(saved)) selectedRetroSeconds = saved;
  } catch {}

  function renderRetroTimeline() {
    const label = selectedRetroSeconds === 60 ? 'Último minuto' : `Últimos ${selectedRetroSeconds} segundos`;
    $('retroSelectionLabel').textContent = label;
    $('timelineFill').style.width = `${selectedRetroSeconds / 60 * 100}%`;
    document.querySelectorAll('.timeline-option').forEach((button) => {
      const selected = Number(button.dataset.seconds) === selectedRetroSeconds;
      button.classList.toggle('is-selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    $('btnRetro').title = `Guarda ${label.toLowerCase()} del búfer`;
    $('btnRetro').querySelector('.retro-time').textContent = selectedRetroSeconds === 60 ? '1 min' : `${selectedRetroSeconds} s`;
  }

  async function cancelRecording() {
    if (!state || !session().recording) return;
    const button = $('btnCancel');
    button.disabled = true;
    try {
      await run('cancel');
      toast({ title: 'Grabación cancelada', detail: 'El clip se ha descartado.' });
    } catch (error) {
      toast({ kind: 'error', title: error.message });
    } finally {
      if (state) renderActions(session());
    }
  }

  async function startRecording() {
    if (!state || session().recording || $('btnRec').disabled) return;
    await run('start');
  }

  async function stopRecording() {
    if (!state || !session().recording) return;
    const result = await run('stop');
    if (result && result.clip) {
      const clip = result.clip;
      toast({
        title: state.sessions.length > 1 ? `Clip guardado en ${clip.session}` : 'Clip guardado',
        detail: clip.rel.split('/').slice(0, -1).join('/') || clip.name,
        actionLabel: 'Abrir carpeta',
        onAction: () => run('open_folder', { path: clip.path }),
      });
    }
  }

  function toggleRecording() {
    if (state && activeId) (session().recording ? stopRecording() : startRecording());
  }

  async function saveRetrospective() {
    if (!state || !activeId) return;
    const button = $('btnRetro');
    button.disabled = true;
    try {
      const seconds = selectedRetroSeconds;
      const result = await api('retrospective', { seconds });
      if (result && result.clip) {
        toast({ title: `Última jugada guardada (${seconds >= 60 ? '1 min' : seconds + ' s'})`, detail: result.clip.name,
          actionLabel: 'Abrir carpeta', onAction: () => run('open_folder', { path: result.clip.path }) });
      }
    } catch (error) {
      toast({ kind: 'error', title: error.message });
    } finally { button.disabled = false; }
  }

  // ------------------------------------------------------------------ distribución adaptable
  const appLayout = document.querySelector('.app');
  const splitter = $('layoutSplitter');
  const layoutButton = $('btnCameraLayout');
  const LAYOUT_KEY = 'extremos-layout-v1';
  const defaultLayoutMode = window.matchMedia('(max-width: 980px)').matches ? 'hidden' : 'normal';
  let layoutPrefs = { mode: defaultLayoutMode, share: 0.55, stageHeightRatio: 0.42 };
  try { layoutPrefs = { ...layoutPrefs, ...JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}') }; } catch {}
  const launchMode = new URLSearchParams(window.location.search).get('mode');
  if (launchMode === 'hidden' || launchMode === 'buttons') layoutPrefs.mode = 'hidden';
  if (!['normal', 'hidden'].includes(layoutPrefs.mode)) layoutPrefs.mode = defaultLayoutMode;
  layoutPrefs.share = Math.min(0.85, Math.max(0.15, Number(layoutPrefs.share) || 0.55));
  layoutPrefs.stageHeightRatio = Math.min(0.72, Math.max(0.18, Number(layoutPrefs.stageHeightRatio) || 0.42));

  function saveLayoutPrefs() {
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layoutPrefs)); } catch {}
  }
  function isNarrowLayout() { return window.matchMedia('(max-width: 980px), (orientation: portrait)').matches; }
  function applyLayoutMode() {
    appLayout.classList.remove('camera-mini');
    appLayout.classList.toggle('camera-hidden', layoutPrefs.mode === 'hidden');
    layoutButton.textContent = layoutPrefs.mode === 'normal' ? 'Cámara normal' : 'Mostrar cámara';
    layoutButton.setAttribute('aria-label', layoutPrefs.mode === 'normal' ? 'Ocultar cámara' : 'Mostrar cámara');
    splitter.setAttribute('aria-orientation', isNarrowLayout() ? 'horizontal' : 'vertical');
    splitter.setAttribute('aria-valuenow', String(Math.round(layoutPrefs.share * 100)));
    splitter.hidden = layoutPrefs.mode !== 'normal';
    if (isNarrowLayout()) {
      const h = Math.round(window.innerHeight * layoutPrefs.stageHeightRatio);
      appLayout.style.setProperty('--stage-height', `${Math.max(120, Math.min(window.innerHeight * 0.72, h))}px`);
      appLayout.style.gridTemplateColumns = '';
    } else {
      appLayout.style.removeProperty('--stage-height');
      const available = Math.max(0, appLayout.clientWidth - 12);
      const minPanel = Math.min(320, Math.max(180, available * 0.35));
      const minStage = Math.min(220, Math.max(120, available * 0.25));
      const stage = Math.round(Math.max(minStage, Math.min(available - minPanel, available * layoutPrefs.share)));
      appLayout.style.gridTemplateColumns = layoutPrefs.mode === 'hidden' ? '0px 0px minmax(0, 1fr)' : `${stage}px 12px minmax(0, 1fr)`;
    }
  }
  layoutButton.addEventListener('click', () => {
    layoutPrefs.mode = layoutPrefs.mode === 'normal' ? 'hidden' : 'normal';
    saveLayoutPrefs(); applyLayoutMode();
  });
  let dragLayout = null;
  splitter.addEventListener('pointerdown', (event) => {
    if (layoutPrefs.mode !== 'normal') return;
    dragLayout = { id: event.pointerId, x: event.clientX, y: event.clientY, share: layoutPrefs.share, height: appLayout.querySelector('.stage-wrap').getBoundingClientRect().height };
    splitter.setPointerCapture(event.pointerId); event.preventDefault();
  });
  splitter.addEventListener('pointermove', (event) => {
    if (!dragLayout || dragLayout.id !== event.pointerId) return;
    if (isNarrowLayout()) {
      const top = appLayout.getBoundingClientRect().top;
      const h = Math.max(120, Math.min(window.innerHeight * 0.72, event.clientY - top));
      layoutPrefs.stageHeightRatio = h / Math.max(1, window.innerHeight);
      appLayout.style.setProperty('--stage-height', `${Math.round(h)}px`);
    } else {
      const rect = appLayout.getBoundingClientRect();
      const usable = Math.max(1, rect.width - 12);
      const minPanel = Math.min(320, Math.max(180, usable * 0.35));
      const minStage = Math.min(220, Math.max(120, usable * 0.25));
      const stage = Math.max(minStage, Math.min(usable - minPanel, event.clientX - rect.left));
      layoutPrefs.share = stage / usable;
      appLayout.style.gridTemplateColumns = `${Math.round(stage)}px 12px minmax(0, 1fr)`;
      splitter.setAttribute('aria-valuenow', String(Math.round(layoutPrefs.share * 100)));
    }
  });
  const finishLayoutDrag = (event) => {
    if (!dragLayout || (event && dragLayout.id !== event.pointerId)) return;
    dragLayout = null; saveLayoutPrefs(); applyLayoutMode();
  };
  splitter.addEventListener('pointerup', finishLayoutDrag);
  splitter.addEventListener('pointercancel', finishLayoutDrag);
  window.addEventListener('resize', applyLayoutMode);
  applyLayoutMode();

  // ------------------------------------------------------------------ eventos
  $('btnRec').addEventListener('click', startRecording);
  $('btnStop').addEventListener('click', stopRecording);$('btnCancel').addEventListener('click', cancelRecording);
  $('btnRetro').addEventListener('click', saveRetrospective);$('timelineOptions').addEventListener('click', (event) => {
    const option = event.target.closest('[data-seconds]');
    if (!option) return;
    selectedRetroSeconds = Number(option.dataset.seconds);
    try { localStorage.setItem('extremos-retro-seconds', String(selectedRetroSeconds)); } catch {}
    renderRetroTimeline();
  });
  $('btnConnect').addEventListener('click', () => run('connect', { index: session().camera.index }));$('btnTabletCamera').addEventListener('click', toggleTabletCamera);
  renderRetroTimeline();
  $('btnSettings').addEventListener('click', () => openSheet('settings'));
  $('btnConnectDevice').addEventListener('click', showDeviceConnect);$('btnTeam').addEventListener('click', () => openSheet('teams'));
  $('btnDisplay').addEventListener('click', () => openDisplay(null));$('btnDisplayAll').addEventListener('click', () => openDisplay(null));
  $('btnDisplayOne').addEventListener('click', () => openDisplay(activeId));$('backdrop').addEventListener('click', closeSheet);
  document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', closeSheet));
  $('btnApplyCamera').addEventListener('click', applyCamera);
  $('btnConnectDroidcamDirect').addEventListener('click', async () => {
    const host = $('droidcamDirectIp').value.trim();
    const port = Number($('droidcamDirectPort').value || 4747);
    const resolution = RESOLUTIONS.find((r) => r.label === ui.pending.resolution) || RESOLUTIONS[1];
    const button = $('btnConnectDroidcamDirect');
    if (!host) { toast({ kind: 'error', title: 'Introduce la IP que muestra DroidCam en el móvil.' }); return; }
    button.disabled = true;
    button.textContent = 'Conectando al móvil directamente…';
    try {
      const result = await run('connect_droidcam_direct', { host, port, width: resolution.width, height: resolution.height });
      if (result) { closeSheet(); toast({ title: 'DroidCam directo conectado', detail: 'Flujo IP Cam hacia Python, sin driver virtual de Windows.' }); }
    } finally {
      button.disabled = false;
      button.textContent = 'Conectar DroidCam directo';
    }
  });

  $('btnAddYoutubeCamera').addEventListener('click', async () => {
    const urlInput = $('youtubeUrl');
    const url = urlInput ? urlInput.value.trim() : window.prompt('Pega el enlace del directo de YouTube:');
    if (!url) {
      toast({ kind: 'error', title: 'Introduce un enlace válido de YouTube.' });
      return;
    }

    const cropType = ui.pending.youtubeCrop || 'full';

    const button = $('btnAddYoutubeCamera');
    button.disabled = true;
    button.textContent = 'Conectando con YouTube…';

    try {
      const result = await run('connect_youtube', {
        url: url,
        crop_type: cropType
      });

      if (result) {
        closeSheet();
        toast({ title: `Nueva cámara de YouTube añadida exitosamente` });
      }
    } finally {
      button.disabled = false;
      button.textContent = 'Añadir nueva cámara desde YouTube';
    }
  });

  $('btnChooseDir').addEventListener('click', chooseDirectory);
  $('btnOpenBase').addEventListener('click', () => run('open_folder', {}));
  $('btnAddSession').addEventListener('click', async () => { await addSession(); closeSheet(); });$('btnRemoveSession').addEventListener('click', async () => {
    const result = await run('remove_session');
    if (result) closeSheet();
  });
  $('btnImport').addEventListener('click', importTeam);$('sessionNameForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const result = await run('rename_session', { label: $('sessionName').value });
    if (result) toast({ title: 'Sesión renombrada' });
  });
  $('sessionPavilion').addEventListener('change', async () => {
    const result = await run('assign_pavilion', { pavilion: $('sessionPavilion').value });
    if (result) toast({ title: 'Pabellón de la pista actualizado' });
  });
  $('btnCreatePavilion').addEventListener('click', async () => {
    const name = window.prompt('Nombre del nuevo pabellón:');
    if (!name || !name.trim()) return;
    const result = await run('create_pavilion', { name: name.trim() });
    if (result) {
      await run('assign_pavilion', { pavilion: result.pavilion });
      toast({ title: 'Pabellón creado' });
    }
  });
  $('btnRenamePavilion').addEventListener('click', async () => {
    const pavilion = (state.pavilions || []).find((p) => String(p.id) === $('sessionPavilion').value);
    if (!pavilion) return;
    const name = window.prompt('Nuevo nombre del pabellón:', pavilion.name);
    if (!name || !name.trim()) return;
    const result = await run('rename_pavilion', { pavilion: pavilion.id, name: name.trim() });
    if (result) toast({ title: 'Pabellón renombrado' });
  });
  $('btnDeletePavilion').addEventListener('click', async () => {
    const pavilion = (state.pavilions || []).find((p) => String(p.id) === $('sessionPavilion').value);
    if (!pavilion || !window.confirm(`¿Eliminar ${pavilion.name}?`)) return;
    const result = await run('delete_pavilion', { pavilion: pavilion.id });
    if (result) toast({ title: 'Pabellón eliminado' });
  });

  for (const kind of ['players', 'exercises']) {
    $(kind === 'players' ? 'editPlayers' : 'editExercises').addEventListener('click', () => {
      ui.editing[kind] = !ui.editing[kind];
      ui.adding = null;
      ui.picked = null;
      render();
    });
  }

  $('feed').addEventListener('error', () => {
    if (streamKey === null) return;
    const expected = streamKey;
    setTimeout(() => {
      if (streamKey !== expected) return;
      const [id, generation] = expected.split(':');
      $('feed').src = `/stream?t=${encodeURIComponent(TOKEN)}&s=${encodeURIComponent(id)}&g=${generation}&r=${Date.now()}`;
    }, 1000);
  });

  const typing = (target) => target instanceof HTMLElement && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA');

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && ui.sheet) { closeSheet(); return; }
    if (typing(event.target) || ui.sheet || event.ctrlKey || event.metaKey || event.altKey || !state || !activeId) return;
    if (event.code === 'Space') {
      event.preventDefault();
      if (!event.repeat) toggleRecording();
    } else if (/^[1-9]$/.test(event.key) && !session().recording) {
      const player = displayOrder(profileOf(session()))[Number(event.key) - 1];
      if (player) choose({ player: player.id });
    }
  });

  document.addEventListener('keyup', (event) => {
    if (event.code === 'Space' && !typing(event.target) && !ui.sheet) event.preventDefault();
  });

  connectEvents();
})();