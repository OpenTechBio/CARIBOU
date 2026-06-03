import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Artifact } from '../../../core/models/session.model';

@Component({
  selector: 'app-artifact-card',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './artifact-card.html',
  styleUrl: './artifact-card.scss',
})
export class ArtifactCardComponent {
  @Input() artifact!: Artifact;

  get isPlot(): boolean {
    return this.artifact.type === 'plot';
  }

  get downloadUrl(): string {
    const base = document.baseURI.replace(/\/$/, '');
    return `${base}/api/sessions/${this.artifact.session_id}/artifacts/${this.artifact.id}/download`;
  }

  formatSize(bytes: number): string {
    if (bytes > 1e6) return (bytes / 1e6).toFixed(1) + ' MB';
    return (bytes / 1e3).toFixed(0) + ' KB';
  }
}
