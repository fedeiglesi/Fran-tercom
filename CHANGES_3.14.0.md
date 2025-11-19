# Fran 3.14.0 Applied to Fran-3.13.2

This branch contains all the Fran 3.14.0 improvements applied to the Fran-3.13.2 base.

## Applied Changes
1. MAX_FAISS_K = 200 constant
2. PENDING_ACTION_TTL = 15 constant  
3. Fuzzy cache increased to 10000
4. Autocorrect vocabulary expanded (+12 terms)
5. Autocorrect threshold lowered (90% → 78%)
6. Semantic search with FAISS cap and family boosting
7. Auto-cleanup for pending actions
8. format_products_for_llm function
9. PRODUCT_RESPONSE_SCHEMA for JSON validation
10. generate_smart_ai_reply_v2 rewritten with validation
11. Quality assessment logging to DEBUG
12. Health endpoint updated to v3.14.0
13. .gitignore added

## Commits
- 3f6bb3f: Implement Fran 3.14.0 improvements
- ca38085: Add .gitignore and remove __pycache__

Ready for merge into Fran-3.13.2.
