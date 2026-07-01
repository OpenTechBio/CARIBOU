import {
  Directive, ElementRef, HostListener, Input, OnChanges, OnDestroy, OnInit, inject
} from '@angular/core';

type TooltipPosition = 'top' | 'bottom' | 'left' | 'right';

/**
 * Instant tooltips for any element with a `title=` (or explicit `appTooltip=`)
 * attribute. Native browser tooltips are gated by an OS-level ~500ms hover
 * delay that CSS can't override — this directive steals the `title` attribute
 * on init, then renders a positioned tooltip element on `mouseenter`/`focusin`
 * with zero delay.
 *
 * We keep the tooltip in `document.body` so it never gets clipped by an
 * ancestor `overflow: hidden` (session cards, code blocks, chat panels).
 */
@Directive({
  selector: '[appTooltip], [tooltip], [title]',
  standalone: true,
})
export class TooltipDirective implements OnInit, OnChanges, OnDestroy {
  @Input('appTooltip') tooltipInput = '';
  @Input('tooltipPosition') position: TooltipPosition = 'top';

  private el = inject(ElementRef<HTMLElement>);
  private tipEl: HTMLElement | null = null;
  private text = '';

  ngOnInit(): void {
    // Prefer explicit [appTooltip]; fall back to native title. Either way we
    // strip the DOM `title` attribute so the browser's slow tooltip never fires.
    const nativeTitle = this.el.nativeElement.getAttribute('title') ?? '';
    this.text = this.tooltipInput || nativeTitle;
    if (nativeTitle) this.el.nativeElement.removeAttribute('title');
  }

  ngOnDestroy(): void {
    this.hide();
  }

  @HostListener('mouseenter') onEnter(): void { this.show(); }
  @HostListener('mouseleave') onLeave(): void { this.hide(); }
  @HostListener('focusin')    onFocus(): void { this.show(); }
  @HostListener('focusout')   onBlur():  void { this.hide(); }
  // Hide on click so the tooltip doesn't linger after activation.
  @HostListener('mousedown')  onDown():  void { this.hide(); }

  // Re-read tooltip text when the input changes (dynamic bindings).
  ngOnChanges(): void {
    if (this.tooltipInput) this.text = this.tooltipInput;
    if (this.tipEl) this.tipEl.textContent = this.text;
  }

  private show(): void {
    if (!this.text || this.tipEl) return;
    const tip = document.createElement('div');
    tip.className = `app-tooltip pos-${this.position}`;
    tip.setAttribute('role', 'tooltip');
    tip.textContent = this.text;
    document.body.appendChild(tip);
    this.tipEl = tip;
    this.reposition();
  }

  private hide(): void {
    if (this.tipEl) {
      this.tipEl.remove();
      this.tipEl = null;
    }
  }

  private reposition(): void {
    if (!this.tipEl) return;
    const anchor = this.el.nativeElement.getBoundingClientRect();
    const tip = this.tipEl;
    const gap = 8;
    // Force layout to measure the tooltip after appending.
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let top = 0;
    let left = 0;
    switch (this.position) {
      case 'bottom':
        top = anchor.bottom + gap;
        left = anchor.left + anchor.width / 2 - tw / 2;
        break;
      case 'left':
        top = anchor.top + anchor.height / 2 - th / 2;
        left = anchor.left - tw - gap;
        break;
      case 'right':
        top = anchor.top + anchor.height / 2 - th / 2;
        left = anchor.right + gap;
        break;
      case 'top':
      default:
        top = anchor.top - th - gap;
        left = anchor.left + anchor.width / 2 - tw / 2;
        // If the top would be off-screen, flip to bottom.
        if (top < 4) {
          top = anchor.bottom + gap;
          tip.classList.remove('pos-top');
          tip.classList.add('pos-bottom');
        }
        break;
    }
    // Clamp horizontally so long text stays visible.
    left = Math.max(4, Math.min(left, window.innerWidth - tw - 4));
    top = Math.max(4, Math.min(top, window.innerHeight - th - 4));
    tip.style.top = `${top}px`;
    tip.style.left = `${left}px`;
  }
}
