import { Component, OnInit, inject } from '@angular/core';
import { Router, RouterOutlet } from '@angular/router';
import { ToastHostComponent } from './shared/components/toast-host/toast-host';
import { requestedSessionId } from './core/utils/app-navigation';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, ToastHostComponent],
  template: `
    <router-outlet />
    <app-toast-host />
  `,
  styles: [':host { display: block; height: 100%; }']
})
export class App implements OnInit {
  private router = inject(Router);

  ngOnInit(): void {
    const sessionId = requestedSessionId();
    if (sessionId) {
      void this.router.navigate(['/session', sessionId], { replaceUrl: true });
    }
  }
}
