"""Chat API routes (Phase 5, §4.5).

Exposes the single chat endpoint over the ``ChatService`` instance owned by
the FastAPI app (``app.state.chat_service``, set up in ``main.py``'s
lifespan hook).

``trace_id`` is generated per request at this boundary (§2.1 / §4.6): a new
uuid for every incoming message, propagated to the service so the whole
graph run shares one correlation id. No history is accepted from the caller.
"""
from __future__ import annotations

import uuid
from html import escape

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from ai_customer_assistant.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])


@router.get("/tickets/verify", include_in_schema=False, response_class=HTMLResponse)
async def verify_ticket_email(
    request: Request, token: str = Query(min_length=20)
) -> HTMLResponse:
    """Consume an emailed one-time token and show ticket confirmation."""
    verifier = request.app.state.ticket_verification_service
    verified = await verifier.verify(token)
    if verified is None:
        return HTMLResponse(
            _verification_page(
                title="Verification link unavailable",
                message="This verification link is invalid, expired, or has already been used.",
                ticket_id=None,
                thread_id=None,
            ),
            status_code=400,
        )
    ticket = verified.ticket
    return HTMLResponse(
        _verification_page(
            title="Thank you for verification",
            message="Your email has been verified and your support ticket has been created.",
            ticket_id=ticket.ticket_id,
            thread_id=verified.thread_id,
        )
    )


def _verification_page(
    *, title: str, message: str, ticket_id: str | None, thread_id: str | None
) -> str:
    ticket_html = (
        f'<p class="ticket">Ticket ID: <strong>{escape(ticket_id)}</strong></p>'
        if ticket_id
        else ""
    )
    chat_script = (
        f"""
    <script>
      (function () {{
        var threadId = {thread_id!r};
        var ticketId = {ticket_id!r};
        var text = "Thank you for verification. Your ticket has been created"
          + (ticketId ? " (ID " + ticketId + ")." : ".")
          + " Our support team will follow up with you.";
        try {{
          var key = "aca.chat.threads.v1";
          var threads = JSON.parse(localStorage.getItem(key) || "[]");
          var found = null;
          for (var i = 0; i < threads.length; i++) {{
            if (threads[i].id === threadId) found = threads[i];
          }}
          if (!found) {{
            found = {{
              id: threadId,
              title: "Support ticket",
              createdAt: Date.now(),
              updatedAt: Date.now(),
              messages: []
            }};
            threads.unshift(found);
          }}
          found.messages = found.messages || [];
          found.messages.push({{ role: "assistant", content: text }});
          found.updatedAt = Date.now();
          localStorage.setItem(key, JSON.stringify(threads));
        }} catch (e) {{}}
        setTimeout(function () {{
          window.location.href = "/#/chat?thread_id=" + encodeURIComponent(threadId);
        }}, 900);
      }})();
    </script>"""
        if thread_id
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)}</title>
    <style>
      body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: Arial, sans-serif; background: #f6f8fb; color: #182230; }}
      main {{ width: min(92vw, 520px); padding: 40px; text-align: center; background: #fff; border-radius: 16px; box-shadow: 0 12px 32px #1822301a; }}
      .icon {{ display: inline-grid; place-items: center; width: 52px; height: 52px; border-radius: 50%; background: #dcfce7; color: #15803d; font-size: 28px; }}
      h1 {{ margin: 18px 0 10px; font-size: 25px; }}
      p {{ line-height: 1.55; color: #4b5563; }}
      .ticket {{ margin-top: 24px; padding: 12px; border-radius: 8px; background: #eef6ff; color: #1e3a5f; }}
    </style>
  </head>
  <body>
    <main>
      <div class="icon">✓</div>
      <h1>{escape(title)}</h1>
      <p>{escape(message)}</p>
      {ticket_html}
    </main>
    {chat_script}
  </body>
</html>"""


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, payload: ChatRequest) -> ChatResponse:
    service = request.app.state.chat_service
    trace_id = str(uuid.uuid4())
    reply, citations = await service.handle_message_turn(
        payload.thread_id,
        payload.message,
        trace_id=trace_id,
    )
    return ChatResponse(
        thread_id=payload.thread_id,
        reply=reply,
        trace_id=trace_id,
        citations=citations,
    )
