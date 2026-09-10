// Review page. Every control here only edits a local list of rows and then
// sends that list to /apply; the server decides what actually changed and
// re-renders only those clips. Nothing on this page cuts video directly.
(function () {
  const dataNode = document.getElementById('reviewData');
  if (!dataNode) return;
  const data = JSON.parse(dataNode.textContent);
  const { jobId, duration, editable, words } = data;
  let rows = data.clips.map(c => ({ ...c }));
  let dirty = false;

  const $ = id => document.getElementById(id);
  const player = $('player');
  const clipPlayer = $('clipPlayer');
  const rowsEl = $('clipRows');
  const timeline = $('timeline');
  const textBox = $('cuts');
  const applyBtn = $('applyBtn');
  const dirtyNote = $('dirtyNote');

  // ---------------------------------------------------------- helpers ----
  const fmt = s => {
    s = Math.max(0, s);
    const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${(s % 60).toFixed(3).padStart(6, '0')}`;
  };
  const clamp = v => Math.min(Math.max(0, v), duration || v);
  const nearestWord = (t, dir) => {
    // dir < 0: previous boundary strictly before t; dir > 0: next strictly after.
    if (!words.length) return null;
    if (dir < 0) { for (let i = words.length - 1; i >= 0; i--) if (words[i] < t - 0.001) return words[i]; }
    else { for (const w of words) if (w > t + 0.001) return w; }
    return null;
  };
  const snapToWord = (t, tolerance = 0.15) => {
    let best = null, bestD = tolerance;
    for (const w of words) { const d = Math.abs(w - t); if (d < bestD) { best = w; bestD = d; } }
    return best === null ? t : best;
  };
  const markDirty = () => {
    dirty = true;
    if (applyBtn) applyBtn.disabled = false;
    if (dirtyNote) dirtyNote.hidden = false;
    syncText();
    drawTimeline();
  };

  // ------------------------------------------------------------- rows ----
  function render() {
    if (!rowsEl) return;
    rowsEl.innerHTML = '';
    rows.forEach((row, i) => {
      const tr = document.createElement('tr');
      tr.dataset.index = i;
      const len = row.end - row.start;
      tr.innerHTML = `
        <td class="mono seq">${String(row.sequence || i + 1).padStart(2, '0')}</td>
        <td class="title-cell">
          ${editable ? `<input type="text" class="label" value="${escapeHtml(row.label)}" placeholder="untitled">`
                     : `<span>${escapeHtml(row.label) || '<span class=muted>untitled</span>'}</span>`}
          ${row.error ? `<div class="err">${escapeHtml(row.error)}</div>` : ''}
        </td>
        <td class="edge">
          <span class="mono">${fmt(row.start)}</span>
          ${editable ? `<div class="nudge">
            <button data-act="start" data-d="-1" title="1s earlier">&minus;1s</button>
            <button data-act="start" data-w="-1" title="previous word">&lsaquo;w</button>
            <button data-act="start" data-w="1" title="next word">w&rsaquo;</button>
            <button data-act="start" data-d="1" title="1s later">+1s</button>
          </div>` : ''}
        </td>
        <td class="edge">
          <span class="mono">${fmt(row.end)}</span>
          ${editable ? `<div class="nudge">
            <button data-act="end" data-d="-1" title="1s earlier">&minus;1s</button>
            <button data-act="end" data-w="-1" title="previous word">&lsaquo;w</button>
            <button data-act="end" data-w="1" title="next word">w&rsaquo;</button>
            <button data-act="end" data-d="1" title="1s later">+1s</button>
          </div>` : ''}
        </td>
        <td class="mono">${len.toFixed(1)}s</td>
        <td><span class="badge ${row.status || 'pending'}">${row.status || 'new'}</span></td>
        <td class="actions">
          ${player ? `<button data-act="audition" title="Play this range in the source player">&#9654; range</button>` : ''}
          ${row.status === 'complete' && row.id ? `<button data-act="playclip" title="Play the rendered file">&#9654; clip</button>
            <a href="/jobs/${jobId}/clips/${row.id}">Download</a>` : ''}
          ${editable ? `<button data-act="delete" class="danger" title="Remove this clip">&times;</button>` : ''}
        </td>`;
      rowsEl.appendChild(tr);
    });
    drawTimeline();
  }

  const escapeHtml = s => String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  if (rowsEl) rowsEl.addEventListener('click', e => {
    const btn = e.target.closest('button'); if (!btn) return;
    const tr = btn.closest('tr'); const i = +tr.dataset.index; const row = rows[i];
    const act = btn.dataset.act;
    if (act === 'delete') { rows.splice(i, 1); render(); markDirty(); return; }
    if (act === 'audition') { audition(row.start, row.end); return; }
    if (act === 'playclip') { if (clipPlayer) { clipPlayer.src = `/jobs/${jobId}/clips/${row.id}`; clipPlayer.hidden = false; clipPlayer.play(); } return; }
    if (act === 'start' || act === 'end') {
      let v = row[act];
      if (btn.dataset.d) v = v + Number(btn.dataset.d);
      if (btn.dataset.w) { const w = nearestWord(v, Number(btn.dataset.w)); if (w === null) return; v = w; }
      v = clamp(Math.round(v * 1000) / 1000);
      if (act === 'start' && v >= row.end - 1) return;   // keep at least 1s
      if (act === 'end' && v <= row.start + 1) return;
      row[act] = v; row.status = row.id ? 'pending' : row.status; render(); markDirty();
      if (player) { player.currentTime = v; }
    }
  });
  if (rowsEl) rowsEl.addEventListener('input', e => {
    if (!e.target.classList.contains('label')) return;
    const i = +e.target.closest('tr').dataset.index;
    rows[i].label = e.target.value; if (rows[i].id) rows[i].status = 'pending';
    markDirty();
  });

  // ---------------------------------------------------------- audition ----
  let stopAt = null;
  function audition(start, end) {
    if (!player) return;
    player.currentTime = start; stopAt = end; player.play();
  }
  if (player) player.addEventListener('timeupdate', () => {
    if (stopAt !== null && player.currentTime >= stopAt) { player.pause(); stopAt = null; }
    const t = player.currentTime;
    document.querySelectorAll('.tline').forEach(l => {
      l.classList.toggle('playing', t >= +l.dataset.start && t < +l.dataset.end);
    });
  });

  // --------------------------------------------------------- timeline ----
  function drawTimeline() {
    if (!timeline || !duration) return;
    timeline.innerHTML = '';
    rows.forEach((row, i) => {
      const block = document.createElement('div');
      block.className = 'tclip' + (row.status === 'pending' ? ' pending' : '');
      block.style.left = (row.start / duration * 100) + '%';
      block.style.width = ((row.end - row.start) / duration * 100) + '%';
      block.title = row.label || `clip ${i + 1}`;
      block.innerHTML = `<span class="num">${i + 1}</span>` +
        (editable ? `<span class="handle l" data-edge="start"></span><span class="handle r" data-edge="end"></span>` : '');
      block.dataset.index = i;
      timeline.appendChild(block);
    });
    if (player) {
      const cursor = document.createElement('div'); cursor.className = 'cursor'; cursor.id = 'tcursor';
      timeline.appendChild(cursor);
    }
  }
  if (timeline) {
    timeline.addEventListener('click', e => {
      if (e.target.classList.contains('handle')) return;
      const block = e.target.closest('.tclip');
      const rect = timeline.getBoundingClientRect();
      const t = (e.clientX - rect.left) / rect.width * duration;
      if (player) { player.currentTime = clamp(t); }
      if (block) audition(rows[+block.dataset.index].start, rows[+block.dataset.index].end);
    });
    let drag = null;
    timeline.addEventListener('mousedown', e => {
      const h = e.target.closest('.handle'); if (!h || !editable) return;
      drag = { i: +h.closest('.tclip').dataset.index, edge: h.dataset.edge };
      e.preventDefault();
    });
    window.addEventListener('mousemove', e => {
      if (!drag) return;
      const rect = timeline.getBoundingClientRect();
      let t = clamp((e.clientX - rect.left) / rect.width * duration);
      t = snapToWord(t);
      const row = rows[drag.i];
      if (drag.edge === 'start' && t < row.end - 1) row.start = Math.round(t * 1000) / 1000;
      if (drag.edge === 'end' && t > row.start + 1) row.end = Math.round(t * 1000) / 1000;
      if (row.id) row.status = 'pending';
      drawTimeline(); if (player) player.currentTime = t;
    });
    window.addEventListener('mouseup', () => { if (drag) { drag = null; render(); markDirty(); } });
    if (player) player.addEventListener('timeupdate', () => {
      const c = $('tcursor'); if (c && duration) c.style.left = (player.currentTime / duration * 100) + '%';
    });
  }

  // ----------------------------------------------------------- adding ----
  let pendingStart = null;
  const setStart = $('markIn'), setEnd = $('markOut');
  if (setStart) setStart.onclick = () => {
    if (!player) return;
    pendingStart = Math.round(player.currentTime * 1000) / 1000;
    setStart.textContent = `start ${fmt(pendingStart)}`;
    setEnd.disabled = false;
  };
  if (setEnd) setEnd.onclick = () => {
    if (!player || pendingStart === null) return;
    const end = Math.round(player.currentTime * 1000) / 1000;
    if (end <= pendingStart + 1) return;
    rows.push({ id: null, label: '', start: pendingStart, end, status: 'new' });
    pendingStart = null; setStart.textContent = 'set start'; setEnd.disabled = true;
    render(); markDirty();
  };

  // transcript: click seeks, shift-click opens a range, next click closes it
  let rangeStart = null;
  document.querySelectorAll('.tline').forEach(line => line.onclick = e => {
    const s = +line.dataset.start;
    if (e.shiftKey && editable) {
      document.querySelectorAll('.tline').forEach(l => l.classList.remove('range-start'));
      rangeStart = s; line.classList.add('range-start'); return;
    }
    if (rangeStart !== null) {
      const end = +line.dataset.end;
      if (end > rangeStart + 1) { rows.push({ id: null, label: '', start: rangeStart, end, status: 'new' }); render(); markDirty(); }
      rangeStart = null; document.querySelectorAll('.tline').forEach(l => l.classList.remove('range-start')); return;
    }
    if (player) { player.currentTime = s; player.play(); }
  });

  const find = $('findText');
  if (find) find.oninput = () => {
    const q = find.value.trim().toLowerCase();
    document.querySelectorAll('.tline').forEach(l => {
      const hit = q && l.querySelector('.ttext').textContent.toLowerCase().includes(q);
      l.classList.toggle('hit', !!hit); l.classList.toggle('dim', !!q && !hit);
    });
  };

  // ---------------------------------------------------------- text box ----
  function syncText() {
    if (!textBox) return;
    textBox.value = rows.map(r => `${fmt(r.start)} - ${fmt(r.end)}${r.label ? ' | ' + r.label : ''}`).join('\n') + (rows.length ? '\n' : '');
  }

  // -------------------------------------------------------------- apply ----
  if (applyBtn) applyBtn.onclick = async () => {
    applyBtn.disabled = true; applyBtn.textContent = 'Saving';
    const body = { rows: rows.map(r => ({ id: r.id, start: r.start, end: r.end, label: r.label || null })) };
    try {
      const res = await fetch(`/jobs/${jobId}/apply`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (!res.ok) { const err = (await res.json()).error || 'Could not save.'; alert(err); applyBtn.disabled = false; applyBtn.textContent = 'Apply changes'; return; }
      location.reload();
    } catch (e) { alert('Could not reach the server: ' + e.message); applyBtn.disabled = false; applyBtn.textContent = 'Apply changes'; }
  };
  window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

  render(); syncText();
})();
