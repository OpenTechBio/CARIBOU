import {
  Component, OnInit, OnDestroy, inject, signal, ViewChild,
  ElementRef, AfterViewChecked, computed
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { Subscription } from 'rxjs';
import { SessionService } from '../../core/services/session.service';
import { AgentStreamService } from '../../core/services/agent-stream.service';
import { Message, Artifact, CodeEvent } from '../../core/models/session.model';
import {
  MessageCompleteData, AgentSwitchData, CodeSubmittedData,
  CodeResultData, ArtifactEventData, ErrorData, StatusChangeData
} from '../../core/models/events.model';
import { MessageBubbleComponent } from '../../shared/components/message-bubble/message-bubble';
import { ArtifactCardComponent } from '../../shared/components/artifact-card/artifact-card';
import { StatusIndicatorComponent } from '../../shared/components/status-indicator/status-indicator';

export interface ErrorRecord {
  code: string;
  message: string;
  fatal: boolean;
  timestamp: string;
  suggested_fix?: string | null;
}

export interface StatusEntry {
  status: string;
  reason: string | null;
  timestamp: string;
  count: number;  // collapsed repeat count
}

interface ChatItem {
  kind: 'message' | 'delegation' | 'code' | 'error';
  message?: Message;
  delegation?: { from: string; to: string; command: string };
  codeEvent?: { submitted: CodeSubmittedData; result?: CodeResultData };
  error?: ErrorRecord;
}

@Component({
  selector: 'app-session',
  standalone: true,
  imports: [
    CommonModule, FormsModule,
    MessageBubbleComponent, ArtifactCardComponent, StatusIndicatorComponent
  ],
  templateUrl: './session.html',
  styleUrl: './session.scss',
})
export class SessionComponent implements OnInit, OnDestroy, AfterViewChecked {
  @ViewChild('chatBottom') chatBottom!: ElementRef;

  private route = inject(ActivatedRoute);
  private router = inject(Router);
  sessionSvc = inject(SessionService);
  stream = inject(AgentStreamService);

  chatItems = signal<ChatItem[]>([]);
  artifacts = signal<Artifact[]>([]);
  userInput = signal('');
  pendingCode = signal<Map<string, CodeSubmittedData>>(new Map());
  errorLog = signal<ErrorRecord[]>([]);
  statusLog = signal<StatusEntry[]>([]);
  waitingForAgent = signal(false);
  showExtend = signal(false);
  extendTurns = signal(10);
  extending = signal(false);
  extendError = signal<string | null>(null);
  autoScroll = true;
  private subs = new Subscription();
  private shouldScrollToBottom = false;

  session = this.sessionSvc.currentSession;
  status = computed(() => this.session()?.status ?? 'stopped');
  currentAgent = computed(() => this.session()?.current_agent ?? '');
  isIdle = computed(() => this.status() === 'idle');
  isRunning = computed(() => this.status() === 'running');
  isError = computed(() => this.status() === 'error');
  isInitializing = computed(() => this.status() === 'initializing');
  // Thinking = running but no token has arrived yet (LLM is processing)
  isThinking = computed(() => this.isRunning() && !this.stream.isStreaming());
  lastError = computed(() => {
    const errs = this.errorLog();
    return errs.length ? errs[errs.length - 1] : null;
  });

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('id')!;
    this.sessionSvc.getSession(id).subscribe(() => {
      this.sessionSvc.getMessages(id).subscribe(msgs => {
        const items: ChatItem[] = msgs.map(m => ({ kind: 'message' as const, message: m }));
        this.chatItems.set(items);
        this.shouldScrollToBottom = true;
      });
      this.sessionSvc.getArtifacts(id).subscribe(a => this.artifacts.set(a));
    });

    this.stream.connect(id);

    this.subs.add(this.stream.messageComplete$.subscribe(ev => {
      const d = ev.data as MessageCompleteData;
      this.chatItems.update(items => [...items, { kind: 'message', message: d.message }]);
      this.shouldScrollToBottom = true;
    }));

    this.subs.add(this.stream.agentSwitch$.subscribe(ev => {
      const d = ev.data as AgentSwitchData;
      this.chatItems.update(items => [
        ...items,
        { kind: 'delegation', delegation: { from: d.from_agent, to: d.to_agent, command: d.command } }
      ]);
    }));

    this.subs.add(this.stream.codeSubmitted$.subscribe(ev => {
      const d = ev.data as CodeSubmittedData;
      const key = `${ev.turn}-${d.block_index}`;
      this.pendingCode.update(m => { const n = new Map(m); n.set(key, d); return n; });
    }));

    this.subs.add(this.stream.codeResult$.subscribe(ev => {
      const result = ev.data as CodeResultData;
      const key = `${ev.turn}-${result.block_index}`;
      const pending = this.pendingCode();
      const submitted = pending.get(key);
      if (submitted) {
        this.chatItems.update(items => [...items, { kind: 'code', codeEvent: { submitted, result } }]);
        this.pendingCode.update(m => { const n = new Map(m); n.delete(key); return n; });
        this.shouldScrollToBottom = true;
      }
    }));

    this.subs.add(this.stream.artifacts$.subscribe(() => {
      this.sessionSvc.getArtifacts(id).subscribe(a => this.artifacts.set(a));
    }));

    // Error tracking — show inline in chat and persist in error log
    this.subs.add(this.stream.errors$.subscribe(ev => {
      this.waitingForAgent.set(false);
      const d = ev.data as ErrorData;
      const record: ErrorRecord = {
        code: d.code,
        message: d.message,
        fatal: d.fatal,
        timestamp: ev.timestamp,
        suggested_fix: d.suggested_fix ?? null,
      };
      this.errorLog.update(errs => [...errs, record]);
      this.chatItems.update(items => [...items, { kind: 'error', error: record }]);
      this.shouldScrollToBottom = true;
    }));

    // Status change log — collapse consecutive identical entries
    this.subs.add(this.stream.statusChanges$.subscribe(ev => {
      this.waitingForAgent.set(false);
      const d = ev.data as StatusChangeData;
      this.statusLog.update(log => {
        const last = log[log.length - 1];
        // Collapse runs of the same status+reason into a count
        if (last && last.status === d.status && last.reason === d.reason) {
          return [...log.slice(0, -1), { ...last, count: last.count + 1 }];
        }
        return [...log, { status: d.status, reason: d.reason ?? null, timestamp: ev.timestamp, count: 1 }];
      });
    }));
  }

  ngAfterViewChecked(): void {
    if (this.shouldScrollToBottom && this.autoScroll) {
      this.scrollToBottom();
      this.shouldScrollToBottom = false;
    }
  }

  ngOnDestroy(): void {
    this.subs.unsubscribe();
    this.stream.disconnect();
  }

  sendMessage(): void {
    const content = this.userInput().trim();
    if (!content) return;
    const s = this.session();
    if (!s) return;

    // Auto sessions start themselves — interactive sessions need a run trigger
    if (s.status === 'idle' && s.current_turn === 0 && s.mode === 'interactive') {
      this.stream.startRun(content);
    } else {
      this.stream.sendUserMessage(content);
    }
    this.waitingForAgent.set(true);
    this.userInput.set('');
    this.chatItems.update(items => [...items, {
      kind: 'message',
      message: {
        id: 'pending-' + Date.now(),
        session_id: s.id,
        turn: s.current_turn + 1,
        role: 'user',
        agent_name: '',
        content,
        timestamp: new Date().toISOString(),
        is_delegation: false,
      }
    }]);
    this.shouldScrollToBottom = true;
  }

  stopSession(): void {
    this.stream.stop();
  }

  canExtend = computed(() =>
    this.session()?.mode === 'auto' && this.status() === 'stopped'
  );

  confirmExtend(): void {
    const s = this.session();
    if (!s) return;
    this.extending.set(true);
    this.extendError.set(null);
    this.sessionSvc.extendSession(s.id, this.extendTurns()).subscribe({
      next: () => {
        this.extending.set(false);
        this.showExtend.set(false);
      },
      error: (err) => {
        this.extending.set(false);
        this.extendError.set(err?.error?.detail ?? 'Failed to extend session.');
      },
    });
  }

  goBack(): void {
    this.router.navigate(['/']);
  }

  private scrollToBottom(): void {
    try {
      this.chatBottom.nativeElement.scrollIntoView({ behavior: 'smooth' });
    } catch { /* noop */ }
  }

  onScroll(event: Event): void {
    const el = event.target as HTMLElement;
    this.autoScroll = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  onKeyDown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.sendMessage();
    }
  }
}
