import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, Output } from '@angular/core';
import { FormsModule } from '@angular/forms';
import {
  DEEPSEEK_MODELS,
  PresetExecutor,
  PresetProfile,
  PresetProvider,
  PresetSummary,
} from '../../../core/models/experiment-control.model';
import { TooltipDirective } from '../../../shared/directives/tooltip.directive';

@Component({
  selector: 'app-resources-step',
  standalone: true,
  imports: [CommonModule, FormsModule, TooltipDirective],
  templateUrl: './resources-step.component.html',
  styleUrls: ['./resources-step.component.scss'],
})
export class ResourcesStepComponent {
  readonly deepseekModels = DEEPSEEK_MODELS;

  @Input() preset: PresetSummary | null = null;
  @Input() profile: PresetProfile = 'fast';
  @Input() maxTurns = 10;
  @Input() provider: PresetProvider = 'openai';
  @Input() modelName = 'gpt-4.1-2025-04-14';
  @Input() executor: PresetExecutor = 'slurm';
  @Input() owner = '';
  @Input() reviewer = '';

  @Output() profileChange = new EventEmitter<PresetProfile>();
  @Output() maxTurnsChange = new EventEmitter<number>();
  @Output() providerChange = new EventEmitter<PresetProvider>();
  @Output() modelNameChange = new EventEmitter<string>();
  @Output() executorChange = new EventEmitter<PresetExecutor>();
  @Output() ownerChange = new EventEmitter<string>();
  @Output() reviewerChange = new EventEmitter<string>();

  get cpuCores(): number {
    return this.preset?.resource_profiles[this.profile].cpu_cores ?? 0;
  }

  get memoryGB(): number {
    const bytes = this.preset?.resource_profiles[this.profile].memory_bytes ?? 0;
    return bytes / 1e9;
  }

  get wallHours(): number {
    const seconds = this.preset?.resource_profiles[this.profile].wall_seconds ?? 0;
    return seconds / 3600;
  }
}
