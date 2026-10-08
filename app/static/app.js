'use strict';
document.querySelectorAll('[data-open]').forEach(button => button.addEventListener('click', () => document.getElementById(button.dataset.open).showModal()));
document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
document.querySelectorAll('[data-back]').forEach(button => button.addEventListener('click', event => { event.preventDefault(); history.back(); }));
if (document.querySelector('[data-processing]')) setTimeout(() => { if (!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)) location.reload(); }, 5000);
document.querySelectorAll('[data-auto-upload]').forEach(form => {
  const input = form.querySelector('input[type=file]');
  const status = form.querySelector('[data-upload-status]');
  let dragDepth = 0;

  const hasFiles = event => Array.from(event.dataTransfer?.types || []).includes('Files');
  const isJpeg = file => file.type === 'image/jpeg' || /\.jpe?g$/i.test(file.name);
  const skippedField = form.querySelector('input[name=skipped]');
  // Phone photo pickers ignore the accept filter, so the selection is checked here: non-JPEGs are
  // left out and named, both while uploading and in the notice after the upload.
  const upload = files => {
    const transfer = new DataTransfer();
    const skipped = [];
    Array.from(files).forEach(file => (isJpeg(file) ? transfer.items.add(file) : skipped.push(file.name)));
    skippedField.value = skipped.join(', ');
    if (!transfer.files.length) {
      status.textContent = form.dataset.msgJpeg + (skipped.length ? ' ' + form.dataset.msgSkipped.replace('{names}', skipped.join(', ')) : '');
      input.value = '';
      return;
    }
    input.files = transfer.files;
    form.requestSubmit();
  };

  form.addEventListener('submit', () => {
    const count = input.files.length;
    form.classList.remove('is-dragging');
    form.classList.add('is-uploading');
    form.setAttribute('aria-busy', 'true');
    status.textContent = (count === 1 ? form.dataset.msgUploadingOne : form.dataset.msgUploadingMany).replace('{n}', count)
      + (skippedField.value ? ' ' + form.dataset.msgSkipped.replace('{names}', skippedField.value) : '');
  });
  input.addEventListener('change', () => upload(input.files));
  form.addEventListener('dragenter', event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    form.classList.add('is-dragging');
    status.textContent = form.dataset.msgDrop;
  });
  form.addEventListener('dragover', event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  });
  form.addEventListener('dragleave', event => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) form.classList.remove('is-dragging');
  });
  form.addEventListener('drop', event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    form.classList.remove('is-dragging');
    upload(event.dataTransfer.files);
  });
});
document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => {
  if (!window.confirm(form.dataset.confirm)) event.preventDefault();
}));
document.querySelectorAll('button[data-confirm]').forEach(button => button.addEventListener('click', event => {
  if (!window.confirm(button.dataset.confirm)) event.preventDefault();
}));
document.addEventListener('click', event => document.querySelectorAll('.lang-menu[open]').forEach(menu => { if (!menu.contains(event.target)) menu.removeAttribute('open'); }));
document.querySelectorAll('[data-preview]').forEach(dialog => {
  const tiles = Array.from(document.querySelectorAll('[data-preview-tile]')).filter(tile => tile.querySelector('[data-preview-info]'));
  if (!tiles.length) return;
  const image = dialog.querySelector('[data-preview-img]');
  const panel = dialog.querySelector('[data-preview-panel]');
  const counter = dialog.querySelector('[data-preview-counter]');
  let index = 0;
  // The chosen EXIF tab stays open while paging through photos, to compare them side by side.
  let exifTab = '';
  const selectExifTab = tab => {
    if (!tab) return;
    exifTab = tab.dataset.exifTab;
    tab.closest('[data-exif]').querySelectorAll('[role=tab]').forEach(other => {
      const selected = other === tab;
      other.setAttribute('aria-selected', selected);
      document.getElementById(other.getAttribute('aria-controls')).classList.toggle('active', selected);
    });
    loadMap();
  };
  // OpenStreetMap only learns where a photo was taken once its Location tab is opened.
  const loadMap = () => panel.querySelectorAll('.exif-panel.active [data-map-x]').forEach(map => {
    Object.assign(map.firstElementChild.style, { left: `calc(50% - ${map.dataset.mapX}px)`, top: `calc(50% - ${map.dataset.mapY}px)` });
    map.querySelectorAll('img[data-src]').forEach(tile => { tile.src = tile.dataset.src; tile.removeAttribute('data-src'); });
  });
  panel.addEventListener('click', event => selectExifTab(event.target.closest('[data-exif-tab]')));
  const show = i => {
    index = (i + tiles.length) % tiles.length;
    const source = tiles[index].querySelector('img');
    image.src = source.src;
    image.alt = source.alt;
    panel.replaceChildren(tiles[index].querySelector('[data-preview-info]').content.cloneNode(true));
    selectExifTab(panel.querySelector(`[data-exif-tab="${exifTab}"]`));
    loadMap();
    counter.textContent = `${index + 1} / ${tiles.length}`;
  };
  tiles.forEach((tile, i) => tile.querySelector('[data-preview-open]').addEventListener('click', () => { show(i); dialog.showModal(); }));
  dialog.querySelector('[data-preview-prev]').addEventListener('click', () => show(index - 1));
  dialog.querySelector('[data-preview-next]').addEventListener('click', () => show(index + 1));
  dialog.addEventListener('keydown', event => {
    if (event.key === 'ArrowLeft') { event.preventDefault(); show(index - 1); }
    if (event.key === 'ArrowRight') { event.preventDefault(); show(index + 1); }
  });
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
});
