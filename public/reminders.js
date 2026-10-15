// Upload reminders on the sidebar — loaded by every page.
//
// Asks /api/reminders what's due or missed (rules in reminders.py), then:
//   - puts a number on the department tab: amber = due today, red = missed;
//   - puts a dot on the menu item that needs doing, with a one-line reason
//     underneath (visible when the tab is open);
//   - a reminder with no menu item of its own (the Monday open-jobs check)
//     gets its own line at the bottom of the tab, with a "Mark as done" button.
// Saving the report clears it on the next page load — or straight away, when
// the page's save code calls window.refreshReminders() after a good save.
// If the request fails, nothing is drawn — a reminder problem must never
// break a page.
(function () {
  const CSS = `
    .rem-badge { display: inline-flex; align-items: center; justify-content: center;
      min-width: 18px; height: 18px; padding: 0 5px; margin-left: 8px; border-radius: 9px;
      font-size: 10.5px; font-weight: bold; letter-spacing: 0; vertical-align: middle; }
    .rem-badge.rem-overdue { background: #d64545; color: #fff; }
    .rem-badge.rem-due { background: #e0a020; color: #243547; }
    .rem-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
      margin-left: 8px; vertical-align: middle; }
    .rem-dot.rem-overdue { background: #d64545; }
    .rem-dot.rem-due { background: #e0a020; }
    .rem-note { font-size: 10.5px; line-height: 1.3; padding: 0 16px 6px 42px; margin-top: -6px; }
    .rem-note.rem-overdue { color: #ff8a8a; }
    .rem-note.rem-due { color: #e6b450; }
    .rem-item { cursor: default; }
    .rem-done { display: block; margin-top: 5px; padding: 3px 8px; font-size: 10.5px; font-weight: bold;
      color: #95C11F; background: transparent; border: 1px solid #95C11F; border-radius: 4px; cursor: pointer; }
    .rem-done:hover { background: rgba(149,193,31,0.15); }
    .rem-done:disabled { opacity: 0.6; cursor: wait; }
    @media (max-width: 768px) { .rem-note { display: none; } }
    @media print { .rem-badge, .rem-dot, .rem-note, .rem-item { display: none !important; } }
  `;

  function addStyles() {
    if (document.getElementById('rem-styles')) return;
    const style = document.createElement('style');
    style.id = 'rem-styles';
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  function clear() {
    document.querySelectorAll('.rem-badge, .rem-dot, .rem-note, .rem-item').forEach(el => el.remove());
  }

  function makeNote(item, cls) {
    const note = document.createElement('div');
    note.className = 'rem-note ' + cls;
    note.textContent = item.detail;
    return note;
  }

  function makeDot(item, cls) {
    const dot = document.createElement('span');
    dot.className = 'rem-dot ' + cls;
    dot.title = item.detail;
    return dot;
  }

  // A reminder with no menu item of its own: a plain line at the bottom of
  // its tab, plus the "Mark as done" button if it's cleared by hand.
  function addOwnLine(dept, item, cls) {
    const group = document.getElementById('nav-group-' + dept);
    if (!group) return;
    const line = document.createElement('div');
    line.className = 'nav-item rem-item';
    line.textContent = item.label;
    line.appendChild(makeDot(item, cls));
    const note = makeNote(item, cls);
    if (item.ack) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'rem-done';
      btn.textContent = '✓ Mark as done';
      btn.addEventListener('click', e => { e.stopPropagation(); markDone(item.id, btn); });
      note.appendChild(btn);
    }
    group.appendChild(line);
    group.appendChild(note);
  }

  function draw(reminders) {
    const open = reminders.filter(r => r.status === 'due' || r.status === 'overdue');
    const byDept = {};
    open.forEach(r => { (byDept[r.dept] = byDept[r.dept] || []).push(r); });

    Object.keys(byDept).forEach(dept => {
      const items = byDept[dept];
      const level = items.some(i => i.status === 'overdue') ? 'rem-overdue' : 'rem-due';

      // Number on the tab, next to its name.
      const header = document.getElementById('nav-header-' + dept);
      const nameSpan = header && header.querySelector('span');
      if (nameSpan) {
        const badge = document.createElement('span');
        badge.className = 'rem-badge ' + level;
        badge.textContent = items.length;
        badge.title = items.map(i => `${i.label}: ${i.detail}`).join('\n');
        nameSpan.appendChild(badge);
      }

      // Dot + reason on the menu item itself, or a line of its own.
      items.forEach(i => {
        const cls = i.status === 'overdue' ? 'rem-overdue' : 'rem-due';
        if (!i.href) { addOwnLine(dept, i, cls); return; }
        document.querySelectorAll(`a.nav-item[href="${i.href}"]`).forEach(link => {
          link.appendChild(makeDot(i, cls));
          link.insertAdjacentElement('afterend', makeNote(i, cls));
        });
      });
    });
  }

  async function markDone(id, btn) {
    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
      const res = await fetch('/api/reminders/ack', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id }),
      });
      const data = await res.json();
      if (!res.ok || data.error || !Array.isArray(data.reminders)) throw new Error(data.error || res.status);
      clear();
      draw(data.reminders);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "Couldn't save — try again";
    }
  }

  async function refresh() {
    let data;
    try {
      const res = await fetch('/api/reminders', { cache: 'no-store' });
      data = await res.json();
    } catch (e) {
      return;
    }
    if (!data || !Array.isArray(data.reminders)) return;
    clear();
    draw(data.reminders);
  }

  // Let a page ask for a fresh check right after it saves something, so the
  // badge and the "not saved yet" note disappear without a page refresh.
  // Everything else in here stays private to this file.
  window.refreshReminders = refresh;

  function start() {
    addStyles();
    refresh();
    // The splash screen can stay open all day — check again every 10 minutes.
    setInterval(refresh, 10 * 60 * 1000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
