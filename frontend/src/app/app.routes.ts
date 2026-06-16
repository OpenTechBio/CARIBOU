import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    loadComponent: () =>
      import('./features/dashboard/dashboard').then(m => m.DashboardComponent),
  },
  {
    path: 'session/:id',
    loadComponent: () =>
      import('./features/session/session').then(m => m.SessionComponent),
  },
  {
    path: 'settings',
    loadComponent: () =>
      import('./features/settings/settings').then(m => m.SettingsComponent),
  },
  {
    path: 'blueprints',
    loadComponent: () =>
      import('./features/blueprint-editor/blueprint-editor').then(m => m.BlueprintEditorComponent),
  },
  {
    path: 'blueprints/:name',
    loadComponent: () =>
      import('./features/blueprint-editor/blueprint-editor').then(m => m.BlueprintEditorComponent),
  },
  { path: '**', redirectTo: '' },
];
