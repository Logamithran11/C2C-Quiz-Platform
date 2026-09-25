'use strict';

document.addEventListener('DOMContentLoaded', function() {
  const builder = document.getElementById('quiz-form');
  if (builder) {
  const list = document.getElementById('questions');
  const errorBox = document.getElementById('builder-error');
  let sequence = 0;

  function renumber() {
    const cards = [...list.children];
    cards.forEach((card, index) => {
      card.querySelector('h3').textContent = `Question ${index + 1}`;
      card.querySelector('.remove-question').disabled = cards.length === 1;
    });
    document.getElementById('question-count').textContent = `${cards.length} question${cards.length === 1 ? '' : 's'}`;
  }

  function addQuestion(question = {text: '', options: ['', '', '', ''], correct_option: null}) {
    const key = sequence++;
    const card = document.createElement('section');
    card.className = 'card question-editor stack';
    card.innerHTML = '<div class="spread"><h3></h3><button type="button" class="button ghost small remove-question">Remove</button></div>';
    const label = document.createElement('label');
    label.textContent = 'Question text';
    const text = document.createElement('textarea');
    text.className = 'question-text';
    text.required = true;
    text.maxLength = 2000;
    text.rows = 2;
    text.value = question.text;
    label.append(text);
    card.append(label);
    const options = document.createElement('fieldset');
    options.className = 'builder-options';
    const legend = document.createElement('legend');
    legend.textContent = 'Enter four distinct options and select the correct answer';
    options.append(legend);
    for (let i = 0; i < 4; i++) {
      const row = document.createElement('div');
      row.className = 'builder-option';
      const radioLabel = document.createElement('label');
      radioLabel.className = 'correct-selector';
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = `correct_${key}`;
      radio.value = String(i);
      radio.required = true;
      radio.checked = question.correct_option === i;
      radio.setAttribute('aria-label', `Option ${'ABCD'[i]} is correct`);
      radioLabel.append(radio, document.createTextNode('ABCD'[i]));
      const option = document.createElement('input');
      option.type = 'text';
      option.className = 'option-text';
      option.required = true;
      option.maxLength = 500;
      option.placeholder = `Option ${'ABCD'[i]}`;
      option.setAttribute('aria-label', `Option ${'ABCD'[i]} text`);
      option.value = question.options[i];
      row.append(radioLabel, option);
      options.append(row);
    }
    card.append(options);
    card.querySelector('.remove-question').addEventListener('click', () => {
      if (list.children.length > 1) {
        card.remove();
        renumber();
        document.getElementById('add-question').focus();
      }
    });
    list.append(card);
    renumber();
    return text;
  }

  const bankIdSelect = document.getElementById('quiz-bank-id');
  const bankArea = document.getElementById('bank-selection-area');
  const manualArea = document.getElementById('manual-questions-area');
  const radioManual = document.getElementById('source-manual');
  const radioBank = document.getElementById('source-bank');
  const availableCount = document.getElementById('bank-available-count');
  const addQuestionBtn = document.getElementById('add-question');

  function updateSourceToggle() {
    if (radioBank && radioBank.checked) {
      if (bankArea) {
        bankArea.style.display = 'block';
        bankArea.querySelectorAll('input, select').forEach(el => el.disabled = false);
      }
      if (manualArea) {
        manualArea.style.display = 'none';
        manualArea.querySelectorAll('input, select, textarea').forEach(el => el.disabled = true);
      }
      updateBankCount();
    } else {
      if (bankArea) {
        bankArea.style.display = 'none';
        bankArea.querySelectorAll('input, select').forEach(el => el.disabled = true);
      }
      if (manualArea) {
        manualArea.style.display = 'block';
        manualArea.querySelectorAll('input, select, textarea').forEach(el => el.disabled = false);
      }
    }
  }

  function updateBankCount() {
    if (bankIdSelect && availableCount) {
      const selected = bankIdSelect.options[bankIdSelect.selectedIndex];
      if (selected && selected.value) {
        availableCount.textContent = `Available: ${selected.dataset.count} questions`;
      } else {
        availableCount.textContent = '';
      }
    }
  }

  if (radioManual) radioManual.addEventListener('change', updateSourceToggle);
  if (radioBank) radioBank.addEventListener('change', updateSourceToggle);
  if (bankIdSelect) bankIdSelect.addEventListener('change', updateBankCount);

  updateSourceToggle();

  const initial = JSON.parse(builder.dataset.questions);
  if (initial.length) initial.forEach(q => addQuestion(q));
  else addQuestion();
  
  addQuestionBtn.addEventListener('click', (e) => {
    e.preventDefault();
    addQuestion(undefined).focus();
  });
  
  builder.addEventListener('submit', (event) => {
    errorBox.textContent = '';
    const isBank = radioBank && radioBank.checked;
    let questions = [];
    
    if (!isBank) {
      questions = [...list.children].map((card) => ({
        text: card.querySelector('.question-text').value.trim(),
        options: [...card.querySelectorAll('.option-text')].map((input) => input.value.trim()),
        correct_option: Number(card.querySelector('input[type="radio"]:checked').value)
      }));
      const invalid = questions.findIndex((question) => !question.text || question.options.some((option) => !option)
        || new Set(question.options.map((option) => option.toLowerCase())).size !== 4);
      if (invalid !== -1) {
        event.preventDefault();
        errorBox.textContent = `Question ${invalid + 1}: enter question text and four nonempty, distinct options.`;
        return;
      }
    } else {
      if (!bankIdSelect || !bankIdSelect.value) {
        event.preventDefault();
        errorBox.textContent = 'Please select a Question Bank.';
        return;
      }
      const countInput = document.getElementById('quiz-bank-count');
      const count = Number(countInput.value);
      const selectedOption = bankIdSelect.options[bankIdSelect.selectedIndex];
      const maxAvailable = selectedOption ? Number(selectedOption.dataset.count) : 0;
      
      if (!countInput.value || count < 1 || count > maxAvailable) {
        event.preventDefault();
        errorBox.textContent = `Please enter a valid number of questions to include (1 to ${maxAvailable}).`;
        return;
      }
    }

    const instructionsEl = document.getElementById('quiz-instructions');

    document.getElementById('quiz-data').value = JSON.stringify({
      title: document.getElementById('quiz-title').value.trim(),
      description: document.getElementById('quiz-description').value.trim(),
      instructions: instructionsEl ? instructionsEl.value.trim() : '',
      time_limit: document.getElementById('quiz-time').value,
      pass_fail_enabled: document.getElementById('pass-fail-enabled').checked,
      pass_percentage: document.getElementById('pass-percentage').value,
      randomize_questions: document.getElementById('randomize-questions').checked,
      randomize_options: document.getElementById('randomize-options').checked,
      allow_review: document.getElementById('allow-review') ? document.getElementById('allow-review').checked : false,
      scheduled_start: document.getElementById('scheduled-start').value,
      scheduled_end: document.getElementById('scheduled-end').value,
      bank_id: isBank ? bankIdSelect.value : null,
      bank_question_count: isBank ? document.getElementById('quiz-bank-count').value : null,
      questions
    });
  });
}
});
