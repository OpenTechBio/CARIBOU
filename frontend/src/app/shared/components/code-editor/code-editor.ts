import {
  Component, ElementRef, EventEmitter, Input, Output, ViewChild,
  AfterViewInit, OnChanges, SimpleChanges, inject, HostListener
} from '@angular/core';
import { CommonModule } from '@angular/common';
import hljs from 'highlight.js/lib/core';
import python from 'highlight.js/lib/languages/python';
import json from 'highlight.js/lib/languages/json';
import { ToastService } from '../../../core/services/toast.service';

hljs.registerLanguage('python', python);
hljs.registerLanguage('json', json);

@Component({
  selector: 'app-code-editor',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="code-editor" [class.readonly]="readonly">
      <div class="editor-toolbar">
        <span class="editor-language">{{ language }}</span>
        <span class="editor-hint">Tab / Shift+Tab to indent · Cmd/Ctrl+A to select all</span>
        @if (showCopy) {
          <button type="button" class="editor-copy" (click)="copy()" aria-label="Copy code to clipboard">
            Copy
          </button>
        }
      </div>
      <div class="editor-body">
        <pre class="editor-highlight" aria-hidden="true"><code #highlight [class]="'language-' + language"></code></pre>
        <textarea
          #textarea
          class="editor-textarea"
          [value]="value"
          [attr.aria-label]="ariaLabel || 'Code editor'"
          [attr.spellcheck]="false"
          [readonly]="readonly"
          (input)="onInput($event)"
          (keydown)="onKeyDown($event)"
          (scroll)="onScroll()"
        ></textarea>
      </div>
    </div>
  `,
  styles: [`
    :host { display: block; height: 100%; }
    .code-editor {
      display: flex;
      flex-direction: column;
      height: 100%;
      border: 1px solid var(--border, #D4CEB8);
      border-radius: 8px;
      background: var(--surface, #F7F4EB);
      overflow: hidden;
    }
    .code-editor.readonly { background: var(--surface-2, #EEE9D8); }
    .editor-toolbar {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.35rem 0.6rem;
      border-bottom: 1px solid var(--border, #D4CEB8);
      background: var(--surface-2, #EEE9D8);
      font-size: 0.7rem;
      color: var(--text-muted, #6B6555);
    }
    .editor-language {
      font-family: monospace;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--primary, #31446B);
    }
    .editor-hint { flex: 1; }
    .editor-copy {
      background: transparent;
      border: 1px solid var(--border, #D4CEB8);
      border-radius: 4px;
      padding: 0.15rem 0.5rem;
      font-size: 0.72rem;
      color: var(--text-muted, #6B6555);
      cursor: pointer;
    }
    .editor-copy:hover { color: var(--text, #1A1710); background: var(--bg, #FFF); }
    .editor-copy:focus-visible { outline: 2px solid var(--primary, #31446B); outline-offset: 2px; }

    .editor-body {
      position: relative;
      flex: 1;
      overflow: hidden;
    }
    .editor-highlight, .editor-textarea {
      margin: 0;
      padding: 0.75rem;
      font-family: 'SF Mono', ui-monospace, Menlo, Consolas, monospace;
      font-size: 0.82rem;
      line-height: 1.55;
      tab-size: 4;
      white-space: pre;
      overflow: auto;
      box-sizing: border-box;
      position: absolute;
      inset: 0;
    }
    .editor-highlight {
      pointer-events: none;
      z-index: 1;
      color: transparent;
      code { background: transparent !important; padding: 0 !important; font-family: inherit; }
    }
    .editor-textarea {
      z-index: 2;
      background: transparent;
      color: transparent;
      caret-color: var(--text, #1A1710);
      border: none;
      outline: none;
      resize: none;
      -webkit-text-fill-color: transparent;
      &::selection { background: rgba(49,68,107,0.25); -webkit-text-fill-color: var(--text, #1A1710); }
      &:focus-visible { outline: none; }
    }

    /* If highlight.js fails / no highlighting yet, fall back to visible textarea */
    .editor-highlight code:empty ~ * { color: var(--text, #1A1710); }
    .editor-textarea:focus + .editor-highlight { }

    /* Highlight.js "github" tokens tuned to the light palette */
    :host ::ng-deep .hljs-keyword,
    :host ::ng-deep .hljs-selector-tag { color: #A03828; font-weight: 600; }
    :host ::ng-deep .hljs-string,
    :host ::ng-deep .hljs-attr        { color: #2A5C3A; }
    :host ::ng-deep .hljs-number,
    :host ::ng-deep .hljs-literal     { color: #BB6600; }
    :host ::ng-deep .hljs-comment,
    :host ::ng-deep .hljs-quote       { color: #6B6555; font-style: italic; }
    :host ::ng-deep .hljs-built_in,
    :host ::ng-deep .hljs-title       { color: #31446B; font-weight: 600; }
    :host ::ng-deep .hljs-params      { color: #1A1710; }
    :host ::ng-deep .hljs-punctuation { color: #1A1710; }
    :host ::ng-deep code              { color: #1A1710; }
  `],
})
export class CodeEditorComponent implements AfterViewInit, OnChanges {
  @Input() value = '';
  @Input() language: 'python' | 'json' = 'python';
  @Input() readonly = false;
  @Input() showCopy = true;
  @Input() ariaLabel = '';
  @Output() valueChange = new EventEmitter<string>();

  @ViewChild('textarea', { static: true }) textarea!: ElementRef<HTMLTextAreaElement>;
  @ViewChild('highlight', { static: true }) highlight!: ElementRef<HTMLElement>;

  private toasts = inject(ToastService);

  ngAfterViewInit(): void {
    this.render();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['value'] || changes['language']) this.render();
  }

  onInput(event: Event): void {
    const t = event.target as HTMLTextAreaElement;
    this.value = t.value;
    this.valueChange.emit(this.value);
    this.render();
  }

  onScroll(): void {
    if (!this.textarea || !this.highlight) return;
    const ta = this.textarea.nativeElement;
    const pre = this.highlight.nativeElement.parentElement;
    if (pre) {
      pre.scrollTop = ta.scrollTop;
      pre.scrollLeft = ta.scrollLeft;
    }
  }

  onKeyDown(event: KeyboardEvent): void {
    if (event.key !== 'Tab') return;
    event.preventDefault();
    if (this.readonly) return;

    const ta = event.target as HTMLTextAreaElement;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    const value = ta.value;
    const indent = '    ';

    if (start === end) {
      if (event.shiftKey) {
        // Shift+Tab on single line: dedent up to 4 spaces before caret
        const lineStart = value.lastIndexOf('\n', start - 1) + 1;
        const stripped = value.slice(lineStart, start).replace(/^ {1,4}/, '');
        const removed = (start - lineStart) - stripped.length;
        const newValue = value.slice(0, lineStart) + stripped + value.slice(start);
        this.applyChange(newValue, Math.max(lineStart, start - removed));
      } else {
        const newValue = value.slice(0, start) + indent + value.slice(end);
        this.applyChange(newValue, start + indent.length);
      }
      return;
    }

    // Multi-line selection: indent/dedent every affected line.
    const lineStart = value.lastIndexOf('\n', start - 1) + 1;
    const selection = value.slice(lineStart, end);
    const lines = selection.split('\n');
    let modifiedLen = 0;
    const modified = lines.map(line => {
      if (event.shiftKey) {
        const stripped = line.replace(/^ {1,4}/, '');
        modifiedLen += stripped.length - line.length;
        return stripped;
      }
      modifiedLen += indent.length;
      return indent + line;
    }).join('\n');
    const newValue = value.slice(0, lineStart) + modified + value.slice(end);
    this.applyChange(newValue, start, end + modifiedLen);
  }

  copy(): void {
    try {
      navigator.clipboard.writeText(this.value);
      this.toasts.show({ kind: 'success', title: 'Copied to clipboard', ttlMs: 2000 });
    } catch {
      this.toasts.show({ kind: 'error', title: 'Copy failed', ttlMs: 2000 });
    }
  }

  private applyChange(next: string, caret: number, caretEnd?: number): void {
    this.value = next;
    this.valueChange.emit(next);
    this.render();
    requestAnimationFrame(() => {
      const ta = this.textarea.nativeElement;
      ta.value = next;
      ta.selectionStart = caret;
      ta.selectionEnd = caretEnd ?? caret;
    });
  }

  private render(): void {
    if (!this.highlight) return;
    const code = this.highlight.nativeElement;
    try {
      // append trailing newline so the last line stays visible under the caret
      const result = hljs.highlight(this.value + '\n', { language: this.language, ignoreIllegals: true });
      code.innerHTML = result.value;
      code.className = `language-${this.language} hljs`;
    } catch {
      code.textContent = this.value;
    }
  }
}
