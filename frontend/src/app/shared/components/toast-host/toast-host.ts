import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ToastService } from '../../../core/services/toast.service';

@Component({
  selector: 'app-toast-host',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="toast-host" aria-live="polite" aria-atomic="false">
      @for (t of toasts.toasts(); track t.id) {
        <div class="toast" [class]="'toast-' + t.kind" role="status">
          <div class="toast-body">
            <div class="toast-title">{{ t.title }}</div>
            @if (t.detail) { <div class="toast-detail">{{ t.detail }}</div> }
          </div>
          @if (t.action) {
            <button class="toast-action" type="button" (click)="run(t)">{{ t.action.label }}</button>
          }
          <button class="toast-close" type="button" aria-label="Dismiss notification"
            (click)="toasts.dismiss(t.id)">✕</button>
        </div>
      }
    </div>
  `,
  styles: [`
    .toast-host {
      position: fixed;
      bottom: 1rem;
      right: 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      z-index: 9000;
      max-width: min(420px, calc(100vw - 2rem));
    }
    .toast {
      display: flex;
      gap: 0.75rem;
      align-items: flex-start;
      padding: 0.7rem 0.9rem;
      background: var(--surface, #F7F4EB);
      border: 1px solid var(--border, #D4CEB8);
      border-left: 3px solid var(--primary, #31446B);
      border-radius: 8px;
      box-shadow: 0 6px 20px rgba(0,20,40,0.12);
      animation: toast-slide-in 0.18s ease-out;
    }
    .toast-success { border-left-color: var(--success, #2A5C3A); }
    .toast-warn    { border-left-color: var(--accent, #BBAE6A); }
    .toast-error   { border-left-color: var(--error, #A03828); }
    .toast-body    { flex: 1; min-width: 0; }
    .toast-title   { font-weight: 600; font-size: 0.85rem; color: var(--text, #1A1710); }
    .toast-detail  {
      font-size: 0.75rem;
      color: var(--text-muted, #6B6555);
      margin-top: 0.15rem;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .toast-action, .toast-close {
      background: transparent;
      border: 1px solid transparent;
      border-radius: 4px;
      cursor: pointer;
      font-size: 0.75rem;
      padding: 0.2rem 0.55rem;
    }
    .toast-action {
      color: var(--primary, #31446B);
      font-weight: 600;
      border-color: var(--border, #D4CEB8);
    }
    .toast-action:hover  { background: var(--primary-bg, #FDF6D0); }
    .toast-close         { color: var(--text-muted, #6B6555); font-size: 0.8rem; padding: 0.15rem 0.4rem; }
    .toast-close:hover   { color: var(--text, #1A1710); background: var(--surface-2, #EEE9D8); }
    .toast-action:focus-visible,
    .toast-close:focus-visible {
      outline: 2px solid var(--primary, #31446B);
      outline-offset: 2px;
    }
    @keyframes toast-slide-in {
      from { transform: translateX(20px); opacity: 0; }
      to   { transform: translateX(0);    opacity: 1; }
    }
  `],
})
export class ToastHostComponent {
  toasts = inject(ToastService);
  run(t: import('../../../core/services/toast.service').Toast): void {
    t.action?.run();
    this.toasts.dismiss(t.id);
  }
}
