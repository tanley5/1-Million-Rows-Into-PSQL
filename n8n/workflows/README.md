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

The same files are mounted read-only at `/workflows` inside the n8n container
for CLI-based imports.

## API target

The n8n container receives:

```text
CSV_PIPELINE_API_URL=http://api:8000
```

Use `{{$env.CSV_PIPELINE_API_URL}}` in HTTP Request node URLs. Do not use
`localhost:8000` from n8n; inside its container, `localhost` points back to
n8n rather than FastAPI.

## Build boundary

The skeleton phase stops before adding or activating Form nodes. The next
`n8n-forms` phase will turn these guides into:

1. Upload CSV → call `POST /uploads` → display sample/stats.
2. Approve/Deny form → call the selected upload endpoint → display status.
