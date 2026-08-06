import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, tap } from 'rxjs';
import {
  Artifact, CodeEvent, EvaluationResult, Message, MemoryState, Session, SessionCreateRequest,
  SessionForkRequest, SessionResumeRequest
} from '../models/session.model';

@Injectable({ providedIn: 'root' })
export class SessionService {
  private http = inject(HttpClient);

  readonly sessions = signal<Session[]>([]);
  readonly currentSession = signal<Session | null>(null);

  loadSessions(): Observable<Session[]> {
    return this.http.get<Session[]>('api/sessions').pipe(
      tap(s => this.sessions.set(s))
    );
  }

  createSession(config: SessionCreateRequest): Observable<Session> {
    return this.http.post<Session>('api/sessions', config).pipe(
      tap(s => {
        this.sessions.update(all => [s, ...all]);
        this.currentSession.set(s);
      })
    );
  }

  resumeSession(id: string, request: SessionResumeRequest): Observable<Session> {
    return this.http.post<Session>(`api/sessions/${id}/resume`, request).pipe(
      tap(s => this.updateLocal(s))
    );
  }

  forkSession(id: string, request: SessionForkRequest): Observable<Session> {
    return this.http.post<Session>(`api/sessions/${id}/fork`, request).pipe(
      tap(s => this.sessions.update(all => [s, ...all]))
    );
  }

  retryRecovery(id: string, request: SessionResumeRequest): Observable<Session> {
    return this.http.post<Session>(`api/sessions/${id}/recovery/retry`, request).pipe(
      tap(s => this.updateLocal(s))
    );
  }

  acceptPartialRecovery(id: string): Observable<Session> {
    return this.http.post<Session>(`api/sessions/${id}/recovery/accept-partial`, {}).pipe(
      tap(s => this.updateLocal(s))
    );
  }

  getSession(id: string): Observable<Session> {
    return this.http.get<Session>(`api/sessions/${id}`).pipe(
      tap(s => {
        this.currentSession.set(s);
        this.sessions.update(all => all.map(x => x.id === s.id ? s : x));
      })
    );
  }

  deleteSession(id: string): Observable<void> {
    return this.http.delete<void>(`api/sessions/${id}`).pipe(
      tap(() => {
        this.sessions.update(all => all.filter(s => s.id !== id));
        if (this.currentSession()?.id === id) {
          this.currentSession.set(null);
        }
      })
    );
  }

  getMessages(id: string, offset = 0, limit = 500): Observable<Message[]> {
    return this.http.get<Message[]>(`api/sessions/${id}/messages`, {
      params: { offset: String(offset), limit: String(limit) }
    });
  }

  getArtifacts(id: string): Observable<Artifact[]> {
    return this.http.get<Artifact[]>(`api/sessions/${id}/artifacts`);
  }

  getCodeEvents(id: string): Observable<CodeEvent[]> {
    return this.http.get<CodeEvent[]>(`api/sessions/${id}/code_events`);
  }

  artifactDownloadUrl(sessionId: string, artifactId: string): string {
    const base = document.baseURI.replace(/\/$/, '');
    return `${base}/api/sessions/${sessionId}/artifacts/${artifactId}/download`;
  }

  notebookDownloadUrl(sessionId: string): string {
    const base = document.baseURI.replace(/\/$/, '');
    return `${base}/api/sessions/${sessionId}/notebook`;
  }

  getMemoryState(id: string): Observable<MemoryState> {
    return this.http.get<MemoryState>(`api/sessions/${id}/memory`);
  }

  evaluate(id: string): Observable<EvaluationResult> {
    return this.http.post<EvaluationResult>(`api/sessions/${id}/evaluate`, {});
  }

  updateLocal(session: Session): void {
    this.currentSession.set(session);
    this.sessions.update(all => all.map(s => s.id === session.id ? session : s));
  }

  clearCurrentSession(): void {
    this.currentSession.set(null);
  }
}
