const { createApp, ref, reactive, watch, nextTick, onMounted, onUnmounted } = Vue;

// ── Toasts: auto-hide after 4 s ──────────────────────────────────────────────
const toastsEl = document.getElementById('toasts-container');
if (toastsEl) {
  setTimeout(() => { toastsEl.style.opacity = '0'; toastsEl.style.transition = 'opacity 0.4s'; }, 4000);
  setTimeout(() => { toastsEl.style.display = 'none'; }, 4400);
}

// ── Header app: theme toggle + user menu ─────────────────────────────────────
const headerEl = document.getElementById('header-app');
if (headerEl) {
  createApp({
    setup() {
      const menuOpen = ref(false);

      const toggleTheme = () => {
        const cur = document.documentElement.getAttribute('data-theme') || 'light';
        const next = cur === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('theme', next);
      };

      const toggleMenu = () => { menuOpen.value = !menuOpen.value; };
      const closeMenu = () => { menuOpen.value = false; };

      const onOutside = (e) => {
        const menu = headerEl.querySelector('.user-menu');
        if (menuOpen.value && menu && !menu.contains(e.target)) closeMenu();
      };
      const onKey = (e) => { if (e.key === 'Escape') closeMenu(); };

      onMounted(() => {
        window.addEventListener('mousedown', onOutside, true);
        window.addEventListener('touchstart', onOutside, true);
        window.addEventListener('keydown', onKey);
      });
      onUnmounted(() => {
        window.removeEventListener('mousedown', onOutside, true);
        window.removeEventListener('touchstart', onOutside, true);
        window.removeEventListener('keydown', onKey);
      });

      return { menuOpen, toggleTheme, toggleMenu, closeMenu };
    },
  }).mount(headerEl);
}

// ── File manager app ─────────────────────────────────────────────────────────
const fmEl = document.getElementById('file-manager-app');
if (fmEl) {
  const cfg = window.__FM_CONFIG__ || {};

  createApp({
    setup() {
      const viewMode = ref(localStorage.getItem('viewMode') || 'grid');
      const selected = ref(new Set());
      const dragging = ref(false);
      const uploads = ref([]);
      const showUploadDock = ref(false);
      const confirm = reactive({ open: false, title: '', body: '', onConfirm: null, danger: true });
      const folderForm = reactive({ open: false, name: '' });
      const fileInputRef = ref(null);
      const folderNameRef = ref(null);

      watch(viewMode, v => localStorage.setItem('viewMode', v));

      const setView = (mode) => { viewMode.value = mode; };

      const keyOf = (type, id) => `${type}:${id}`;
      const isSelected = (type, id) => selected.value.has(keyOf(type, id));
      const toggleSelect = (type, id) => {
        const s = new Set(selected.value);
        const key = keyOf(type, id);
        if (s.has(key)) s.delete(key); else s.add(key);
        selected.value = s;
      };
      const clearSelection = () => { selected.value = new Set(); };
      const selectionCount = () => selected.value.size;

      const askDelete = ({ type, id, name }) => {
        const isFolder = type === 'folder';
        Object.assign(confirm, {
          open: true,
          title: isFolder ? `删除文件夹 "${name}"?` : `删除文件 "${name}"?`,
          body: isFolder
            ? '该文件夹及其所有子文件夹和文件都将被永久删除,此操作不可撤销。'
            : '该文件将被永久删除,此操作不可撤销。',
          danger: true,
          onConfirm: () => submitDelete(type, id),
        });
      };
      const askBulkDelete = () => {
        const count = selected.value.size;
        if (!count) return;
        Object.assign(confirm, {
          open: true,
          title: `删除选中的 ${count} 项?`,
          body: '这些项及其所有子内容将被永久删除,此操作不可撤销。',
          danger: true,
          onConfirm: () => submitBulkDelete(),
        });
      };
      const cancelConfirm = () => { confirm.open = false; confirm.onConfirm = null; };
      const runConfirm = () => {
        const fn = confirm.onConfirm;
        confirm.open = false;
        confirm.onConfirm = null;
        if (fn) fn();
      };

      const postForm = (url, body) => {
        const form = body instanceof FormData ? body : new FormData();
        form.append('csrfmiddlewaretoken', cfg.csrfToken);
        if (!(body instanceof FormData)) {
          Object.entries(body).forEach(([k, v]) => form.append(k, v));
        }
        const f = document.createElement('form');
        f.method = 'POST';
        f.action = url;
        form.forEach((value, key) => {
          const inp = document.createElement('input');
          inp.type = 'hidden';
          inp.name = key;
          inp.value = value;
          f.appendChild(inp);
        });
        document.body.appendChild(f);
        f.submit();
      };

      const submitDelete = (type, id) => {
        const url = (type === 'folder' ? cfg.deleteFolderUrl : cfg.deleteFileUrl).replace('__ID__', id);
        postForm(url, {});
      };
      const submitBulkDelete = () => {
        const folderIds = [], fileIds = [];
        selected.value.forEach(key => {
          const [type, id] = key.split(':');
          if (type === 'folder') folderIds.push(id); else fileIds.push(id);
        });
        const body = new FormData();
        folderIds.forEach(id => body.append('folder_ids[]', id));
        fileIds.forEach(id => body.append('file_ids[]', id));
        postForm(cfg.bulkDeleteUrl, body);
      };

      const openFolderDialog = () => {
        folderForm.open = true;
        folderForm.name = '';
        nextTick(() => { if (folderNameRef.value) folderNameRef.value.focus(); });
      };
      const closeFolderDialog = () => { folderForm.open = false; };
      const submitFolder = () => {
        const name = (folderForm.name || '').trim();
        if (!name) return;
        const body = new FormData();
        body.append('csrfmiddlewaretoken', cfg.csrfToken);
        body.append('name', name);
        body.append('create_folder', '1');
        postForm(window.location.pathname, body);
      };

      const triggerFilePicker = () => { if (fileInputRef.value) fileInputRef.value.click(); };
      const onFilesPicked = (event) => {
        const files = Array.from(event.target.files || []);
        if (files.length) uploadFiles(files);
        event.target.value = '';
      };

      const uploadFiles = (files) => {
        showUploadDock.value = true;
        files.forEach(file => uploadOne(file));
      };

      const uploadOne = (file) => {
        const entry = reactive({
          name: file.name,
          size: file.size,
          pct: 0,
          status: 'uploading',
          id: Math.random().toString(36).slice(2),
        });
        uploads.value = [entry, ...uploads.value];

        const xhr = new XMLHttpRequest();
        xhr.open('POST', window.location.pathname);
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');

        xhr.upload.addEventListener('progress', e => {
          if (e.lengthComputable) entry.pct = Math.round((e.loaded / e.total) * 99);
        });
        xhr.addEventListener('load', () => {
          if (xhr.status >= 200 && xhr.status < 400) {
            entry.pct = 100;
            entry.status = 'done';
            maybeRefresh();
          } else {
            entry.status = 'error';
          }
        });
        xhr.addEventListener('error', () => { entry.status = 'error'; });

        const form = new FormData();
        form.append('csrfmiddlewaretoken', cfg.csrfToken);
        form.append('upload_file', '1');
        form.append('files', file);
        xhr.send(form);
      };

      const maybeRefresh = () => {
        if (uploads.value.every(u => u.status !== 'uploading')) {
          setTimeout(() => window.location.reload(), 500);
        }
      };

      const closeUploadDock = () => {
        uploads.value = [];
        showUploadDock.value = false;
      };

      const bindDrag = () => {
        let counter = 0;
        const isFileDrag = e => Array.from(e.dataTransfer?.types || []).includes('Files');
        window.addEventListener('dragenter', e => {
          if (!isFileDrag(e)) return;
          counter++;
          dragging.value = true;
        });
        window.addEventListener('dragover', e => { if (isFileDrag(e)) e.preventDefault(); });
        window.addEventListener('dragleave', e => {
          if (!isFileDrag(e)) return;
          counter = Math.max(0, counter - 1);
          if (counter === 0) dragging.value = false;
        });
        window.addEventListener('drop', e => {
          if (!isFileDrag(e)) return;
          e.preventDefault();
          counter = 0;
          dragging.value = false;
          const files = Array.from(e.dataTransfer.files || []);
          if (files.length) uploadFiles(files);
        });
      };

      const onGlobalKey = (e) => {
        if (e.key === 'Escape') { cancelConfirm(); closeFolderDialog(); }
      };

      onMounted(() => {
        bindDrag();
        window.addEventListener('keydown', onGlobalKey);
      });
      onUnmounted(() => {
        window.removeEventListener('keydown', onGlobalKey);
      });

      return {
        viewMode, selected, dragging, uploads, showUploadDock,
        confirm, folderForm, fileInputRef, folderNameRef,
        setView, isSelected, toggleSelect, clearSelection, selectionCount,
        askDelete, askBulkDelete, cancelConfirm, runConfirm,
        openFolderDialog, closeFolderDialog, submitFolder,
        triggerFilePicker, onFilesPicked, closeUploadDock,
      };
    },
  }).mount(fmEl);
}
