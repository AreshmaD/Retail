# Security And Safety Boundaries

Scout is a demo, not a production security system. Its important portfolio value is that high-risk commerce actions are structurally separated from agentic assistance.

## Commerce Mutation Boundary

Specialists never receive tools for checkout, payment, refunds, cancellation, order mutation, inventory mutation, SQL, shell, or unrestricted HTTP. Deterministic checkout remains in `backend/src/scout/api/checkout.py` and `backend/src/scout/services/payment_service.py`.

The agent can recommend products and direct the customer to secure checkout, but it cannot charge a card or create a payment through an agent tool.

## MCP Boundary

The local Scout MCP registry exposes 11 approved read tools:

`search`, `search_external_offers`, `recommend_products`, `stock`, `alternatives`, `fulfillment_options`, `stores`, `order_history`, `return_eligibility`, `orders`, `retrieve_policy_chunks`.

`ENABLE_STRIPE_MCP=false` disables Stripe MCP configuration and discovery for local demos. If `ENABLE_STRIPE_MCP=true`, raw Stripe MCP loading behavior is preserved, but specialist allowlists still prevent those raw tools from being passed to agents.

## Specialist Allowlists

| Agent | Allowed tools |
|---|---|
| `recommend_agent` | `recommend_products`, `search`, `stock`, `alternatives` |
| `inventory_agent` | `search`, `stock`, `stores`, `fulfillment_options` |
| `order_agent` | `orders`, `order_history`, `return_eligibility` |
| `external_offer_agent` | `search_external_offers` |
| `policy_agent` | `retrieve_policy_chunks` |

## Evidence And Rendering Safety

- Tool wrappers record sanitized `EvidenceEntry` and `ToolCallRecord` sidecar data.
- `ProposedClaim` objects link factual claims to evidence IDs.
- `verify_claims()` approves only supported domain claims.
- `render_verified_response()` rebuilds factual replies and product cards from approved claims.
- One bounded targeted correction can run only when needed and eligible; its raw output is passed through the complete verification pipeline before customer output.

## Known Non-Production Limits

- No authentication or user authorization.
- Synthetic order IDs can be queried without proving customer identity.
- No rate limiting, audit storage, abuse detection, or human review workflow.
- Stripe checkout is test-mode/demo code and should not be treated as production payment infrastructure without a full security review.
