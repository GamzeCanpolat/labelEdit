/**
 * Maps Düzenleyici — Edit Flow Frontend
 * SOLID Prensiplerine Uygun Modüler Mimari:
 *   - EditStateManager  : Uygulama durumu ve geçmiş takibi (backStack)
 *   - EditApiClient     : Backend API haberleşmesi (Action, Undo, Screenshot)
 *   - EditUIController  : DOM güncellemeleri ve kullanıcı arayüzü
 *   - EditKeyboardHandler: Klavye kısayolları (Sil, Atla, Tut, Geri, Harita)
 *   - ToastService      : Şık bildirimler
 *   - EditApp           : Ana orkestrasyon
 */
'use strict';

/* ────────────────────────────────────────────────────────────────────────────
   EditStateManager
   ──────────────────────────────────────────────────────────────────────────── */
class EditStateManager {
  constructor() {
    this._state = {
      isStarted:       false,
      isLoading:       false,
      currentRowIndex: null,
      currentName:     '',
      currentUrl:      '',
      currentPosition: 0,
      totalPending:    0,
      deletedCount:    0,
      keptCount:       0,
      skippedCount:    0,
      queueSize:       0,
      currentItem:     null,
      nextName:        '',
      sourceCompleted: false,
      backStack:       [], // Geri gitme için önceki kayıtlar
      forwardStack:    [],
    };
  }
  get(key)        { return this._state[key]; }
  set(key, value) { this._state[key] = value; }
  update(partial) { Object.assign(this._state, partial); }
}

/* ────────────────────────────────────────────────────────────────────────────
   EditApiClient
   ──────────────────────────────────────────────────────────────────────────── */
class EditApiClient {
  constructor(baseUrl = '') { this._base = baseUrl; }

  async request(path, body) {
    const res = await fetch(this._base + path, body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!res.ok) {
      const error = await res.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }
  getStatus() { return this.request('/api/edit/status'); }
  getSchema(id) { return this.request('/api/edit/schema' + (id ? `?workspace_id=${encodeURIComponent(id)}` : '')); }
  getFields(row) { return this.request(`/api/edit/fields/${row}`); }
  saveMapping(id, mapping) { return this.request(`/api/workspaces/${encodeURIComponent(id)}/mapping`, { mapping }); }

  async startSession(startIndex, workspaceId = null) {
    const body = { start_index: startIndex };
    if (workspaceId) body.workspace_id = workspaceId;
    const res = await fetch(`${this._base}/api/edit/session/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async getNextScreenshot() {
    const res = await fetch(`${this._base}/api/edit/screenshot/next`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async getFieldOptions() {
    const res = await fetch(`${this._base}/api/edit/field-options`);
    if (!res.ok) throw new Error('Marka ve kategori seçenekleri alınamadı.');
    return res.json();
  }

  async sendAction(rowIndex, action, fields = {}) {
    const res = await fetch(`${this._base}/api/edit/action`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ row_index: rowIndex, action, fields }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async undoAction(rowIndex) {
    const res = await fetch(`${this._base}/api/edit/action/undo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ row_index: rowIndex }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async stopSession() {
    return fetch(`${this._base}/api/edit/session/stop`, { method: 'POST' });
  }
}

/* ────────────────────────────────────────────────────────────────────────────
   ToastService
   ──────────────────────────────────────────────────────────────────────────── */
class ToastService {
  constructor(containerId) { this._container = document.getElementById(containerId); }
  show(message, type = 'info', duration = 2800) {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    this._container.appendChild(toast);
    setTimeout(() => toast.remove(), duration);
  }
  success(msg) { this.show(msg, 'success'); }
  error(msg)   { this.show(msg, 'error'); }
  info(msg)    { this.show(msg, 'info'); }
}

/* ────────────────────────────────────────────────────────────────────────────
   EditUIController
   ──────────────────────────────────────────────────────────────────────────── */
class EditUIController {
  constructor() {
    this.startScreen        = document.getElementById('start-screen');
    this.startBtn           = document.getElementById('start-btn');
    this.startIndexInput    = document.getElementById('start-index-input');
    this.loadingOverlay     = document.getElementById('loading-overlay');
    this.loadingText        = document.getElementById('loading-text');
    this.screenshotImg      = document.getElementById('screenshot-img');
    this.errorBadge         = document.getElementById('error-badge');
    this.completedScreen    = document.getElementById('completed-screen');
    this.completedSub       = document.getElementById('completed-sub');

    // Header
    this.progressFill       = document.getElementById('progress-bar-fill');
    this.progressText       = document.getElementById('progress-text');
    this.bufferIndicator    = document.getElementById('buffer-indicator');
    this.bufferText         = document.getElementById('buffer-text');
    this.backBtn            = document.getElementById('back-btn');
    this.openUrlBtn         = document.getElementById('open-url-btn');

    // Info panel
    this.businessName       = document.getElementById('business-name');
    this.urlPreview         = document.getElementById('url-preview');
    this.nextBusinessName   = document.getElementById('next-business-name');
    this.kontrolValueText   = document.getElementById('kontrol-value-text');
    this.statCurrent        = document.getElementById('stat-current');
    this.statRemaining      = document.getElementById('stat-remaining');
    this.statDeleted        = document.getElementById('stat-deleted');
    this.statSkipped        = document.getElementById('stat-skipped');
    this.statKept           = document.getElementById('stat-kept');
    this.historyList        = document.getElementById('history-list');

    // Action buttons
    this.deleteBtn          = document.getElementById('delete-btn');
    this.skipBtn            = document.getElementById('skip-btn');
    this.keepBtn            = document.getElementById('keep-btn');
    this.actionHint         = document.getElementById('action-hint');
    this.editFields = {
      brand: document.getElementById('edit-brand'),
      main_category: document.getElementById('edit-main-category'),
      sub_category: document.getElementById('edit-sub-category'),
      poi_state: document.getElementById('edit-poi-state'),
    };
    this.brandSelectBtn = document.getElementById('brand-select-btn');
    this.poiState = document.getElementById('edit-poi-state');
    this.poiStateDisplay = document.getElementById('poi-state-display');
    this.mappingBtn = document.getElementById('column-mapping-btn');
    this.mappingDialog = document.getElementById('mapping-dialog');
    this.retryMapBtn = document.getElementById('retry-map-btn');
    this.retryNextBtn = document.getElementById('retry-next-btn');
    this.schema = null;
    this.fieldLabels = { maps_url: 'Maps bağlantısı', name: 'İşletme adı', brand: 'Marka', main_category: 'Ana kategori', sub_category: 'Alt kategori', poi_state: 'İşletme durumu' };
  }

  setSchema(schema) {
    this.schema = schema;
    this.mappingBtn.disabled = this._actionsLoading || !schema.workspace_id;
    document.getElementById('field-mapping-summary').textContent = Object.entries(this.fieldLabels)
      .map(([key, name]) => `${name}: ${schema.mapping[key] || 'Dosyada yok'}`).join('\n');
  }

  openMapping(isStarted) {
    if (!this.schema) return;
    const container = document.getElementById('mapping-fields');
    container.replaceChildren();
    for (const [key, name] of Object.entries(this.fieldLabels)) {
      const label = document.createElement('label');
      label.className = 'mapping-field';
      const title = document.createElement('span'); title.textContent = name;
      const select = document.createElement('select');
      select.className = 'form-select'; select.dataset.role = key;
      select.append(new Option(['maps_url', 'name'].includes(key) ? 'Sütun seçin' : 'Otomatik / yeni sütun', ''));
      for (const column of this.schema.columns) select.append(new Option(column, column));
      select.value = this.schema.mapping[key] || '';
      select.disabled = isStarted && ['maps_url', 'name'].includes(key);
      label.append(title, select); container.append(label);
    }
    document.getElementById('mapping-error').textContent = '';
    document.getElementById('mapping-help').textContent = isStarted
      ? 'Marka, kategori ve durum eşleştirmelerini değiştirebilirsin. Maps ve işletme adı oturum boyunca sabit kalır.'
      : 'Eksik marka ve kategori sütunları, bir değer yazıp Tut dediğinde oluşturulur.';
    this.mappingDialog.showModal();
  }

  getMapping() {
    return Object.fromEntries([...this.mappingDialog.querySelectorAll('select')].filter(select => !select.disabled)
      .map(select => [select.dataset.role, select.value]));
  }

  setFieldOptions(options) {
    const lists = { brand: 'brand-options', main_category: 'main-category-options', sub_category: 'sub-category-options' };
    for (const [key, id] of Object.entries(lists)) {
      const list = document.getElementById(id);
      list.replaceChildren(...(options[key] || []).map(value => new Option(value, value)));
    }
  }

  getFieldValues() {
    const fields = Object.fromEntries(Object.entries(this.editFields)
      .filter(([key]) => key !== 'poi_state').map(([key, input]) => [key, input.value.trim()]));
    // Kaynaktaki farklı durum kodları kullanıcı seçmedikçe dönüştürülmez.
    if (this._poiStateMissing && this.poiState.value !== this._originalPoiSelection) fields.poi_state = this.poiState.value;
    return fields;
  }

  setSaveState(message, saved = false) {
    const status = document.getElementById('save-state');
    status.textContent = message;
    status.dataset.saved = String(saved);
  }

  updateFields(fields = {}) {
    for (const [key, input] of Object.entries(this.editFields)) {
      if (key !== 'poi_state') input.value = fields[key] || '';
    }
    const raw = String(fields.poi_state || '').trim();
    const state = raw.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '')
      .replace(/ı/g, 'i').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ');
    this._poiStateMissing = ['', 'unknown', 'bilgi yok', 'bilinmiyor', 'null', 'none', 'nan', 'n a', 'na']
      .includes(state.replace(/[^a-z0-9]+/g, ' ').trim());
    let selected = '';
    if (['open', 'operational', 'acik', 'active'].includes(state)) {
      selected = 'OPERATIONAL';
    } else if (['closed temporarily', 'temporarily closed', 'gecici olarak kapali'].includes(state)) {
      selected = 'CLOSED_TEMPORARILY';
    } else if (['closed permanently', 'permanently closed', 'kalici olarak kapali'].includes(state)) {
      selected = 'CLOSED_PERMANENTLY';
    } else if (['closed', 'kapali', 'inactive'].includes(state)) {
      selected = 'CLOSED_PERMANENTLY';
    }
    this.poiState.querySelectorAll('[data-source]').forEach(option => option.remove());
    if (raw && !selected && !this._poiStateMissing) {
      const original = new Option(`Mevcut: ${raw}`, raw);
      original.dataset.source = 'true';
      this.poiState.append(original);
      selected = raw;
    }
    this.poiState.value = selected;
    this._originalPoiSelection = selected;
    this.poiState.hidden = !this._poiStateMissing;
    this.poiState.disabled = this._actionsLoading || !this._poiStateMissing;
    this.poiStateDisplay.hidden = this._poiStateMissing;
    this.poiStateDisplay.textContent = {
      OPERATIONAL: 'Açık', CLOSED_PERMANENTLY: 'Kapalı', CLOSED_TEMPORARILY: 'Geçici olarak kapalı',
    }[selected] || raw;
    this.poiStateDisplay.title = raw;
    this.updatePoiStateStyle();
    this.poiState.title = raw;
  }

  updatePoiStateStyle() {
    const value = this.poiState.value;
    this.poiState.dataset.state = value === 'OPERATIONAL' ? 'open'
      : ['CLOSED_PERMANENTLY', 'CLOSED_TEMPORARILY'].includes(value) ? 'closed' : '';
    this.poiStateDisplay.dataset.state = this.poiState.dataset.state;
  }

  /* -- Oturum Başlangıcı */
  showStartLoading() {
    this.startBtn.disabled = true;
    this.startIndexInput.disabled = true;
    this.startBtn.innerHTML = '<span class="spinner" style="width:18px;height:18px;border-width:2px"></span> Başlatılıyor...';
  }
  resetStartButton() {
    this.startBtn.disabled = false;
    this.startIndexInput.disabled = false;
    this.startBtn.innerHTML = '<span>&#9654;</span> Oturumu Başlat';
  }
  getStartIndex() {
    const value = this.startIndexInput.value.trim();
    if (value === '') return 0;
    const startIndex = Number(value);
    return Number.isInteger(startIndex) && startIndex >= 0 ? startIndex : null;
  }
  hideStartScreen() { this.startScreen.classList.add('hidden'); }

  activateUI() {
    this.deleteBtn.disabled  = false;
    this.skipBtn.disabled    = false;
    this.keepBtn.disabled    = false;
    this.openUrlBtn.disabled = false;
    this.actionHint.textContent = 'Delete / ← = Sil  |  Space / ↑ = Atla  |  Enter / → = Tut  |  Alt + ← = Geri';
  }

  setBackEnabled(enabled) {
    if (this.backBtn) this.backBtn.disabled = !enabled;
  }

  /* -- Screenshot Yükleme */
  showLoading(text = 'Harita yükleniyor...') {
    this.loadingOverlay.classList.remove('hidden');
    this.loadingText.textContent = text;
    this.screenshotImg.classList.remove('loaded');
    this.retryMapBtn.hidden = true;
    this.retryNextBtn.hidden = true;
    this.errorBadge.style.display = 'none';
  }
  hideLoading() { this.loadingOverlay.classList.add('hidden'); }

  async displayScreenshot(fileUrl, hasError, errorMsg = '', prefetchedImage = null) {
    if (fileUrl && !hasError) {
      if (prefetchedImage && prefetchedImage !== this.screenshotImg) {
        prefetchedImage.id = 'screenshot-img';
        prefetchedImage.alt = this.screenshotImg.alt;
        this.screenshotImg.replaceWith(prefetchedImage);
        this.screenshotImg = prefetchedImage;
      }
      await new Promise(resolve => {
        const show = () => { this.screenshotImg.classList.add('loaded'); this.hideLoading(); resolve(); };
        this.screenshotImg.onload = show;
        this.screenshotImg.onerror = () => {
          this.screenshotImg.classList.remove('loaded'); this.hideLoading();
          this._showErrorBadge('Görüntü yüklenemedi. Tekrar deneyebilir veya Maps’te açabilirsin.');
          this.retryMapBtn.hidden = false; resolve();
        };
        if (this.screenshotImg.src !== new URL(fileUrl, location.href).href ||
            (this.screenshotImg.complete && !this.screenshotImg.naturalWidth)) this.screenshotImg.src = fileUrl;
        if (this.screenshotImg.complete && this.screenshotImg.naturalWidth) show();
      });
    } else {
      this.screenshotImg.classList.remove('loaded');
      this.hideLoading();
      this._showErrorBadge(errorMsg || 'Screenshot alınamadı');
    }
  }
  _showErrorBadge(msg) {
    this.errorBadge.textContent = '⚠ ' + msg;
    this.errorBadge.style.display = 'block';
  }

  /* -- Bilgi Paneli */
  updateInfo(data) {
    this.businessName.textContent = data.name || '(İsim yok)';
    const shortUrl = data.url ? data.url.replace('https://www.google.com/maps/place/', '').slice(0, 60) : '—';
    this.urlPreview.textContent = shortUrl + '...';
    this.urlPreview.title       = data.url || '';
    this.urlPreview.dataset.url = data.url || '';
    this.kontrolValueText.textContent = data.kontrol_value || '—';
    document.getElementById('kontrol-section').hidden = !data.kontrol_value;
    document.getElementById('record-id').textContent = `Kayıt ${(data.position ?? 0) + 1} / ${data.total ?? 0}`;

    // Sıradaki mekan adı
    if (this.nextBusinessName) {
      this.nextBusinessName.textContent = data.next_name || '(Son kayıt veya yok)';
      this.nextBusinessName.title = data.next_name || '';
    }

    const pos       = (data.position ?? 0) + 1;
    const total     = data.total ?? 0;
    const remaining = total - pos;

    this.statCurrent.textContent   = pos.toLocaleString('tr-TR');
    this.statRemaining.textContent = Math.max(0, remaining).toLocaleString('tr-TR');
    this.statDeleted.textContent   = (data.deletedCount ?? 0).toLocaleString('tr-TR');
    if (this.statSkipped) this.statSkipped.textContent = (data.skippedCount ?? 0).toLocaleString('tr-TR');
    this.statKept.textContent      = (data.keptCount ?? 0).toLocaleString('tr-TR');

    const pct = total > 0 ? Math.round((pos / total) * 100) : 0;
    this.progressFill.style.width = pct + '%';
    this.progressText.textContent = `${pos} / ${total} kayıt`;
  }

  /* -- Buffer Göstergesi */
  updateBuffer(buffer = {}) {
    if (typeof buffer === 'number') buffer = { ready: buffer };
    const ready = buffer.ready || 0;
    this.bufferIndicator.dataset.ready = String(ready > 0);
    this.bufferText.textContent = ready > 0 ? `${ready} harita hazır`
      : buffer.preparing ? 'Sıradaki haritalar hazırlanıyor…'
      : buffer.failed ? 'Bazı haritalar açılamadı' : 'Sırada bekleyen harita yok';
    this.bufferIndicator.title = `${ready} sıradaki harita hazır · ${buffer.preparing || 0} hazırlanıyor`;
  }

  /* -- İşlem durumu */
  setActionsLoading(loading) {
    this._actionsLoading = loading;
    for (const input of Object.values(this.editFields)) input.disabled = loading;
    this.poiState.disabled = loading || !this._poiStateMissing;
    this.brandSelectBtn.disabled = loading;
    this.deleteBtn.disabled = loading;
    this.skipBtn.disabled   = loading;
    this.keepBtn.disabled   = loading;
    this.mappingBtn.disabled = loading || !this.schema?.workspace_id;
    this.openUrlBtn.disabled = loading;
    document.getElementById('save-state').textContent = '';
  }

  /* -- Geçmiş */
  addHistory(name, action) {
    const empty = this.historyList.querySelector('p');
    if (empty) empty.remove();

    const item = document.createElement('div');
    item.className = 'edit-history-item';
    let actionClass = 'kept';
    let actionLabel = '✓ Tutuldu';
    if (action === 'delete') {
      actionClass = 'deleted';
      actionLabel = '🗑 Silindi';
    } else if (action === 'skip') {
      actionClass = 'skipped';
      actionLabel = '⏭ Atlandı';
    }

    item.innerHTML = `
      <div class="edit-history-name" title="${this._esc(name)}">${this._esc(name)}</div>
      <div class="edit-history-action ${actionClass}">${actionLabel}</div>
    `;
    this.historyList.insertBefore(item, this.historyList.firstChild);
    const items = this.historyList.querySelectorAll('.edit-history-item');
    if (items.length > 20) items[items.length - 1].remove();
  }

  /* -- Tamamlandı */
  showCompleted(deletedCount, keptCount, skippedCount) {
    for (const input of Object.values(this.editFields)) input.disabled = true;
    this.brandSelectBtn.disabled = true;
    this.completedSub.textContent =
      `${deletedCount} silindi, ${skippedCount || 0} atlandı, ${keptCount} tutuldu.`;
    this.completedScreen.classList.add('visible');
    this.deleteBtn.disabled  = true;
    this.skipBtn.disabled    = true;
    this.keepBtn.disabled    = true;
    this.openUrlBtn.disabled = true;
    if (this.backBtn) this.backBtn.disabled = true;
  }

  _esc(str) {
    return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
}

/* ────────────────────────────────────────────────────────────────────────────
   EditKeyboardHandler
   ──────────────────────────────────────────────────────────────────────────── */
class EditKeyboardHandler {
  constructor(onDelete, onSkip, onKeep, onBack, onOpenUrl) {
    this._onDelete  = onDelete;
    this._onSkip    = onSkip;
    this._onKeep    = onKeep;
    this._onBack    = onBack;
    this._onOpenUrl = onOpenUrl;
    this._enabled   = false;
  }
  enable()  { this._enabled = true; }
  disable() { this._enabled = false; }

  attach() {
    document.addEventListener('keydown', (e) => {
      if (!this._enabled) return;

      const target = e.target;
      if (target?.closest('dialog')) return;
      const isInput = target && (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(target.tagName) || target.isContentEditable);
      if (isInput) return;

      // Geri Dön: Alt + Sol Ok, Backspace (input dışında) veya Ctrl + Z
      if ((e.altKey && e.key === 'ArrowLeft') || (e.ctrlKey && (e.key === 'z' || e.key === 'Z')) || (!isInput && e.key === 'Backspace')) {
        e.preventDefault();
        this._onBack();
        return;
      }

      if (isInput) return;

      // Delete veya Sol Ok -> Sil
      if ((e.key === 'Delete' || e.key === 'ArrowLeft') && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        this._onDelete();
        return;
      }

      // Space, Yukarı Ok veya A -> Atla
      if ((e.key === ' ' || e.key === 'ArrowUp' || e.key === 'a' || e.key === 'A') && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        this._onSkip();
        return;
      }

      // Enter veya Sağ Ok -> Tut
      if ((e.key === 'Enter' || e.key === 'ArrowRight') && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        this._onKeep();
        return;
      }

      // Ctrl + O -> Haritayı aç
      if (e.key === 'o' && e.ctrlKey) {
        e.preventDefault();
        this._onOpenUrl();
      }
    });
  }
}

/* ────────────────────────────────────────────────────────────────────────────
   EditApp — Ana Orkestrasyon
   ──────────────────────────────────────────────────────────────────────────── */
class EditApp {
  constructor() {
    this._state    = new EditStateManager();
    this._api      = new EditApiClient();
    this._ui       = new EditUIController();
    this._toast    = new ToastService('toast-container');
    this._keyboard = new EditKeyboardHandler(
      () => this._handleDelete(),
      () => this._handleSkip(),
      () => this._handleKeep(),
      () => this._handleBack(),
      () => this._handleOpenUrl(),
    );
    this._isTransitioning = false;
    this._isActing        = false;
    this._statusTimer = null;
    this._prefetched = new Map();
    this._lastSaveMessage = '';
    this._lastSaveConfirmed = false;
  }

  init() {
    this._keyboard.attach();
    this._bindEvents();
    if (window.WorkspaceClient) {
      window.WorkspaceClient.init({
        onWorkspaceChanged: (ws) => {
          const previous = this._state.get('workspace');
          if (this._state.get('isStarted') && previous?.id !== ws.id) {
            location.assign('/islem'); return;
          }
          this._state.set('workspace', ws);
          this._refreshSchema(ws.id);
          for (const id of ['export-btn', 'completed-export-btn']) {
            document.getElementById(id).href = `/api/edit/export?workspace_id=${encodeURIComponent(ws.id)}`;
          }
        }
      }).then(() => {
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get('autostart') === '1' && !this._state.get('isStarted')) {
          setTimeout(() => this._startSession(), 100);
        }
      });
    }
  }

  _bindEvents() {
    this._ui.poiState.addEventListener('change', () => this._ui.updatePoiStateStyle());
    for (const input of Object.values(this._ui.editFields)) {
      input.addEventListener('input', () => {
        const item = this._state.get('currentItem');
        const changed = item && Object.entries(this._ui.getFieldValues())
          .some(([key, value]) => value !== (item.fields?.[key] || ''));
        this._ui.setSaveState(changed ? 'Kaydedilmemiş değişiklikler' : this._lastSaveMessage,
          !changed && this._lastSaveConfirmed);
      });
    }
    this._ui.mappingBtn.addEventListener('click', () => this._ui.openMapping(this._state.get('isStarted')));
    document.getElementById('mapping-close-btn').addEventListener('click', () => this._ui.mappingDialog.close());
    document.getElementById('mapping-save-btn').addEventListener('click', () => this._saveMapping());
    this._ui.retryMapBtn.addEventListener('click', async () => {
      const item = this._state.get('currentItem');
      if (!item || this._isActing || this._isTransitioning) return;
      this._isTransitioning = true;
      try { this._ui.showLoading(); await this._showItem(item); }
      finally { this._isTransitioning = false; }
    });
    this._ui.retryNextBtn.addEventListener('click', () => this._loadNextScreenshot());
    this._ui.startBtn.addEventListener('click', () => this._startSession());
    this._ui.deleteBtn.addEventListener('click', () => this._handleDelete());
    this._ui.skipBtn.addEventListener('click', () => this._handleSkip());
    this._ui.keepBtn.addEventListener('click', () => this._handleKeep());
    if (this._ui.backBtn) this._ui.backBtn.addEventListener('click', () => this._handleBack());
    this._ui.openUrlBtn.addEventListener('click', () => this._handleOpenUrl());
    this._ui.brandSelectBtn.addEventListener('click', () => {
      const input = this._ui.editFields.brand;
      input.focus();
      try { input.showPicker?.(); } catch (_) { /* Yazı alanından da seçim yapılabilir. */ }
    });
    this._ui.urlPreview.addEventListener('click', () => {
      const url = this._ui.urlPreview.dataset.url;
      if (url) navigator.clipboard.writeText(url).then(() => this._toast.success('URL kopyalandı!'));
    });
  }

  async _refreshSchema(id) {
    try {
      const schema = await this._api.getSchema(id);
      if (id && this._state.get('workspace')?.id !== id) return;
      this._ui.setSchema(schema);
    } catch (_) { this._ui.mappingBtn.disabled = true; }
  }

  async _saveMapping() {
    const button = document.getElementById('mapping-save-btn');
    const error = document.getElementById('mapping-error');
    button.disabled = true; error.textContent = '';
    try {
      const id = this._ui.schema.workspace_id;
      await this._api.saveMapping(id, this._ui.getMapping());
      await this._refreshSchema(id);
      if (this._state.get('isStarted')) {
        this._ui.setFieldOptions(await this._api.getFieldOptions());
        const item = this._state.get('currentItem');
        if (item) {
          const draft = Object.fromEntries(Object.entries(this._ui.getFieldValues())
            .filter(([key, value]) => value !== (item.fields?.[key] || '')));
          item.fields = await this._api.getFields(item.row_index);
          this._ui.updateFields(item.fields);
          for (const [key, value] of Object.entries(draft)) {
            if (key !== 'poi_state' || this._ui._poiStateMissing) this._ui.editFields[key].value = value;
          }
          this._ui.updatePoiStateStyle();
          if (Object.keys(draft).length) this._ui.setSaveState('Kaydedilmemiş değişiklikler');
        }
      }
      this._ui.mappingDialog.close();
      this._toast.success('Sütun eşleştirmeleri kaydedildi.');
    } catch (err) { error.textContent = err.message; }
    finally { button.disabled = false; }
  }

  _preloadImages(previews = []) {
    for (const preview of previews) {
      if (!preview.file_url || this._prefetched.has(preview.file_url)) continue;
      const img = new Image();
      img.decoding = 'async';
      img.onload = () => img.decode?.().catch(() => {});
      img.onerror = () => this._prefetched.delete(preview.file_url);
      this._prefetched.set(preview.file_url, img);
      img.src = preview.file_url;
    }
    while (this._prefetched.size > 6) this._prefetched.delete(this._prefetched.keys().next().value);
  }

  async _pollStatus() {
    clearTimeout(this._statusTimer);
    if (!this._state.get('isStarted') || this._state.get('sourceCompleted')) return;
    try {
      if (!document.hidden) {
        const data = await this._api.getStatus();
        if (data.status === 'running') {
          this._ui.updateBuffer(data.buffer || data.worker_stats?.queue_size || 0);
          this._preloadImages(data.previews);
        }
      }
    } catch (_) { /* Buffer takibindeki bağlantı hatası kaydı kesintiye uğratmaz. */ }
    if (!this._state.get('sourceCompleted')) this._statusTimer = setTimeout(() => this._pollStatus(), 1500);
  }

  /* -- Oturum Başlat */
  async _startSession() {
    const startIndex = this._ui.getStartIndex();
    if (startIndex === null) {
      this._toast.error('Başlangıç indeksi 0 veya daha büyük tam sayı olmalı.');
      this._ui.startIndexInput.focus();
      return;
    }

    const ws = window.WorkspaceClient?.activeWorkspace;
    const wsId = ws ? ws.id : null;

    this._ui.showStartLoading();
    try {
      const data = await this._api.startSession(startIndex, wsId);
      this._api.getFieldOptions().then(options => this._ui.setFieldOptions(options))
        .catch(() => this._toast.error('Öneriler alınamadı; marka ve kategoriyi elle yazabilirsiniz.'));
      this._state.update({ isStarted: true, totalPending: data.pending });
      this._ui.hideStartScreen();
      this._ui.activateUI();
      this._keyboard.enable();
      this._pollStatus();
      this._toast.success(`İndeks ${data.start_index}'den itibaren ${data.pending} kayıt işlenecek`);
      await this._loadNextScreenshot();
    } catch (err) {
      this._toast.error(`Hata: ${err.message}`);
      this._ui.resetStartButton();
    }
  }

  /* -- Sonraki Screenshot */
  async _loadNextScreenshot() {
    if (this._isTransitioning) return;
    this._isTransitioning = true;
    this._state.update({ currentItem: null, currentRowIndex: null });
    this._ui.showLoading();
    this._ui.setActionsLoading(true);

    try {
      let data = this._state.get('forwardStack').pop() || await this._api.getNextScreenshot();
      while (data.status === 'preparing') {
        this._ui.updateBuffer(data.buffer);
        this._ui.loadingText.textContent = 'Sıradaki harita hazırlanıyor…';
        await new Promise(resolve => setTimeout(resolve, 500));
        data = await this._api.getNextScreenshot();
      }
      if (data.status === 'completed') {
        this._state.set('sourceCompleted', true);
        clearTimeout(this._statusTimer);
        this._ui.showCompleted(
          this._state.get('deletedCount'),
          this._state.get('keptCount'),
          this._state.get('skippedCount')
        );
        return;
      }
      await this._showItem(data);
    } catch (err) {
      this._toast.error(`Screenshot alınamadı: ${err.message}`);
      this._ui.hideLoading();
      this._ui.setActionsLoading(!this._state.get('currentItem'));
      this._ui.retryNextBtn.hidden = false;
    } finally {
      this._isTransitioning = false;
    }
  }

  async _showItem(item) {
    if (!item) return;
    this._ui.setActionsLoading(true);
    this._ui.updateFields(item.fields);
    this._state.update({
      currentItem:     item,
      currentRowIndex: item.row_index,
      currentName:     item.name,
      currentUrl:      item.url,
      nextName:        item.next_name || '',
      queueSize:       item.queue_size ?? this._state.get('queueSize'),
    });
    this._ui.updateInfo({
      name:          item.name,
      next_name:     item.next_name || '',
      url:           item.url,
      kontrol_value: item.kontrol_value || '',
      position:      item.position,
      total:         item.total,
      deletedCount:  this._state.get('deletedCount'),
      skippedCount:  this._state.get('skippedCount'),
      keptCount:     this._state.get('keptCount'),
    });
    this._ui.updateBuffer(item.buffer || item.queue_size || 0);
    document.getElementById('image-source').textContent = item.from_cache ? 'Önbellekten yüklendi' : 'Harita görüntüsü';
    await this._ui.displayScreenshot(item.file_url, !!item.error, item.error, this._prefetched.get(item.file_url));
    this._ui.setActionsLoading(false);
    this._ui.setSaveState(this._lastSaveMessage, this._lastSaveConfirmed);
    this._ui.setBackEnabled(this._state.get('backStack').length > 0);
  }

  /* -- Sil */
  async _handleDelete() {
    if (this._isTransitioning || this._isActing) return;
    const current = this._state.get('currentItem');
    const rowIndex = this._state.get('currentRowIndex');
    if (rowIndex === null || !current) return;

    this._isActing = true;
    this._ui.setActionsLoading(true);
    try {
      await this._api.sendAction(rowIndex, 'delete');
      this._lastSaveMessage = 'Son silme işlemi veritabanına kaydedildi.';
      this._lastSaveConfirmed = true;
      const name = this._state.get('currentName');
      this._ui.addHistory(name, 'delete');
      this._state.set('deletedCount', this._state.get('deletedCount') + 1);
      this._ui.statDeleted.textContent = this._state.get('deletedCount').toLocaleString('tr-TR');
      this._toast.show(`🗑 Silindi: "${name}"`, 'error', 2200);

      // BackStack'e ekle
      this._state.get('backStack').push({ item: { ...current }, action: 'delete' });
      this._ui.setBackEnabled(true);

      await this._loadNextScreenshot();
    } catch (err) {
      this._toast.error(`Silme hatası: ${err.message}`);
      this._ui.setActionsLoading(false);
    } finally {
      this._isActing = false;
    }
  }

  /* -- Atla */
  async _handleSkip() {
    if (this._isTransitioning || this._isActing) return;
    const current = this._state.get('currentItem');
    const rowIndex = this._state.get('currentRowIndex');
    if (rowIndex === null || !current) return;

    this._isActing = true;
    this._ui.setActionsLoading(true);
    try {
      await this._api.sendAction(rowIndex, 'skip');
      this._lastSaveMessage = 'Kayıt atlandı; alan değişiklikleri kaydedilmedi.';
      this._lastSaveConfirmed = false;
      const name = this._state.get('currentName');
      this._ui.addHistory(name, 'skip');
      this._state.set('skippedCount', this._state.get('skippedCount') + 1);
      if (this._ui.statSkipped) {
        this._ui.statSkipped.textContent = this._state.get('skippedCount').toLocaleString('tr-TR');
      }
      this._toast.info(`⏭ Atlandı: "${name}"`);

      // BackStack'e ekle
      this._state.get('backStack').push({ item: { ...current }, action: 'skip' });
      this._ui.setBackEnabled(true);

      await this._loadNextScreenshot();
    } catch (err) {
      this._toast.error(`Atlama hatası: ${err.message}`);
      this._ui.setActionsLoading(false);
    } finally {
      this._isActing = false;
    }
  }

  /* -- Tut */
  async _handleKeep() {
    if (this._isTransitioning || this._isActing) return;
    const current = this._state.get('currentItem');
    const rowIndex = this._state.get('currentRowIndex');
    if (rowIndex === null || !current) return;

    this._isActing = true;
    this._ui.setActionsLoading(true);
    try {
      const fields = this._ui.getFieldValues();
      this._ui.setSaveState('Veritabanına kaydediliyor…');
      await this._api.sendAction(rowIndex, 'keep', fields);
      this._lastSaveMessage = 'Son işlem veritabanına kaydedildi.';
      this._lastSaveConfirmed = true;
      this._refreshSchema(this._state.get('workspace')?.id);
      current.fields = { ...current.fields, ...fields };
      for (const [key, value] of Object.entries(fields)) {
        const list = this._ui.editFields[key].list;
        if (!list) continue;
        if (value && ![...list.options].some(option => option.value === value)) list.append(new Option(value, value));
      }
      const name = this._state.get('currentName');
      this._ui.addHistory(name, 'keep');
      this._state.set('keptCount', this._state.get('keptCount') + 1);
      this._ui.statKept.textContent = this._state.get('keptCount').toLocaleString('tr-TR');
      this._toast.success(`✓ Tutuldu: "${name}"`);

      // BackStack'e ekle
      this._state.get('backStack').push({ item: { ...current }, action: 'keep' });
      this._ui.setBackEnabled(true);

      await this._loadNextScreenshot();
    } catch (err) {
      this._toast.error(`Tutma hatası: ${err.message}`);
      this._ui.setActionsLoading(false);
      this._ui.setSaveState('Kayıt başarısız; değişiklikler kaydedilmedi.');
    } finally {
      this._isActing = false;
    }
  }

  /* -- Geri Dön */
  async _handleBack() {
    if (this._isTransitioning || this._isActing) return;
    const backStack = this._state.get('backStack');
    if (backStack.length === 0) {
      this._toast.info('Geri dönülecek önceki kayıt yok.');
      return;
    }

    const { item, action } = backStack[backStack.length - 1];
    this._isActing = true;

    try {
      this._ui.setActionsLoading(true);
      // Backend'deki işlemi geri al
      await this._api.undoAction(item.row_index);
      this._lastSaveMessage = 'Son işlem geri alındı.';
      this._lastSaveConfirmed = true;
      backStack.pop();
      const current = this._state.get('currentItem');
      if (current) this._state.get('forwardStack').push(current);

      // İstatistikleri düşür
      if (action === 'delete') {
        this._state.set('deletedCount', Math.max(0, this._state.get('deletedCount') - 1));
        this._ui.statDeleted.textContent = this._state.get('deletedCount').toLocaleString('tr-TR');
      } else if (action === 'skip') {
        this._state.set('skippedCount', Math.max(0, this._state.get('skippedCount') - 1));
        if (this._ui.statSkipped) {
          this._ui.statSkipped.textContent = this._state.get('skippedCount').toLocaleString('tr-TR');
        }
      } else if (action === 'keep') {
        this._state.set('keptCount', Math.max(0, this._state.get('keptCount') - 1));
        this._ui.statKept.textContent = this._state.get('keptCount').toLocaleString('tr-TR');
      }

      this._toast.info(`↩ Geri dönüldü: "${item.name}"`);
      await this._showItem(item);
    } catch (err) {
      this._toast.error(`Geri alma hatası: ${err.message}`);
      this._ui.setActionsLoading(false);
    } finally {
      this._isActing = false;
    }
  }

  /* -- Haritayı Aç */
  _handleOpenUrl() {
    const url = this._state.get('currentUrl');
    if (url) window.open(url, '_blank', 'noopener,noreferrer');
  }
}

/* Bootstrap */
document.addEventListener('DOMContentLoaded', () => {
  const app = new EditApp();
  app.init();
});
