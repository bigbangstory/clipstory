// The transcript document.
//
// Everything here edits a local list of deletions and autosaves it. Nothing on
// this page encodes video: the preview works by making the player skip the
// deleted ranges, which is why editing feels instant on hardware that would
// take minutes to actually render the result.
(function () {
  const node = document.getElementById('editData');
  if (!node) return;
  let state = JSON.parse(node.textContent);
  const jobId = state.job_id;
  const duration = state.duration;

  const $ = id => document.getElementById(id);
  const doc = $('document');
  const player = $('player');
  const undoStack = [];
  let words = state.words.slice();       // [{s, e, t, d}]
  let selection = null;                   // {from, to} indices, inclusive
  let saveTimer = null;

  // ------------------------------------------------------------- helpers ----
  const clock = s => {
    s = Math.max(0, s);
    const m = Math.floor(s / 60);
    return `${String(m).padStart(2, '0')}:${(s % 60).toFixed(0).padStart(2, '0')}`;
  };
  const pretty = s => {
    if (s < 60) return `${s.toFixed(1)}s`;
    const m = Math.floor(s / 60);
    return `${m}m ${Math.round(s % 60)}s`;
  };
  const inSelection = i => selection && i >= Math.min(selection.from, selection.to)
                                     && i <= Math.max(selection.from, selection.to);

  // ---------------------------------------------------------- rendering ----
  function render() {
    // Paragraphs break on a noticeable pause, which is close enough to where a
    // speaker actually changes thought, and keeps the document readable.
    const html = [];
    let open = false;
    words.forEach((w, i) => {
      const gap = i > 0 ? w.s - words[i - 1].e : 0;
      if (!open || gap > 1.5) {
        if (open) html.push('</p>');
        html.push(`<p class="para"><span class="ptime mono">${clock(w.s)}</span>`);
        open = true;
      }
      const classes = ['w'];
      if (w.d) classes.push('cut');
      if (inSelection(i)) classes.push('sel');
      html.push(`<span class="${classes.join(' ')}" data-i="${i}">${escapeHtml(w.t)}</span> `);
    });
    if (open) html.push('</p>');
    doc.innerHTML = html.join('');
    refreshButtons();
  }

  const escapeHtml = s => String(s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function refreshButtons() {
    const has = selection !== null;
    const anySelectedKept = has && sliceIndices().some(i => !words[i].d);
    const anySelectedCut = has && sliceIndices().some(i => words[i].d);
    $('strikeBtn').disabled = !anySelectedKept;
    $('restoreBtn').disabled = !anySelectedCut;
    $('undoBtn').disabled = undoStack.length === 0;
  }

  function sliceIndices() {
    if (!selection) return [];
    const a = Math.min(selection.from, selection.to);
    const b = Math.max(selection.from, selection.to);
    return Array.from({ length: b - a + 1 }, (_, k) => a + k);
  }

  function paint(figures) {
    $('figKept').textContent = pretty(figures.kept_duration);
    $('figRemoved').textContent = pretty(figures.removed_duration);
    $('figSegments').textContent = Math.max(0, figures.segment_count - 1);

    const s = figures.summary || {};
    const bits = [];
    if (s.filler) bits.push(`${s.filler.count} filler word${s.filler.count === 1 ? '' : 's'}`);
    if (s.silence) bits.push(`${s.silence.count} pause${s.silence.count === 1 ? '' : 's'} shortened`);
    if (s.manual) bits.push(`${s.manual.count} of your own cut${s.manual.count === 1 ? '' : 's'}`);
    $('breakdown').textContent = bits.length ? `Removed: ${bits.join(', ')}.` : 'Nothing removed yet.';

    const problem = $('editProblem');
    if (figures.problem) {
      problem.textContent = figures.problem;
      problem.style.display = '';
      $('exportBtn').disabled = true;
    } else {
      problem.style.display = 'none';
      $('exportBtn').disabled = false;
    }
  }

  // ------------------------------------------------------------ editing ----
  function snapshot() {
    undoStack.push(words.map(w => w.d));
    if (undoStack.length > 50) undoStack.shift();
  }

  function setDeleted(indices, deleted) {
    if (!indices.length) return;
    snapshot();
    indices.forEach(i => { words[i].d = deleted; });
    // Drop the selection afterwards. Leaving it on makes struck words look
    // both selected and cut at the same time, which reads as a half-applied
    // action rather than a finished one.
    selection = null;
    render();
    save();
  }

  $('strikeBtn').onclick = () => setDeleted(sliceIndices().filter(i => !words[i].d), true);
  $('restoreBtn').onclick = () => setDeleted(sliceIndices().filter(i => words[i].d), false);
  $('undoBtn').onclick = () => {
    const previous = undoStack.pop();
    if (!previous) return;
    words.forEach((w, i) => { w.d = previous[i]; });
    render();
    save();
  };

  // Drag to select. A plain click without dragging seeks instead, so the
  // common action (listen to this bit) needs no modifier.
  let dragging = false, dragged = false;
  doc.addEventListener('mousedown', e => {
    const span = e.target.closest('.w');
    if (!span) return;
    dragging = true; dragged = false;
    selection = { from: +span.dataset.i, to: +span.dataset.i };
    render();
    e.preventDefault();
  });
  doc.addEventListener('mousemove', e => {
    if (!dragging) return;
    const span = e.target.closest('.w');
    if (!span) return;
    const i = +span.dataset.i;
    if (i !== selection.to) { selection.to = i; dragged = true; render(); }
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    if (!dragged && selection) {
      const word = words[selection.from];
      if (word.d) {
        // Clicking a struck word puts it back. The quickest possible undo of
        // a cleanup pass that went one word too far.
        setDeleted([selection.from], false);
        selection = null;
        render();
        return;
      }
      if (player) { player.currentTime = word.s; player.play(); }
    }
    refreshButtons();
  });

  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if ((e.key === 'Delete' || e.key === 'Backspace') && selection) {
      e.preventDefault();
      setDeleted(sliceIndices().filter(i => !words[i].d), true);
    } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      $('undoBtn').click();
    } else if (e.key === 'Escape') {
      selection = null; render();
    }
  });

  // ---------------------------------------------------------- persisting ----
  function deletionsFromWords() {
    // Consecutive struck words become one range. Reason is always manual here;
    // the server keeps its own record of which ranges a cleanup pass added.
    const out = [];
    let run = null;
    words.forEach(w => {
      if (w.d) {
        if (run && Math.abs(w.s - run.end) < 1.0) run.end = Math.max(run.end, w.e);
        else { if (run) out.push(run); run = { start: w.s, end: w.e }; }
      } else if (run) { out.push(run); run = null; }
    });
    if (run) out.push(run);
    return out.map(r => ({ start: r.start, end: r.end, reason: 'manual' }));
  }

  function save() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        const res = await fetch(`/jobs/${jobId}/edit`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ deletions: deletionsFromWords() }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          $('editProblem').textContent = err.error || 'Could not save your changes.';
          $('editProblem').style.display = '';
          return;
        }
        state = await res.json();
        words = state.words.slice();
        keepRanges = state.keep_ranges;
        paint(state);
        render();
      } catch (e) {
        $('editProblem').textContent = 'Could not reach the server. Your changes are not saved.';
        $('editProblem').style.display = '';
      }
    }, 400);
  }

  // ------------------------------------------------------------ cleanup ----
  $('cleanupBtn').onclick = async () => {
    const button = $('cleanupBtn');
    button.disabled = true; button.textContent = 'Working';
    try {
      const res = await fetch(`/jobs/${jobId}/edit/cleanup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          remove_fillers: $('optFillers').checked,
          include_conversational: $('optConversational').checked,
          shorten_silences: $('optSilence').checked,
        }),
      });
      const data = await res.json();
      if (!res.ok) { alert(data.error || 'Cleanup failed.'); return; }
      state = data;
      words = state.words.slice();
      keepRanges = state.keep_ranges;
      undoStack.length = 0;
      paint(state);
      render();
    } catch (e) {
      alert('Could not reach the server: ' + e.message);
    } finally {
      button.disabled = false; button.textContent = 'Run cleanup';
    }
  };

  // ------------------------------------------------------------- export ----
  $('exportBtn').onclick = async () => {
    const button = $('exportBtn');
    button.disabled = true; button.textContent = 'Queueing';
    // Flush any pending autosave first, so the export reflects what is on
    // screen rather than the state from before the last keystroke.
    clearTimeout(saveTimer);
    try {
      await fetch(`/jobs/${jobId}/edit`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ deletions: deletionsFromWords() }),
      });
      const res = await fetch(`/jobs/${jobId}/edit/export`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        $('editProblem').textContent = err.error || 'Could not start the export.';
        $('editProblem').style.display = '';
        button.disabled = false; button.textContent = 'Export edited video';
        return;
      }
      location.reload();
    } catch (e) {
      $('editProblem').textContent = 'Could not reach the server: ' + e.message;
      $('editProblem').style.display = '';
      button.disabled = false; button.textContent = 'Export edited video';
    }
  };

  // ------------------------------------------------------------ preview ----
  // The whole reason this is affordable: the edit is previewed by seeking past
  // the removed ranges, not by rendering anything.
  let keepRanges = state.keep_ranges;
  if (player) {
    player.addEventListener('timeupdate', () => {
      const t = player.currentTime;
      if ($('previewEdited').checked && keepRanges.length) {
        const inside = keepRanges.some(([a, b]) => t >= a - 0.01 && t < b);
        if (!inside) {
          const next = keepRanges.find(([a]) => a > t);
          if (next) player.currentTime = next[0];
          else { player.pause(); player.currentTime = keepRanges[keepRanges.length - 1][1]; }
        }
      }
      // Follow along in the document.
      doc.querySelectorAll('.w.playing').forEach(el => el.classList.remove('playing'));
      const index = words.findIndex(w => t >= w.s && t < w.e);
      if (index >= 0) {
        const el = doc.querySelector(`.w[data-i="${index}"]`);
        if (el) el.classList.add('playing');
      }
    });
  }

  paint(state);
  render();
})();
