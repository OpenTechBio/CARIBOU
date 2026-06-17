import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { HttpClient } from '@angular/common/http';

interface CodeSampleInfo {
  filename: string;
  size_bytes: number;
  is_builtin: boolean;
}

interface CodeSampleContent {
  filename: string;
  content: string;
  is_builtin: boolean;
}

@Component({
  selector: 'app-code-samples',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  templateUrl: './code-samples.html',
  styleUrl: './code-samples.scss',
})
export class CodeSamplesComponent implements OnInit {
  private http = inject(HttpClient);

  samples = signal<CodeSampleInfo[]>([]);
  selected = signal<CodeSampleContent | null>(null);
  editorContent = signal('');
  isDirty = signal(false);

  loading = signal(false);
  saving = signal(false);
  deleting = signal(false);
  importing = signal(false);

  saveResult = signal<{ ok: boolean; message: string } | null>(null);
  importPath = signal('');
  importError = signal<string | null>(null);

  showNewDialog = signal(false);
  newFilename = signal('');
  newFilenameError = signal<string | null>(null);

  showDeleteConfirm = signal(false);

  showCloneDialog = signal(false);
  cloneFilename = signal('');
  cloneFilenameError = signal<string | null>(null);

  get builtinSamples() {
    return this.samples().filter(s => s.is_builtin);
  }

  get userSamples() {
    return this.samples().filter(s => !s.is_builtin);
  }

  ngOnInit(): void {
    this.loadList();
  }

  loadList(): void {
    this.http.get<CodeSampleInfo[]>('api/config/code-samples').subscribe({
      next: (list) => this.samples.set(list),
      error: () => {},
    });
  }

  selectSample(filename: string): void {
    if (this.isDirty()) {
      if (!confirm('You have unsaved changes. Discard them?')) return;
    }
    this.saveResult.set(null);
    this.loading.set(true);
    this.http.get<CodeSampleContent>(`api/config/code-samples/${encodeURIComponent(filename)}`).subscribe({
      next: (s) => {
        this.loading.set(false);
        this.selected.set(s);
        this.editorContent.set(s.content);
        this.isDirty.set(false);
      },
      error: () => {
        this.loading.set(false);
      },
    });
  }

  onContentChange(value: string): void {
    this.editorContent.set(value);
    const sel = this.selected();
    this.isDirty.set(sel !== null && value !== sel.content);
  }

  onTabKey(event: Event): void {
    event.preventDefault();
    const textarea = event.target as HTMLTextAreaElement;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const spaces = '    ';
    const newValue = textarea.value.substring(0, start) + spaces + textarea.value.substring(end);
    this.editorContent.set(newValue);
    this.isDirty.set(true);
    requestAnimationFrame(() => {
      textarea.selectionStart = start + spaces.length;
      textarea.selectionEnd = start + spaces.length;
    });
  }

  save(): void {
    const sel = this.selected();
    if (!sel || sel.is_builtin) return;
    this.saving.set(true);
    this.saveResult.set(null);
    this.http.put<CodeSampleContent>(
      `api/config/code-samples/${encodeURIComponent(sel.filename)}`,
      { content: this.editorContent() },
    ).subscribe({
      next: (updated) => {
        this.saving.set(false);
        this.selected.set(updated);
        this.isDirty.set(false);
        this.saveResult.set({ ok: true, message: 'Saved.' });
        this.loadList();
      },
      error: (err) => {
        this.saving.set(false);
        this.saveResult.set({ ok: false, message: err?.error?.detail ?? 'Save failed.' });
      },
    });
  }

  requestDelete(): void {
    this.showDeleteConfirm.set(true);
  }

  cancelDelete(): void {
    this.showDeleteConfirm.set(false);
  }

  confirmDelete(): void {
    const sel = this.selected();
    if (!sel) return;
    this.deleting.set(true);
    this.showDeleteConfirm.set(false);
    this.http.delete(`api/config/code-samples/${encodeURIComponent(sel.filename)}`).subscribe({
      next: () => {
        this.deleting.set(false);
        this.selected.set(null);
        this.editorContent.set('');
        this.isDirty.set(false);
        this.loadList();
      },
      error: (err) => {
        this.deleting.set(false);
        this.saveResult.set({ ok: false, message: err?.error?.detail ?? 'Delete failed.' });
      },
    });
  }

  openNewDialog(): void {
    this.newFilename.set('');
    this.newFilenameError.set(null);
    this.showNewDialog.set(true);
  }

  cancelNewDialog(): void {
    this.showNewDialog.set(false);
  }

  submitNew(): void {
    const name = this.newFilename().trim();
    if (!name) {
      this.newFilenameError.set('Filename is required.');
      return;
    }
    if (name.includes('/') || name.includes('\\')) {
      this.newFilenameError.set('Filename must not contain path separators.');
      return;
    }
    const filename = name.endsWith('.py') ? name : name + '.py';
    this.http.post<CodeSampleContent>('api/config/code-samples', { filename, content: '' }).subscribe({
      next: (created) => {
        this.showNewDialog.set(false);
        this.loadList();
        this.selected.set(created);
        this.editorContent.set('');
        this.isDirty.set(false);
        this.saveResult.set(null);
      },
      error: (err) => {
        this.newFilenameError.set(err?.error?.detail ?? 'Failed to create file.');
      },
    });
  }

  openCloneDialog(): void {
    const sel = this.selected();
    if (!sel) return;
    const base = sel.filename.replace(/\.py$/, '');
    this.cloneFilename.set(base + '_copy');
    this.cloneFilenameError.set(null);
    this.showCloneDialog.set(true);
  }

  cancelCloneDialog(): void {
    this.showCloneDialog.set(false);
  }

  submitClone(): void {
    const sel = this.selected();
    if (!sel) return;
    const name = this.cloneFilename().trim();
    if (!name) {
      this.cloneFilenameError.set('Filename is required.');
      return;
    }
    if (name.includes('/') || name.includes('\\')) {
      this.cloneFilenameError.set('Filename must not contain path separators.');
      return;
    }
    const filename = name.endsWith('.py') ? name : name + '.py';
    this.http.post<CodeSampleContent>('api/config/code-samples', {
      filename,
      content: this.editorContent(),
    }).subscribe({
      next: (created) => {
        this.showCloneDialog.set(false);
        this.loadList();
        this.selected.set(created);
        this.editorContent.set(created.content);
        this.isDirty.set(false);
        this.saveResult.set({ ok: true, message: `Cloned as ${created.filename}.` });
      },
      error: (err) => {
        this.cloneFilenameError.set(err?.error?.detail ?? 'Failed to clone.');
      },
    });
  }

  importSample(): void {
    const path = this.importPath().trim();
    if (!path) return;
    this.importing.set(true);
    this.importError.set(null);
    this.http.post<{ filename: string; destination: string }>(
      'api/config/code-samples/import',
      { source_path: path },
    ).subscribe({
      next: (res) => {
        this.importing.set(false);
        this.importPath.set('');
        this.loadList();
        this.selectSample(res.filename);
      },
      error: (err) => {
        this.importing.set(false);
        this.importError.set(err?.error?.detail ?? 'Import failed.');
      },
    });
  }

  formatSize(bytes: number): string {
    if (bytes > 1e6) return (bytes / 1e6).toFixed(1) + ' MB';
    if (bytes > 1e3) return (bytes / 1e3).toFixed(0) + ' KB';
    return bytes + ' B';
  }
}
