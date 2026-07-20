const { createApp, ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } = Vue;

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

      watch(
        () => confirm.open || folderForm.open,
        (open) => document.body.classList.toggle('modal-open', open),
      );

      // ── Items + sorting ──────────────────────────────────────────────────────
      // Manual (server position) order is the source of truth in `folders`/`files`.
      const folders = ref(Array.isArray(cfg.folders) ? cfg.folders : []);
      const files = ref(Array.isArray(cfg.files) ? cfg.files : []);
      const sortMode = ref(localStorage.getItem('sortMode') || 'manual'); // manual | name | date
      const sortAsc = ref(localStorage.getItem('sortAsc') !== 'false');   // default ascending

      watch(viewMode, v => localStorage.setItem('viewMode', v));
      watch(sortMode, v => localStorage.setItem('sortMode', v));
      watch(sortAsc, v => localStorage.setItem('sortAsc', String(v)));

      const sortList = (arr, dateKey) => {
        if (sortMode.value === 'manual') return arr;
        const dir = sortAsc.value ? 1 : -1;
        return arr.slice().sort((a, b) => {
          let r;
          if (sortMode.value === 'name') {
            r = (a.name || '').localeCompare(b.name || '', 'zh', { numeric: true });
          } else {
            r = new Date(a[dateKey]) - new Date(b[dateKey]);
          }
          return r * dir;
        });
      };
      const sortedFolders = computed(() => sortList(folders.value, 'created_at'));
      const sortedFiles = computed(() => sortList(files.value, 'uploaded_at'));

      const fmtDate = (iso) => {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        const p = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
      };

      // ── Drag-and-drop reordering (only in manual sort mode) ──────────────────
      const dragKind = ref(null);   // 'folder' | 'file'
      const dragIndex = ref(null);
      const dragOverIndex = ref(null);

      const onDragStart = (kind, index, ev) => {
        if (sortMode.value !== 'manual') return;
        dragKind.value = kind;
        dragIndex.value = index;
        if (ev.dataTransfer) {
          ev.dataTransfer.effectAllowed = 'move';
          try { ev.dataTransfer.setData('text/plain', String(index)); } catch (e) { /* noop */ }
        }
      };
      const onDragOver = (kind, index) => {
        if (sortMode.value !== 'manual' || dragKind.value !== kind) return;
        dragOverIndex.value = index;
      };
      const onDragEnd = () => { dragKind.value = null; dragIndex.value = null; dragOverIndex.value = null; };
      const onDrop = (kind, index) => {
        if (sortMode.value !== 'manual' || dragKind.value !== kind) { onDragEnd(); return; }
        const from = dragIndex.value;
        if (from === null || from === index) { onDragEnd(); return; }
        const arr = kind === 'folder' ? folders : files;
        const next = arr.value.slice();
        const [moved] = next.splice(from, 1);
        next.splice(index, 0, moved);
        arr.value = next;
        onDragEnd();
        persistOrder(kind);
      };
      const persistOrder = (kind) => {
        const arr = kind === 'folder' ? folders.value : files.value;
        const body = new FormData();
        body.append('csrfmiddlewaretoken', cfg.csrfToken);
        body.append('kind', kind);
        arr.forEach(it => body.append('ids[]', it.id));
        fetch(cfg.reorderUrl, {
          method: 'POST',
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
          body,
        }).catch(() => { /* best-effort; order is restored on next reload */ });
      };

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
        document.body.classList.remove('modal-open');
      });

      return {
        viewMode, selected, dragging, uploads, showUploadDock,
        confirm, folderForm, fileInputRef, folderNameRef,
        folders, files, sortMode, sortAsc, sortedFolders, sortedFiles, fmtDate,
        dragKind, dragIndex, dragOverIndex,
        onDragStart, onDragOver, onDrop, onDragEnd,
        setView, isSelected, toggleSelect, clearSelection, selectionCount,
        askDelete, askBulkDelete, cancelConfirm, runConfirm,
        openFolderDialog, closeFolderDialog, submitFolder,
        triggerFilePicker, onFilesPicked, closeUploadDock,
      };
    },
  }).mount(fmEl);
}

// ── User management app (staff only) ─────────────────────────────────────────
const umEl = document.getElementById('user-management-app');
if (umEl) {
  const cfg = window.__UM_CONFIG__ || {};

  // Build + auto-submit a hidden POST form (full reload), mirroring the file
  // manager's pattern so server-side messages surface as toasts.
  const submitForm = (url, fields) => {
    const f = document.createElement('form');
    f.method = 'POST';
    f.action = url;
    const all = Object.assign({ csrfmiddlewaretoken: cfg.csrfToken }, fields);
    Object.entries(all).forEach(([key, value]) => {
      if (value === undefined || value === null) return;
      const inp = document.createElement('input');
      inp.type = 'hidden';
      inp.name = key;
      inp.value = value === true ? 'on' : value;
      f.appendChild(inp);
    });
    document.body.appendChild(f);
    f.submit();
  };

  const fmtDate = (iso) => {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };

  createApp({
    setup() {
      const users = ref(Array.isArray(cfg.users) ? cfg.users : []);
      const isSuperuser = !!cfg.isSuperuser;
      const createForm = reactive({ open: false, username: '', email: '', password: '', is_staff: false });
      const pwForm = reactive({ open: false, id: null, username: '', password: '' });
      const confirm = reactive({ open: false, title: '', body: '', action: '确定', danger: true, onConfirm: null });
      const createNameRef = ref(null);
      const pwInputRef = ref(null);

      watch(
        () => createForm.open || pwForm.open || confirm.open,
        (open) => document.body.classList.toggle('modal-open', open),
      );

      // A non-superuser staff member can't act on a superuser account.
      const canModify = (u) => isSuperuser || !u.is_superuser;

      const openCreate = () => {
        Object.assign(createForm, { open: true, username: '', email: '', password: '', is_staff: false });
        nextTick(() => { if (createNameRef.value) createNameRef.value.focus(); });
      };
      const closeCreate = () => { createForm.open = false; };
      const submitCreate = () => {
        const username = (createForm.username || '').trim();
        if (!username || !createForm.password) return;
        submitForm(cfg.createUrl, {
          username,
          email: (createForm.email || '').trim(),
          password: createForm.password,
          is_staff: createForm.is_staff ? 'on' : '',
        });
      };

      const openPassword = (u) => {
        Object.assign(pwForm, { open: true, id: u.id, username: u.username, password: '' });
        nextTick(() => { if (pwInputRef.value) pwInputRef.value.focus(); });
      };
      const closePassword = () => { pwForm.open = false; };
      const submitPassword = () => {
        if (!pwForm.password) return;
        submitForm(cfg.setPasswordUrl.replace('__ID__', pwForm.id), { password: pwForm.password });
      };

      const askToggle = (u) => {
        Object.assign(confirm, {
          open: true,
          title: u.is_active ? `停用 "${u.username}"?` : `启用 "${u.username}"?`,
          body: u.is_active ? '停用后该用户将无法登录系统。' : '启用后该用户可以重新登录系统。',
          action: u.is_active ? '停用' : '启用',
          danger: u.is_active,
          onConfirm: () => submitForm(cfg.toggleActiveUrl.replace('__ID__', u.id), {}),
        });
      };
      const askDelete = (u) => {
        Object.assign(confirm, {
          open: true,
          title: `删除用户 "${u.username}"?`,
          body: '该用户及其上传的所有文件都将被永久删除,此操作不可撤销。',
          action: '删除',
          danger: true,
          onConfirm: () => submitForm(cfg.deleteUrl.replace('__ID__', u.id), {}),
        });
      };
      const cancelConfirm = () => { confirm.open = false; confirm.onConfirm = null; };
      const runConfirm = () => {
        const fn = confirm.onConfirm;
        confirm.open = false;
        confirm.onConfirm = null;
        if (fn) fn();
      };

      const onKey = (e) => {
        if (e.key === 'Escape') { closeCreate(); closePassword(); cancelConfirm(); }
      };
      onMounted(() => window.addEventListener('keydown', onKey));
      onUnmounted(() => {
        window.removeEventListener('keydown', onKey);
        document.body.classList.remove('modal-open');
      });

      return {
        users, isSuperuser, createForm, pwForm, confirm,
        createNameRef, pwInputRef, fmtDate, canModify,
        openCreate, closeCreate, submitCreate,
        openPassword, closePassword, submitPassword,
        askToggle, askDelete, cancelConfirm, runConfirm,
      };
    },
  }).mount(umEl);
}
