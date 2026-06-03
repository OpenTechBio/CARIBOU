import { Injectable, inject, signal, OnDestroy } from '@angular/core';
import { Subject, filter, Observable } from 'rxjs';
import {
  AgentEvent, AgentEventType, AgentEventEnvelope,
  TokenEventData, MessageCompleteData, AgentSwitchData,
  CodeSubmittedData, CodeResultData, ArtifactEventData,
  StatusChangeData, ErrorData
} from '../models/events.model';
import { SessionService } from './session.service';
import { SessionStatus } from '../models/session.model';

// Derive WebSocket base from document.baseURI so OOD path prefix is included.
// e.g. https://isvood001.mskcc.org/node/iscb014/8000/ → wss://isvood001.mskcc.org/node/iscb014/8000
const _base = document.baseURI.replace(/\/$/, '');
const WS_BASE = _base.replace(/^http/, 'ws');
const MAX_RETRIES = 5;

@Injectable({ providedIn: 'root' })
export class AgentStreamService implements OnDestroy {
  private sessionSvc = inject(SessionService);

  private ws: WebSocket | null = null;
  private sessionId: string | null = null;
  private retries = 0;
  private pingInterval: ReturnType<typeof setInterval> | null = null;

  private _events$ = new Subject<AgentEvent>();
  readonly events$: Observable<AgentEvent> = this._events$.asObservable();

  // Typed sub-streams
  readonly tokens$ = this.ofType<TokenEventData>('token');
  readonly messageComplete$ = this.ofType<MessageCompleteData>('message_complete');
  readonly agentSwitch$ = this.ofType<AgentSwitchData>('agent_switch');
  readonly codeSubmitted$ = this.ofType<CodeSubmittedData>('code_submitted');
  readonly codeResult$ = this.ofType<CodeResultData>('code_result');
  readonly artifacts$ = this.ofType<ArtifactEventData>('artifact');
  readonly statusChanges$ = this.ofType<StatusChangeData>('status_change');
  readonly errors$ = this.ofType<ErrorData>('error');

  // Current streaming state
  readonly streamingContent = signal<string>('');
  readonly streamingAgent = signal<string>('');
  readonly isStreaming = signal<boolean>(false);

  connect(sessionId: string): void {
    this.disconnect();
    this.sessionId = sessionId;
    this.retries = 0;
    this._doConnect();
  }

  disconnect(): void {
    this._clearPing();
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
    this.sessionId = null;
    this.streamingContent.set('');
    this.streamingAgent.set('');
    this.isStreaming.set(false);
  }

  send(msg: { type: string; content?: string }): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  startRun(prompt: string): void {
    this.send({ type: 'run', content: prompt });
  }

  sendUserMessage(content: string): void {
    this.send({ type: 'user_message', content });
  }

  stop(): void {
    this.send({ type: 'stop' });
  }

  ngOnDestroy(): void {
    this.disconnect();
  }

  // ---------------------------------------------------------------------------

  private ofType<T>(type: AgentEventType): Observable<AgentEventEnvelope<T>> {
    return this.events$.pipe(
      filter(e => e.type === type)
    ) as Observable<AgentEventEnvelope<T>>;
  }

  private _doConnect(): void {
    if (!this.sessionId) return;
    const url = `${WS_BASE}/ws/sessions/${this.sessionId}`;
    this.ws = new WebSocket(url);

    this.ws.onmessage = (ev) => {
      try {
        const event: AgentEvent = JSON.parse(ev.data);
        this._handleEvent(event);
        this._events$.next(event);
      } catch { /* malformed message */ }
    };

    this.ws.onopen = () => {
      this.retries = 0;
      this._startPing();
    };

    this.ws.onclose = () => {
      this._clearPing();
      const session = this.sessionSvc.currentSession();
      const dead = session?.status === 'stopped' || session?.status === 'error';
      if (!dead && this.retries < MAX_RETRIES) {
        const delay = Math.min(1000 * 2 ** this.retries, 16000);
        this.retries++;
        setTimeout(() => this._doConnect(), delay);
      }
    };

    this.ws.onerror = () => { this.ws?.close(); };
  }

  private _handleEvent(event: AgentEvent): void {
    switch (event.type) {
      case 'token': {
        const d = event.data as TokenEventData;
        this.isStreaming.set(true);
        this.streamingAgent.set(d.agent_name);
        this.streamingContent.update(c => c + d.token);
        break;
      }
      case 'message_complete': {
        this.streamingContent.set('');
        this.streamingAgent.set('');
        this.isStreaming.set(false);
        break;
      }
      case 'status_change': {
        const d = event.data as StatusChangeData;
        const session = this.sessionSvc.currentSession();
        if (session) {
          this.sessionSvc.updateLocal({ ...session, status: d.status as SessionStatus });
        }
        break;
      }
      case 'agent_switch': {
        const d = event.data as AgentSwitchData;
        const session = this.sessionSvc.currentSession();
        if (session) {
          this.sessionSvc.updateLocal({ ...session, current_agent: d.to_agent });
        }
        break;
      }
    }
  }

  private _startPing(): void {
    this.pingInterval = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, 20000);
  }

  private _clearPing(): void {
    if (this.pingInterval !== null) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }
}
