/**
 * Proje V1 — Ana Sayfa
 * SADECE Excel yükleme ve SQLite üzerinden var olan Excel dosyalarını seçme.
 */
'use strict';

const HomeApp = {
  workspaces: [],
  selectedWsId: null,
  selectedFile: null,

  async init() {
    this.bindUploadEvents();
    this.bindSelectEvents();

    if (window.WorkspaceClient) {
      await window.WorkspaceClient.init({
        onWorkspaceChanged: (ws) => {
          if (ws) {
            this.selectedWsId = ws.id;
            this.syncDropdown();
            this.renderDetails();
          }
        }
      });
    }

    await this.refresh();
  },

  async refresh() {
    try {
      const [listRes, activeRes] = await Promise.all([
        fetch('/api/workspaces').then(r => r.json()),
        fetch('/api/workspaces/active').then(r => r.json()),
      ]);

      this.workspaces = listRes || [];

      // Aktif olanı veya ilkini seç
      const activeWs = activeRes?.active;
      if (activeWs) {
        this.selectedWsId = activeWs.id;
      } else if (this.workspaces.length > 0 && !this.selectedWsId) {
        this.selectedWsId = this.workspaces[0].id;
      }

      this.renderDropdown();
      this.renderDetails();
    } catch (err) {
      console.error('Veriler yüklenirken hata:', err);
      this.showToast('SQLite verileri yüklenemedi: ' + err.message, 'error');
    }
  },

  renderDropdown() {
    const selectEl = document.getElementById('ws-select-dropdown');
    if (!selectEl) return;

    if (this.workspaces.length === 0) {
      selectEl.innerHTML = '<option value="">SQLite üzerinde kayıtlı Excel bulunamadı</option>';
      return;
    }

    selectEl.innerHTML = this.workspaces.map(ws => {
      const stats = ws.stats || { total: ws.total_rows || 0, pending: ws.pending_rows || 0 };
      const isSelected = ws.id === this.selectedWsId;
      return `<option value="${ws.id}" ${isSelected ? 'selected' : ''}>
        ${this.escapeHtml(ws.name)} (${stats.total.toLocaleString('tr-TR')} kayıt &bull; ${stats.pending.toLocaleString('tr-TR')} bekleyen)
      </option>`;
    }).join('');
  },

  syncDropdown() {
    const selectEl = document.getElementById('ws-select-dropdown');
    if (selectEl && this.selectedWsId) {
      selectEl.value = this.selectedWsId;
    }
  },

  bindSelectEvents() {
    const selectEl = document.getElementById('ws-select-dropdown');
    if (!selectEl) return;

    selectEl.addEventListener('change', (e) => {
      this.selectedWsId = e.target.value;
      this.renderDetails();
    });
  },

  renderDetails() {
    const container = document.getElementById('selected-ws-details');
    const startBtn = document.getElementById('btn-start-process');
    if (!container) return;

    const ws = this.workspaces.find(w => w.id === this.selectedWsId);
    if (!ws) {
      container.innerHTML = `
        <div style="text-align: center; padding: 32px 16px; color: var(--text-muted); background: var(--bg-elevated); border: 1px dashed var(--border-medium); border-radius: var(--radius-md);">
          <div style="font-size: 36px; margin-bottom: 8px;">📂</div>
          <div style="font-size: 14px; font-weight: 600; color: var(--text-secondary); margin-bottom: 4px;">Henüz bir dosya seçilmedi</div>
          <p style="font-size: 12px;">Yukarıdaki listeden bir Excel dosyası seçin veya sol taraftan yeni dosya yükleyin.</p>
        </div>
      `;
      if (startBtn) startBtn.disabled = true;
      return;
    }

    if (startBtn) startBtn.disabled = false;

    const stats = ws.stats || {
      total: ws.total_rows || 0,
      pending: ws.pending_rows || 0,
      deleted: ws.deleted_rows || 0,
      kept: ws.kept_rows || 0
    };
    const completed = stats.total - stats.pending;
    const pct = stats.total > 0 ? Math.round((completed / stats.total) * 100) : 0;
    const mapping = ws.column_mapping || {};

    container.innerHTML = `
      <div style="background: var(--bg-elevated); border: 1px solid var(--border-medium); border-radius: var(--radius-md); padding: 18px;">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px;">
          <div>
            <div style="font-size: 16px; font-weight: 700; color: var(--text-primary); margin-bottom: 4px;">
              📄 ${this.escapeHtml(ws.name)}
            </div>
            <div style="font-size: 12px; color: var(--text-muted);">
              Kaynak: <strong>${this.escapeHtml(ws.source_file)}</strong>
              ${ws.sheet_name ? `&bull; Sayfa: <strong>${this.escapeHtml(ws.sheet_name)}</strong>` : ''}
            </div>
          </div>
          <div style="display: flex; gap: 6px;">
            <button class="btn-icon btn-sm" onclick="WorkspaceClient.openMappingModal('${ws.id}')" title="Sütun Eşleştirmesini Ayarla">
              ⚙️
            </button>
            <button class="btn-icon btn-sm" onclick="WorkspaceClient.downloadExcel('${ws.id}')" title="Excel Olarak İndir">
              📥
            </button>
            <button class="btn-icon btn-sm btn-delete-ws" onclick="HomeApp.deleteWs('${ws.id}', '${this.escapeHtml(ws.name)}')" title="Sil">
              🗑️
            </button>
          </div>
        </div>

        <!-- İlerleme Çubuğu -->
        <div style="margin-bottom: 14px;">
          <div style="display: flex; justify-content: space-between; font-size: 11px; margin-bottom: 6px; color: var(--text-secondary);">
            <span>İlerleme: <strong>%${pct}</strong> (${completed} / ${stats.total})</span>
            <span style="color: var(--warning); font-weight: 600;">Kalan: ${stats.pending}</span>
          </div>
          <div style="height: 6px; background: var(--border-subtle); border-radius: 999px; overflow: hidden;">
            <div style="height: 100%; width: ${pct}%; background: var(--accent-grad); border-radius: 999px;"></div>
          </div>
        </div>

        <!-- 4 Kolon Sayaç -->
        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 14px; text-align: center;">
          <div style="background: var(--bg-base); padding: 8px 6px; border-radius: 6px; border: 1px solid var(--border-subtle);">
            <div style="font-size: 16px; font-weight: 700; color: var(--text-primary); font-family: 'JetBrains Mono', monospace;">${stats.total}</div>
            <div style="font-size: 10px; color: var(--text-muted); text-transform: uppercase;">Toplam</div>
          </div>
          <div style="background: var(--bg-base); padding: 8px 6px; border-radius: 6px; border: 1px solid var(--border-subtle);">
            <div style="font-size: 16px; font-weight: 700; color: var(--warning); font-family: 'JetBrains Mono', monospace;">${stats.pending}</div>
            <div style="font-size: 10px; color: var(--text-muted); text-transform: uppercase;">Bekleyen</div>
          </div>
          <div style="background: var(--bg-base); padding: 8px 6px; border-radius: 6px; border: 1px solid var(--border-subtle);">
            <div style="font-size: 16px; font-weight: 700; color: var(--success); font-family: 'JetBrains Mono', monospace;">${stats.kept || 0}</div>
            <div style="font-size: 10px; color: var(--text-muted); text-transform: uppercase;">Tutuldu</div>
          </div>
          <div style="background: var(--bg-base); padding: 8px 6px; border-radius: 6px; border: 1px solid var(--border-subtle);">
            <div style="font-size: 16px; font-weight: 700; color: var(--error); font-family: 'JetBrains Mono', monospace;">${stats.deleted || 0}</div>
            <div style="font-size: 10px; color: var(--text-muted); text-transform: uppercase;">Silindi</div>
          </div>
        </div>

        <!-- Kolon Eşleşme Özeti -->
        <div style="display: flex; flex-wrap: wrap; gap: 6px;">
          <span class="ws-map-chip" style="font-size: 11px; padding: 2px 7px;">
            📍 Maps: <strong>${this.escapeHtml(mapping.maps_url || 'Eşlenmedi!')}</strong>
          </span>
          <span class="ws-map-chip" style="font-size: 11px; padding: 2px 7px;">
            🏷️ İsim: <strong>${this.escapeHtml(mapping.name || 'Otomatik')}</strong>
          </span>
          ${mapping.status ? `
            <span class="ws-map-chip" style="font-size: 11px; padding: 2px 7px;">
              📝 Kontrol: <strong>${this.escapeHtml(mapping.status)}</strong>
            </span>
          ` : ''}
        </div>
      </div>
    `;
  },

  async startProcessing() {
    if (!this.selectedWsId) {
      this.showToast('Lütfen önce bir Excel dosyası seçin.', 'error');
      return;
    }

    const btn = document.getElementById('btn-start-process');
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '⏳ Başlatılıyor...';
    }

    try {
      const res = await fetch(`/api/workspaces/${this.selectedWsId}/activate`, { method: 'POST' });
      if (!res.ok) throw new Error('Seçilen çalışma ortamı aktif yapılamadı.');

      // Doğrudan İşlem Sayfasına yönlendir!
      window.location.href = '/islem';
    } catch (err) {
      this.showToast(err.message, 'error');
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span>⚡</span> Seçili Dosyayla İşleme Başla (Tut / Sil / Atla) &rarr;';
      }
    }
  },

  async deleteWs(wsId, name) {
    if (!confirm(`"${name}" veritabanını silmek istediğinize emin misiniz?`)) return;
    try {
      const res = await fetch(`/api/workspaces/${wsId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('Silinemedi');
      this.selectedWsId = null;
      await this.refresh();
      this.showToast('Excel çalışma ortamı silindi', 'info');
    } catch (err) {
      alert('Silme hatası: ' + err.message);
    }
  },

  bindUploadEvents() {
    const dropZone = document.getElementById('home-drop-zone');
    const fileInput = document.getElementById('home-file-input');
    const form = document.getElementById('home-upload-form');

    if (!dropZone || !fileInput) return;

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
        this.handleFile(e.dataTransfer.files[0]);
      }
    });

    fileInput.addEventListener('change', () => {
      if (fileInput.files.length > 0) {
        this.handleFile(fileInput.files[0]);
      }
    });

    if (form) {
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        this.submitUpload();
      });
    }
  },

  handleFile(file) {
    const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
    if (!['.xlsx', '.xlsm', '.csv'].includes(ext)) {
      this.showToast('Yalnızca .xlsx, .xlsm ve .csv dosyaları desteklenir.', 'error');
      return;
    }

    this.selectedFile = file;
    const badge = document.getElementById('home-selected-file-badge');
    const nameInput = document.getElementById('home-upload-name');
    const submitBtn = document.getElementById('home-upload-btn');

    if (badge) {
      const sizeKb = Math.round(file.size / 1024);
      badge.innerHTML = `
        <div>
          <strong>📄 ${this.escapeHtml(file.name)}</strong>
          <span style="opacity: 0.7; margin-left: 6px;">(${sizeKb > 1024 ? (sizeKb / 1024).toFixed(1) + ' MB' : sizeKb + ' KB'})</span>
        </div>
        <button type="button" onclick="HomeApp.clearSelectedFile()" style="background:transparent;border:none;color:#ff6b6b;cursor:pointer;font-weight:700;">&times;</button>
      `;
      badge.classList.remove('hidden');
    }

    if (nameInput && (!nameInput.value || nameInput.value.trim() === '')) {
      nameInput.value = file.name.replace(/\.[^/.]+$/, '');
    }

    if (submitBtn) submitBtn.disabled = false;
  },

  clearSelectedFile() {
    this.selectedFile = null;
    const fileInput = document.getElementById('home-file-input');
    if (fileInput) fileInput.value = '';
    const badge = document.getElementById('home-selected-file-badge');
    if (badge) badge.classList.add('hidden');
    const submitBtn = document.getElementById('home-upload-btn');
    if (submitBtn) submitBtn.disabled = true;
  },

  async submitUpload() {
    if (!this.selectedFile) {
      this.showToast('Lütfen önce bir Excel dosyası seçin.', 'error');
      return;
    }

    const submitBtn = document.getElementById('home-upload-btn');
    const nameInput = document.getElementById('home-upload-name');
    const origText = submitBtn ? submitBtn.innerHTML : '';

    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.innerHTML = '⏳ Yükleniyor ve SQLite Tablosu Açılıyor...';
    }

    const formData = new FormData();
    formData.append('file', this.selectedFile);
    if (nameInput && nameInput.value.trim()) {
      formData.append('name', nameInput.value.trim());
    }

    try {
      const res = await fetch('/api/workspaces/upload', {
        method: 'POST',
        body: formData,
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Yükleme hatası: HTTP ${res.status}`);
      }

      const newWs = await res.json();
      this.clearSelectedFile();
      if (nameInput) nameInput.value = '';

      // Yeni ortamı aktif ve seçili yap
      this.selectedWsId = newWs.id;
      await fetch(`/api/workspaces/${newWs.id}/activate`, { method: 'POST' });
      await this.refresh();

      this.showToast(`'${newWs.name}' başarıyla SQLite'a yüklendi ve seçildi!`, 'success');

      // Sütun eşleştirme modalını aç
      if (window.WorkspaceClient) {
        window.WorkspaceClient.openMappingModal(newWs.id);
      }
    } catch (err) {
      this.showToast(err.message, 'error');
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = origText;
      }
    }
  },

  showToast(msg, type = 'info') {
    if (window.WorkspaceClient && window.WorkspaceClient.showToast) {
      window.WorkspaceClient.showToast(msg, type);
      return;
    }
    alert(msg);
  },

  escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
};

document.addEventListener('DOMContentLoaded', () => {
  HomeApp.init();
});
