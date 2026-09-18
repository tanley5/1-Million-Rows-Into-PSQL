# n8n workflow skeletons

These JSON files are layout guides, not executable workflows. They contain
Sticky Notes showing where to create the Form Trigger, HTTP Request, Switch,
and Form Ending nodes.

## Import the skeletons

After the Compose stack is healthy and initial n8n owner setup is complete:

1. Open `http://localhost:5678`.
2. Choose **Import from File**.
3. Import `upload-preview.skeleton.json`.
4. Import `approve-deny.skeleton.json`.

The same files are mounted read-only at `/workflows` inside the n8n container.

## API target

```text
CSV_PIPELINE_API_URL=http://api:8000
```

Use `{{$env.CSV_PIPELINE_API_URL}}` in HTTP Request URLs. Do not use
`localhost:8000` from inside the n8n container.

## Sectioned approve (current API)

- `POST /uploads` — returns `section_count` (≤100k rows per section).
- `POST /uploads/{id}/approve` — continues past section failures; **200** all done, **207** partial.
- `GET /uploads/{id}/status` — per-section completed/failed/pending.
- `POST /uploads/{id}/sections/{section_id}/approve` — retry one section.
- `POST /uploads/{id}/deny` — drop staging + progress.

## Build boundary

This phase stops before adding or activating Form nodes. The `n8n-forms`
phase turns these guides into working forms.
