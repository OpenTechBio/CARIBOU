import {
  Component, OnInit, inject, signal, computed
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { SessionService } from '../../core/services/session.service';
import { ConfigService } from '../../core/services/config.service';
import { DatasetService } from '../../core/services/dataset.service';
import { Session, SessionCreateRequest, Dataset, AgentBlueprint, LLMBackend } from '../../core/models/session.model';
import { StatusIndicatorComponent } from '../../shared/components/status-indicator/status-indicator';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink, StatusIndicatorComponent],
  templateUrl: './dashboard.html',
  styleUrl: './dashboard.scss',
})
export class DashboardComponent implements OnInit {
  private router = inject(Router);
  sessionSvc = inject(SessionService);
  configSvc = inject(ConfigService);
  datasetSvc = inject(DatasetService);

  datasets = signal<Dataset[]>([]);
  showCreateDialog = signal(false);
  creating = signal(false);
  createError = signal<string | null>(null);
  pendingDeleteId = signal<string | null>(null);

  get pendingDeleteSession() {
    return this.sessionSvc.sessions().find(s => s.id === this.pendingDeleteId());
  }

  // Create form state
  form: SessionCreateRequest = {
    mode: 'auto',
    run_mode: 'full_system',
    agent_system: '',
    llm_backend: '',
    sandbox_type: 'singularity',
    dataset_path: '',
    max_turns: 20,
    initial_prompt: '',
  };

  availableBackends = computed(() =>
    this.configSvc.backends().filter(b => b.available)
  );

  ngOnInit(): void {
    this.sessionSvc.loadSessions().subscribe();
    this.configSvc.loadAll().subscribe(() => {
      const bp = this.configSvc.blueprints()[0];
      if (bp) this.form.agent_system = bp.name;
      const be = this.availableBackends()[0];
      if (be) this.form.llm_backend = be.id;
    });
    this.datasetSvc.getDatasets().subscribe(d => this.datasets.set(d));
  }

  openCreate(): void {
    this.createError.set(null);
    this.showCreateDialog.set(true);
  }

  cancelCreate(): void {
    this.showCreateDialog.set(false);
  }

  submitCreate(): void {
    if (!this.form.agent_system || !this.form.llm_backend || !this.form.dataset_path) {
      this.createError.set('Blueprint, backend, and dataset are required.');
      return;
    }
    this.creating.set(true);
    this.createError.set(null);
    this.sessionSvc.createSession(this.form).subscribe({
      next: (s) => {
        this.creating.set(false);
        this.showCreateDialog.set(false);
        this.router.navigate(['/session', s.id]);
      },
      error: (err) => {
        this.creating.set(false);
        this.createError.set(err?.error?.detail ?? 'Failed to create session.');
      },
    });
  }

  openSession(id: string): void {
    this.router.navigate(['/session', id]);
  }

  requestDelete(id: string, event: Event): void {
    event.stopPropagation();
    this.pendingDeleteId.set(id);
  }

  confirmDelete(): void {
    const id = this.pendingDeleteId();
    if (!id) return;
    this.sessionSvc.deleteSession(id).subscribe();
    this.pendingDeleteId.set(null);
  }

  cancelDelete(): void {
    this.pendingDeleteId.set(null);
  }

  onFileSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    this.datasetSvc.uploadDataset(file).subscribe(ev => {
      if (ev.dataset) {
        this.datasets.update(d => [...d, ev.dataset!]);
        this.form.dataset_path = ev.dataset!.path;
      }
    });
  }

  formatSize(bytes: number): string {
    if (bytes > 1e9) return (bytes / 1e9).toFixed(1) + ' GB';
    if (bytes > 1e6) return (bytes / 1e6).toFixed(1) + ' MB';
    return (bytes / 1e3).toFixed(0) + ' KB';
  }
}
