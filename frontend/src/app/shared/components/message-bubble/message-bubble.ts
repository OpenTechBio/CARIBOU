import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Message } from '../../../core/models/session.model';
import { MarkdownPipe } from '../../pipes/markdown.pipe';
import { TooltipDirective } from '../../directives/tooltip.directive';

@Component({
  selector: 'app-message-bubble',
  standalone: true,
  imports: [CommonModule, MarkdownPipe, TooltipDirective],
  templateUrl: './message-bubble.html',
  styleUrl: './message-bubble.scss',
})
export class MessageBubbleComponent {
  @Input() message!: Message;
  @Input() streaming = false;
}
