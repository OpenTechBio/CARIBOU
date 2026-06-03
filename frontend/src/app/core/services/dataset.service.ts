import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpEventType } from '@angular/common/http';
import { Observable, map, filter } from 'rxjs';
import { Dataset } from '../models/session.model';

@Injectable({ providedIn: 'root' })
export class DatasetService {
  private http = inject(HttpClient);

  getDatasets(): Observable<Dataset[]> {
    return this.http.get<Dataset[]>('api/datasets');
  }

  uploadDataset(file: File): Observable<{ progress: number; dataset?: Dataset }> {
    const form = new FormData();
    form.append('file', file, file.name);
    return this.http.post<Dataset>('api/datasets', form, {
      reportProgress: true,
      observe: 'events',
    }).pipe(
      map(event => {
        if (event.type === HttpEventType.UploadProgress) {
          const progress = event.total ? Math.round(100 * event.loaded / event.total) : 0;
          return { progress };
        }
        if (event.type === HttpEventType.Response) {
          return { progress: 100, dataset: event.body as Dataset };
        }
        return { progress: 0 };
      }),
      filter(e => e.progress > 0 || !!e.dataset)
    );
  }

  deleteDataset(filename: string): Observable<void> {
    return this.http.delete<void>(`api/datasets/${filename}`);
  }
}
