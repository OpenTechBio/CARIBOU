import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { Router } from '@angular/router';

interface ServerSettings {
  caribou_home: string;
  sessions_dir: string;
  uploads_dir: string;
  env_file: string;
  api_keys: Record<string, string>;
}

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './settings.html',
  styleUrl: './settings.scss',
})
export class SettingsComponent implements OnInit {
  private http = inject(HttpClient);
  private router = inject(Router);

  settings = signal<ServerSettings | null>(null);
  loading = signal(true);
  saving = signal(false);
  saveResult = signal<{ ok: boolean; message: string } | null>(null);

  // Editable fields
  sessionsDir = signal('');
  openaiKey = signal('');
  anthropicKey = signal('');
  deepseekKey = signal('');

  showKeys: Record<string, boolean> = {
    openai: false, anthropic: false, deepseek: false,
  };

  ngOnInit(): void {
    this.http.get<ServerSettings>('api/settings').subscribe({
      next: (s) => {
        this.settings.set(s);
        this.sessionsDir.set(s.sessions_dir);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  save(): void {
    this.saving.set(true);
    this.saveResult.set(null);
    const body: Record<string, string> = {};
    if (this.sessionsDir() !== this.settings()?.sessions_dir) {
      body['sessions_dir'] = this.sessionsDir();
    }
    if (this.openaiKey()) body['openai_api_key'] = this.openaiKey();
    if (this.anthropicKey()) body['anthropic_api_key'] = this.anthropicKey();
    if (this.deepseekKey()) body['deepseek_api_key'] = this.deepseekKey();

    if (Object.keys(body).length === 0) {
      this.saving.set(false);
      this.saveResult.set({ ok: true, message: 'Nothing to save.' });
      return;
    }

    this.http.patch<{ updated: string[] }>('api/settings', body).subscribe({
      next: (r) => {
        this.saving.set(false);
        this.openaiKey.set('');
        this.anthropicKey.set('');
        this.deepseekKey.set('');
        this.saveResult.set({ ok: true, message: `Saved: ${r.updated.join(', ')}` });
        // Reload settings to show updated masks
        this.http.get<ServerSettings>('api/settings').subscribe(s => this.settings.set(s));
      },
      error: (err) => {
        this.saving.set(false);
        this.saveResult.set({ ok: false, message: err?.error?.detail ?? 'Save failed.' });
      },
    });
  }

  goBack(): void {
    this.router.navigate(['/']);
  }
}
