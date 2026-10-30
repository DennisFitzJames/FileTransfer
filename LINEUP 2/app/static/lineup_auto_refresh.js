/* LINEUP Live Pages — publication-aware refresh. No extra dependencies. */
(() => {
  'use strict';
  const root = document.body;
  const loadedPublication = Number(root.dataset.lineupPublishedMs);
  if (!Number.isFinite(loadedPublication) || loadedPublication <= 0) return;

  const POLL_INTERVAL_MS = 30000;
  const IDLE_GRACE_MS = 12000;
  const STORAGE_KEY = `lineup-view:${location.pathname}`;
  const preservedIds = ['wc-search', 'wc-filter', 'plan-search', 'status-filter'];
  let lastInteraction = 0;
  let formModified = false;
  let newVersionAvailable = false;
  let fetching = false;
  let banner = null;

  function restoreView() {
    try {
      const saved = sessionStorage.getItem(STORAGE_KEY);
      if (!saved) return;
      sessionStorage.removeItem(STORAGE_KEY);
      const state = JSON.parse(saved);
      for (const id of preservedIds) {
        const element = document.getElementById(id);
        if (element && state.filters && Object.prototype.hasOwnProperty.call(state.filters, id)) {
          element.value = state.filters[id];
          element.dispatchEvent(new Event('input', { bubbles: true }));
          element.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }
      if (typeof state.screen === 'string') {
        const target = [...document.querySelectorAll('.screen-dot')]
          .find((el) => el.dataset.screenTarget === state.screen);
        if (target) target.click();
      }
      if (Number.isFinite(state.scrollY)) {
        requestAnimationFrame(() => window.scrollTo(state.scrollX || 0, state.scrollY));
      }
      if (state.hash && location.hash !== state.hash) {
        // Do not navigate: the hash is restored for operation links only.
        history.replaceState(null, '', location.pathname + location.search + state.hash);
      }
    } catch (error) {
      console.debug('LINEUP: unable to restore browser view', error);
    }
  }

  function saveView() {
    try {
      const filters = {};
      for (const id of preservedIds) {
        const element = document.getElementById(id);
        if (element) filters[id] = element.value;
      }
      const activeScreen = document.querySelector('.screen-dot.active');
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
        filters,
        screen: activeScreen?.dataset.screenTarget ?? null,
        scrollX: window.scrollX,
        scrollY: window.scrollY,
        hash: location.hash,
      }));
    } catch (error) {
      console.debug('LINEUP: unable to save browser view', error);
    }
  }

  function hasActiveEditing() {
    const active = document.activeElement;
    const typing = active && active !== document.body && (
      active.matches('input:not([type="button"]):not([type="submit"]), textarea, select')
      || active.isContentEditable
    );
    const modalOpen = !!document.querySelector('.why-backdrop.open, .split-modal.open');
    const recentlyActive = lastInteraction && Date.now() - lastInteraction < IDLE_GRACE_MS;
    return formModified || typing || modalOpen || recentlyActive;
  }

  function refresh(force = false) {
    if (formModified && force && !window.confirm(
      'You have unsaved form changes. Refresh and discard those changes?'
    )) return;
    if (!force && hasActiveEditing()) return;
    saveView();
    window.location.reload();
  }

  function showNotice() {
    if (banner) return;
    banner = document.createElement('div');
    banner.setAttribute('role', 'status');
    banner.setAttribute('aria-live', 'polite');
    banner.style.cssText = [
      'position:fixed','bottom:20px','right:20px','z-index:9999',
      'display:flex','align-items:center','gap:12px','flex-wrap:wrap',
      'max-width:min(430px,calc(100vw - 40px))',
      'background:#1e2535','color:#f9fafb','padding:12px 14px',
      'border:1px solid #65708c','border-radius:14px',
      'font:600 13px/1.4 system-ui,sans-serif',
      'box-shadow:0 14px 35px rgba(0,0,0,.24)',
    ].join(';');
    const label = document.createElement('span');
    label.textContent = 'New production data is available';
    const action = document.createElement('button');
    action.type = 'button';
    action.textContent = 'Refresh now';
    action.style.cssText = 'border:0;border-radius:9px;padding:8px 11px;cursor:pointer;font:700 12px system-ui,sans-serif;background:#d9e5ff;color:#17233b';
    action.addEventListener('click', () => refresh(true));
    banner.append(label, action);
    document.body.appendChild(banner);
  }

  async function checkForPublication() {
    if (fetching || document.visibilityState === 'hidden') return;
    fetching = true;
    try {
      const reply = await fetch('/api/lineup-publication', {
        method: 'GET', cache: 'no-store', headers: { Accept: 'application/json' },
      });
      if (!reply.ok) return;
      const state = await reply.json();
      if (!state.available || state.processing === 'running') return;
      const publishedMs = Number(state.published_ms);
      // A generated dashboard stamps its revision just before atomic publication;
      // allow for its sub-second file write time to avoid a reload loop.
      if (Number.isFinite(publishedMs) && publishedMs > loadedPublication + 1500) {
        newVersionAvailable = true;
        if (!hasActiveEditing()) refresh();
        else showNotice();
      }
    } catch (error) {
      // A temporary server restart/offline interval must not disrupt the page.
      console.debug('LINEUP freshness check unavailable', error);
    } finally {
      fetching = false;
    }
  }

  document.addEventListener('input', (event) => {
    lastInteraction = Date.now();
    if (event.target.closest('form')) formModified = true;
  }, { passive: true });
  document.addEventListener('change', (event) => {
    lastInteraction = Date.now();
    if (event.target.closest('form')) formModified = true;
  }, { passive: true });
  document.addEventListener('keydown', () => { lastInteraction = Date.now(); }, { passive: true });
  document.addEventListener('pointerdown', () => { lastInteraction = Date.now(); }, { passive: true });
  document.addEventListener('scroll', () => { lastInteraction = Date.now(); }, { passive: true });
  document.addEventListener('submit', () => { formModified = false; });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') checkForPublication();
  });

  restoreView();
  // If new data arrived while the user was editing, refresh after they finish.
  setInterval(() => {
    if (newVersionAvailable && !hasActiveEditing()) refresh();
  }, 5000);
  setInterval(checkForPublication, POLL_INTERVAL_MS);
  checkForPublication();
})();
