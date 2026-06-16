import { Component, OnInit, inject, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { ConfigService } from '../../core/services/config.service';
import { AgentBlueprint } from '../../core/models/session.model';
import {
  AgentConfig, AgentEntry, BlueprintContent, CommandConfig,
  CommandEntry, SaveBlueprintRequest,
} from '../../core/models/blueprint.model';

@Component({
  selector: 'app-blueprint-editor',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './blueprint-editor.html',
  styleUrl: './blueprint-editor.scss',
})
export class BlueprintEditorComponent implements OnInit {
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private http = inject(HttpClient);
  configSvc = inject(ConfigService);

  // Sidebar
  allBlueprints = signal<AgentBlueprint[]>([]);

  // Editor state
  selectedName = signal<string | null>(null);
  isPackageDefault = signal(false);
  blueprintName = signal('');
  globalPolicy = signal('');
  agents = signal<AgentEntry[]>([]);

  // Tabs
  activeTab = signal<'form' | 'json'>('form');
  rawJson = signal('');
  jsonError = signal<string | null>(null);

  // Action state
  saving = signal(false);
  deleting = signal(false);
  saveResult = signal<{ ok: boolean; message: string } | null>(null);

  // Validation
  validationErrors = computed(() => this._validate(this.agents(), this.blueprintName()));

  packageBlueprints = computed(() => this.allBlueprints().filter(b => !this._isUserBlueprint(b.name)));
  userBlueprints = computed(() => this.allBlueprints().filter(b => this._isUserBlueprint(b.name)));

  ngOnInit(): void {
    this.configSvc.loadAll().subscribe(() => {
      this.allBlueprints.set(this.configSvc.blueprints());
    });

    const name = this.route.snapshot.paramMap.get('name');
    if (name) {
      this.loadBlueprint(name);
    }
  }

  loadBlueprint(name: string): void {
    this.saveResult.set(null);
    this.http.get<BlueprintContent>(`api/config/blueprints/${name}`).subscribe({
      next: (bp) => {
        this.selectedName.set(name);
        this.isPackageDefault.set(bp.is_package_default);
        this.blueprintName.set(bp.name);
        this.globalPolicy.set(bp.global_policy);
        this.agents.set(this._toAgentEntries(bp.agents));
        this.activeTab.set('form');
        this.router.navigate(['/blueprints', name], { replaceUrl: true });
      },
      error: () => {
        this.saveResult.set({ ok: false, message: `Failed to load blueprint '${name}'.` });
      },
    });
  }

  newBlueprint(): void {
    this.selectedName.set(null);
    this.isPackageDefault.set(false);
    this.blueprintName.set('');
    this.globalPolicy.set('');
    this.agents.set([]);
    this.activeTab.set('form');
    this.rawJson.set('');
    this.saveResult.set(null);
    this.router.navigate(['/blueprints'], { replaceUrl: true });
  }

  useAsTemplate(): void {
    this.selectedName.set(null);
    this.isPackageDefault.set(false);
    this.blueprintName.set('');
    this.saveResult.set(null);
    this.router.navigate(['/blueprints'], { replaceUrl: true });
  }

  switchTab(tab: 'form' | 'json'): void {
    if (tab === 'json' && this.activeTab() === 'form') {
      this.rawJson.set(this._serializeToJson());
      this.jsonError.set(null);
    } else if (tab === 'form' && this.activeTab() === 'json') {
      const err = this._applyJsonToForm(this.rawJson());
      if (err) {
        this.jsonError.set(err);
        return;
      }
      this.jsonError.set(null);
    }
    this.activeTab.set(tab);
  }

  save(): void {
    if (this.activeTab() === 'json') {
      const err = this._applyJsonToForm(this.rawJson());
      if (err) {
        this.jsonError.set(err);
        return;
      }
      this.jsonError.set(null);
    }

    const errors = this.validationErrors();
    if (errors.length) return;

    const req: SaveBlueprintRequest = {
      name: this.blueprintName(),
      global_policy: this.globalPolicy(),
      agents: this._toAgentConfigMap(this.agents()),
    };

    this.saving.set(true);
    this.saveResult.set(null);

    const isExistingUser = this.selectedName() !== null && !this.isPackageDefault();
    const req$ = isExistingUser
      ? this.http.put<BlueprintContent>(`api/config/blueprints/${this.selectedName()}`, req)
      : this.http.post<BlueprintContent>('api/config/blueprints', req);

    req$.subscribe({
      next: (bp) => {
        this.saving.set(false);
        this.selectedName.set(bp.name);
        this.isPackageDefault.set(false);
        this.saveResult.set({ ok: true, message: 'Blueprint saved.' });
        this._refreshSidebar();
        this.router.navigate(['/blueprints', bp.name], { replaceUrl: true });
      },
      error: (err) => {
        this.saving.set(false);
        this.saveResult.set({ ok: false, message: err?.error?.detail ?? 'Save failed.' });
      },
    });
  }

  deleteBlueprint(): void {
    const name = this.selectedName();
    if (!name) return;
    this.deleting.set(true);
    this.http.delete(`api/config/blueprints/${name}`).subscribe({
      next: () => {
        this.deleting.set(false);
        this._refreshSidebar();
        this.newBlueprint();
      },
      error: (err) => {
        this.deleting.set(false);
        this.saveResult.set({ ok: false, message: err?.error?.detail ?? 'Delete failed.' });
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Agent editing
  // ---------------------------------------------------------------------------

  addAgent(): void {
    this.agents.update(list => [...list, {
      key: '', prompt: '', ragEnabled: false, commands: [], codeSamples: [],
    }]);
  }

  removeAgent(index: number): void {
    this.agents.update(list => list.filter((_, i) => i !== index));
  }

  addCommand(agentIndex: number): void {
    this.agents.update(list => {
      const updated = [...list];
      updated[agentIndex] = {
        ...updated[agentIndex],
        commands: [...updated[agentIndex].commands, { key: '', target_agent: '', description: '' }],
      };
      return updated;
    });
  }

  removeCommand(agentIndex: number, cmdIndex: number): void {
    this.agents.update(list => {
      const updated = [...list];
      updated[agentIndex] = {
        ...updated[agentIndex],
        commands: updated[agentIndex].commands.filter((_, i) => i !== cmdIndex),
      };
      return updated;
    });
  }

  addCodeSample(agentIndex: number): void {
    this.agents.update(list => {
      const updated = [...list];
      updated[agentIndex] = {
        ...updated[agentIndex],
        codeSamples: [...updated[agentIndex].codeSamples, ''],
      };
      return updated;
    });
  }

  removeCodeSample(agentIndex: number, sampleIndex: number): void {
    this.agents.update(list => {
      const updated = [...list];
      updated[agentIndex] = {
        ...updated[agentIndex],
        codeSamples: updated[agentIndex].codeSamples.filter((_, i) => i !== sampleIndex),
      };
      return updated;
    });
  }

  updateAgentField<K extends keyof AgentEntry>(agentIndex: number, field: K, value: AgentEntry[K]): void {
    this.agents.update(list => {
      const updated = [...list];
      updated[agentIndex] = { ...updated[agentIndex], [field]: value };
      return updated;
    });
  }

  updateCommandField<K extends keyof CommandEntry>(agentIndex: number, cmdIndex: number, field: K, value: CommandEntry[K]): void {
    this.agents.update(list => {
      const updated = [...list];
      const cmds = [...updated[agentIndex].commands];
      cmds[cmdIndex] = { ...cmds[cmdIndex], [field]: value };
      updated[agentIndex] = { ...updated[agentIndex], commands: cmds };
      return updated;
    });
  }

  updateCodeSample(agentIndex: number, sampleIndex: number, value: string): void {
    this.agents.update(list => {
      const updated = [...list];
      const samples = [...updated[agentIndex].codeSamples];
      samples[sampleIndex] = value;
      updated[agentIndex] = { ...updated[agentIndex], codeSamples: samples };
      return updated;
    });
  }

  agentKeys = computed(() => this.agents().map(a => a.key));

  goBack(): void {
    this.router.navigate(['/']);
  }

  // ---------------------------------------------------------------------------
  // Private helpers
  // ---------------------------------------------------------------------------

  private _isUserBlueprint(name: string): boolean {
    const bp = this.allBlueprints().find(b => b.name === name);
    return bp ? !bp.is_package_default : false;
  }

  private _toAgentEntries(agents: Record<string, AgentConfig>): AgentEntry[] {
    return Object.entries(agents).map(([key, a]) => ({
      key,
      prompt: a.prompt,
      ragEnabled: a.rag_enabled,
      commands: Object.entries(a.neighbors).map(([cmdKey, cmd]) => ({
        key: cmdKey,
        target_agent: cmd.target_agent,
        description: cmd.description,
      })),
      codeSamples: a.code_samples ?? [],
    }));
  }

  private _toAgentConfigMap(entries: AgentEntry[]): Record<string, AgentConfig> {
    const map: Record<string, AgentConfig> = {};
    for (const entry of entries) {
      const neighbors: Record<string, CommandConfig> = {};
      for (const cmd of entry.commands) {
        neighbors[cmd.key] = { target_agent: cmd.target_agent, description: cmd.description };
      }
      map[entry.key] = {
        prompt: entry.prompt,
        rag_enabled: entry.ragEnabled,
        neighbors,
        code_samples: entry.codeSamples.filter(s => s.trim()),
      };
    }
    return map;
  }

  private _serializeToJson(): string {
    return JSON.stringify({
      name: this.blueprintName(),
      global_policy: this.globalPolicy(),
      agents: this._toAgentConfigMap(this.agents()),
    }, null, 2);
  }

  private _applyJsonToForm(raw: string): string | null {
    try {
      const parsed = JSON.parse(raw);
      if (parsed.name !== undefined) this.blueprintName.set(parsed.name);
      if (parsed.global_policy !== undefined) this.globalPolicy.set(parsed.global_policy);
      if (parsed.agents && typeof parsed.agents === 'object') {
        this.agents.set(this._toAgentEntries(parsed.agents));
      }
      return null;
    } catch (e) {
      return 'Invalid JSON: ' + (e as Error).message;
    }
  }

  private _validate(agents: AgentEntry[], name: string): string[] {
    const errors: string[] = [];
    if (!name.trim()) errors.push('Blueprint name is required.');
    if (name.includes('/') || name.includes('\\') || name.endsWith('.json')) {
      errors.push('Blueprint name must not contain path separators or .json extension.');
    }
    if (agents.length === 0) errors.push('At least one agent is required.');
    const keys = new Set<string>();
    for (const agent of agents) {
      if (!agent.key.trim()) { errors.push('All agent names must be non-empty.'); continue; }
      if (keys.has(agent.key)) errors.push(`Duplicate agent name: '${agent.key}'.`);
      keys.add(agent.key);
    }
    for (const agent of agents) {
      for (const cmd of agent.commands) {
        if (cmd.target_agent && !keys.has(cmd.target_agent)) {
          errors.push(`Agent '${agent.key}': command '${cmd.key}' references unknown agent '${cmd.target_agent}'.`);
        }
      }
    }
    return errors;
  }

  private _refreshSidebar(): void {
    this.configSvc.loadAll().subscribe(() => {
      this.allBlueprints.set(this.configSvc.blueprints());
    });
  }
}
