import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, Output } from '@angular/core';
import { PresetSummary } from '../../../core/models/experiment-control.model';
import { TooltipDirective } from '../../../shared/directives/tooltip.directive';

@Component({
  selector: 'app-preset-step',
  standalone: true,
  imports: [CommonModule, TooltipDirective],
  templateUrl: './preset-step.component.html',
  styleUrls: ['./preset-step.component.scss'],
})
export class PresetStepComponent {
  @Input() presets: PresetSummary[] = [];
  @Input() selectedPreset: string | null = null;
  @Output() selectedPresetChange = new EventEmitter<string>();
}
