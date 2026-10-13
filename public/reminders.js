// Upload reminders on the sidebar — loaded by every page.
//
// Asks /api/reminders what's due or missed (rules in reminders.py), then:
//   - puts a number on the department tab: amber = due today, red = missed;
//   - puts a dot on the menu item that needs doing, with a one-line reason
//     underneath (visible when the tab is open).
// Saving the report clears it on the next page load. If the request fails,
// nothing is drawn — a reminder problem must never break a page.
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
    @media (max-width: 768px) { .rem-note { display: none; } }
    @media print { .rem-badge, .rem-dot, .rem-note { display: none !important; } }
  `;

  function addStyles() {
    if (document.getElementById('rem-styles')) return;
    const style = document.createElement('style');
    style.id = 'rem-styles';
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  function clear() {
    document.querySelectorAll('.rem-badge, .rem-dot, .rem-note').forEach(el => el.remove());
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

      // Dot + reason on the menu item itself.
      items.forEach(i => {
        const cls = i.status === 'overdue' ? 'rem-overdue' : 'rem-due';
        document.querySelectorAll(`a.nav-item[href="${i.href}"]`).forEach(link => {
          const dot = document.createElement('span');
          dot.className = 'rem-dot ' + cls;
          dot.title = i.detail;
          link.appendChild(dot);
          const note = document.createElement('div');
          note.className = 'rem-note ' + cls;
          note.textContent = i.detail;
          link.insertAdjacentElement('afterend', note);
        });
      });
    });
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

  function start() {
    addStyles();
    refresh();
    // The splash screen can stay open all day — check again every 10 minutes.
    setInterval(refresh, 10 * 60 * 1000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
