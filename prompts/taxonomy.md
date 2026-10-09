"category" is exactly ONE of these 8 (pick the primary one):
- security: authorization / IDOR, injection, live secrets, PII or sensitive-data exposure, a security control that stops working. Plain validation that only yields a wrong value is correctness.
- requirements: the change does not do what the MR description / linked spec asks — a core goal missing, the wrong thing built, or behaviour nobody asked for. ONLY with a clause from the provided description to cite; if the context has no description or it does not state the requirement, never use this category.
- correctness: wrong results, crashes, races, transaction inconsistency, data loss — for this change's own behaviour.
- compatibility: breaks something that already exists outside this change — an API / schema / data-format contract, existing callers or clients, old rows, DB migrations / backfills, rollout order.
- operability: a failure cannot be detected, retried, compensated or rolled back, or a config / deploy gap makes the service unusable. A log that leaks data is security.
- performance: N+1, unbounded work, memory or query growth that times out or exhausts resources under a load you can state. "Could be faster" is not a finding.
- verification: tests that test the wrong thing, assertions that cannot fail, key cases skipped — CI will be green while the defect ships; or a stated test requirement left unverified. If you already report the product defect itself, do NOT also report the missing test.
- maintainability: duplication, dead code, misleading names, needless defensive code or over-engineering — with a concrete maintenance consequence. Pure style: drop it.

Tie-break when several fit (this is NOT the scan order): security > compatibility > requirements > correctness > operability > performance > verification > maintainability.
One finding per independently fixable defect; one root cause with several consequences is ONE finding under its most direct, most severe consequence. Never output pure style, a vague "should add tests", or "looks AI-written" as a finding.

Severity is strict and needs a concrete, reproducible scenario at every level:
- high: real exploitation / data loss or corruption / cross-user contamination / core flow broken for most users / irreversible migration / service down under normal load / CI green on a security, data-integrity or core-flow defect.
- medium: a common input, state, client or load produces the failure; a clear acceptance criterion of a common flow is missed.
- low: a narrow, recoverable case with a visible, concrete effect. "Might be nice" is not low — it is dropped.
