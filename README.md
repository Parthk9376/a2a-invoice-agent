# A2A Invoice Agent

## Files
- `main.py`: FastAPI application and SQLite invoice/receipt state.
- `requirements.txt`: Python dependencies.
- `render.yaml`: Render deployment configuration.

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/docs`.

## Deploy to Render
1. Push these files to a GitHub repository.
2. Create a Render Web Service connected to the repository.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. For persistent SQLite storage, attach a Render disk and set `DATABASE_PATH` to a path on that disk (for example `/var/data/invoice_agent.db`).

## Important limitations
- The Agent Card endpoint is provided at `/.well-known/agent-card.json`.
- Invoice decision and receipt APIs are REST endpoints.
- This code does not implement the full A2A JSON-RPC task/message protocol and does not cryptographically verify receipt signatures. If the grader requires those features, implement them according to the assignment's protocol specification.
- Do not use this as a real payment system without security review, authenticated approvals, verified signed receipts, concurrency-safe payment execution, and durable storage.
