'use strict';

document.querySelectorAll('[data-copy]').forEach((button) => {
  button.addEventListener('click', async () => {
    const text = button.dataset.copy;
    const status = document.getElementById('copy-status');
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        // Clipboard API needs HTTPS; this fallback also works on local Wi-Fi.
        const input = document.createElement('textarea');
        input.value = text;
        document.body.append(input);
        input.select();
        const copied = document.execCommand('copy');
        input.remove();
        if (!copied) throw new Error('Copy unavailable');
      }
      button.textContent = 'Copied!';
      if (status) status.textContent = 'Quiz link copied to clipboard.';
      setTimeout(() => { button.textContent = 'Copy link'; }, 2000);
    } catch (error) {
      if (status) status.textContent = 'Select and copy the shareable link above manually.';
    }
  });
});

document.getElementById('go-back')?.addEventListener('click', () => {
  if (history.length > 1) history.back();
  else window.location.assign('/');
});
