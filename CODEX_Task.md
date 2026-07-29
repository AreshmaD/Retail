## Current upgrade task

Add the earlier project’s structured evidence model, broad deterministic
claim verification, approved-only response rendering, and one bounded
correction pass to the current five-agent architecture.

Preserve:
- Supervisor and five create_react_agent specialists
- Current agent tool allowlists
- Existing MCP tool names
- Repository-only SQL access
- Shared services
- Deterministic checkout and Stripe path
- POST /chat and POST /chat/stream contracts
- Current frontend compatibility
- Existing workflow/tool-call/recursion limits
- Current model-provider support

Do not:
- Replace the current graph with the earlier fixed workflow
- Introduce checkout or mutation tools
- Redesign typed per-agent state in this upgrade
- Modify unrelated UI or commerce behavior
- Parse factual claims back from free-form assistant prose
- Continue to another phase when tests fail

Implementation order:
1. Baseline inspection
2. Evidence schemas
3. Automatic evidence recording
4. Proposed claims
5. Broad deterministic verification
6. Approved-only rendering
7. One targeted correction pass
8. Adversarial tests