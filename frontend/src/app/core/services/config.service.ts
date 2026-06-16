import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { tap } from 'rxjs/operators';
import { Observable } from 'rxjs';
import { AgentBlueprint, LLMBackend, OllamaModelsResponse, ServerStatus } from '../models/session.model';

@Injectable({ providedIn: 'root' })
export class ConfigService {
  private http = inject(HttpClient);

  readonly serverStatus = signal<ServerStatus | null>(null);
  readonly blueprints = signal<AgentBlueprint[]>([]);
  readonly backends = signal<LLMBackend[]>([]);
  readonly ollamaModels = signal<OllamaModelsResponse | null>(null);

  loadAll(): Observable<unknown> {
    return new Observable(obs => {
      let done = 0;
      const check = () => { if (++done === 4) { obs.next(null); obs.complete(); } };
      this.http.get<ServerStatus>('api/status').subscribe(s => { this.serverStatus.set(s); check(); });
      this.http.get<AgentBlueprint[]>('api/config/blueprints').subscribe(b => { this.blueprints.set(b); check(); });
      this.http.get<LLMBackend[]>('api/config/backends').subscribe(b => { this.backends.set(b); check(); });
      this.http.get<OllamaModelsResponse>('api/config/ollama/models').subscribe({
        next: o => { this.ollamaModels.set(o); check(); },
        error: () => { this.ollamaModels.set(null); check(); },
      });
    });
  }

  getStatus(): Observable<ServerStatus> {
    return this.http.get<ServerStatus>('api/status').pipe(
      tap(s => this.serverStatus.set(s))
    );
  }

  getBlueprints(): Observable<AgentBlueprint[]> {
    return this.http.get<AgentBlueprint[]>('api/config/blueprints').pipe(
      tap(b => this.blueprints.set(b))
    );
  }

  getBackends(): Observable<LLMBackend[]> {
    return this.http.get<LLMBackend[]>('api/config/backends').pipe(
      tap(b => this.backends.set(b))
    );
  }

  getOllamaModels(): Observable<OllamaModelsResponse> {
    return this.http.get<OllamaModelsResponse>('api/config/ollama/models').pipe(
      tap(o => this.ollamaModels.set(o))
    );
  }
}
