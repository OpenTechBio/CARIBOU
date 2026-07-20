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
    path: 'experiments',
    loadComponent: () =>
      import('./pages/experiments/experiments').then(m => m.ExperimentsComponent),
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
  {
    path: 'code-samples',
    loadComponent: () =>
      import('./pages/code-samples/code-samples').then(m => m.CodeSamplesComponent),
  },
  {
    path: 'experiments/wizard',
    loadComponent: () =>
      import('./pages/experiments-wizard/wizard.component').then(m => m.WizardComponent),
  },
  { path: '**', redirectTo: '' },
];
