import {
  Component, OnInit, OnDestroy, inject, signal, ViewChild,
  ElementRef, AfterViewChecked, computed, HostListener, effect
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { Subscription, interval } from 'rxjs';
import { SessionService } from '../../core/services/session.service';
import { AgentStreamService } from '../../core/services/agent-stream.service';
import { ToastService } from '../../core/services/toast.service';
import { PreferencesService } from '../../core/services/preferences.service';
import { SessionCacheService } from '../../core/services/session-cache.service';
import { Message, Artifact } from '../../core/models/session.model';
import {
  MessageCompleteData, AgentSwitchData, CodeSubmittedData,
  CodeResultData, ErrorData, StatusChangeData
} from '../../core/models/events.model';
import { MessageBubbleComponent } from '../../shared/components/message-bubble/message-bubble';
import { ArtifactCardComponent } from '../../shared/components/artifact-card/artifact-card';
import { StatusIndicatorComponent } from '../../shared/components/status-indicator/status-indicator';
import { IconComponent } from '../../shared/components/icon/icon';
import { TooltipDirective } from '../../shared/directives/tooltip.directive';

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
  count: number;
}

interface ChatItem {
  kind: 'message' | 'delegation' | 'code' | 'error';
  turn?: number;
  message?: Message;
  delegation?: { from: string; to: string; command: string };
  codeEvent?: { submitted: CodeSubmittedData; result?: CodeResultData };
  error?: ErrorRecord;
}

const COMPACT_AFTER_ITEMS = 40;
const VISIBLE_RECENT_ITEMS = 20;
const AUTO_SCROLL_THRESHOLD_PX = 120;
const INPUT_HISTORY_KEY = 'caribou:input-history:v1';
const INPUT_HISTORY_LIMIT = 25;

type ArtifactFilter = 'all' | 'plot' | 'data' | 'other';

@Component({
  selector: 'app-session',
  standalone: true,
  imports: [
    CommonModule, FormsModule,
    MessageBubbleComponent, ArtifactCardComponent, StatusIndicatorComponent,
    IconComponent, TooltipDirective,
  ],
  templateUrl: './session.html',
  styleUrl: './session.scss',
})
export class SessionComponent implements OnInit, OnDestroy, AfterViewChecked {
  @ViewChild('chatBottom') chatBottom!: ElementRef;
  @ViewChild('chatPanel') chatPanel!: ElementRef<HTMLElement>;
  @ViewChild('messageInput') messageInput!: ElementRef<HTMLTextAreaElement>;
  readonly COMPACT_AFTER_ITEMS = COMPACT_AFTER_ITEMS;

  private route = inject(ActivatedRoute);
  private router = inject(Router);
  sessionSvc = inject(SessionService);
  stream = inject(AgentStreamService);
  private toasts = inject(ToastService);
  private prefsSvc = inject(PreferencesService);
  private cache = inject(SessionCacheService);

  chatItems = signal<ChatItem[]>([]);
  olderConversationExpanded = signal(false);
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
  awaitingCodeResult = signal(false);
  showTimeline = signal(false);
  artifactFilter = signal<ArtifactFilter>('all');
  artifactSearch = signal('');
  showConnectionBanner = computed(() => {
    const s = this.stream.connectionState();
    return s === 'reconnecting' || s === 'expired';
  });
  reconnectCountdownSec = signal(0);
  showShortcutHelp = signal(false);
  autoScrollEnabled = signal(true);
  showJumpToLatest = signal(false);
  sessionElapsedSec = signal(0);
  private sessionStartTs: number | null = null;
  private lastCompletedStatus: string | null = null;
  private originalTitle = typeof document !== 'undefined' ? document.title : '';
  private inputHistory: string[] = this.loadHistory();
  private historyIndex = -1;
  private historyDraft = '';

  private subs = new Subscription();
  private shouldScrollToBottom = false;
  private cacheHydrated = false;

  session = this.sessionSvc.currentSession;
  status = computed(() => this.session()?.status ?? 'stopped');
  currentAgent = computed(() => this.session()?.current_agent ?? '');
  isIdle = computed(() => this.status() === 'idle');
  isRunning = computed(() => this.status() === 'running');
  isError = computed(() => this.status() === 'error');
  isInitializing = computed(() => this.status() === 'initializing');
  isThinking = computed(() => this.isRunning() && !this.stream.isStreaming() && !this.awaitingCodeResult());
  isInitialInteractiveTurn = computed(() =>
    this.session()?.mode === 'interactive' && (this.session()?.current_turn ?? 0) <= 1
  );
  waitingLabel = computed(() =>
    this.isInitialInteractiveTurn() ? 'Getting environment set up…' : 'Waiting for agent…'
  );
  thinkingLabel = computed(() =>
    this.isInitialInteractiveTurn() ? 'Getting environment set up…' : (this.currentAgent() || 'Agent')
  );
  lastError = computed(() => {
    const errs = this.errorLog();
    return errs.length ? errs[errs.length - 1] : null;
  });
  runState = computed<'idle' | 'thinking' | 'generating' | 'executing' | 'stopped' | 'error' | 'initializing'>(() => {
    if (this.isError()) return 'error';
    if (this.isInitializing()) return 'initializing';
    if (this.status() === 'stopped') return 'stopped';
    if (this.awaitingCodeResult()) return 'executing';
    if (this.stream.isStreaming()) return 'generating';
    if (this.isRunning()) return 'thinking';
    return 'idle';
  });
  runStateLabel = computed(() => {
    switch (this.runState()) {
      case 'generating':   return 'Agent generating';
      case 'thinking':     return 'Agent thinking';
      case 'executing':    return 'Running code';
      case 'initializing': return 'Initializing';
      case 'stopped':      return 'Stopped';
      case 'error':        return 'Errored';
      default:             return 'Idle';
    }
  });
  hiddenChatItemCount = computed(() => {
    const count = this.chatItems().length;
    return !this.olderConversationExpanded() && count > COMPACT_AFTER_ITEMS
      ? count - VISIBLE_RECENT_ITEMS
      : 0;
  });
  visibleChatItems = computed(() => {
    const items = this.chatItems();
    const hidden = this.hiddenChatItemCount();
    return hidden ? items.slice(hidden) : items;
  });
  canExtend = computed(() =>
    this.session()?.mode === 'auto' && this.status() === 'stopped'
  );

  /** Turn -> chatItem index (first item at that turn). Used for jump-to-turn. */
  turnAnchors = computed(() => {
    const anchors: { turn: number; index: number }[] = [];
    const items = this.chatItems();
    const seen = new Set<number>();
    items.forEach((item, i) => {
      const turn = item.turn ?? item.message?.turn ?? 0;
      if (turn && !seen.has(turn)) {
        seen.add(turn);
        anchors.push({ turn, index: i });
      }
    });
    return anchors;
  });

  avgTurnDurationMs = computed(() => {
    const s = this.session();
    if (!s || !s.current_turn || !this.sessionStartTs) return 0;
    const elapsed = this.sessionElapsedSec() * 1000;
    if (elapsed <= 0) return 0;
    return elapsed / Math.max(s.current_turn, 1);
  });

  eta = computed(() => {
    const s = this.session();
    if (!s || !s.max_turns || !s.current_turn) return null;
    const avg = this.avgTurnDurationMs();
    if (avg <= 0) return null;
    const remaining = Math.max(0, s.max_turns - s.current_turn);
    if (!remaining) return null;
    return Math.round((remaining * avg) / 1000);
  });

  filteredArtifacts = computed(() => {
    const filter = this.artifactFilter();
    const q = this.artifactSearch().trim().toLowerCase();
    return this.artifacts()
      .filter(a => {
        if (filter === 'all') return true;
        if (filter === 'plot') return a.type === 'plot';
        if (filter === 'data') return a.type === 'data';
        return a.type !== 'plot' && a.type !== 'data';
      })
      .filter(a => !q || a.filename.toLowerCase().includes(q));
  });

  artifactCounts = computed(() => {
    const arts = this.artifacts();
    return {
      all: arts.length,
      plot: arts.filter(a => a.type === 'plot').length,
      data: arts.filter(a => a.type === 'data').length,
      other: arts.filter(a => a.type !== 'plot' && a.type !== 'data').length,
    };
  });

  constructor() {
    // Reactive tab-title notifications when session completes.
    effect(() => {
      const status = this.status();
      const prefs = this.prefsSvc.prefs();
      const s = this.session();
      if (!s) return;
      // Only fire once per transition into stopped/error.
      if (status === 'stopped' || status === 'error') {
        if (this.lastCompletedStatus !== status) {
          this.lastCompletedStatus = status;
          this.notifyCompletion(status);
        }
        if (prefs.tabTitleNotifications && document.hidden) {
          const prefix = status === 'stopped' ? '(done)' : '(error)';
          document.title = `${prefix} ${this.originalTitle}`;
        }
      } else if (status === 'running' || status === 'initializing' || status === 'idle') {
        this.lastCompletedStatus = null;
        document.title = this.originalTitle;
      }
    });

    // Persist chat / artifacts / logs to sessionStorage for refresh recovery.
    effect(() => {
      const s = this.session();
      if (!s || !this.cacheHydrated) return;
      this.cache.write(s.id, {
        chatItems: this.chatItems(),
        artifacts: this.artifacts(),
        errorLog: this.errorLog(),
        statusLog: this.statusLog(),
      });
    });
  }

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('id')!;
    // Hydrate from sessionStorage FIRST so refresh isn't jarring.
    const cached = this.cache.read(id);
    if (cached) {
      const stale = Date.now() - cached.cachedAt > 5 * 60 * 1000;
      if (!stale) {
        this.chatItems.set(cached.chatItems as ChatItem[] ?? []);
        this.artifacts.set(cached.artifacts as Artifact[] ?? []);
        this.errorLog.set(cached.errorLog as ErrorRecord[] ?? []);
        this.statusLog.set(cached.statusLog as StatusEntry[] ?? []);
      }
    }
    this.cacheHydrated = true;

    this.sessionSvc.getSession(id).subscribe(() => {
      this.sessionSvc.getMessages(id).subscribe(msgs => {
        // Only replace chat when server has more/newer messages than cache.
        if (msgs.length >= this.chatItems().filter(c => c.kind === 'message').length) {
          const items: ChatItem[] = msgs.map(m => ({ kind: 'message' as const, message: m, turn: m.turn }));
          this.chatItems.set(items);
          this.shouldScrollToBottom = true;
        }
      });
      this.sessionSvc.getArtifacts(id).subscribe(a => this.artifacts.set(a));
      this.sessionStartTs = Date.now();
    });

    this.stream.connect(id);

    // Sub-second timer so elapsed/ETA update live.
    this.subs.add(interval(1000).subscribe(() => {
      if (this.sessionStartTs && (this.isRunning() || this.isInitializing())) {
        this.sessionElapsedSec.set(Math.floor((Date.now() - this.sessionStartTs) / 1000));
      }
      const at = this.stream.nextRetryAt();
      if (at) {
        this.reconnectCountdownSec.set(Math.max(0, Math.ceil((at - Date.now()) / 1000)));
      } else {
        this.reconnectCountdownSec.set(0);
      }
    }));

    this.subs.add(this.stream.messageComplete$.subscribe(ev => {
      const d = ev.data as MessageCompleteData;
      this.upsertMessage(d.message);
      this.awaitingCodeResult.set(false);
      if (this.autoScrollEnabled()) this.shouldScrollToBottom = true;
    }));

    this.subs.add(this.stream.agentSwitch$.subscribe(ev => {
      const d = ev.data as AgentSwitchData;
      this.chatItems.update(items => [
        ...items,
        { kind: 'delegation', turn: ev.turn, delegation: { from: d.from_agent, to: d.to_agent, command: d.command } }
      ]);
    }));

    this.subs.add(this.stream.codeSubmitted$.subscribe(ev => {
      const d = ev.data as CodeSubmittedData;
      const key = `${ev.turn}-${d.block_index}`;
      this.pendingCode.update(m => { const n = new Map(m); n.set(key, d); return n; });
      this.awaitingCodeResult.set(true);
    }));

    this.subs.add(this.stream.codeResult$.subscribe(ev => {
      const result = ev.data as CodeResultData;
      const key = `${ev.turn}-${result.block_index}`;
      const pending = this.pendingCode();
      const submitted = pending.get(key);
      if (submitted) {
        this.chatItems.update(items => [...items, { kind: 'code', turn: ev.turn, codeEvent: { submitted, result } }]);
        this.pendingCode.update(m => { const n = new Map(m); n.delete(key); return n; });
        if (this.autoScrollEnabled()) this.shouldScrollToBottom = true;
      }
      // Only clear waiting state when all outstanding blocks are resolved.
      if (this.pendingCode().size === 0) {
        this.awaitingCodeResult.set(false);
      }
    }));

    this.subs.add(this.stream.artifacts$.subscribe(() => {
      this.sessionSvc.getArtifacts(id).subscribe(a => this.artifacts.set(a));
    }));

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
      this.chatItems.update(items => [...items, { kind: 'error', turn: ev.turn, error: record }]);
      if (this.autoScrollEnabled()) this.shouldScrollToBottom = true;
      if (!d.fatal && this.prefsSvc.prefs().toastNotifications) {
        this.toasts.show({
          kind: 'warn',
          title: `${d.code}`,
          detail: d.message,
          ttlMs: 6000,
        });
      }
    }));

    this.subs.add(this.stream.statusChanges$.subscribe(ev => {
      this.waitingForAgent.set(false);
      const d = ev.data as StatusChangeData;
      this.statusLog.update(log => {
        const last = log[log.length - 1];
        if (last && last.status === d.status && last.reason === d.reason) {
          return [...log.slice(0, -1), { ...last, count: last.count + 1 }];
        }
        return [...log, { status: d.status, reason: d.reason ?? null, timestamp: ev.timestamp, count: 1 }];
      });
    }));
  }

  ngAfterViewChecked(): void {
    if (this.shouldScrollToBottom && this.autoScrollEnabled()) {
      this.scrollToBottom();
      this.shouldScrollToBottom = false;
    }
  }

  ngOnDestroy(): void {
    this.subs.unsubscribe();
    this.stream.disconnect();
    document.title = this.originalTitle;
  }

  sendMessage(): void {
    const content = this.userInput().trim();
    if (!content) return;
    const s = this.session();
    if (!s) return;

    if (s.status === 'idle' && s.current_turn === 0 && s.mode === 'interactive') {
      this.stream.startRun(content);
    } else {
      this.stream.sendUserMessage(content);
    }
    this.waitingForAgent.set(true);
    this.userInput.set('');
    this.pushHistory(content);
    this.chatItems.update(items => [...items, {
      kind: 'message',
      turn: s.current_turn + 1,
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

  retryReconnect(): void {
    this.stream.retryNow();
  }

  retryLastAction(): void {
    // Best-effort recovery: for a session in error, users can extend to try again.
    if (this.canExtend()) {
      this.showExtend.set(true);
    }
  }

  copyToClipboard(text: string): void {
    try {
      navigator.clipboard.writeText(text);
      this.toasts.show({ kind: 'success', title: 'Copied to clipboard', ttlMs: 2000 });
    } catch {}
  }

  copyErrorToClipboard(err: ErrorRecord): void {
    const payload = [
      `[${err.code}] ${err.message}`,
      err.suggested_fix ? `Suggested fix: ${err.suggested_fix}` : null,
      `At: ${err.timestamp}`,
    ].filter(Boolean).join('\n');
    this.copyToClipboard(payload);
  }

  formatDuration(seconds: number | null): string {
    if (seconds === null) return '—';
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m < 60) return `${m}m ${s.toString().padStart(2, '0')}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${(m % 60).toString().padStart(2, '0')}m`;
  }

  formatMs(ms: number): string {
    if (!isFinite(ms) || ms <= 0) return '—';
    return this.formatDuration(Math.round(ms / 1000));
  }

  isSlowBlock(codeEvent: { result?: CodeResultData }): boolean {
    const ms = codeEvent.result?.duration_ms ?? 0;
    return ms >= this.prefsSvc.prefs().slowCodeThresholdMs;
  }

  jumpToTurn(turn: number): void {
    const el = document.querySelector(`[data-turn="${turn}"]`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      this.autoScrollEnabled.set(false);
      this.showJumpToLatest.set(true);
    }
  }

  jumpToLatest(): void {
    this.autoScrollEnabled.set(true);
    this.showJumpToLatest.set(false);
    this.scrollToBottom();
  }

  toggleOlderConversation(): void {
    this.olderConversationExpanded.update(v => !v);
  }

  toggleTimeline(): void {
    this.showTimeline.update(v => !v);
  }

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
        this.extendError.set(err?.error?.detail ?? 'Failed to continue the run.');
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
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const wasEnabled = this.autoScrollEnabled();
    const nearBottom = distanceFromBottom < AUTO_SCROLL_THRESHOLD_PX;
    // Only flip state on change to avoid signal churn.
    if (nearBottom && !wasEnabled) {
      this.autoScrollEnabled.set(true);
      this.showJumpToLatest.set(false);
    } else if (!nearBottom && wasEnabled) {
      this.autoScrollEnabled.set(false);
      this.showJumpToLatest.set(true);
    }
  }

  onKeyDown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.sendMessage();
      return;
    }
    // Input history — only when caret in empty single-line context
    if (event.key === 'ArrowUp' && !event.shiftKey && !this.userInput().includes('\n') && this.inputHistory.length) {
      if (this.historyIndex === -1) this.historyDraft = this.userInput();
      this.historyIndex = Math.min(this.historyIndex + 1, this.inputHistory.length - 1);
      this.userInput.set(this.inputHistory[this.inputHistory.length - 1 - this.historyIndex] ?? '');
      event.preventDefault();
      return;
    }
    if (event.key === 'ArrowDown' && !event.shiftKey && this.historyIndex >= 0) {
      this.historyIndex--;
      if (this.historyIndex === -1) this.userInput.set(this.historyDraft);
      else this.userInput.set(this.inputHistory[this.inputHistory.length - 1 - this.historyIndex] ?? '');
      event.preventDefault();
    }
  }

  @HostListener('window:keydown', ['$event'])
  onGlobalKeyDown(event: KeyboardEvent): void {
    const target = event.target as HTMLElement | null;
    const editable = target && (
      target.tagName === 'INPUT' ||
      target.tagName === 'TEXTAREA' ||
      target.isContentEditable
    );
    // Esc — stop run
    if (event.key === 'Escape' && this.isRunning()) {
      event.preventDefault();
      this.stopSession();
      this.toasts.show({ kind: 'info', title: 'Stopping session…', ttlMs: 2000 });
      return;
    }
    // Cmd/Ctrl-K — focus input
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      this.messageInput?.nativeElement?.focus();
      return;
    }
    // Cmd/Ctrl-/ — show shortcuts
    if ((event.metaKey || event.ctrlKey) && event.key === '/') {
      event.preventDefault();
      this.showShortcutHelp.update(v => !v);
      return;
    }
    // "/" outside editable — focus input
    if (event.key === '/' && !editable) {
      event.preventDefault();
      this.messageInput?.nativeElement?.focus();
    }
  }

  private notifyCompletion(status: string): void {
    const prefs = this.prefsSvc.prefs();
    if (!prefs.toastNotifications) return;
    const stopped = status === 'stopped';
    this.toasts.show({
      kind: stopped ? 'success' : 'error',
      title: stopped ? 'Session complete' : 'Session ended with an error',
      detail: stopped ? 'The agent finished its run.' : this.lastError()?.message ?? undefined,
      ttlMs: 8000,
    });
    if (prefs.soundOnComplete) this.playBeep(stopped);
  }

  private playBeep(good: boolean): void {
    try {
      const AC = (window as any).AudioContext || (window as any).webkitAudioContext;
      if (!AC) return;
      const ctx = new AC();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = good ? 880 : 260;
      gain.gain.value = 0.05;
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      setTimeout(() => { osc.stop(); ctx.close(); }, 180);
    } catch {}
  }

  private upsertMessage(message: Message): void {
    this.chatItems.update(items => {
      if (items.some(item => item.kind === 'message' && item.message?.id === message.id)) {
        return items;
      }
      const pendingIndex = items.findIndex(item =>
        item.kind === 'message' &&
        item.message?.id.startsWith('pending-') &&
        item.message.role === message.role &&
        item.message.content === message.content
      );
      if (pendingIndex >= 0) {
        return items.map((item, index) =>
          index === pendingIndex ? { kind: 'message', message, turn: message.turn } : item
        );
      }
      return [...items, { kind: 'message', message, turn: message.turn }];
    });
  }

  private loadHistory(): string[] {
    try {
      const raw = localStorage.getItem(INPUT_HISTORY_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  private pushHistory(entry: string): void {
    const trimmed = entry.trim();
    if (!trimmed) return;
    // Dedup consecutive identical entries.
    if (this.inputHistory[this.inputHistory.length - 1] === trimmed) return;
    this.inputHistory.push(trimmed);
    if (this.inputHistory.length > INPUT_HISTORY_LIMIT) {
      this.inputHistory.splice(0, this.inputHistory.length - INPUT_HISTORY_LIMIT);
    }
    this.historyIndex = -1;
    this.historyDraft = '';
    try { localStorage.setItem(INPUT_HISTORY_KEY, JSON.stringify(this.inputHistory)); } catch {}
  }
}
