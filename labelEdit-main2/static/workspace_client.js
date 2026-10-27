/**
 * Proje V1 — Çalışma Ortamları (Workspaces) ve Dinamik Sütun Yönetimi İstemcisi
 * 
 * Çoklu Excel yükleme, dinamik sütun eşleştirme (column mapping),
 * SQLite tablosuna yeni sütun ekleme ve Excel dışa aktarma (export) işlemlerini yönetir.
 */

const WorkspaceClient = {
  activeWorkspace: null,
  workspaces: [],
  onWorkspaceChanged: null,

  async init(options = {}) {
    this.onWorkspaceChanged = options.onWorkspaceChanged || null;
    this.injectModals();
    this.bindEvents();
    await this.refreshWorkspaces();
  },

  async refreshWorkspaces() {
    try {
      const [wsListRes, activeRes] = await Promise.all([
        fetch('/api/workspaces').then(r => r.json()),
        fetch('/api/workspaces/active').then(r => r.json()),
      ]);

      this.workspaces = wsListRes || [];
      this.activeWorkspace = activeRes.active || (this.workspaces.length > 0 ? this.workspaces[0] : null);

      this.updateHeaderUI();
      this.updateStartScreenUI();
      this.renderWorkspaceList();

      if (this.onWorkspaceChanged && this.activeWorkspace) {
        this.onWorkspaceChanged(this.activeWorkspace);
      }
    } catch (err) {
      console.error('Çalışma ortamları yüklenirken hata:', err);
    }
  },

  updateHeaderUI() {
    const pill = document.getElementById('workspace-pill');
    const nameEl = document.getElementById('header-ws-name');
    const badgeEl = document.getElementById('header-ws-badge');
    const exportBtn = document.getElementById('export-excel-btn');

    if (!pill || !nameEl) return;

    if (this.activeWorkspace) {
      nameEl.textContent = this.activeWorkspace.name;
      const stats = this.activeWorkspace.stats || {
        total: this.activeWorkspace.total_rows || 0,
        pending: this.activeWorkspace.pending_rows || 0,
      };
      if (badgeEl) {
        badgeEl.textContent = `${stats.total - stats.pending} / ${stats.total}`;
      }
      if (exportBtn) {
        exportBtn.style.display = 'inline-flex';
        exportBtn.onclick = () => this.downloadExcel(this.activeWorkspace.id);
      }
    } else {
      nameEl.textContent = 'Excel Seçilmedi';
      if (badgeEl) badgeEl.textContent = '0 / 0';
      if (exportBtn) exportBtn.style.display = 'none';
    }
  },

  updateStartScreenUI() {
    const container = document.getElementById('ws-start-card');
    if (!container) return;

    if (!this.activeWorkspace) {
      container.innerHTML = `
        <div class="ws-empty-card">
          <div style="font-size: 32px; margin-bottom: 8px;">📂</div>
          <div style="font-weight: 600; font-size: 14px; margin-bottom: 4px;">Henüz bir çalışma ortamı seçilmedi</div>
          <p style="font-size: 12px; color: var(--text-muted); margin-bottom: 12px;">
            Çalışmak istediğiniz Excel dosyasını yükleyin veya mevcut bir ortamı seçin.
          </p>
          <button class="btn btn-secondary btn-sm" onclick="WorkspaceClient.openWorkspaceModal('upload')">
            + Yeni Excel Yükle
          </button>
        </div>
      `;
      return;
    }

    const ws = this.activeWorkspace;
    const stats = ws.stats || { total: ws.total_rows || 0, pending: ws.pending_rows || 0, processed: ws.processed_rows || 0 };
    const pct = stats.total > 0 ? Math.round(((stats.total - stats.pending) / stats.total) * 100) : 0;
    const mapping = ws.column_mapping || {};

    container.innerHTML = `
      <div class="ws-active-card">
        <div class="ws-active-top">
          <div class="ws-active-info">
            <span class="ws-active-tag">Aktif Çalışma Ortamı</span>
            <div class="ws-active-title">${this.escapeHtml(ws.name)}</div>
            <div class="ws-active-file">📄 ${this.escapeHtml(ws.source_file)}</div>
          </div>
          <div class="ws-active-actions">
            <button type="button" class="ws-action-btn" title="Farklı Excel Seç / Yükle" onclick="WorkspaceClient.openWorkspaceModal()">
              <span>🔄</span> Değiştir
            </button>
            <button type="button" class="ws-action-btn" title="Sütun Eşleştirmesini Düzenle" onclick="WorkspaceClient.openMappingModal('${ws.id}')">
              <span>⚙️</span> Sütunlar
            </button>
            <button type="button" class="ws-action-btn ws-action-btn--export" title="Excel Olarak İndir" onclick="WorkspaceClient.downloadExcel('${ws.id}')">
              <span>📥</span> İndir
            </button>
          </div>
        </div>

        <div class="ws-stats-grid">
          <div class="ws-stat-item">
            <span class="ws-stat-num">${stats.total}</span>
            <span class="ws-stat-lbl">Toplam Kayıt</span>
          </div>
          <div class="ws-stat-item">
            <span class="ws-stat-num" style="color: var(--warning);">${stats.pending}</span>
            <span class="ws-stat-lbl">Bekleyen</span>
          </div>
          <div class="ws-stat-item">
            <span class="ws-stat-num" style="color: var(--success);">${stats.total - stats.pending}</span>
            <span class="ws-stat-lbl">Tamamlanan (%${pct})</span>
          </div>
        </div>

        <div class="ws-mapping-mini">
          <div class="ws-map-chip" title="Harita Bağlantısı Sütunu">
            📍 Maps: <strong>${this.escapeHtml(mapping.maps_url || 'Eşlenmedi!')}</strong>
          </div>
          <div class="ws-map-chip" title="İsim Sütunu">
            🏷️ İsim: <strong>${this.escapeHtml(mapping.name || 'Otomatik')}</strong>
          </div>
          ${mapping.category ? `
            <div class="ws-map-chip" title="Kategori Sütunu">
              📂 Kategori: <strong>${this.escapeHtml(mapping.category)}</strong>
            </div>
          ` : ''}
          ${mapping.status ? `
            <div class="ws-map-chip" title="Durum Sütunu">
              ✅ Durum: <strong>${this.escapeHtml(mapping.status)}</strong>
            </div>
          ` : ''}
        </div>
      </div>
    `;
  },

  renderWorkspaceList() {
    const listEl = document.getElementById('ws-list-container');
    if (!listEl) return;

    if (this.workspaces.length === 0) {
      listEl.innerHTML = `
        <div style="text-align: center; padding: 40px 20px; color: var(--text-muted);">
          <div style="font-size: 36px; margin-bottom: 8px;">📂</div>
          <p>Kayıtlı çalışma ortamı bulunmuyor.</p>
          <button class="btn btn-primary btn-sm" style="margin-top: 12px;" onclick="WorkspaceClient.switchTab('upload')">
            + İlk Excel Dosyasını Yükle
          </button>
        </div>
      `;
      return;
    }

    listEl.innerHTML = this.workspaces.map(ws => {
      const isActive = this.activeWorkspace && this.activeWorkspace.id === ws.id;
      const stats = ws.stats || { total: ws.total_rows || 0, pending: ws.pending_rows || 0, processed: ws.processed_rows || 0 };
      const pct = stats.total > 0 ? Math.round(((stats.total - stats.pending) / stats.total) * 100) : 0;

      return `
        <div class="ws-item-card ${isActive ? 'active-ws-border' : ''}">
          <div class="ws-item-header">
            <div>
              <div style="display: flex; align-items: center; gap: 8px;">
                <span class="ws-item-title">${this.escapeHtml(ws.name)}</span>
                ${isActive ? '<span class="badge-active">Aktif</span>' : ''}
              </div>
              <div class="ws-item-sub">📄 ${this.escapeHtml(ws.source_file)} &bull; ${stats.total} satır</div>
            </div>
            <div class="ws-item-actions">
              ${!isActive ? `
                <button class="btn btn-sm btn-primary" onclick="WorkspaceClient.activateWorkspace('${ws.id}')">
                  Seç &amp; Çalış
                </button>
              ` : `
                <span style="font-size: 12px; color: var(--success); font-weight: 600;">Seçili</span>
              `}
              <button class="btn-icon btn-sm" title="Sütun Eşleştirmesini Ayarla" onclick="WorkspaceClient.openMappingModal('${ws.id}')">
                ⚙️
              </button>
              <button class="btn-icon btn-sm" title="Excel Olarak İndir" onclick="WorkspaceClient.downloadExcel('${ws.id}')">
                📥
              </button>
              <button class="btn-icon btn-sm btn-delete-ws" title="Sil" onclick="WorkspaceClient.deleteWorkspace('${ws.id}', '${this.escapeHtml(ws.name)}')">
                🗑️
              </button>
            </div>
          </div>

          <div class="ws-item-progress">
            <div class="ws-item-bar">
              <div class="ws-item-fill" style="width: ${pct}%"></div>
            </div>
            <div class="ws-item-nums">
              <span>${stats.total - stats.pending} / ${stats.total} (%${pct})</span>
              <span>Bekleyen: ${stats.pending}</span>
            </div>
          </div>
        </div>
      `;
    }).join('');
  },

  async activateWorkspace(wsId) {
    try {
      const res = await fetch(`/api/workspaces/${wsId}/activate`, { method: 'POST' });
      if (!res.ok) throw new Error('Çalışma ortamı etkinleştirilemedi');
      await this.refreshWorkspaces();
      this.closeWorkspaceModal();
      this.showToast('Çalışma ortamı seçildi: ' + (this.activeWorkspace?.name || ''), 'success');
    } catch (e) {
      alert('Hata: ' + e.message);
    }
  },

  async deleteWorkspace(wsId, name) {
    if (!confirm(`"${name}" çalışma ortamını ve ilişkili SQLite veritabanını silmek istediğinize emin misiniz?`)) {
      return;
    }
    try {
      const res = await fetch(`/api/workspaces/${wsId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('Silinemedi');
      await this.refreshWorkspaces();
      this.showToast('Çalışma ortamı silindi', 'info');
    } catch (e) {
      alert('Silme hatası: ' + e.message);
    }
  },

  downloadExcel(wsId) {
    const id = wsId || (this.activeWorkspace ? this.activeWorkspace.id : null);
    if (!id) {
      alert('İndirilecek çalışma ortamı seçilmedi.');
      return;
    }
    window.location.href = `/api/workspaces/${id}/export`;
    this.showToast('Excel dosyası hazırlanıyor ve indiriliyor...', 'info');
  },

  openWorkspaceModal(tab = 'list') {
    const modal = document.getElementById('ws-hub-modal');
    if (modal) {
      modal.classList.add('modal-visible');
      this.switchTab(tab);
    }
  },

  closeWorkspaceModal() {
    const modal = document.getElementById('ws-hub-modal');
    if (modal) modal.classList.remove('modal-visible');
  },

  switchTab(tab) {
    const listTabBtn = document.getElementById('ws-tab-list-btn');
    const uploadTabBtn = document.getElementById('ws-tab-upload-btn');
    const listPane = document.getElementById('ws-pane-list');
    const uploadPane = document.getElementById('ws-pane-upload');

    if (tab === 'list') {
      listTabBtn?.classList.add('tab-active');
      uploadTabBtn?.classList.remove('tab-active');
      listPane?.classList.remove('hidden');
      uploadPane?.classList.add('hidden');
    } else {
      listTabBtn?.classList.remove('tab-active');
      uploadTabBtn?.classList.add('tab-active');
      listPane?.classList.add('hidden');
      uploadPane?.classList.remove('hidden');
    }
  },

  // ── Sütun Eşleştirme & Yönetim Modalı ──────────────────────────────────────

  async openMappingModal(wsId) {
    const modal = document.getElementById('ws-mapping-modal');
    if (!modal) return;

    try {
      const res = await fetch(`/api/workspaces/${wsId}`);
      if (!res.ok) throw new Error('Çalışma ortamı bilgisi alınamadı');
      const ws = await res.json();

      modal.dataset.wsId = wsId;
      document.getElementById('map-modal-title').textContent = `Sütun Eşleştirme & Yönetimi — ${ws.name}`;

      const columns = ws.columns || [];
      const mapping = ws.column_mapping || {};

      // Çok sayfalı Excel ise sayfa seçiciyi göster
      const sheetRow = document.getElementById('ws-sheet-select-row');
      const sheetSelect = document.getElementById('map-select-sheet');
      if (ws.available_sheets && ws.available_sheets.length > 1) {
        if (sheetRow) sheetRow.style.display = 'block';
        if (sheetSelect) {
          sheetSelect.innerHTML = '';
          ws.available_sheets.forEach(sh => {
            const opt = document.createElement('option');
            opt.value = sh;
            opt.textContent = `${sh} ${sh === ws.sheet_name ? '✓ (Seçili Sayfa)' : ''}`;
            if (sh === ws.sheet_name) opt.selected = true;
            sheetSelect.appendChild(opt);
          });
        }
      } else if (sheetRow) {
        sheetRow.style.display = 'none';
      }

      // Dropdownları doldur
      this.populateSelect('map-select-url', columns, mapping.maps_url, true);
      this.populateSelect('map-select-name', columns, mapping.name, false, '(Yok / Otomatik Türet)');
      this.populateSelect('map-select-category', columns, mapping.category, false, '(Henüz Belirlenmedi)');
      this.populateSelect('map-select-status', columns, mapping.status, false, '(Sistem Tarafından Yönetilsin)');

      // Eklenen sütunların listesi
      this.renderExtraColumnsChips(ws.extra_columns || []);

      modal.classList.add('modal-visible');
    } catch (e) {
      alert('Sütun eşleştirme açılırken hata: ' + e.message);
    }
  },

  async handleChangeSheet() {
    const modal = document.getElementById('ws-mapping-modal');
    const wsId = modal?.dataset?.wsId;
    const sheetSelect = document.getElementById('map-select-sheet');
    const newSheet = sheetSelect?.value;

    if (!wsId || !newSheet) return;

    const btn = document.getElementById('ws-change-sheet-btn');
    const origText = btn ? btn.innerHTML : '';
    if (btn) btn.innerHTML = '⏳ Yükleniyor...';

    try {
      const res = await fetch(`/api/workspaces/${wsId}/change-sheet`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sheet_name: newSheet }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Sayfa değiştirilemedi');
      }
      this.showToast(`'${newSheet}' sayfası ve verileri yüklendi!`, 'success');
      await this.openMappingModal(wsId);
      await this.refreshWorkspaces();
    } catch (e) {
      alert('Sayfa değiştirme hatası: ' + e.message);
    } finally {
      if (btn) btn.innerHTML = origText;
    }
  },

  closeMappingModal() {
    const modal = document.getElementById('ws-mapping-modal');
    if (modal) modal.classList.remove('modal-visible');
  },

  populateSelect(selectId, columns, selectedValue, isRequired = false, emptyLabel = '(Seçiniz)') {
    const el = document.getElementById(selectId);
    if (!el) return;

    el.innerHTML = '';
    if (!isRequired) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = emptyLabel;
      el.appendChild(opt);
    }

    columns.forEach(col => {
      const opt = document.createElement('option');
      opt.value = col;
      opt.textContent = col;
      if (col === selectedValue) opt.selected = true;
      el.appendChild(opt);
    });
  },

  renderExtraColumnsChips(extraColumns) {
    const container = document.getElementById('ws-extra-cols-chips');
    if (!container) return;

    if (!extraColumns || extraColumns.length === 0) {
      container.innerHTML = '<span style="font-size: 11px; color: var(--text-muted); font-style: italic;">Henüz ek sütun eklenmedi.</span>';
      return;
    }

    container.innerHTML = extraColumns.map(col => `
      <span class="ws-extra-col-badge">
        ➕ ${this.escapeHtml(col)}
      </span>
    `).join('');
  },

  async handleAddColumn() {
    const modal = document.getElementById('ws-mapping-modal');
    const wsId = modal?.dataset?.wsId;
    const input = document.getElementById('new-column-input');
    const colName = input?.value?.trim();

    if (!wsId || !colName) {
      alert('Lütfen eklenecek sütun adını girin.');
      return;
    }

    try {
      const res = await fetch(`/api/workspaces/${wsId}/columns/add`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: colName }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Sütun eklenemedi');
      }

      input.value = '';
      this.showToast(`'${colName}' sütunu başarıyla veritabanına eklendi!`, 'success');
      // Ekranı güncelle
      await this.openMappingModal(wsId);
    } catch (e) {
      alert('Hata: ' + e.message);
    }
  },

  async handleSaveMapping() {
    const modal = document.getElementById('ws-mapping-modal');
    const wsId = modal?.dataset?.wsId;
    if (!wsId) return;

    const mapsUrl = document.getElementById('map-select-url')?.value;
    const name = document.getElementById('map-select-name')?.value;
    const category = document.getElementById('map-select-category')?.value;
    const status = document.getElementById('map-select-status')?.value;

    if (!mapsUrl) {
      alert('Google Maps URL sütunu zorunludur. Lütfen harita bağlantısının bulunduğu sütunu seçin.');
      return;
    }

    try {
      const res = await fetch(`/api/workspaces/${wsId}/mapping`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mapping: {
            maps_url: mapsUrl,
            name: name || '',
            category: category || '',
            status: status || '',
          }
        }),
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Eşleştirme kaydedilemedi');
      }

      this.closeMappingModal();
      await this.activateWorkspace(wsId);
      this.showToast('Sütun eşleştirmesi başarıyla kaydedildi!', 'success');
    } catch (e) {
      alert('Hata: ' + e.message);
    }
  },

  // ── Dosya Yükleme (Upload) ──────────────────────────────────────────────────

  bindEvents() {
    // Sürükle bırak alanı
    const dropZone = document.getElementById('ws-drop-zone');
    const fileInput = document.getElementById('ws-file-input');

    if (dropZone && fileInput) {
      dropZone.addEventListener('click', () => fileInput.click());

      dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('drop-zone-active');
      });

      dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('drop-zone-active');
      });

      dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drop-zone-active');
        if (e.dataTransfer.files.length > 0) {
          this.handleFileSelected(e.dataTransfer.files[0]);
        }
      });

      fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
          this.handleFileSelected(fileInput.files[0]);
        }
      });
    }

    const uploadForm = document.getElementById('ws-upload-form');
    if (uploadForm) {
      uploadForm.addEventListener('submit', (e) => {
        e.preventDefault();
        this.submitUpload();
      });
    }
  },

  selectedUploadFile: null,

  handleFileSelected(file) {
    this.selectedUploadFile = file;
    const nameInput = document.getElementById('ws-upload-name');
    const fileLabel = document.getElementById('ws-selected-file-label');

    if (fileLabel) {
      fileLabel.innerHTML = `<strong>Seçilen Dosya:</strong> ${this.escapeHtml(file.name)} (${Math.round(file.size / 1024)} KB)`;
      fileLabel.classList.remove('hidden');
    }

    if (nameInput && (!nameInput.value || nameInput.value.trim() === '')) {
      nameInput.value = file.name.replace(/\.[^/.]+$/, '');
    }
  },

  async submitUpload() {
    if (!this.selectedUploadFile) {
      alert('Lütfen bir Excel veya CSV dosyası seçin.');
      return;
    }

    const nameInput = document.getElementById('ws-upload-name');
    const btn = document.getElementById('ws-upload-submit-btn');
    const originalText = btn ? btn.innerHTML : '';

    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '⏳ Dosya Yükleniyor ve SQLite Tablosu Oluşturuluyor...';
    }

    const formData = new FormData();
    formData.append('file', this.selectedUploadFile);
    if (nameInput && nameInput.value.trim()) {
      formData.append('name', nameInput.value.trim());
    }

    try {
      const res = await fetch('/api/workspaces/upload', {
        method: 'POST',
        body: formData,
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Dosya yükleme başarısız');
      }

      const newWs = await res.json();
      this.selectedUploadFile = null;
      document.getElementById('ws-file-input').value = '';
      document.getElementById('ws-selected-file-label')?.classList.add('hidden');
      nameInput.value = '';

      this.closeWorkspaceModal();
      await this.refreshWorkspaces();

      // Yüklenen dosya için doğrudan Sütun Eşleştirme ekranını aç!
      this.openMappingModal(newWs.id);
      this.showToast('Excel başarıyla yüklendi! Lütfen sütun eşleştirmesini kontrol edin.', 'success');
    } catch (e) {
      alert('Hata: ' + e.message);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = originalText;
      }
    }
  },

  showToast(msg, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = msg;
    container.appendChild(toast);
    setTimeout(() => {
      toast.classList.add('fade-out');
      setTimeout(() => toast.remove(), 400);
    }, 3500);
  },

  escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  },

  // ── HTML Modal Şablonları ──────────────────────────────────────────────────

  injectModals() {
    if (document.getElementById('ws-hub-modal')) return;

    const modalContainer = document.createElement('div');
    modalContainer.innerHTML = `
      <!-- Workspace Hub Modal -->
      <div id="ws-hub-modal" class="modal-backdrop">
        <div class="modal-card ws-hub-card">
          <div class="modal-header">
            <div style="display: flex; align-items: center; gap: 10px;">
              <span style="font-size: 24px;">📁</span>
              <div>
                <h2 style="font-size: 18px; font-weight: 700; color: var(--text-primary);">Çalışma Ortamları</h2>
                <p style="font-size: 12px; color: var(--text-secondary);">Farklı Excel dosyalarınızı yükleyin, yönetin ve üzerlerinde çalışın.</p>
              </div>
            </div>
            <button class="modal-close-btn" onclick="WorkspaceClient.closeWorkspaceModal()">&times;</button>
          </div>

          <!-- Sekmeler -->
          <div class="ws-tabs">
            <button class="ws-tab-btn tab-active" id="ws-tab-list-btn" onclick="WorkspaceClient.switchTab('list')">
              📑 Kayıtlı Ortamlar
            </button>
            <button class="ws-tab-btn" id="ws-tab-upload-btn" onclick="WorkspaceClient.switchTab('upload')">
              ➕ Yeni Excel Yükle
            </button>
          </div>

          <!-- Sekme 1: Liste -->
          <div id="ws-pane-list" class="ws-tab-pane">
            <div id="ws-list-container" class="ws-list-scroll"></div>
          </div>

          <!-- Sekme 2: Yükle -->
          <div id="ws-pane-upload" class="ws-tab-pane hidden">
            <form id="ws-upload-form">
              <div id="ws-drop-zone" class="ws-drop-zone">
                <div style="font-size: 40px; margin-bottom: 10px;">📥</div>
                <div style="font-weight: 600; font-size: 15px; margin-bottom: 6px;">Excel (.xlsx, .xlsm, .csv) dosyanızı buraya bırakın</div>
                <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 14px;">veya dosya seçmek için tıklayın</div>
                <span class="btn btn-secondary btn-sm">Dosya Seç</span>
                <input type="file" id="ws-file-input" accept=".xlsx,.xlsm,.csv" style="display: none;" />
              </div>
              <div id="ws-selected-file-label" class="ws-file-badge hidden"></div>

              <div style="margin-top: 16px;">
                <label style="display: block; font-size: 12px; font-weight: 600; margin-bottom: 6px; color: var(--text-secondary);">
                  Çalışma Ortamı Adı (İsteğe Bağlı)
                </label>
                <input type="text" id="ws-upload-name" class="form-input" placeholder="Ör: Mobilya Mağazaları Taraması" autocomplete="off" />
              </div>

              <div style="margin-top: 20px; display: flex; justify-content: flex-end; gap: 10px;">
                <button type="button" class="btn btn-secondary" onclick="WorkspaceClient.switchTab('list')">İptal</button>
                <button type="submit" id="ws-upload-submit-btn" class="btn btn-primary">
                  🚀 Yükle &amp; Çalışma Ortamı Oluştur
                </button>
              </div>
            </form>
          </div>
        </div>
      </div>

      <!-- Sütun Eşleştirme ve Sütun Ekleme Modalı -->
      <div id="ws-mapping-modal" class="modal-backdrop">
        <div class="modal-card ws-mapping-card">
          <div class="modal-header">
            <div style="display: flex; align-items: center; gap: 10px;">
              <span style="font-size: 24px;">⚙️</span>
              <div>
                <h2 id="map-modal-title" style="font-size: 17px; font-weight: 700;">Sütun Eşleştirme &amp; Yönetimi</h2>
                <p style="font-size: 12px; color: var(--text-secondary);">Excel'deki sütunları sistem alanlarıyla eşleştirin ve gerekirse yeni sütun ekleyin.</p>
              </div>
            </div>
            <button class="modal-close-btn" onclick="WorkspaceClient.closeMappingModal()">&times;</button>
          </div>

          <div class="ws-mapping-body">
            <div class="alert-info-box">
              💡 <strong>Dinamik Eşleştirme:</strong> Excel dosyanızdaki sütun isimleri sistemle birebir aynı olmak zorunda değildir. Örneğin Excel'inizdeki <em>"Firma Bağlantısı"</em> sütununu sistemin <em>"Google Maps URL"</em> rolüne kolayca eşleyebilirsiniz.
            </div>

            <!-- Çok Sayfalı Excel için Sayfa Seçici -->
            <div id="ws-sheet-select-row" class="ws-sheet-selector-box" style="display: none; margin-bottom: 18px; padding: 12px 16px; background: rgba(99, 102, 241, 0.07); border: 1px solid rgba(99, 102, 241, 0.25); border-radius: var(--radius-sm);">
              <label style="display: block; font-size: 12px; font-weight: 700; color: #818cf8; margin-bottom: 6px;">
                📄 Excel Sayfası (Sheet) Seçimi
              </label>
              <div style="display: flex; gap: 8px;">
                <select id="map-select-sheet" class="form-select" style="flex: 1;"></select>
                <button type="button" id="ws-change-sheet-btn" class="btn btn-secondary btn-sm" onclick="WorkspaceClient.handleChangeSheet()">
                  Sayfayı Yükle
                </button>
              </div>
              <span style="display: block; font-size: 11px; color: var(--text-muted); margin-top: 4px;">
                Birden fazla sayfa içeren Excel'lerde mekanların bulunduğu asıl sayfayı buradan seçebilirsiniz.
              </span>
            </div>

            <!-- Eşleştirme Grid -->
            <div class="mapping-grid">
              <div class="mapping-field">
                <label class="mapping-label">
                  📍 Google Maps Bağlantısı (URL) <span style="color: var(--error);">*</span>
                </label>
                <select id="map-select-url" class="form-select"></select>
                <span class="mapping-hint">Ekran görüntüsü ve harita açmak için zorunludur.</span>
              </div>

              <div class="mapping-field">
                <label class="mapping-label">
                  🏷️ Mekan / Başlık Adı
                </label>
                <select id="map-select-name" class="form-select"></select>
                <span class="mapping-hint">Başlıkta ve kartlarda gösterilecek işletme adı.</span>
              </div>

              <div class="mapping-field">
                <label class="mapping-label">
                  📂 Kategori / Etiket Sütunu
                </label>
                <select id="map-select-category" class="form-select"></select>
                <span class="mapping-hint">Etiketlerin yazılacağı sütun. Yoksa aşağıdan ekleyebilirsiniz.</span>
              </div>

              <div class="mapping-field">
                <label class="mapping-label">
                  ✅ Kontrol / Durum Sütunu
                </label>
                <select id="map-select-status" class="form-select"></select>
                <span class="mapping-hint">Düzenle akışında incelenecek veya filtrelenecek sütun.</span>
              </div>
            </div>

            <!-- Yeni Sütun Ekleme Bölümü -->
            <div class="add-col-section">
              <div style="font-size: 13px; font-weight: 700; margin-bottom: 6px; color: var(--text-primary); display: flex; align-items: center; gap: 6px;">
                <span>➕</span> Yeni Sütun Ekle (SQLite Tablosuna Doğrudan Eklenir)
              </div>
              <p style="font-size: 11px; color: var(--text-muted); margin-bottom: 10px;">
                Excel dosyanızda eksik olan alanları (örn: <em>Kategori</em>, <em>Notlar</em>, <em>Telefon Kontrol</em>) anında ekleyin.
              </p>
              <div style="display: flex; gap: 8px;">
                <input type="text" id="new-column-input" class="form-input" style="flex: 1;" placeholder="Sütun Adı (Ör: Kategori)" />
                <button type="button" class="btn btn-secondary" onclick="WorkspaceClient.handleAddColumn()">
                  + Ekle
                </button>
              </div>
              <div style="margin-top: 10px;">
                <span style="font-size: 11px; font-weight: 600; color: var(--text-secondary); margin-right: 6px;">Eklenen Özel Sütunlar:</span>
                <span id="ws-extra-cols-chips" style="display: inline-flex; flex-wrap: wrap; gap: 4px;"></span>
              </div>
            </div>
          </div>

          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="WorkspaceClient.closeMappingModal()">İptal</button>
            <button type="button" class="btn btn-primary" onclick="WorkspaceClient.handleSaveMapping()">
              💾 Eşleştirmeyi Kaydet &amp; Çalışmaya Başla
            </button>
          </div>
        </div>
      </div>
    `;

    document.body.appendChild(modalContainer);
  }
};

window.WorkspaceClient = WorkspaceClient;
