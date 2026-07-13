import { Pipe, PipeTransform, inject, SecurityContext } from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { marked, Renderer } from 'marked';
import hljs from 'highlight.js';

// Configure marked once at module load
const renderer = new Renderer();

// Code blocks — delegate to highlight.js, fall back gracefully
renderer.code = ({ text, lang }: { text: string; lang?: string }): string => {
  const language = lang && hljs.getLanguage(lang) ? lang : 'plaintext';
  const highlighted = hljs.highlight(text, { language }).value;
  const label = lang || '';
  return `<div class="md-code-block">
    ${label ? `<div class="md-code-lang">${label}</div>` : ''}
    <pre><code class="hljs language-${language}">${highlighted}</code></pre>
  </div>`;
};

// Inline code
renderer.codespan = ({ text }: { text: string }): string =>
  `<code class="md-inline-code">${text}</code>`;

marked.setOptions({ renderer, gfm: true, breaks: true });


@Pipe({ name: 'markdown', standalone: true, pure: true })
export class MarkdownPipe implements PipeTransform {
  private sanitizer = inject(DomSanitizer);

  transform(value: string | undefined | null): SafeHtml {
    if (!value) return '';
    const html = marked.parse(value) as string;
    // Sanitize to strip any script/event-handler injection while keeping code
    return this.sanitizer.sanitize(SecurityContext.HTML, html) ?? '';
  }
}
