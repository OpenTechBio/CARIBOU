import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SessionStatus } from '../../../core/models/session.model';

@Component({
  selector: 'app-status-indicator',
  standalone: true,
  imports: [CommonModule],
  template: `<span class="status-chip" [class]="'status-' + status">{{ status }}</span>`,
  styles: [`
    .status-chip {
      font-size: 0.65rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      padding: 0.15rem 0.45rem;
      border-radius: 4px;
      &.status-initializing { background: #EAF0F8; color: #31446B; border: 1px solid #C0CEDE; }
      &.status-idle         { background: #EEF6F0; color: #2A5C3A; border: 1px solid #B8DCC4; }
      &.status-running      { background: #FDF6D0; color: #5A4800; border: 1px solid #E0CC80; animation: pulse 1.5s ease-in-out infinite; }
      &.status-stopped      { background: #F2F0EA; color: #6B6555; border: 1px solid #D4CEB8; }
      &.status-error        { background: #FDF0ED; color: #A03828; border: 1px solid #E8C4BC; }
    }
    @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:0.5 } }
  `]
})
export class StatusIndicatorComponent {
  @Input() status: SessionStatus | string = 'stopped';
}
