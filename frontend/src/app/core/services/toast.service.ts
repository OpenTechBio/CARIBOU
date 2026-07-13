import { Injectable, signal } from '@angular/core';

export type ToastKind = 'info' | 'success' | 'warn' | 'error';

export interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  detail?: string;
  action?: { label: string; run: () => void };
  createdAt: number;
  ttlMs: number;
}

@Injectable({ providedIn: 'root' })
export class ToastService {
  private nextId = 1;
  readonly toasts = signal<Toast[]>([]);

  show(t: Omit<Toast, 'id' | 'createdAt'> & { ttlMs?: number }): number {
    const id = this.nextId++;
    const toast: Toast = {
      id,
      kind: t.kind,
      title: t.title,
      detail: t.detail,
      action: t.action,
      createdAt: Date.now(),
      ttlMs: t.ttlMs ?? 5000,
    };
    this.toasts.update(all => [...all, toast]);
    if (toast.ttlMs > 0) {
      setTimeout(() => this.dismiss(id), toast.ttlMs);
    }
    return id;
  }

  dismiss(id: number): void {
    this.toasts.update(all => all.filter(t => t.id !== id));
  }

  clearAll(): void {
    this.toasts.set([]);
  }
}
