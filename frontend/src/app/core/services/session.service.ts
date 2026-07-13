import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, tap } from 'rxjs';
import {
  Artifact, CodeEvent, Message, Session, SessionCreateRequest
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

  updateLocal(session: Session): void {
    this.currentSession.set(session);
    this.sessions.update(all => all.map(s => s.id === session.id ? session : s));
  }
}
