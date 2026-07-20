import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, Output } from '@angular/core';
import { Dataset } from '../../../core/models/session.model';
import { TooltipDirective } from '../../../shared/directives/tooltip.directive';

@Component({
  selector: 'app-dataset-step',
  standalone: true,
  imports: [CommonModule, TooltipDirective],
  templateUrl: './dataset-step.component.html',
  styleUrls: ['./dataset-step.component.scss'],
})
export class DatasetStepComponent {
  @Input() datasets: Dataset[] = [];
  @Input() selectedDataset: Dataset | null = null;
  @Output() selectedDatasetChange = new EventEmitter<Dataset>();

  formatSize(bytes: number): string {
    if (bytes >= 1e9) return (bytes / 1e9).toFixed(1) + ' GB';
    if (bytes >= 1e6) return (bytes / 1e6).toFixed(1) + ' MB';
    return (bytes / 1e3).toFixed(0) + ' KB';
  }
}
