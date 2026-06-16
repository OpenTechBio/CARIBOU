import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    loadComponent: () =>
      import('./pages/dashboard/dashboard').then(m => m.DashboardComponent),
  },
  {
    path: 'session/:id',
    loadComponent: () =>
      import('./pages/session/session').then(m => m.SessionComponent),
  },
  {
    path: 'settings',
    loadComponent: () =>
      import('./pages/settings/settings').then(m => m.SettingsComponent),
  },
  {
    path: 'blueprints',
    loadComponent: () =>
      import('./pages/blueprint-editor/blueprint-editor').then(m => m.BlueprintEditorComponent),
  },
  {
    path: 'blueprints/:name',
    loadComponent: () =>
      import('./pages/blueprint-editor/blueprint-editor').then(m => m.BlueprintEditorComponent),
  },
  { path: '**', redirectTo: '' },
];
