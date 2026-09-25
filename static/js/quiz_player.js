'use strict';

const player = document.getElementById('quiz-player');
if (player) {
  const panels = [...player.querySelectorAll('.question-panel')];
  const navigation = [...player.querySelectorAll('.nav-question')];
  const form = document.getElementById('answer-form');
  const errorBox = document.getElementById('player-error');
  const saveStatus = document.getElementById('save-status');
  const submitButton = document.getElementById('submit-quiz');
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let current = 0;
  let submitting = false;
  let finished = false;
  let retryAt = 0;
  let pendingSaves = 0;
  let saveRevision = 0;
  let unsavedChanges = false;
  let saveChain = Promise.resolve();
  let isOnline = navigator.onLine;
  // Use elapsed monotonic time, not the student's wall clock.
  let clockAnchor = performance.now();
  let remainingAtAnchor = Number(player.dataset.deadline) - Number(player.dataset.serverNow);

  // Connection status elements
  const connectionStatus = document.getElementById('connection-status');
  const deadlineWarning = document.getElementById('deadline-warning');
  const timingCountdown = document.getElementById('timing-countdown');

  // Dismiss restored notice after a few seconds
  const restoredNotice = document.getElementById('restored-notice');
  if (restoredNotice) {
    setTimeout(() => { restoredNotice.style.display = 'none'; }, 5000);
  }

  function remaining() {
    return Math.max(0, remainingAtAnchor - (performance.now() - clockAnchor) / 1000);
  }

  function snapshot() {
    return Object.fromEntries(panels.map((panel) => {
      const checked = panel.querySelector('input:checked');
      return [panel.dataset.id, checked ? Number(checked.value) : null];
    }));
  }

  function updateProgress() {
    const answers = snapshot();
    const answered = Object.values(answers).filter((value) => value !== null).length;
    document.getElementById('progress-text').textContent = `${answered} of ${panels.length} answered`;
    document.getElementById('answer-progress').value = answered;
    navigation.forEach((button, index) => {
      button.classList.toggle('answered', answers[panels[index].dataset.id] !== null);
      button.classList.toggle('current', index === current);
      button.setAttribute('aria-current', index === current ? 'step' : 'false');
      button.setAttribute('aria-label', `Question ${index + 1}, ${answers[panels[index].dataset.id] !== null ? 'answered' : 'unanswered'}`);
    });
  }

  function showQuestion(index) {
    current = Math.max(0, Math.min(index, panels.length - 1));
    panels.forEach((panel, i) => { panel.hidden = i !== current; });
    document.getElementById('previous-question').disabled = current === 0;
    document.getElementById('next-question').disabled = current === panels.length - 1;
    updateProgress();
  }

  // --- Connection monitoring ---
  function showConnectionStatus(type, message) {
    if (!connectionStatus) return;
    connectionStatus.style.display = 'block';
    if (type === 'offline') {
      connectionStatus.style.background = '#fff3e0';
      connectionStatus.style.borderColor = '#ffcc80';
      connectionStatus.style.color = '#e65100';
      connectionStatus.style.border = '1px solid #ffcc80';
    } else if (type === 'online') {
      connectionStatus.style.background = '#e8f5e9';
      connectionStatus.style.borderColor = '#a5d6a7';
      connectionStatus.style.color = '#2e7d32';
      connectionStatus.style.border = '1px solid #a5d6a7';
    }
    connectionStatus.textContent = message;
  }

  function hideConnectionStatus() {
    if (connectionStatus) connectionStatus.style.display = 'none';
  }

  window.addEventListener('offline', () => {
    isOnline = false;
    showConnectionStatus('offline', '⚠ Connection interrupted — Your last saved answers are safe. We\'ll try to save your latest answer when the connection returns.');
    saveStatus.textContent = '⚠ Offline — waiting to reconnect';
  });

  window.addEventListener('online', () => {
    isOnline = true;
    showConnectionStatus('online', '✓ Connection restored');
    saveStatus.textContent = '✓ Connection restored';
    queueSave();
    setTimeout(hideConnectionStatus, 4000);
  });

  // --- Deadline warning ---
  function updateDeadlineWarning(seconds) {
    if (!deadlineWarning || finished) return;
    if (seconds <= 0) {
      deadlineWarning.style.display = 'block';
      deadlineWarning.style.background = '#fce4ec';
      deadlineWarning.style.borderColor = '#ef9a9a';
      deadlineWarning.style.color = '#c62828';
      deadlineWarning.textContent = '⏰ Quiz time has ended.';
    } else if (seconds <= 60) {
      deadlineWarning.style.display = 'block';
      deadlineWarning.textContent = '⚠ Less than 1 minute remaining. Submit your quiz now.';
    } else if (seconds <= 300) {
      deadlineWarning.style.display = 'block';
      deadlineWarning.textContent = '⚠ Less than 5 minutes remaining. Please complete and submit your quiz.';
    } else {
      deadlineWarning.style.display = 'none';
    }
  }

  // --- Autosave: uses its own AbortController, 10s timeout, errors are non-fatal ---
  let autosaveController = null;

  async function post(url, answers) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify({answers})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Unable to save. Please try again.');
      return data;
    } finally {
      clearTimeout(timeout);
    }
  }

  // --- Final submission: dedicated fetch, longer timeout, never shared with autosave ---
  async function submitPost(url, answers) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort('submission timeout'), 30000);
    try {
      const response = await fetch(url, {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify({answers})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Submission failed. Please try again.');
      return data;
    } finally {
      clearTimeout(timeout);
    }
  }

  function goToResult(url) {
    finished = true;
    if (deadlineWarning) deadlineWarning.style.display = 'none';
    window.location.replace(url);
  }

  function queueSave() {
    if (submitting || finished) return;
    const answers = snapshot();
    const revision = ++saveRevision;
    unsavedChanges = true;
    pendingSaves++;
    saveStatus.textContent = 'Saving...';
    // Keep one request in flight and skip superseded snapshots. Submission sends
    // the latest selections itself instead of waiting through a stale backlog.
    saveChain = saveChain.then(async () => {
      try {
        if (finished || submitting || revision !== saveRevision) return;
        const data = await post(player.dataset.saveUrl, answers);
        if (data.submitted) {
          goToResult(data.result_url);
          return;
        }
        // Never extend the displayed timer because of network latency.
        remainingAtAnchor = Math.min(remaining(), data.deadline - data.server_now);
        clockAnchor = performance.now();
        if (revision === saveRevision) {
          unsavedChanges = false;
          saveStatus.textContent = '✓ Progress saved';
          errorBox.textContent = '';
        }
      } catch (error) {
        // Silently ignore intentional autosave cancellations (AbortError).
        if (error.name === 'AbortError') return;
        saveStatus.textContent = '⚠ Unable to save — checking connection...';
        errorBox.textContent = 'Your current selections will be retried on the next answer change or submission.';
      } finally {
        pendingSaves--;
      }
    });
  }

  async function submit(auto = false) {
    if (submitting || finished) return;
    const answers = snapshot();
    const blank = Object.values(answers).filter((value) => value === null).length;
    
    if (!auto) {
      const submitModal = document.getElementById('submit-modal');
      const answeredCount = document.getElementById('modal-answered-count');
      const unansweredWarning = document.getElementById('modal-unanswered-warning');
      const unansweredCount = document.getElementById('modal-unanswered-count');
      const submitCancel = document.getElementById('submit-cancel');
      const submitConfirm = document.getElementById('submit-confirm');
      
      const total = panels.length;
      const answered = total - blank;
      
      answeredCount.textContent = answered;
      if (blank > 0) {
        unansweredWarning.style.display = 'block';
        unansweredCount.textContent = blank;
      } else {
        unansweredWarning.style.display = 'none';
      }
      
      submitModal.hidden = false;
      
      return new Promise((resolve) => {
        const onCancel = () => {
          submitModal.hidden = true;
          cleanup();
          resolve(false);
        };
        const onConfirm = () => {
          submitModal.hidden = true;
          cleanup();
          doSubmit();
          resolve(true);
        };
        const onOutsideClick = (e) => {
          if (e.target === submitModal) {
            submitModal.hidden = true;
            cleanup();
            resolve(false);
          }
        };
        const cleanup = () => {
          submitCancel.removeEventListener('click', onCancel);
          submitConfirm.removeEventListener('click', onConfirm);
          submitModal.removeEventListener('click', onOutsideClick);
        };
        
        submitCancel.addEventListener('click', onCancel);
        submitConfirm.addEventListener('click', onConfirm);
        submitModal.addEventListener('click', onOutsideClick);
      });
    }
    
    doSubmit(auto);
  }

  async function doSubmit(auto = false) {
    if (submitting || finished) return;
    submitting = true;
    const answers = snapshot();
    form.querySelectorAll('input, button').forEach((input) => { input.disabled = true; });
    submitButton.textContent = auto ? 'Time is up · Submitting…' : 'Submitting…';
    errorBox.textContent = '';
    try {
      // Wait for any in-flight autosave to settle (it checks `submitting` so no new ones start).
      await saveChain;
      if (finished) return;
      // Use the dedicated submitPost — separate controller, longer timeout.
      const data = await submitPost(player.dataset.submitUrl, answers);
      goToResult(data.result_url);
    } catch (error) {
      // Never surface raw AbortError ("signal is aborted without reason") to the student.
      const message = (error.name === 'AbortError')
        ? 'Submission timed out. Please check your connection and try again.'
        : 'Submission failed. Please try again.';
      errorBox.textContent = `${message} Keep this page open. After the deadline, only previously saved answers count.`;
      retryAt = performance.now() + 5000;
      if (remaining() > 0) {
        form.querySelectorAll('input, button').forEach((input) => { input.disabled = false; });
        showQuestion(current);
      }
      submitButton.disabled = false;
      submitButton.textContent = 'Retry submission';
    } finally {
      submitting = false;
    }
  }

  form.addEventListener('change', () => { updateProgress(); queueSave(); });
  form.addEventListener('submit', (event) => { event.preventDefault(); submit(remaining() <= 0); });
  panels.forEach((panel) => panel.querySelector('.clear-answer').addEventListener('click', () => {
    panel.querySelectorAll('input').forEach((input) => { input.checked = false; });
    updateProgress();
    queueSave();
  }));
  navigation.forEach((button, index) => button.addEventListener('click', () => showQuestion(index)));
  document.getElementById('previous-question').addEventListener('click', () => showQuestion(current - 1));
  document.getElementById('next-question').addEventListener('click', () => showQuestion(current + 1));
  window.addEventListener('beforeunload', (event) => {
    if (!finished) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
  function tick() {
    const seconds = Math.ceil(remaining());
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    // Main countdown: show HH:MM:SS if >= 1 hour, else MM:SS
    if (hours > 0) {
      document.getElementById('countdown').textContent = `${String(hours).padStart(2, '0')}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
    } else {
      document.getElementById('countdown').textContent = `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
    }
    document.querySelector('.timer').classList.toggle('urgent', seconds <= 60);
    // Update timing panel countdown
    if (timingCountdown) {
      timingCountdown.textContent = `${String(hours).padStart(2, '0')}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
    }
    // Deadline warnings
    updateDeadlineWarning(seconds);
    if (seconds <= 0 && performance.now() >= retryAt) submit(true);
  }
  showQuestion(0);
  tick();
  setInterval(tick, 500);
  document.addEventListener('visibilitychange', () => {
    // Resynchronize with the server after a suspended/background browser tab.
    if (!document.hidden) queueSave();
    tick();
  });
}
