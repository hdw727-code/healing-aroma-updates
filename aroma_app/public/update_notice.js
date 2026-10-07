/* Startup notice for backend-validated releases. This module never applies an update. */
(() => {
  'use strict';

  const STATES = new Set(['idle', 'checking', 'up_to_date', 'available', 'incompatible',
    'offline', 'timeout', 'invalid', 'recovery_required']);
  const REASONS = new Set(['schema_incompatible', 'manual_check_required', 'supervisor_not_attached', 'recovery_required']);
  const VERSION = /^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
  const POLL_INTERVAL = 1500, MAX_POLLS = 16, MAX_POLL_TIME = 30000;
  // Memory only: a page reload starts a new notice session.
  const dismissedSequences = new Set();
  const integer = (value, minimum = 0) => Number.isSafeInteger(value) && value >= minimum;
  const version = value => typeof value === 'string' && value.length <= 100 && VERSION.test(value);
  const boundedText = (value, maximum) => typeof value === 'string' && value.length <= maximum;

  function checkedStatus(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
        typeof value.enabled !== 'boolean' || typeof value.automatic_check !== 'boolean' ||
        !STATES.has(value.state) || !version(value.current_version) ||
        !integer(value.current_schema, 1) || !integer(value.current_sequence) ||
        typeof value.update_available !== 'boolean' || typeof value.apply_enabled !== 'boolean' ||
        value.manual_apply_only !== true ||
        !(value.apply_disabled_reason === null || REASONS.has(value.apply_disabled_reason)) ||
        !(value.checked_at === null || boundedText(value.checked_at, 100)) ||
        !(value.error_code === null || boundedText(value.error_code, 100)) ||
        !boundedText(value.message, 2000)) throw new Error('update_notice_status_invalid');
    let candidate = null;
    if (value.candidate !== null) {
      const c = value.candidate;
      if (!c || typeof c !== 'object' || Array.isArray(c) || !version(c.version) ||
          !integer(c.sequence, 1) || !integer(c.schema_target, 1) ||
          typeof c.compatible !== 'boolean' || typeof c.newer !== 'boolean') {
        throw new Error('update_notice_candidate_invalid');
      }
      candidate = Object.freeze({version: c.version, sequence: c.sequence,
        schema_target: c.schema_target, compatible: c.compatible, newer: c.newer});
    }
    return Object.freeze({...value, candidate});
  }

  function noticeKind(status) {
    const candidate = status?.candidate;
    if (!status?.enabled || !candidate || candidate.newer !== true ||
        candidate.sequence <= status.current_sequence || dismissedSequences.has(candidate.sequence)) return '';
    if (status.state === 'available' && status.update_available === true && candidate.compatible === true) return 'available';
    if (status.state === 'incompatible' && candidate.compatible === false && status.apply_enabled === false &&
        status.apply_disabled_reason === 'schema_incompatible') return 'incompatible';
    return '';
  }

  function create({api, onOpenUpdates} = {}) {
    if (typeof api !== 'function') throw new TypeError('update_notice_api_required');
    const state = {started: false, disposed: false, epoch: 0, request: 0, status: null,
      root: null, timer: null, polls: 0, checkingSince: null, read: null, manual: null};

    function clearTimer() { if (state.timer !== null) clearTimeout(state.timer); state.timer = null; }
    function removeNotice() { state.root?.remove(); state.root = null; }
    function resetPolling() { clearTimer(); state.polls = 0; state.checkingSince = null; }

    function render() {
      const kind = noticeKind(state.status);
      if (!kind || state.disposed) { removeNotice(); return; }
      const status = state.status, candidate = status.candidate;
      let root = state.root;
      if (!root?.isConnected) {
        root = document.getElementById('app-update-notice') || document.createElement('aside');
        root.id = 'app-update-notice';
        root.className = 'app-update-notice notice';
        root.setAttribute('role', 'status');
        root.setAttribute('aria-live', 'polite');
        root.setAttribute('aria-atomic', 'true');
        if (!root.isConnected) {
          const main = document.getElementById('main');
          const header = document.querySelector('.app-header') || document.querySelector('header');
          if (header?.parentNode && !main?.contains(header)) header.insertAdjacentElement('afterend', root);
          else if (main?.parentNode) main.parentNode.insertBefore(root, main);
          else document.body.appendChild(root);
        }
        state.root = root;
      }
      root.replaceChildren();
      const copy = document.createElement('div');
      copy.className = 'app-update-notice-copy';
      const title = document.createElement('h2');
      title.className = 'app-update-notice-title';
      title.textContent = kind === 'available' ? '새 버전이 있어요' : '새 버전 안내';
      const message = document.createElement('p');
      message.textContent = kind === 'available'
        ? `새 버전 ${candidate.version}의 변경 내용과 적용 가능 여부를 확인해 주세요.`
        : `새 버전 ${candidate.version}은 자료 형식이 달라 지금 적용할 수 없어요.`;
      copy.appendChild(title); copy.appendChild(message); root.appendChild(copy);

      const actions = document.createElement('div');
      actions.className = 'app-update-notice-actions button-row';
      const open = document.createElement('button');
      open.type = 'button'; open.className = 'btn';
      open.textContent = kind === 'available' ? '업데이트 확인' : '업데이트 안내';
      open.dataset.updateNoticeAction = 'open';
      open.disabled = typeof onOpenUpdates !== 'function';
      open.addEventListener('click', () => {
        if (state.disposed || !noticeKind(state.status) || typeof onOpenUpdates !== 'function') return;
        try { Promise.resolve(onOpenUpdates(state.status)).catch(() => {}); } catch (_) {}
      });
      const dismiss = document.createElement('button');
      dismiss.type = 'button'; dismiss.className = 'btn secondary'; dismiss.textContent = '나중에';
      dismiss.dataset.updateNoticeAction = 'dismiss';
      dismiss.addEventListener('click', () => {
        if (state.status?.candidate) dismissedSequences.add(state.status.candidate.sequence);
        removeNotice();
      });
      actions.appendChild(open); actions.appendChild(dismiss); root.appendChild(actions);
    }

    function schedule() {
      clearTimer();
      if (state.disposed || !state.started || state.status?.state !== 'checking') {
        state.polls = 0; state.checkingSince = null; return;
      }
      if (state.checkingSince === null) state.checkingSince = Date.now();
      if (state.polls >= MAX_POLLS || Date.now() - state.checkingSince >= MAX_POLL_TIME) return;
      state.polls++;
      state.timer = setTimeout(() => { state.timer = null; refresh(); }, POLL_INTERVAL);
    }

    async function request(manual) {
      if (!state.started || state.disposed) return null;
      clearTimer();
      const epoch = state.epoch, serial = ++state.request;
      try {
        const raw = await api(manual ? '/api/updates/notice/check' : '/api/updates/status',
          manual ? {method: 'POST', body: {}, timeoutMs: 5000} : {timeoutMs: 5000});
        if (state.disposed || epoch !== state.epoch || serial !== state.request) return null;
        state.status = checkedStatus(raw);
        render(); schedule(); return state.status;
      } catch (_) {
        if (!state.disposed && epoch === state.epoch && serial === state.request) {
          state.status = null; resetPolling(); removeNotice();
        }
        return null;
      }
    }

    function refresh() {
      if (!state.started || state.disposed) return Promise.resolve(null);
      if (state.manual) return state.manual;
      if (state.read) return state.read;
      const pending = request(false);
      state.read = pending;
      pending.finally(() => { if (state.read === pending) state.read = null; });
      return pending;
    }

    function manualCheck() {
      if (!state.started || state.disposed) return Promise.resolve(null);
      if (state.manual) return state.manual;
      resetPolling();
      // An earlier GET may still finish; request serials ignore that response.
      state.read = null;
      const pending = request(true);
      state.manual = pending;
      pending.finally(() => { if (state.manual === pending) state.manual = null; });
      return pending;
    }

    return {
      start() {
        if (state.disposed) return Promise.resolve(null);
        if (state.started) return state.manual || state.read || Promise.resolve(state.status);
        state.started = true; state.epoch++; return refresh();
      },
      refresh,
      manualCheck,
      getStatus() { return state.status; },
      dispose() {
        state.disposed = true; state.started = false; state.epoch++; state.request++;
        resetPolling(); state.status = null; removeNotice();
      }
    };
  }

  globalThis.AromaUpdateNotice = {create, checkedStatus};
})();
