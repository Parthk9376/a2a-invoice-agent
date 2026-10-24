import os
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

APP_TITLE = "A2A Invoice Agent"
DATABASE_PATH = os.getenv("DATABASE_PATH", "invoice_agent.db")
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://a2a-invoice-agent-3ztr.onrender.com"
).rstrip("/")

app = FastAPI(title=APP_TITLE, version="1.0.0")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect():
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                invoice_id TEXT PRIMARY KEY,
                vendor TEXT NOT NULL,
                amount REAL NOT NULL,
                approval_status TEXT NOT NULL,
                approval_limit REAL NOT NULL,
                payment_status TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS receipts (
                receipt_id TEXT PRIMARY KEY,
                invoice_id TEXT NOT NULL UNIQUE,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY(invoice_id) REFERENCES invoices(invoice_id)
            )
        """)


@app.on_event("startup")
def startup():
    init_db()


class Invoice(BaseModel):
    invoice_id: str = Field(min_length=1)
    vendor: str = Field(min_length=1)
    amount: float = Field(gt=0)
    approval_status: str = Field(min_length=1)
    approval_limit: float = Field(ge=0)
    payment_status: str = "unpaid"
    evidence: List[str] = Field(default_factory=list)


class ReceiptRequest(BaseModel):
    invoice_id: str = Field(min_length=1)
    receipt_id: str = Field(min_length=1)


@app.get("/")
def home():
    return {
        "service": APP_TITLE,
        "status": "running",
        "agent_card": "/.well-known/agent-card.json",
        "docs": "/docs"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/.well-known/agent-card.json")
def agent_card():
    # Agent Card is public and does not require authentication.
    return JSONResponse(
        content={
            "protocolVersion": "0.3.0",
            "name": APP_TITLE,
            "description": (
                "Evaluates invoice evidence and approval limits, "
                "prevents repeated processing, and records receipts."
            ),
            "url": PUBLIC_BASE_URL + "/",
            "version": "1.0.0",
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "stateTransitionHistory": False
            },
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "invoice-decide",
                    "name": "Invoice Decision",
                    "description": (
                        "Evaluate an invoice using approval status, "
                        "approval limit, payment status, and evidence."
                    ),
                    "tags": ["invoice", "approval", "payment"],
                    "examples": [
                        "Evaluate an invoice before payment"
                    ]
                },
                {
                    "id": "invoice-receipt",
                    "name": "Record Receipt",
                    "description": (
                        "Record a receipt for an approved invoice once."
                    ),
                    "tags": ["receipt", "idempotency"]
                }
            ]
        },
        media_type="application/json"
    )


@app.post("/invoice/decide")
def decide_invoice(invoice: Invoice):
    invoice_id = invoice.invoice_id.strip()
    vendor = invoice.vendor.strip()
    evidence = [item.strip() for item in invoice.evidence if item.strip()]

    if not invoice_id or not vendor:
        raise HTTPException(status_code=422, detail="invoice_id and vendor are required")

    # Check stored state first: retries must never re-authorize a payment.
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()

        if existing:
            return {
                "invoice_id": invoice_id,
                "action": "no_action",
                "decision": existing["decision"],
                "reason": "Invoice was already processed; no action repeated.",
                "duplicate": True,
                "stored_decision": existing["decision"],
                "payment_status": existing["payment_status"]
            }

        paid_status = invoice.payment_status.strip().lower()
        approval = invoice.approval_status.strip().lower()

        if paid_status in {"paid", "completed", "settled"}:
            decision = "reject"
            action = "no_action"
            reason = "Invoice is already marked as paid."
        elif approval not in {"approved", "approved_by_authority", "authorized"}:
            decision = "hold"
            action = "request_approval"
            reason = "Approval status does not establish valid approval."
        elif invoice.amount > invoice.approval_limit:
            decision = "hold"
            action = "request_higher_approval"
            reason = "Invoice amount exceeds the stated approval limit."
        elif not evidence:
            decision = "hold"
            action = "request_evidence"
            reason = "No supporting evidence was provided."
        else:
            decision = "approved"
            action = "approve_for_payment"
            reason = "Approval, amount limit, unpaid status, and evidence checks passed."

        now = utc_now()
        conn.execute("""
            INSERT INTO invoices (
                invoice_id, vendor, amount, approval_status, approval_limit,
                payment_status, evidence_json, decision, reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            invoice_id, vendor, invoice.amount, invoice.approval_status,
            invoice.approval_limit, invoice.payment_status,
            json.dumps(evidence), decision, reason, now, now
        ))

    return {
        "invoice_id": invoice_id,
        "vendor": vendor,
        "amount": invoice.amount,
        "action": action,
        "decision": decision,
        "reason": reason,
        "evidence": evidence,
        "duplicate": False
    }


@app.post("/invoice/receipt")
def record_receipt(receipt: ReceiptRequest):
    invoice_id = receipt.invoice_id.strip()
    receipt_id = receipt.receipt_id.strip()

    with db_connect() as conn:
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()

        if invoice is None:
            raise HTTPException(
                status_code=409,
                detail="Cannot record receipt: invoice has not been evaluated."
            )

        if invoice["decision"] != "approved":
            raise HTTPException(
                status_code=409,
                detail="Cannot record receipt: invoice is not approved for payment."
            )

        prior = conn.execute(
            "SELECT * FROM receipts WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()
        if prior:
            return {
                "invoice_id": invoice_id,
                "receipt_id": prior["receipt_id"],
                "status": "already_recorded",
                "duplicate": True,
                "recorded_at": prior["recorded_at"]
            }

        receipt_used = conn.execute(
            "SELECT invoice_id FROM receipts WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        if receipt_used:
            raise HTTPException(
                status_code=409,
                detail="Receipt ID has already been used for another invoice."
            )

        now = utc_now()
        conn.execute(
            "INSERT INTO receipts (receipt_id, invoice_id, recorded_at) VALUES (?, ?, ?)",
            (receipt_id, invoice_id, now)
        )
        conn.execute("""
            UPDATE invoices
            SET payment_status = 'paid', updated_at = ?
            WHERE invoice_id = ?
        """, (now, invoice_id))

    return {
        "invoice_id": invoice_id,
        "receipt_id": receipt_id,
        "status": "recorded",
        "duplicate": False,
        "recorded_at": now
    }


# Minimal JSON-RPC entry point so an A2A client can discover a protocol endpoint.
# The invoice REST endpoints above remain available for the assignment's API contract.
@app.post("/")
async def json_rpc_root(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"}
            }
        )

    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": body.get("id") if isinstance(body, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}
            }
        )

    method = body.get("method")
    request_id = body.get("id")

    if method in {"message/send", "message/sendStream"}:
        # This service's supported business API is exposed through its REST tools.
        # Do not pretend a generic A2A message was processed as a payment.
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": (
                        "Generic A2A message processing is not configured. "
                        "Use POST /invoice/decide and POST /invoice/receipt."
                    )
                }
            },
            media_type="application/json"
        )

    return JSONResponse(
        content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"}
        },
        media_type="application/json"
    )
