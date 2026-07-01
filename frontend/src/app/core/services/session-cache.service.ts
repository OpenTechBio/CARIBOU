import { Injectable } from '@angular/core';

const PREFIX = 'caribou:session-cache:v1:';
const MAX_ITEMS = 400;

export interface CachedSessionState {
  chatItems: unknown[];
  artifacts: unknown[];
  errorLog: unknown[];
  statusLog: unknown[];
  cachedAt: number;
}

@Injectable({ providedIn: 'root' })
export class SessionCacheService {
  read(sessionId: string): CachedSessionState | null {
    try {
      const raw = sessionStorage.getItem(PREFIX + sessionId);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as CachedSessionState;
      if (!parsed || typeof parsed !== 'object') return null;
      return parsed;
    } catch {
      return null;
    }
  }

  write(sessionId: string, state: Omit<CachedSessionState, 'cachedAt'>): void {
    try {
      const payload: CachedSessionState = {
        chatItems: (state.chatItems ?? []).slice(-MAX_ITEMS),
        artifacts: state.artifacts ?? [],
        errorLog: (state.errorLog ?? []).slice(-50),
        statusLog: (state.statusLog ?? []).slice(-50),
        cachedAt: Date.now(),
      };
      sessionStorage.setItem(PREFIX + sessionId, JSON.stringify(payload));
    } catch {}
  }

  clear(sessionId: string): void {
    try { sessionStorage.removeItem(PREFIX + sessionId); } catch {}
  }
}
