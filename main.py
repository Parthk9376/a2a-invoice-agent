
import os
import json
import sqlite3
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="A2A Invoice Agent", version="1.0.0")

DB_PATH = os.getenv("DB_PATH", "invoices.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                invoice_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                receipt_id TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        yield conn
        conn.commit()
    finally:
        conn.close()


class Invoice(BaseModel):
    invoice_id: str = Field(min_length=1)
    vendor: str = Field(min_length=1)
    amount: float = Field(gt=0)
    approval_status: str
    approval_limit: float = Field(ge=0)
    payment_status: str = "unpaid"
    evidence: list[str] = Field(default_factory=list)


class PaymentReceipt(BaseModel):
    invoice_id: str
    receipt_id: str = Field(min_length=1)


@app.get("/")
def home():
    return {
        "service": "A2A Invoice Agent",
        "status": "running",
        "docs": "/docs"
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/invoice/decide")
def decide(invoice: Invoice):
    evidence = list(invoice.evidence)

    with get_db() as conn:
        existing = conn.execute(
            "SELECT status, receipt_id FROM invoices WHERE invoice_id = ?",
            (invoice.invoice_id,)
        ).fetchone()

        # Never initiate a second payment for a recorded paid invoice.
        if existing and existing["status"] == "paid":
            return {
                "invoice_id": invoice.invoice_id,
                "action": "already_paid",
                "reason": "A payment is already recorded for this invoice.",
                "receipt_id": existing["receipt_id"],
                "evidence": ["Stored payment state"]
            }

        if invoice.payment_status.lower() == "paid":
            return {
                "invoice_id": invoice.invoice_id,
                "action": "verify_payment",
                "reason": "Verify the existing payment and obtain its receipt.",
                "evidence": evidence
            }

        if invoice.approval_status.lower() != "approved":
            action = "hold"
            reason = "Valid approval has not been established."

        elif invoice.amount > invoice.approval_limit:
            action = "hold"
            reason = "Invoice amount exceeds the approval limit."

        elif not evidence:
            action = "hold"
            reason = "Supporting evidence is missing."

        else:
            action = "approve_for_payment"
            reason = "Approval, amount limit, and evidence checks passed."

        if action == "approve_for_payment":
            # Record authorization, NOT payment completion.
            conn.execute("""
                INSERT INTO invoices (invoice_id, status)
                VALUES (?, 'approved')
                ON CONFLICT(invoice_id) DO NOTHING
            """)

        return {
            "invoice_id": invoice.invoice_id,
            "vendor": invoice.vendor,
            "amount": invoice.amount,
            "action": action,
            "reason": reason,
            "evidence": evidence
        }


@app.post("/invoice/receipt")
def record_receipt(receipt: PaymentReceipt):
    # Call only after independently verifying a genuine payment receipt.
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, receipt_id FROM invoices WHERE invoice_id = ?",
            (receipt.invoice_id,)
        ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Invoice was not previously authorized."
            )

        if row["status"] == "paid":
            return {
                "invoice_id": receipt.invoice_id,
                "status": "paid",
                "receipt_id": row["receipt_id"],
                "message": "Existing receipt returned; no duplicate recorded."
            }

        if row["status"] != "approved":
            raise HTTPException(
                status_code=409,
                detail="Invoice is not authorized for payment."
            )

        conn.execute("""
            UPDATE invoices
            SET status = 'paid',
                receipt_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE invoice_id = ? AND status = 'approved'
        """, (receipt.receipt_id, receipt.invoice_id))

        updated = conn.execute(
            "SELECT status, receipt_id FROM invoices WHERE invoice_id = ?",
            (receipt.invoice_id,)
        ).fetchone()

        return {
            "invoice_id": receipt.invoice_id,
            "status": updated["status"],
            "receipt_id": updated["receipt_id"]
        }
