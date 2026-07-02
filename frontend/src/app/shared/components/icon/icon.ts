import { Component, Input, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';

/**
 * Icon set. Paths are Lucide-style — monoline, 24×24 viewBox, stroke-based,
 * MIT-licensed (Lucide, github.com/lucide-icons/lucide). Adding a new icon
 * means dropping its `<path>` (or `<circle>` etc.) markup into the registry
 * below — no external font or SVG file loading required.
 *
 * Usage:
 *   <app-icon name="check" />
 *   <app-icon name="trash" size="16" />
 *   <app-icon name="chevron-right" class="my-class" />
 *
 * `size` is applied to both width/height in px. Stroke color is inherited
 * via `currentColor`, so styling with CSS `color:` "just works".
 */

interface IconDef {
  /** Inner SVG markup — paths, circles, lines. viewBox is fixed 24×24. */
  body: string;
  /** Optional stroke-linecap/linejoin overrides (defaults: round/round). */
  linecap?: 'round' | 'butt' | 'square';
  linejoin?: 'round' | 'miter' | 'bevel';
}

const ICONS: Record<string, IconDef> = {
  // --- Navigation / directional ---
  'arrow-left':  { body: `<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>` },
  'arrow-right': { body: `<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>` },
  'arrow-up':    { body: `<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>` },
  'arrow-down':  { body: `<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>` },
  'chevron-left':  { body: `<path d="m15 18-6-6 6-6"/>` },
  'chevron-right': { body: `<path d="m9 18 6-6-6-6"/>` },
  'chevron-down':  { body: `<path d="m6 9 6 6 6-6"/>` },
  'chevron-up':    { body: `<path d="m18 15-6-6-6 6"/>` },
  'corner-down-left': { body: `<path d="m9 10-5 5 5 5"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/>` },

  // --- Status / feedback ---
  'check':          { body: `<path d="M20 6 9 17l-5-5"/>` },
  'x':              { body: `<path d="M18 6 6 18"/><path d="m6 6 12 12"/>` },
  'alert-triangle': { body: `<path d="m21.73 18-8-14a2 2 0 0 0-3.46 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3z"/><path d="M12 9v4"/><path d="M12 17h.01"/>` },
  'info':           { body: `<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>` },
  'circle-dot':     { body: `<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/>` },
  'loader':         { body: `<path d="M12 2v4"/><path d="m16.24 7.76 2.83-2.83"/><path d="M18 12h4"/><path d="m16.24 16.24 2.83 2.83"/><path d="M12 18v4"/><path d="m4.93 19.07 2.83-2.83"/><path d="M2 12h4"/><path d="m4.93 4.93 2.83 2.83"/>` },

  // --- Actions ---
  'plus':      { body: `<path d="M5 12h14"/><path d="M12 5v14"/>` },
  'minus':     { body: `<path d="M5 12h14"/>` },
  'trash':     { body: `<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="m19 6-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>` },
  'copy':      { body: `<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>` },
  'clipboard': { body: `<rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>` },
  'download':  { body: `<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>` },
  'upload':    { body: `<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>` },
  'refresh':   { body: `<path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/>` },
  'play':      { body: `<polygon points="6 3 20 12 6 21 6 3"/>` },
  'stop':      { body: `<rect width="12" height="12" x="6" y="6" rx="1.5"/>` },
  'send':      { body: `<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>` },

  // --- Content types ---
  'file-text':      { body: `<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/>` },
  'file-code':      { body: `<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="m9 18 3-3-3-3"/><path d="m15 12 3 3-3 3"/>` },
  'book-open':      { body: `<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>` },
  'notebook':       { body: `<path d="M2 6h4"/><path d="M2 10h4"/><path d="M2 14h4"/><path d="M2 18h4"/><rect width="16" height="20" x="4" y="2" rx="2"/><path d="M16 2v20"/>` },
  'image':          { body: `<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>` },
  'table':          { body: `<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/><path d="M9 3v18"/><path d="M15 3v18"/>` },

  // --- Structural ---
  'settings':   { body: `<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/><circle cx="12" cy="12" r="3"/>` },
  'sliders':    { body: `<line x1="4" x2="4" y1="21" y2="14"/><line x1="4" x2="4" y1="10" y2="3"/><line x1="12" x2="12" y1="21" y2="12"/><line x1="12" x2="12" y1="8" y2="3"/><line x1="20" x2="20" y1="21" y2="16"/><line x1="20" x2="20" y1="12" y2="3"/><line x1="2" x2="6" y1="14" y2="14"/><line x1="10" x2="14" y1="8" y2="8"/><line x1="18" x2="22" y1="16" y2="16"/>` },
  'search':     { body: `<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>` },
  'menu':       { body: `<path d="M4 12h16"/><path d="M4 6h16"/><path d="M4 18h16"/>` },
  'list':       { body: `<line x1="8" x2="21" y1="6" y2="6"/><line x1="8" x2="21" y1="12" y2="12"/><line x1="8" x2="21" y1="18" y2="18"/><line x1="3" x2="3.01" y1="6" y2="6"/><line x1="3" x2="3.01" y1="12" y2="12"/><line x1="3" x2="3.01" y1="18" y2="18"/>` },
  'keyboard':   { body: `<rect width="20" height="16" x="2" y="4" rx="2"/><path d="M6 8h.01"/><path d="M10 8h.01"/><path d="M14 8h.01"/><path d="M18 8h.01"/><path d="M8 12h.01"/><path d="M12 12h.01"/><path d="M16 12h.01"/><path d="M7 16h10"/>` },

  // --- Domain (bio/science) ---
  'microscope': { body: `<path d="M6 18h8"/><path d="M3 22h18"/><path d="M14 22a7 7 0 1 0 0-14h-1"/><path d="M9 14h2"/><path d="M9 12a2 2 0 0 1-2-2V6h6v4a2 2 0 0 1-2 2Z"/><path d="M12 6V3a1 1 0 0 0-1-1H9a1 1 0 0 0-1 1v3"/>` },
  'flask':      { body: `<path d="M10 2v7.31"/><path d="M14 9.3V1.99"/><path d="M8.5 2h7"/><path d="M14 9.3a6.5 6.5 0 1 1-4 0"/>` },
  'dna':        { body: `<path d="m10 16 1.5 1.5"/><path d="m14 8-1.5-1.5"/><path d="M15 2c-1.798 1.998-2.518 3.995-2.807 5.993"/><path d="m16.5 10.5 1 1"/><path d="m17 6-2.891-2.891"/><path d="M2 15c6.667-6 13.333 0 20-6"/><path d="m20 9 .891.891"/><path d="M3.109 14.109 4 15"/><path d="m6.5 12.5 1 1"/><path d="m7 18 2.891 2.891"/><path d="M9 22c1.798-1.998 2.518-3.995 2.807-5.993"/>` },

  // --- Misc actions ---
  'copy-plus':  { body: `<line x1="15" x2="15" y1="12" y2="18"/><line x1="12" x2="18" y1="15" y2="15"/><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>` },
  'link':       { body: `<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>` },
  'external-link': { body: `<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" x2="21" y1="14" y2="3"/>` },
  'clock':      { body: `<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>` },
  'zap':        { body: `<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>` },
  'wifi-off':   { body: `<path d="M12 20h.01"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/><path d="M5 12.859a10 10 0 0 1 5.17-2.69"/><path d="M19 12.859a10 10 0 0 0-2.007-1.523"/><path d="M2 8.82a15 15 0 0 1 4.177-2.643"/><path d="M22 8.82a15 15 0 0 0-11.288-3.764"/><path d="M2 2 22 22"/>` },
  'bell':       { body: `<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>` },
  'moon':       { body: `<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>` },
  'sun':        { body: `<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>` },

  // --- Command key glyph (used in shortcut help) ---
  'command':    { body: `<path d="M15 6v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3"/>` },
  'return-key': { body: `<polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/>` },
};

@Component({
  selector: 'app-icon',
  standalone: true,
  imports: [CommonModule],
  template: `
    @if (safePath()) {
      <svg
        xmlns="http://www.w3.org/2000/svg"
        [attr.width]="size"
        [attr.height]="size"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        [attr.stroke-width]="strokeWidth"
        [attr.stroke-linecap]="def()?.linecap ?? 'round'"
        [attr.stroke-linejoin]="def()?.linejoin ?? 'round'"
        [attr.aria-label]="ariaLabel || null"
        [attr.aria-hidden]="ariaLabel ? null : 'true'"
        role="img"
        [innerHTML]="safePath()"
      ></svg>
    }
  `,
  styles: [`
    :host {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      color: currentColor;
      line-height: 0;
      vertical-align: middle;
    }
    svg { display: block; }
  `],
})
export class IconComponent {
  @Input() name = '';
  @Input() size: number | string = 16;
  @Input() strokeWidth: number | string = 2;
  @Input() ariaLabel = '';

  private sanitizer = inject(DomSanitizer);
  private _name = signal(this.name);

  def = computed<IconDef | null>(() => ICONS[this._name()] ?? null);
  safePath = computed<SafeHtml | null>(() => {
    const body = this.def()?.body;
    // Registry content is compile-time — trust it, so Angular doesn't strip
    // <path>/<circle>/etc. from the injected SVG innerHTML.
    return body ? this.sanitizer.bypassSecurityTrustHtml(body) : null;
  });

  ngOnChanges(): void {
    this._name.set(this.name);
  }
}

export const AVAILABLE_ICONS = Object.keys(ICONS);
