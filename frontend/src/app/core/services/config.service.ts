import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { tap } from 'rxjs/operators';
import { Observable } from 'rxjs';
import { AgentBlueprint, LLMBackend, ServerStatus } from '../models/session.model';

@Injectable({ providedIn: 'root' })
export class ConfigService {
  private http = inject(HttpClient);

  readonly serverStatus = signal<ServerStatus | null>(null);
  readonly blueprints = signal<AgentBlueprint[]>([]);
  readonly backends = signal<LLMBackend[]>([]);

  loadAll(): Observable<unknown> {
    return new Observable(obs => {
      let done = 0;
      const check = () => { if (++done === 3) { obs.next(null); obs.complete(); } };
      this.http.get<ServerStatus>('api/status').subscribe(s => { this.serverStatus.set(s); check(); });
      this.http.get<AgentBlueprint[]>('api/config/blueprints').subscribe(b => { this.blueprints.set(b); check(); });
      this.http.get<LLMBackend[]>('api/config/backends').subscribe(b => { this.backends.set(b); check(); });
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
}
