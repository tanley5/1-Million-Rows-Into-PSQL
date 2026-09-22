# n8n workflows

## Files

| File | Role |
|------|------|
| `upload-preview.skeleton.json` | Sticky-note layout guide (reference) |
| `approve-deny.skeleton.json` | Sticky-note layout guide (reference) |
| `upload-preview.draft.json` | Spec-aligned upload → preview workflow |
| `approve-deny.draft.json` | Spec-aligned status → approve/deny/retry workflow |

Both drafts are **inactive**. Import into n8n, set env access, then activate.

## Import

With Compose up (`http://localhost:5678`):

1. **Import from File** → `upload-preview.draft.json`
2. **Import from File** → `approve-deny.draft.json`
3. Confirm workflow settings can read env (`CSV_PIPELINE_API_URL=http://api:8000` is set on the n8n service; `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`).
4. Activate both workflows.
5. Open:
   - Upload: `http://localhost:5678/form/csv-upload`
   - Decide: `http://localhost:5678/form/csv-decide?upload_id=<id>`

## Upload workflow

`Upload Form` → `POST /uploads` (multipart field `file`) → completion page with:

- `upload_id`, `section_count`, `section_max_rows`
- five cleaning stats (`rows_in`, `rows_out`, `rows_error`, `normalized_per_column`, `blank_per_column`)
- `columns` + `sample`
- link to the decide form with `upload_id` query param

## Approve / Deny workflow

`Decision Form` fields:

- `upload_id` (text, required; prefills from `?upload_id=`)
- `action` (`Approve All` / `Deny` / `Retry Section`)
- `section_id` (number; used for Retry Section)

Flow:

1. `GET /uploads/{id}/status`
2. Merge form fields with status payload
3. Switch on `action`
4. `POST .../approve` | `POST .../deny` | `POST .../sections/{section_id}/approve`
5. Completion page shows the API JSON (`approved` / `partial` / `denied`, summaries, errors)

## API reminder

- Approve **200** = all sections done, staging dropped
- Approve **207** = partial; staging kept — retry failed section IDs
- **409** = another approve in progress
- **404** = unknown upload
